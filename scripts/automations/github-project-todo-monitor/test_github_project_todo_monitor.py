import dataclasses
import datetime as dt
import hashlib
import hmac
import http.client
import json
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path

from github_project_todo_monitor import (
    Config,
    GitHubClient,
    MonitorError,
    PaperclipClient,
    ProjectItem,
    ProjectSnapshot,
    StateStore,
    TodoMonitor,
    UTC,
    WEBHOOK_EVENT_KIND,
    WebhookReceiver,
    check_local_webhook_health,
    verify_webhook_signature,
    wake_payload,
)


def when(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def issue_item(
    item_id: str,
    number: int,
    status: str,
    status_updated_at: str,
    issue_created_at: str,
    labels=(),
) -> ProjectItem:
    return ProjectItem(
        item_id=item_id,
        item_updated_at=status_updated_at,
        status=status,
        status_option_id=f"option-{status.lower()}",
        status_updated_at=status_updated_at,
        content_type="Issue",
        repository="mywaffles/newco.core",
        issue_number=number,
        issue_url=f"https://github.com/mywaffles/newco.core/issues/{number}",
        issue_created_at=issue_created_at,
        labels=tuple(labels),
    )


def snapshot(*items: ProjectItem) -> ProjectSnapshot:
    return ProjectSnapshot(project_id="PVT_project4", project_title="newco.core Issues", items=tuple(items))


def webhook_payload(
    *,
    repository: str = "mywaffles/newco.core",
    number: int = 91,
    action: str = "opened",
    state: str = "open",
    created_at: str = "2026-05-01T00:00:00Z",
    updated_at: str = "2026-08-08T12:00:00Z",
):
    return {
        "action": action,
        "repository": {"full_name": repository},
        "issue": {
            "number": number,
            "html_url": f"https://github.com/{repository}/issues/{number}",
            "created_at": created_at,
            "updated_at": updated_at,
            "state": state,
        },
    }


class FakeGitHub:
    def __init__(self, value: ProjectSnapshot) -> None:
        self.value = value

    def fetch_project(self) -> ProjectSnapshot:
        return self.value


class FakePaperclip:
    def __init__(self) -> None:
        self.agent_id = "dev-manager-id"
        self.active = False
        self.runs = []
        self.wake_calls = []
        self.audit_calls = []
        self.capacity_available = True

    def resolve_agent_id(self):
        return self.agent_id

    def has_active_run(self):
        return self.active

    def coding_capacity_available(self):
        return self.capacity_available

    def recent_runs(self):
        return list(self.runs)

    @staticmethod
    def run_for_transition(runs, key, run_id=None):
        fallback = None
        for run in runs:
            if run.get("contextSnapshot", {}).get("taskKey") == key:
                if run_id and run.get("id") == run_id:
                    return run
                fallback = fallback or run
        return fallback

    def wake(self, transition):
        self.wake_calls.append(transition)
        run = {
            "id": f"run-{len(self.wake_calls)}",
            "agentId": self.agent_id,
            "status": "queued",
            "contextSnapshot": {"taskKey": transition.idempotency_key},
        }
        self.runs.append(run)
        return run

    def wake_periodic_audit(self, now):
        self.audit_calls.append(now)
        return {"id": f"audit-{len(self.audit_calls)}", "status": "queued"}


class CaptureRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        return self.responses.pop(0)


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = Config(
            github_owner="mywaffles",
            github_project_number=4,
            expected_project_title="newco.core Issues",
            target_status="Todo",
            github_webhook_hook_id="663155385",
            github_webhook_repositories=(
                "mywaffles/newco.core",
                "mywaffles/paperclip-mywaffles",
                "mywaffles/nocodb",
                "mywaffles/hermes-mywaffles",
            ),
            webhook_listen_host="127.0.0.1",
            webhook_listen_port=8788,
            webhook_path="/",
            webhook_health_path="/health",
            webhook_public_url="https://macbook-air-m5.example.test:8443/github/issues",
            webhook_secret_command=tuple(),
            webhook_secret_env_var="GITHUB_ROUTER_WEBHOOK_SECRET",
            webhook_max_body_bytes=1_048_576,
            poll_interval_seconds=10,
            github_command=("gh",),
            paperclip_command=("paperclipai",),
            paperclip_api_base="https://paperclip.example.test",
            paperclip_company_id="company-id",
            paperclip_agent="dev-manager",
            paperclip_coding_agents=("dev-claude-max", "dev-codex-max"),
            paperclip_api_key_command=tuple(),
            paperclip_api_key_env_var="PAPERCLIP_MONITOR_API_KEY",
            state_path=root / "state.sqlite3",
            log_path=root / "monitor.log",
            lock_path=root / "monitor.lock",
            command_timeout_seconds=12,
            delivery_ambiguity_seconds=120,
            outcome_timeout_seconds=900,
            deferred_retry_seconds=60,
            audit_interval_seconds=1800,
            max_log_bytes=65536,
            log_backups=2,
        )
        self.store = StateStore(self.config.state_path)
        self.initial = when("2026-08-08T12:00:00Z")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def baseline(self, *items):
        return self.store.apply_snapshot(self.config, snapshot(*items), self.initial)

    def start_receiver(self, secret=b"s" * 32):
        config = dataclasses.replace(self.config, webhook_listen_port=0)
        logger = logging.getLogger(f"webhook-test-{id(self)}")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        receiver = WebhookReceiver(config, logger, secret=secret, clock=lambda: self.initial)
        receiver.start()
        self.addCleanup(receiver.stop)
        return receiver

    def post_webhook(
        self,
        receiver,
        payload,
        *,
        secret=b"s" * 32,
        delivery_id="delivery-1",
        github_event="issues",
        hook_id="663155385",
        signature=None,
    ):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = signature or "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        connection = http.client.HTTPConnection("127.0.0.1", receiver.port, timeout=3)
        try:
            connection.request(
                "POST",
                receiver.config.webhook_path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": delivery_id,
                    "X-GitHub-Event": github_event,
                    "X-GitHub-Hook-ID": hook_id,
                    "X-Hub-Signature-256": signature,
                },
            )
            response = connection.getresponse()
            value = json.loads(response.read().decode("utf-8"))
            return response.status, value
        finally:
            connection.close()

    def test_first_snapshot_reconciles_existing_unrouted_todo(self):
        existing = issue_item("item-29", 29, "Todo", "2026-08-08T11:00:00Z", "2026-01-01T00:00:00Z")
        self.assertEqual(self.baseline(existing), 1)
        self.assertTrue(self.store.baseline_complete())
        self.assertEqual(self.store.transition_counts()["pending"], 1)
        self.assertEqual(self.store.apply_snapshot(self.config, snapshot(existing), self.initial), 0)

    def test_first_snapshot_does_not_queue_already_routed_todo(self):
        existing = issue_item(
            "item-29",
            29,
            "Todo",
            "2026-08-08T11:00:00Z",
            "2026-01-01T00:00:00Z",
            labels=("dev-codex-max",),
        )
        self.assertEqual(self.baseline(existing), 0)
        self.assertEqual(self.store.transition_counts()["pending"], 0)

    def test_routing_label_does_not_ack_inflight_transition(self):
        backlog = issue_item("item-29", 29, "Backlog", "2026-08-08T10:00:00Z", "2026-01-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-29", 29, "Todo", "2026-08-08T11:00:00Z", "2026-01-01T00:00:00Z")
        now = when("2026-08-08T11:00:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), now)
        transition = self.store.next_transition(now)
        self.store.mark_delivering(transition.idempotency_key, now)
        labeled = dataclasses.replace(todo, labels=("dev-codex-max",))

        self.store.apply_snapshot(self.config, snapshot(labeled), now + dt.timedelta(seconds=10))

        self.assertEqual(self.store.transition_counts()["delivering"], 1)
        current = self.store.delivering_transitions()[0]
        self.assertIsNone(current.routing_outcome)

    def test_real_transition_queues_once_while_item_stays_todo(self):
        backlog = issue_item("item-30", 30, "Backlog", "2026-08-08T11:00:00Z", "2026-02-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-30", 30, "Todo", "2026-08-08T12:01:00Z", "2026-02-01T00:00:00Z")
        self.assertEqual(self.store.apply_snapshot(self.config, snapshot(todo), when("2026-08-08T12:01:01Z")), 1)
        self.assertEqual(self.store.apply_snapshot(self.config, snapshot(todo), when("2026-08-08T12:01:11Z")), 0)
        self.assertEqual(self.store.transition_counts()["pending"], 1)

    def test_leaving_and_reentering_todo_creates_a_new_transition_key(self):
        backlog = issue_item("item-31", 31, "Backlog", "2026-08-08T11:00:00Z", "2026-03-01T00:00:00Z")
        self.baseline(backlog)
        first_todo = issue_item("item-31", 31, "Todo", "2026-08-08T12:01:00Z", "2026-03-01T00:00:00Z")
        self.store.apply_snapshot(self.config, snapshot(first_todo), when("2026-08-08T12:01:01Z"))
        doing = issue_item("item-31", 31, "In Progress", "2026-08-08T12:02:00Z", "2026-03-01T00:00:00Z")
        self.store.apply_snapshot(self.config, snapshot(doing), when("2026-08-08T12:02:01Z"))
        second_todo = issue_item("item-31", 31, "Todo", "2026-08-08T12:03:00Z", "2026-03-01T00:00:00Z")
        self.store.apply_snapshot(self.config, snapshot(second_todo), when("2026-08-08T12:03:01Z"))
        rows = self.store.db.execute("SELECT idempotency_key FROM transitions ORDER BY detected_at").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["idempotency_key"], rows[1]["idempotency_key"])

    def test_new_issue_added_directly_to_todo_after_baseline_is_queued(self):
        self.baseline(issue_item("item-29", 29, "Todo", "2026-08-08T11:00:00Z", "2026-01-01T00:00:00Z"))
        new_issue = issue_item("item-32", 32, "Todo", "2026-08-08T12:04:00Z", "2026-04-01T00:00:00Z")
        self.assertEqual(self.store.apply_snapshot(self.config, snapshot(new_issue), when("2026-08-08T12:04:01Z")), 1)

    def test_replacement_project_node_is_rejected_after_baseline(self):
        self.baseline(issue_item("item-29", 29, "Todo", "2026-08-08T11:00:00Z", "2026-01-01T00:00:00Z"))
        replacement = ProjectSnapshot(project_id="PVT_replacement", project_title="newco.core Issues", items=tuple())
        with self.assertRaisesRegex(MonitorError, "refusing to treat a replacement board"):
            self.store.apply_snapshot(self.config, replacement, when("2026-08-08T12:04:01Z"))

    def test_multiple_transitions_are_ordered_by_issue_creation_oldest_first(self):
        newer = issue_item("item-40", 40, "Backlog", "2026-08-08T11:00:00Z", "2026-06-01T00:00:00Z")
        older = issue_item("item-39", 39, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(newer, older)
        newer_todo = issue_item("item-40", 40, "Todo", "2026-08-08T12:05:00Z", "2026-06-01T00:00:00Z")
        older_todo = issue_item("item-39", 39, "Todo", "2026-08-08T12:05:00Z", "2026-05-01T00:00:00Z")
        self.store.apply_snapshot(self.config, snapshot(newer_todo, older_todo), when("2026-08-08T12:05:01Z"))
        self.assertEqual(self.store.next_transition(when("2026-08-08T12:05:02Z")).issue_number, 39)

    def test_webhook_signature_matches_github_documented_vector(self):
        secret = b"It's a Secret to Everybody"
        body = b"Hello, World!"
        signature = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
        self.assertTrue(verify_webhook_signature(secret, body, signature))
        self.assertFalse(verify_webhook_signature(secret, body + b"!", signature))
        self.assertFalse(verify_webhook_signature(secret, body, "sha1=bad"))

    def test_signed_issues_webhook_queues_once_and_deduplicates_delivery(self):
        receiver = self.start_receiver()
        payload = webhook_payload(number=92, action="edited")
        first_status, first = self.post_webhook(receiver, payload, delivery_id="delivery-dedupe")
        second_status, second = self.post_webhook(receiver, payload, delivery_id="delivery-dedupe")
        self.assertEqual((first_status, first["status"]), (202, "queued"))
        self.assertEqual((second_status, second["status"]), (200, "duplicate"))
        self.assertEqual(self.store.webhook_receipt_count(), 1)
        self.assertEqual(self.store.transition_counts()["pending"], 1)
        event = self.store.next_transition(self.initial)
        self.assertEqual(event.source_kind, WEBHOOK_EVENT_KIND)
        self.assertEqual(event.source_delivery_id, "delivery-dedupe")
        self.assertEqual(event.github_action, "edited")
        self.assertEqual(event.issue_number, 92)
        wake = wake_payload(self.config, event)
        self.assertEqual(wake["event"], WEBHOOK_EVENT_KIND)
        self.assertEqual(wake["action"], "edited")
        self.assertEqual(wake["deliveryId"], "delivery-dedupe")
        self.assertEqual(wake["projectNumber"], 4)
        self.assertIn("Handle only this GitHub issue", wake["instruction"])

    def test_routing_label_webhook_is_recorded_without_enqueueing(self):
        receiver = self.start_receiver()
        payload = webhook_payload(number=94, action="labeled")
        payload["label"] = {"name": "dev-claude-max"}

        status, value = self.post_webhook(receiver, payload, delivery_id="delivery-routing-label")

        self.assertEqual((status, value["status"]), (200, "ignored"))
        self.assertEqual(self.store.webhook_receipt_count(), 1)
        self.assertEqual(self.store.transition_counts()["pending"], 0)

    def test_invalid_signature_is_rejected_without_a_receipt_or_wake(self):
        receiver = self.start_receiver()
        status, value = self.post_webhook(
            receiver,
            webhook_payload(number=93),
            delivery_id="delivery-invalid-signature",
            signature="sha256=" + ("0" * 64),
        )
        self.assertEqual((status, value["error"]), (401, "invalid_signature"))
        self.assertEqual(self.store.webhook_receipt_count(), 0)
        self.assertEqual(self.store.transition_counts()["pending"], 0)

    def test_unexpected_hook_and_repository_are_rejected(self):
        receiver = self.start_receiver()
        hook_status, hook_value = self.post_webhook(
            receiver,
            webhook_payload(number=94),
            delivery_id="delivery-wrong-hook",
            hook_id="999",
        )
        repository_status, repository_value = self.post_webhook(
            receiver,
            webhook_payload(repository="someone/else", number=94),
            delivery_id="delivery-wrong-repo",
        )
        self.assertEqual((hook_status, hook_value["error"]), (403, "unexpected_hook"))
        self.assertEqual(repository_status, 403)
        self.assertIn("not allowed", repository_value["error"])
        self.assertEqual(self.store.webhook_receipt_count(), 0)

    def test_ping_is_deduplicated_without_queueing_work(self):
        receiver = self.start_receiver()
        first_status, first = self.post_webhook(
            receiver,
            {"zen": "Keep it logically awesome."},
            delivery_id="delivery-ping",
            github_event="ping",
        )
        second_status, second = self.post_webhook(
            receiver,
            {"zen": "Keep it logically awesome."},
            delivery_id="delivery-ping",
            github_event="ping",
        )
        self.assertEqual((first_status, first["status"]), (200, "ping"))
        self.assertEqual((second_status, second["status"]), (200, "duplicate"))
        self.assertEqual(self.store.webhook_receipt_count(), 1)
        self.assertEqual(self.store.transition_counts()["pending"], 0)

    def test_webhook_listener_health_endpoint(self):
        receiver = self.start_receiver()
        test_config = dataclasses.replace(receiver.config, webhook_listen_port=receiver.port)
        self.assertEqual(check_local_webhook_health(test_config), {"ok": True, "statusCode": 200})

    def test_v1_state_migrates_without_losing_pending_project_transition(self):
        state_path = Path(self.temp.name) / "v1.sqlite3"
        database = sqlite3.connect(state_path)
        try:
            database.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata(key, value) VALUES ('schema_version', '1');
                CREATE TABLE monitor_status (
                  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                  daemon_pid INTEGER, daemon_started_at TEXT, daemon_heartbeat_at TEXT,
                  last_poll_started_at TEXT, last_poll_succeeded_at TEXT, last_poll_error TEXT,
                  last_project_id TEXT, last_project_title TEXT, last_item_count INTEGER,
                  last_dispatch_at TEXT, last_dispatch_error TEXT
                );
                INSERT INTO monitor_status(singleton) VALUES (1);
                CREATE TABLE transitions (
                  idempotency_key TEXT PRIMARY KEY, project_item_id TEXT NOT NULL,
                  from_status TEXT, to_status TEXT NOT NULL, status_updated_at TEXT NOT NULL,
                  repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
                  issue_url TEXT NOT NULL, issue_created_at TEXT NOT NULL,
                  detected_at TEXT NOT NULL, delivery_status TEXT NOT NULL DEFAULT 'pending',
                  delivery_attempts INTEGER NOT NULL DEFAULT 0, attempt_started_at TEXT,
                  next_attempt_at TEXT, delivered_at TEXT, paperclip_run_id TEXT, last_error TEXT
                );
                INSERT INTO transitions(
                  idempotency_key, project_item_id, from_status, to_status, status_updated_at,
                  repository, issue_number, issue_url, issue_created_at, detected_at
                ) VALUES (
                  'old-key', 'item-old', 'Backlog', 'Todo', '2026-08-08T12:00:00Z',
                  'mywaffles/newco.core', 88,
                  'https://github.com/mywaffles/newco.core/issues/88',
                  '2026-05-01T00:00:00Z', '2026-08-08T12:00:01Z'
                );
                """
            )
            database.commit()
        finally:
            database.close()
        migrated = StateStore(state_path)
        try:
            self.assertEqual(migrated.get_metadata("schema_version"), "3")
            event = migrated.next_transition(when("2026-08-08T12:00:02Z"))
            self.assertEqual(event.source_kind, "github_project_status_transition")
            self.assertEqual(event.issue_number, 88)
            self.assertEqual(migrated.webhook_receipt_count(), 0)
        finally:
            migrated.close()

    def test_webhook_and_project_events_share_oldest_first_dispatch_queue(self):
        project_issue = issue_item(
            "item-100",
            100,
            "Backlog",
            "2026-08-08T11:00:00Z",
            "2026-06-01T00:00:00Z",
        )
        self.baseline(project_issue)
        receiver = self.start_receiver()
        status, _ = self.post_webhook(
            receiver,
            webhook_payload(number=99, created_at="2026-05-01T00:00:00Z"),
            delivery_id="delivery-older",
        )
        self.assertEqual(status, 202)
        todo = issue_item(
            "item-100",
            100,
            "Todo",
            "2026-08-08T12:05:00Z",
            "2026-06-01T00:00:00Z",
        )
        self.store.apply_snapshot(self.config, snapshot(todo), when("2026-08-08T12:05:01Z"))
        self.assertEqual(self.store.next_transition(when("2026-08-08T12:05:02Z")).issue_number, 99)

    def test_dispatch_waits_while_dev_manager_has_an_active_run(self):
        backlog = issue_item("item-50", 50, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-50", 50, "Todo", "2026-08-08T12:06:00Z", "2026-05-01T00:00:00Z")
        now = when("2026-08-08T12:06:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), now)
        paperclip = FakePaperclip()
        paperclip.active = True
        monitor = TodoMonitor(self.config, self.store, FakeGitHub(snapshot(todo)), paperclip, clock=lambda: now)
        self.assertEqual(monitor.dispatch_once(), "agent_busy")
        self.assertEqual(paperclip.wake_calls, [])
        paperclip.active = False
        self.assertEqual(monitor.dispatch_once(), "awaiting_outcome")
        self.assertEqual(len(paperclip.wake_calls), 1)

    def test_project_transition_stays_pending_until_coding_capacity_returns(self):
        backlog = issue_item("item-51", 51, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-51", 51, "Todo", "2026-08-08T12:06:00Z", "2026-05-01T00:00:00Z")
        now = when("2026-08-08T12:06:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), now)
        paperclip = FakePaperclip()
        paperclip.capacity_available = False
        monitor = TodoMonitor(self.config, self.store, FakeGitHub(snapshot(todo)), paperclip, clock=lambda: now)
        self.assertEqual(monitor.dispatch_once(), "coding_capacity_full")
        self.assertEqual(self.store.transition_counts()["pending"], 1)
        self.assertEqual(paperclip.wake_calls, [])
        paperclip.capacity_available = True
        self.assertEqual(monitor.dispatch_once(), "awaiting_outcome")
        self.assertEqual(len(paperclip.wake_calls), 1)

    def test_deferred_outcome_returns_same_issue_to_retry_queue(self):
        backlog = issue_item("item-52", 52, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-52", 52, "Todo", "2026-08-08T12:06:00Z", "2026-05-01T00:00:00Z")
        now = when("2026-08-08T12:06:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), now)
        transition = self.store.next_transition(now)
        self.assertTrue(self.store.mark_delivering(transition.idempotency_key, now))
        self.assertEqual(
            self.store.acknowledge(
                transition.idempotency_key,
                "deferred",
                "Both coding agents are busy",
                now,
                self.config.deferred_retry_seconds,
            ),
            "deferred",
        )
        self.assertIsNone(self.store.next_transition(now + dt.timedelta(seconds=59)))
        retried = self.store.next_transition(now + dt.timedelta(seconds=60))
        self.assertEqual(retried.idempotency_key, transition.idempotency_key)
        self.assertEqual(retried.routing_outcome, "deferred")

    def test_deferred_oldest_issue_blocks_younger_work_until_retry(self):
        older = issue_item("item-54", 54, "Backlog", "2026-08-08T11:00:00Z", "2026-04-01T00:00:00Z")
        younger = issue_item("item-55", 55, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(older, younger)
        older_todo = dataclasses.replace(older, status="Todo", status_updated_at="2026-08-08T12:06:00Z")
        younger_todo = dataclasses.replace(younger, status="Todo", status_updated_at="2026-08-08T12:06:00Z")
        now = when("2026-08-08T12:06:01Z")
        self.store.apply_snapshot(self.config, snapshot(older_todo, younger_todo), now)
        transition = self.store.next_transition(now)
        self.store.mark_delivering(transition.idempotency_key, now)
        self.store.acknowledge(
            transition.idempotency_key,
            "deferred",
            "Capacity is temporarily unavailable",
            now,
            self.config.deferred_retry_seconds,
        )
        self.assertTrue(self.store.has_pending_transitions())
        self.assertIsNone(self.store.next_transition(now + dt.timedelta(seconds=59)))
        self.assertEqual(
            self.store.next_transition(now + dt.timedelta(seconds=60)).issue_number,
            54,
        )

    def test_idle_router_dispatches_one_periodic_audit_per_interval(self):
        self.baseline(issue_item("item-53", 53, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z"))
        paperclip = FakePaperclip()
        monitor = TodoMonitor(self.config, self.store, FakeGitHub(snapshot()), paperclip, clock=lambda: self.initial)
        self.assertEqual(monitor.dispatch_once(), "periodic_audit_dispatched")
        self.assertEqual(monitor.dispatch_once(), "idle")
        self.assertEqual(len(paperclip.audit_calls), 1)

    def test_accepted_run_waits_for_explicit_outcome_without_a_second_wake(self):
        backlog = issue_item("item-60", 60, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-60", 60, "Todo", "2026-08-08T12:07:00Z", "2026-05-01T00:00:00Z")
        now = when("2026-08-08T12:07:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), now)
        transition = self.store.next_transition(now)
        self.assertTrue(self.store.mark_delivering(transition.idempotency_key, now))
        self.store.close()
        self.store = StateStore(self.config.state_path)
        paperclip = FakePaperclip()
        paperclip.runs.append(
            {
                "id": "already-accepted-run",
                "agentId": paperclip.agent_id,
                "status": "running",
                "contextSnapshot": {"taskKey": transition.idempotency_key},
            }
        )
        monitor = TodoMonitor(self.config, self.store, FakeGitHub(snapshot(todo)), paperclip, clock=lambda: now)
        self.assertEqual(monitor.dispatch_once(), "awaiting_outcome")
        self.assertEqual(paperclip.wake_calls, [])
        self.assertEqual(self.store.transition_counts()["delivering"], 1)
        self.assertEqual(
            self.store.acknowledge(
                transition.idempotency_key,
                "routed",
                "Mapped, assigned, and labeled",
                now,
                self.config.deferred_retry_seconds,
            ),
            "routed",
        )
        self.assertEqual(self.store.transition_counts()["delivered"], 1)

    def test_ambiguous_recent_delivery_is_not_retried(self):
        backlog = issue_item("item-61", 61, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-61", 61, "Todo", "2026-08-08T12:08:00Z", "2026-05-01T00:00:00Z")
        attempt = when("2026-08-08T12:08:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), attempt)
        transition = self.store.next_transition(attempt)
        self.store.mark_delivering(transition.idempotency_key, attempt)
        paperclip = FakePaperclip()
        monitor = TodoMonitor(
            self.config,
            self.store,
            FakeGitHub(snapshot(todo)),
            paperclip,
            clock=lambda: attempt + dt.timedelta(seconds=30),
        )
        self.assertEqual(monitor.dispatch_once(), "awaiting_outcome")
        self.assertEqual(paperclip.wake_calls, [])

    def test_terminal_run_without_outcome_is_retried(self):
        backlog = issue_item("item-62", 62, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-62", 62, "Todo", "2026-08-08T12:08:00Z", "2026-05-01T00:00:00Z")
        attempt = when("2026-08-08T12:08:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), attempt)
        transition = self.store.next_transition(attempt)
        self.store.mark_delivering(transition.idempotency_key, attempt)
        self.store.mark_delivery_accepted(transition.idempotency_key, attempt, "run-without-ack")
        paperclip = FakePaperclip()
        paperclip.runs.append(
            {
                "id": "run-without-ack",
                "agentId": paperclip.agent_id,
                "status": "succeeded",
                "contextSnapshot": {"taskKey": transition.idempotency_key},
            }
        )
        terminal_at = attempt + dt.timedelta(seconds=1)
        monitor = TodoMonitor(
            self.config,
            self.store,
            FakeGitHub(snapshot(todo)),
            paperclip,
            clock=lambda: terminal_at,
        )
        monitor.reconcile_deliveries()
        self.assertEqual(self.store.transition_counts()["pending"], 1)
        self.assertIsNone(self.store.next_transition(terminal_at + dt.timedelta(seconds=59)))
        self.assertEqual(
            self.store.next_transition(terminal_at + dt.timedelta(seconds=60)).idempotency_key,
            transition.idempotency_key,
        )

    def test_payload_is_issue_scoped_and_contains_routing_constraints(self):
        backlog = issue_item("item-70", 70, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-70", 70, "Todo", "2026-08-08T12:09:00Z", "2026-05-01T00:00:00Z")
        now = when("2026-08-08T12:09:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), now)
        transition = self.store.next_transition(now)
        payload = wake_payload(self.config, transition)
        self.assertEqual(payload["repository"], "mywaffles/newco.core")
        self.assertEqual(payload["issueNumber"], 70)
        self.assertEqual(payload["projectNumber"], 4)
        self.assertEqual(payload["status"], "Todo")
        self.assertIn("Handle only this GitHub issue", payload["instruction"])
        self.assertIn("GitHub↔Paperclip mapping", payload["instruction"])
        self.assertEqual(payload["taskKey"], transition.idempotency_key)
        self.assertEqual(payload["acknowledge"]["command"][-2:], ["ack", transition.idempotency_key])
        self.assertEqual(payload["acknowledge"]["reasonFlag"], "--reason")

    def test_github_client_paginates_and_uses_status_value_updated_at(self):
        def page(nodes, has_next, cursor):
            return {
                "data": {
                    "user": {
                        "projectV2": {
                            "id": "PVT_project4",
                            "title": "newco.core Issues",
                            "items": {
                                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                                "nodes": nodes,
                            },
                        }
                    }
                }
            }

        def node(item_id, number, status_updated_at):
            return {
                "id": item_id,
                "type": "ISSUE",
                "isArchived": False,
                "updatedAt": status_updated_at,
                "fieldValueByName": {
                    "name": "Todo",
                    "optionId": "todo-option",
                    "updatedAt": status_updated_at,
                },
                "content": {
                    "__typename": "Issue",
                    "number": number,
                    "url": f"https://github.com/mywaffles/newco.core/issues/{number}",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "repository": {"nameWithOwner": "mywaffles/newco.core"},
                },
            }

        runner = CaptureRunner(
            [
                page([node("item-1", 1, "2026-08-08T12:00:00Z")], True, "cursor-1"),
                page([node("item-2", 2, "2026-08-08T12:01:00Z")], False, None),
            ]
        )
        result = GitHubClient(self.config, runner=runner).fetch_project()
        self.assertEqual([item.item_id for item in result.items], ["item-1", "item-2"])
        self.assertEqual(result.items[1].status_updated_at, "2026-08-08T12:01:00Z")
        self.assertIsNone(runner.calls[0][1]["input_value"]["variables"]["cursor"])
        self.assertEqual(runner.calls[1][1]["input_value"]["variables"]["cursor"], "cursor-1")

    def test_paperclip_wake_command_is_direct_callback_with_deterministic_key(self):
        backlog = issue_item("item-80", 80, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-80", 80, "Todo", "2026-08-08T12:10:00Z", "2026-05-01T00:00:00Z")
        now = when("2026-08-08T12:10:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), now)
        transition = self.store.next_transition(now)
        runner = CaptureRunner(
            [
                [
                    {
                        "id": "mapped-issue-id",
                        "identifier": "MAT-80",
                        "description": "GitHub: https://github.com/mywaffles/newco.core/issues/80",
                        "status": "backlog",
                        "assigneeAgentId": None,
                    }
                ],
                [{"id": "manager-id", "urlKey": "dev-manager", "name": "Dev Manager"}],
                {"id": "mapped-issue-id", "status": "backlog", "assigneeAgentId": "manager-id"},
                {"id": "wake-run", "status": "queued"},
            ]
        )
        response = PaperclipClient(self.config, runner=runner).wake(transition)
        self.assertEqual(response["id"], "wake-run")
        lookup_command = runner.calls[0][0]
        self.assertEqual(lookup_command[:3], ["paperclipai", "issue", "list"])
        self.assertEqual(lookup_command[lookup_command.index("--match") + 1], transition.issue_url)
        update_command = runner.calls[2][0]
        self.assertEqual(update_command[:3], ["paperclipai", "issue", "update"])
        self.assertEqual(update_command[3], "mapped-issue-id")
        self.assertEqual(update_command[update_command.index("--assignee-agent-id") + 1], "manager-id")
        self.assertEqual(update_command[update_command.index("--status") + 1], "backlog")
        command = runner.calls[3][0]
        self.assertEqual(command[:3], ["paperclipai", "agent", "wake"])
        self.assertEqual(command[3], "dev-manager")
        self.assertEqual(command[command.index("--source") + 1], "automation")
        self.assertEqual(command[command.index("--trigger") + 1], "callback")
        reason = command[command.index("--reason") + 1]
        self.assertIn("mywaffles/newco.core#80", reason)
        self.assertIn(transition.idempotency_key, reason)
        self.assertIn("ackCommand=", reason)
        self.assertEqual(
            command[command.index("--idempotency-key") + 1],
            f"{transition.idempotency_key}:attempt:1",
        )
        payload = json.loads(command[command.index("--payload") + 1])
        self.assertEqual(payload["issueNumber"], 80)
        self.assertEqual(payload["taskKey"], transition.idempotency_key)
        self.assertEqual(payload["issueId"], "mapped-issue-id")

    def test_todo_mapping_is_parked_before_issue_bound_wake(self):
        backlog = issue_item("item-84", 84, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-84", 84, "Todo", "2026-08-08T12:10:00Z", "2026-05-01T00:00:00Z")
        now = when("2026-08-08T12:10:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), now)
        transition = self.store.next_transition(now)
        runner = CaptureRunner(
            [
                [
                    {
                        "id": "mapped-issue-id",
                        "identifier": "MAT-84",
                        "description": "GitHub: https://github.com/mywaffles/newco.core/issues/84",
                        "status": "todo",
                        "assigneeAgentId": None,
                    }
                ],
                [{"id": "manager-id", "urlKey": "dev-manager", "name": "Dev Manager"}],
                {"id": "mapped-issue-id", "status": "backlog", "assigneeAgentId": "manager-id"},
                {"id": "wake-run", "status": "queued"},
            ]
        )

        PaperclipClient(self.config, runner=runner).wake(transition)

        update_command = runner.calls[2][0]
        self.assertEqual(update_command[update_command.index("--status") + 1], "backlog")
        payload = json.loads(runner.calls[3][0][runner.calls[3][0].index("--payload") + 1])
        self.assertEqual(payload["issueId"], "mapped-issue-id")

    def test_mapping_owned_by_coder_is_not_stolen(self):
        backlog = issue_item("item-83", 83, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-83", 83, "Todo", "2026-08-08T12:10:00Z", "2026-05-01T00:00:00Z")
        now = when("2026-08-08T12:10:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), now)
        transition = self.store.next_transition(now)
        runner = CaptureRunner(
            [
                [
                    {
                        "id": "mapped-issue-id",
                        "identifier": "MAT-83",
                        "description": "GitHub: https://github.com/mywaffles/newco.core/issues/83",
                        "status": "in_progress",
                        "assigneeAgentId": "coder-id",
                    }
                ],
                [{"id": "manager-id", "urlKey": "dev-manager", "name": "Dev Manager"}],
                {"id": "wake-run", "status": "queued"},
            ]
        )

        PaperclipClient(self.config, runner=runner).wake(transition)

        self.assertEqual(len(runner.calls), 3)
        command = runner.calls[2][0]
        payload = json.loads(command[command.index("--payload") + 1])
        self.assertNotIn("issueId", payload)

    def test_paperclip_wake_without_mapping_has_no_issue_context(self):
        backlog = issue_item("item-81", 81, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-81", 81, "Todo", "2026-08-08T12:10:00Z", "2026-05-01T00:00:00Z")
        now = when("2026-08-08T12:10:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), now)
        transition = self.store.next_transition(now)
        runner = CaptureRunner([[], {"id": "wake-run", "status": "queued"}])

        PaperclipClient(self.config, runner=runner).wake(transition)

        command = runner.calls[1][0]
        payload = json.loads(command[command.index("--payload") + 1])
        self.assertNotIn("issueId", payload)
        self.assertIn("paperclipIssueId=none", command[command.index("--reason") + 1])

    def test_duplicate_paperclip_mappings_block_dispatch(self):
        backlog = issue_item("item-82", 82, "Backlog", "2026-08-08T11:00:00Z", "2026-05-01T00:00:00Z")
        self.baseline(backlog)
        todo = issue_item("item-82", 82, "Todo", "2026-08-08T12:10:00Z", "2026-05-01T00:00:00Z")
        now = when("2026-08-08T12:10:01Z")
        self.store.apply_snapshot(self.config, snapshot(todo), now)
        transition = self.store.next_transition(now)
        description = "GitHub: https://github.com/mywaffles/newco.core/issues/82"
        runner = CaptureRunner(
            [
                [
                    {"id": "one", "identifier": "MAT-1", "description": description},
                    {"id": "two", "identifier": "MAT-2", "description": description},
                ]
            ]
        )

        with self.assertRaisesRegex(MonitorError, "multiple Paperclip mappings"):
            PaperclipClient(self.config, runner=runner).wake(transition)


if __name__ == "__main__":
    unittest.main()
