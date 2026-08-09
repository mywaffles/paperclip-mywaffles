#!/usr/bin/env python3
"""Route signed GitHub issue events and personal Project Todo transitions."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse


GRAPHQL_QUERY = r"""
query ProjectTodoMonitor($login: String!, $number: Int!, $cursor: String) {
  user(login: $login) {
    projectV2(number: $number) {
      id
      title
      items(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          type
          isArchived
          updatedAt
          fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue {
              name
              optionId
              updatedAt
            }
          }
          content {
            __typename
            ... on Issue {
              number
              url
              createdAt
              labels(first: 50) {
                nodes {
                  name
                }
              }
              repository {
                nameWithOwner
              }
            }
          }
        }
      }
    }
  }
}
"""

UTC = dt.timezone.utc
SCHEMA_VERSION = "3"
DEFAULT_CONFIG_PATH = Path.home() / "Library" / "Application Support" / "Paperclip Automations" / "github-project-todo-monitor" / "config.json"
LIVE_RUN_STATUSES = {"queued", "running"}
PROJECT_EVENT_KIND = "github_project_status_transition"
WEBHOOK_EVENT_KIND = "github_issues_webhook"
PERIODIC_AUDIT_EVENT_KIND = "github_periodic_audit"
ROUTING_LABELS = frozenset({"dev-claude-max", "dev-codex-max"})
ROUTING_OUTCOMES = frozenset({"routed", "deferred", "ignored"})


class MonitorError(RuntimeError):
    """A safe, operator-facing monitor error."""


class CommandError(MonitorError):
    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


def utc_now() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def isoformat(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def compact_error(value: object, limit: int = 500) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def json_output(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


@dataclasses.dataclass(frozen=True)
class Config:
    github_owner: str
    github_project_number: int
    expected_project_title: str
    target_status: str
    github_webhook_hook_id: str
    github_webhook_repositories: Tuple[str, ...]
    webhook_listen_host: str
    webhook_listen_port: int
    webhook_path: str
    webhook_health_path: str
    webhook_public_url: str
    webhook_secret_command: Tuple[str, ...]
    webhook_secret_env_var: str
    webhook_max_body_bytes: int
    poll_interval_seconds: int
    github_command: Tuple[str, ...]
    paperclip_command: Tuple[str, ...]
    paperclip_api_base: str
    paperclip_company_id: str
    paperclip_agent: str
    paperclip_coding_agents: Tuple[str, ...]
    paperclip_api_key_command: Tuple[str, ...]
    paperclip_api_key_env_var: str
    state_path: Path
    log_path: Path
    lock_path: Path
    command_timeout_seconds: int
    delivery_ambiguity_seconds: int
    outcome_timeout_seconds: int
    deferred_retry_seconds: int
    audit_interval_seconds: int
    max_log_bytes: int
    log_backups: int

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise MonitorError(f"Config file does not exist: {path}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise MonitorError(f"Unable to read config {path}: {error}") from error
        if not isinstance(raw, dict):
            raise MonitorError("Config root must be a JSON object")

        def required_string(key: str) -> str:
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip():
                raise MonitorError(f"Config field {key!r} must be a non-empty string")
            return value.strip()

        def command(key: str, default: Optional[Sequence[str]] = None) -> Tuple[str, ...]:
            value = raw.get(key, default)
            if not isinstance(value, list) or not value or not all(isinstance(part, str) and part for part in value):
                raise MonitorError(f"Config field {key!r} must be a non-empty string array")
            return tuple(os.path.expanduser(part) for part in value)

        def integer(key: str, default: int, minimum: int, maximum: int) -> int:
            value = raw.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
                raise MonitorError(f"Config field {key!r} must be an integer from {minimum} through {maximum}")
            return value

        def path_value(key: str, default: str) -> str:
            value = raw.get(key, default)
            if not isinstance(value, str) or not value.startswith("/") or "?" in value or "#" in value:
                raise MonitorError(f"Config field {key!r} must be an absolute URL path without a query or fragment")
            return value.rstrip("/") or "/"

        repositories = raw.get("githubWebhookRepositories")
        if not isinstance(repositories, list) or not repositories:
            raise MonitorError("Config field 'githubWebhookRepositories' must be a non-empty owner/repository array")
        normalized_repository_values: List[str] = []
        for repository in repositories:
            normalized = repository.strip() if isinstance(repository, str) else ""
            parts = normalized.split("/")
            if len(parts) != 2 or not all(parts) or any(part != part.strip() for part in parts):
                raise MonitorError(
                    "Config field 'githubWebhookRepositories' must contain only owner/repository names"
                )
            normalized_repository_values.append(normalized)
        normalized_repositories = tuple(dict.fromkeys(normalized_repository_values))

        coding_agents = raw.get("paperclipCodingAgents", ["dev-claude-max", "dev-codex-max"])
        if not isinstance(coding_agents, list) or not all(
            isinstance(agent, str) and agent.strip() for agent in coding_agents
        ):
            raise MonitorError("Config field 'paperclipCodingAgents' must be a string array")
        normalized_coding_agents = tuple(dict.fromkeys(agent.strip() for agent in coding_agents))

        base_dir = path.parent

        def configured_path(key: str, filename: str) -> Path:
            value = raw.get(key)
            candidate = Path(os.path.expanduser(value)) if isinstance(value, str) and value.strip() else base_dir / filename
            return candidate.resolve()

        poll_interval = integer("pollIntervalSeconds", 10, 5, 15)
        webhook_path = path_value("webhookPath", "/")
        webhook_health_path = path_value("webhookHealthPath", "/health")
        if webhook_path == webhook_health_path:
            raise MonitorError("Webhook and health paths must be different")
        webhook_listen_host = str(raw.get("webhookListenHost", "127.0.0.1")).strip()
        if webhook_listen_host not in {"127.0.0.1", "localhost", "::1"}:
            raise MonitorError("Webhook listener must bind to a loopback address")
        webhook_public_url = required_string("webhookPublicUrl").rstrip("/")
        parsed_public_url = urlparse(webhook_public_url)
        if (
            parsed_public_url.scheme != "https"
            or not parsed_public_url.hostname
            or parsed_public_url.username
            or parsed_public_url.password
            or parsed_public_url.query
            or parsed_public_url.fragment
        ):
            raise MonitorError("Config field 'webhookPublicUrl' must be an HTTPS URL without a query or fragment")
        webhook_hook_id = required_string("githubWebhookHookId")
        if not webhook_hook_id.isdigit():
            raise MonitorError("Config field 'githubWebhookHookId' must contain a numeric GitHub hook ID")
        webhook_secret_env_var = str(raw.get("webhookSecretEnvVar", "GITHUB_ROUTER_WEBHOOK_SECRET")).strip()
        webhook_secret_command = command("webhookSecretCommand") if raw.get("webhookSecretCommand") else tuple()
        if not webhook_secret_env_var and not webhook_secret_command:
            raise MonitorError("Configure webhookSecretEnvVar or webhookSecretCommand")
        return cls(
            github_owner=required_string("githubOwner"),
            github_project_number=integer("githubProjectNumber", 4, 1, 1_000_000),
            expected_project_title=required_string("expectedProjectTitle"),
            target_status=required_string("targetStatus"),
            github_webhook_hook_id=webhook_hook_id,
            github_webhook_repositories=normalized_repositories,
            webhook_listen_host=webhook_listen_host,
            webhook_listen_port=integer("webhookListenPort", 8788, 1024, 65535),
            webhook_path=webhook_path,
            webhook_health_path=webhook_health_path,
            webhook_public_url=webhook_public_url,
            webhook_secret_command=webhook_secret_command,
            webhook_secret_env_var=webhook_secret_env_var,
            webhook_max_body_bytes=integer("webhookMaxBodyBytes", 1_048_576, 1024, 10_485_760),
            poll_interval_seconds=poll_interval,
            github_command=command("githubCommand", ["gh"]),
            paperclip_command=command("paperclipCommand", ["paperclipai"]),
            paperclip_api_base=required_string("paperclipApiBase").rstrip("/"),
            paperclip_company_id=required_string("paperclipCompanyId"),
            paperclip_agent=required_string("paperclipAgent"),
            paperclip_coding_agents=normalized_coding_agents,
            paperclip_api_key_command=command("paperclipApiKeyCommand") if raw.get("paperclipApiKeyCommand") else tuple(),
            paperclip_api_key_env_var=str(raw.get("paperclipApiKeyEnvVar", "PAPERCLIP_MONITOR_API_KEY")).strip(),
            state_path=configured_path("statePath", "state.sqlite3"),
            log_path=configured_path("logPath", "monitor.log"),
            lock_path=configured_path("lockPath", "monitor.lock"),
            command_timeout_seconds=integer("commandTimeoutSeconds", 12, 5, 60),
            delivery_ambiguity_seconds=integer("deliveryAmbiguitySeconds", 120, 30, 900),
            outcome_timeout_seconds=integer("outcomeTimeoutSeconds", 900, 60, 3600),
            deferred_retry_seconds=integer("deferredRetrySeconds", 60, 15, 900),
            audit_interval_seconds=integer("auditIntervalSeconds", 1800, 300, 86400),
            max_log_bytes=integer("maxLogBytes", 1_048_576, 65_536, 100_000_000),
            log_backups=integer("logBackups", 3, 1, 20),
        )


@dataclasses.dataclass(frozen=True)
class ProjectItem:
    item_id: str
    item_updated_at: str
    status: Optional[str]
    status_option_id: Optional[str]
    status_updated_at: Optional[str]
    content_type: str
    repository: Optional[str]
    issue_number: Optional[int]
    issue_url: Optional[str]
    issue_created_at: Optional[str]
    labels: Tuple[str, ...] = tuple()
    archived: bool = False


@dataclasses.dataclass(frozen=True)
class ProjectSnapshot:
    project_id: str
    project_title: str
    items: Tuple[ProjectItem, ...]


@dataclasses.dataclass(frozen=True)
class Transition:
    idempotency_key: str
    source_kind: str
    source_delivery_id: Optional[str]
    github_event: Optional[str]
    github_action: Optional[str]
    project_item_id: str
    from_status: Optional[str]
    to_status: str
    status_updated_at: str
    repository: str
    issue_number: int
    issue_url: str
    issue_created_at: str
    detected_at: str
    delivery_status: str
    delivery_attempts: int
    attempt_started_at: Optional[str]
    next_attempt_at: Optional[str]
    paperclip_run_id: Optional[str]
    routing_outcome: Optional[str]
    outcome_reason: Optional[str]
    outcome_at: Optional[str]
    last_error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Transition":
        return cls(**{field.name: row[field.name] for field in dataclasses.fields(cls)})


def transition_key(config: Config, item: ProjectItem) -> str:
    transition_time = item.status_updated_at or item.item_updated_at
    material = "\0".join(
        [config.github_owner, str(config.github_project_number), item.item_id, transition_time, config.target_status]
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:32]
    return f"github-project-todo:v1:{config.github_owner}:{config.github_project_number}:{digest}"


def webhook_key(
    config: Config,
    delivery_id: str,
    action: str,
    repository: str,
    issue_number: int,
) -> str:
    material = "\0".join(
        [config.github_webhook_hook_id, delivery_id, "issues", action, repository, str(issue_number)]
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:32]
    return f"github-issues:v1:{config.github_webhook_hook_id}:{digest}"


def wake_payload(config: Config, transition: Transition) -> Dict[str, object]:
    acknowledge = str(config.state_path.parent / "monitorctl")
    payload: Dict[str, object] = {
        "event": transition.source_kind,
        "repository": transition.repository,
        "issueNumber": transition.issue_number,
        "issueUrl": transition.issue_url,
        "projectNumber": config.github_project_number,
        "status": transition.to_status,
        "taskKey": transition.idempotency_key,
        "idempotencyKey": transition.idempotency_key,
        "deliveryAttempt": transition.delivery_attempts + 1,
        "acknowledge": {
            "command": [acknowledge, "ack", transition.idempotency_key],
            "outcomes": sorted(ROUTING_OUTCOMES),
            "reasonFlag": "--reason",
        },
        "instruction": (
            "Handle only this GitHub issue. Reuse the existing GitHub↔Paperclip mapping. "
            "Do not run a routing routine and do not create a `Route GitHub ToDo issues` wrapper. "
            "Before exiting, acknowledge exactly one outcome with the supplied monitorctl command: "
            "routed only after verifying the mapping, links, assignment, and routing label; deferred when "
            "the issue is still eligible but no safe coding slot exists; ignored only when it is no longer eligible."
        ),
    }
    if transition.source_kind == WEBHOOK_EVENT_KIND:
        payload.update(
            {
                "githubEvent": transition.github_event,
                "action": transition.github_action,
                "deliveryId": transition.source_delivery_id,
            }
        )
    return payload


def verify_webhook_signature(secret: bytes, body: bytes, signature: Optional[str]) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@dataclasses.dataclass(frozen=True)
class WebhookIssue:
    delivery_id: str
    action: str
    repository: str
    issue_number: int
    issue_url: str
    issue_created_at: str
    issue_updated_at: str
    status: str


def parse_issue_webhook(config: Config, delivery_id: str, payload: object) -> WebhookIssue:
    if not delivery_id or len(delivery_id) > 200:
        raise MonitorError("GitHub delivery ID is missing or too long")
    if not isinstance(payload, dict):
        raise MonitorError("GitHub issues payload must be a JSON object")
    action = payload.get("action")
    issue = payload.get("issue")
    repository_value = payload.get("repository")
    repository = repository_value.get("full_name") if isinstance(repository_value, dict) else None
    if not isinstance(action, str) or not action or len(action) > 100:
        raise MonitorError("GitHub issues payload omitted a valid action")
    if not isinstance(repository, str) or repository.casefold() not in {
        allowed.casefold() for allowed in config.github_webhook_repositories
    }:
        raise MonitorError(f"GitHub repository is not allowed: {repository!r}")
    if not isinstance(issue, dict):
        raise MonitorError("GitHub issues payload omitted issue metadata")
    number = issue.get("number")
    url = issue.get("html_url")
    created_at = issue.get("created_at")
    updated_at = issue.get("updated_at")
    status = issue.get("state")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise MonitorError("GitHub issues payload omitted a valid issue number")
    if not isinstance(url, str) or not url.startswith(f"https://github.com/{repository}/issues/"):
        raise MonitorError("GitHub issues payload omitted a valid issue URL")
    try:
        parsed_created_at = parse_time(created_at) if isinstance(created_at, str) else None
        parsed_updated_at = parse_time(updated_at) if isinstance(updated_at, str) else None
    except ValueError as error:
        raise MonitorError("GitHub issues payload contained an invalid issue timestamp") from error
    if parsed_created_at is None:
        raise MonitorError("GitHub issues payload omitted a valid issue creation time")
    if parsed_updated_at is None:
        raise MonitorError("GitHub issues payload omitted a valid issue update time")
    if not isinstance(status, str) or not status:
        raise MonitorError("GitHub issues payload omitted a valid issue state")
    return WebhookIssue(
        delivery_id=delivery_id,
        action=action,
        repository=repository,
        issue_number=number,
        issue_url=url,
        issue_created_at=created_at,
        issue_updated_at=updated_at,
        status=status,
    )


class JsonCommandRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        input_value: Optional[Mapping[str, object]] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout: int,
    ) -> object:
        input_text = json.dumps(input_value) if input_value is not None else None
        try:
            result = subprocess.run(
                list(command),
                input=input_text,
                capture_output=True,
                text=True,
                env=dict(env) if env is not None else None,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise CommandError(f"Command timed out after {timeout}s: {command[0]}", ambiguous=True) from error
        except OSError as error:
            raise CommandError(f"Unable to execute {command[0]}: {error}") from error
        if result.returncode != 0:
            detail = compact_error(result.stderr or result.stdout or f"exit {result.returncode}")
            raise CommandError(f"{command[0]} failed: {detail}", ambiguous=True)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CommandError(f"{command[0]} returned invalid JSON") from error


def load_webhook_secret(config: Config) -> bytes:
    value = os.environ.get(config.webhook_secret_env_var, "").strip() if config.webhook_secret_env_var else ""
    if not value and config.webhook_secret_command:
        try:
            result = subprocess.run(
                list(config.webhook_secret_command),
                capture_output=True,
                text=True,
                timeout=config.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MonitorError(f"Unable to read GitHub webhook secret: {compact_error(error)}") from error
        value = result.stdout.strip()
        if result.returncode != 0 or not value:
            detail = compact_error(result.stderr or "credential command returned no value")
            raise MonitorError(f"Unable to read GitHub webhook secret: {detail}")
    if len(value.encode("utf-8")) < 32:
        raise MonitorError("GitHub webhook secret must contain at least 32 bytes")
    return value.encode("utf-8")


class GitHubClient:
    def __init__(self, config: Config, runner: Optional[JsonCommandRunner] = None) -> None:
        self.config = config
        self.runner = runner or JsonCommandRunner()

    def fetch_project(self) -> ProjectSnapshot:
        cursor: Optional[str] = None
        project_id: Optional[str] = None
        project_title: Optional[str] = None
        items: List[ProjectItem] = []
        while True:
            response = self.runner.run(
                [*self.config.github_command, "api", "graphql", "--input", "-"],
                input_value={
                    "query": GRAPHQL_QUERY,
                    "variables": {
                        "login": self.config.github_owner,
                        "number": self.config.github_project_number,
                        "cursor": cursor,
                    },
                },
                timeout=self.config.command_timeout_seconds,
            )
            if not isinstance(response, dict):
                raise MonitorError("GitHub GraphQL response was not an object")
            errors = response.get("errors")
            if errors:
                raise MonitorError(f"GitHub GraphQL returned errors: {compact_error(errors)}")
            data = response.get("data")
            user = data.get("user") if isinstance(data, dict) else None
            project = user.get("projectV2") if isinstance(user, dict) else None
            if not isinstance(project, dict):
                raise MonitorError(
                    f"GitHub Project {self.config.github_owner}/{self.config.github_project_number} was not found or is not readable"
                )
            current_id = project.get("id")
            current_title = project.get("title")
            if not isinstance(current_id, str) or not isinstance(current_title, str):
                raise MonitorError("GitHub Project response omitted id or title")
            project_id = project_id or current_id
            project_title = project_title or current_title
            if current_id != project_id or current_title != project_title:
                raise MonitorError("GitHub Project identity changed during pagination")
            connection = project.get("items")
            if not isinstance(connection, dict):
                raise MonitorError("GitHub Project response omitted items")
            nodes = connection.get("nodes")
            if not isinstance(nodes, list):
                raise MonitorError("GitHub Project items response was not a list")
            for node in nodes:
                parsed = self._parse_item(node)
                if parsed is not None:
                    items.append(parsed)
            page_info = connection.get("pageInfo")
            if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
                break
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise MonitorError("GitHub Project pagination returned an invalid cursor")
            cursor = next_cursor

        assert project_id is not None and project_title is not None
        if project_title != self.config.expected_project_title:
            raise MonitorError(
                f"Project title mismatch: expected {self.config.expected_project_title!r}, got {project_title!r}"
            )
        return ProjectSnapshot(project_id=project_id, project_title=project_title, items=tuple(items))

    @staticmethod
    def _parse_item(value: object) -> Optional[ProjectItem]:
        if not isinstance(value, dict):
            return None
        item_id = value.get("id")
        item_updated_at = value.get("updatedAt")
        if not isinstance(item_id, str) or not isinstance(item_updated_at, str):
            return None
        field_value = value.get("fieldValueByName")
        status = field_value.get("name") if isinstance(field_value, dict) else None
        option_id = field_value.get("optionId") if isinstance(field_value, dict) else None
        status_updated_at = field_value.get("updatedAt") if isinstance(field_value, dict) else None
        content = value.get("content")
        content_type = content.get("__typename") if isinstance(content, dict) else str(value.get("type") or "UNKNOWN")
        repository_value = content.get("repository") if isinstance(content, dict) else None
        repository = repository_value.get("nameWithOwner") if isinstance(repository_value, dict) else None
        issue_number = content.get("number") if isinstance(content, dict) else None
        labels_value = content.get("labels") if isinstance(content, dict) else None
        label_nodes = labels_value.get("nodes") if isinstance(labels_value, dict) else []
        labels = tuple(
            label["name"]
            for label in label_nodes
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        ) if isinstance(label_nodes, list) else tuple()
        return ProjectItem(
            item_id=item_id,
            item_updated_at=item_updated_at,
            status=status if isinstance(status, str) else None,
            status_option_id=option_id if isinstance(option_id, str) else None,
            status_updated_at=status_updated_at if isinstance(status_updated_at, str) else None,
            content_type=content_type if isinstance(content_type, str) else "UNKNOWN",
            repository=repository if isinstance(repository, str) else None,
            issue_number=issue_number if isinstance(issue_number, int) and not isinstance(issue_number, bool) else None,
            issue_url=content.get("url") if isinstance(content, dict) and isinstance(content.get("url"), str) else None,
            issue_created_at=(
                content.get("createdAt")
                if isinstance(content, dict) and isinstance(content.get("createdAt"), str)
                else None
            ),
            labels=labels,
            archived=bool(value.get("isArchived")),
        )


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def close(self) -> None:
        self.db.close()

    def _initialize(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS monitor_status (
              singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
              daemon_pid INTEGER,
              daemon_started_at TEXT,
              daemon_heartbeat_at TEXT,
              last_poll_started_at TEXT,
              last_poll_succeeded_at TEXT,
              last_poll_error TEXT,
              last_project_id TEXT,
              last_project_title TEXT,
              last_item_count INTEGER,
              webhook_listener_started_at TEXT,
              last_webhook_received_at TEXT,
              last_webhook_delivery_id TEXT,
              last_webhook_event TEXT,
              last_webhook_action TEXT,
              last_webhook_error TEXT,
              last_dispatch_at TEXT,
              last_dispatch_error TEXT
            );

            CREATE TABLE IF NOT EXISTS project_items (
              item_id TEXT PRIMARY KEY,
              item_updated_at TEXT NOT NULL,
              status TEXT,
              status_option_id TEXT,
              status_updated_at TEXT,
              content_type TEXT NOT NULL,
              repository TEXT,
              issue_number INTEGER,
              issue_url TEXT,
              issue_created_at TEXT,
              archived INTEGER NOT NULL DEFAULT 0,
              present INTEGER NOT NULL DEFAULT 1,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transitions (
              idempotency_key TEXT PRIMARY KEY,
              source_kind TEXT NOT NULL DEFAULT 'github_project_status_transition',
              source_delivery_id TEXT,
              github_event TEXT,
              github_action TEXT,
              project_item_id TEXT NOT NULL,
              from_status TEXT,
              to_status TEXT NOT NULL,
              status_updated_at TEXT NOT NULL,
              repository TEXT NOT NULL,
              issue_number INTEGER NOT NULL,
              issue_url TEXT NOT NULL,
              issue_created_at TEXT NOT NULL,
              detected_at TEXT NOT NULL,
              delivery_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (delivery_status IN ('pending', 'delivering', 'delivered')),
              delivery_attempts INTEGER NOT NULL DEFAULT 0,
              attempt_started_at TEXT,
              next_attempt_at TEXT,
              delivered_at TEXT,
              paperclip_run_id TEXT,
              routing_outcome TEXT,
              outcome_reason TEXT,
              outcome_at TEXT,
              last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS webhook_receipts (
              delivery_id TEXT PRIMARY KEY,
              hook_id TEXT NOT NULL,
              github_event TEXT NOT NULL,
              github_action TEXT,
              repository TEXT,
              issue_number INTEGER,
              received_at TEXT NOT NULL,
              result TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS transitions_delivery_order_idx
              ON transitions (delivery_status, next_attempt_at, issue_created_at, detected_at, idempotency_key);
            """
        )
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO monitor_status(singleton) VALUES (1)")
            self.db.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
            )
        version = self.get_metadata("schema_version")
        if version not in {"1", "2", SCHEMA_VERSION}:
            raise MonitorError(f"Unsupported state schema version: {version!r}")
        transition_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(transitions)").fetchall()
        }
        status_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(monitor_status)").fetchall()
        }
        if version == "1" or not {
            "source_kind",
            "source_delivery_id",
            "github_event",
            "github_action",
        }.issubset(transition_columns) or not {
            "webhook_listener_started_at",
            "last_webhook_received_at",
            "last_webhook_delivery_id",
            "last_webhook_event",
            "last_webhook_action",
            "last_webhook_error",
        }.issubset(status_columns):
            self._migrate_v1_to_v2()
            version = "2"
            transition_columns = {
                row["name"] for row in self.db.execute("PRAGMA table_info(transitions)").fetchall()
            }
        if version == "2" or not {"routing_outcome", "outcome_reason", "outcome_at"}.issubset(
            transition_columns
        ):
            self._migrate_v2_to_v3()
        with self.db:
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS transitions_source_delivery_idx "
                "ON transitions(source_delivery_id) WHERE source_delivery_id IS NOT NULL"
            )

    def _migrate_v1_to_v2(self) -> None:
        transition_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(transitions)").fetchall()
        }
        status_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(monitor_status)").fetchall()
        }
        with self.db:
            for name, declaration in (
                ("source_kind", "TEXT NOT NULL DEFAULT 'github_project_status_transition'"),
                ("source_delivery_id", "TEXT"),
                ("github_event", "TEXT"),
                ("github_action", "TEXT"),
            ):
                if name not in transition_columns:
                    self.db.execute(f"ALTER TABLE transitions ADD COLUMN {name} {declaration}")
            for name, declaration in (
                ("webhook_listener_started_at", "TEXT"),
                ("last_webhook_received_at", "TEXT"),
                ("last_webhook_delivery_id", "TEXT"),
                ("last_webhook_event", "TEXT"),
                ("last_webhook_action", "TEXT"),
                ("last_webhook_error", "TEXT"),
            ):
                if name not in status_columns:
                    self.db.execute(f"ALTER TABLE monitor_status ADD COLUMN {name} {declaration}")
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_receipts (
                  delivery_id TEXT PRIMARY KEY,
                  hook_id TEXT NOT NULL,
                  github_event TEXT NOT NULL,
                  github_action TEXT,
                  repository TEXT,
                  issue_number INTEGER,
                  received_at TEXT NOT NULL,
                  result TEXT NOT NULL
                )
                """
            )
            self.set_metadata("schema_version", "2")

    def _migrate_v2_to_v3(self) -> None:
        transition_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(transitions)").fetchall()
        }
        with self.db:
            for name in ("routing_outcome", "outcome_reason", "outcome_at"):
                if name not in transition_columns:
                    self.db.execute(f"ALTER TABLE transitions ADD COLUMN {name} TEXT")
            self.set_metadata("schema_version", SCHEMA_VERSION)

    def get_metadata(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def baseline_complete(self) -> bool:
        return self.get_metadata("baseline_complete") == "1"

    def periodic_audit_due(self, config: Config, now: dt.datetime) -> bool:
        previous = parse_time(self.get_metadata("last_periodic_audit_at"))
        return previous is None or (now - previous).total_seconds() >= config.audit_interval_seconds

    def record_periodic_audit(self, now: dt.datetime, run_id: str) -> None:
        with self.db:
            self.set_metadata("last_periodic_audit_at", isoformat(now))
            self.set_metadata("last_periodic_audit_run_id", run_id)

    def record_daemon_heartbeat(self, now: dt.datetime, *, started_at: Optional[dt.datetime] = None) -> None:
        now_text = isoformat(now)
        with self.db:
            self.db.execute(
                """
                UPDATE monitor_status
                   SET daemon_pid = ?,
                       daemon_started_at = COALESCE(?, daemon_started_at),
                       daemon_heartbeat_at = ?
                 WHERE singleton = 1
                """,
                (os.getpid(), isoformat(started_at) if started_at else None, now_text),
            )

    def clear_daemon_pid(self) -> None:
        with self.db:
            self.db.execute("UPDATE monitor_status SET daemon_pid = NULL WHERE singleton = 1")

    def record_poll_started(self, now: dt.datetime) -> None:
        with self.db:
            self.db.execute(
                "UPDATE monitor_status SET last_poll_started_at = ? WHERE singleton = 1", (isoformat(now),)
            )

    def record_poll_failure(self, error: object) -> None:
        with self.db:
            self.db.execute(
                "UPDATE monitor_status SET last_poll_error = ? WHERE singleton = 1", (compact_error(error),)
            )

    def apply_snapshot(self, config: Config, snapshot: ProjectSnapshot, now: dt.datetime) -> int:
        now_text = isoformat(now)
        target = config.target_status.casefold()
        queued = 0
        with self.db:
            baseline = self.baseline_complete()
            recorded_project_id = self.get_metadata("github_project_id")
            if baseline and recorded_project_id and recorded_project_id != snapshot.project_id:
                raise MonitorError(
                    f"GitHub Project node changed from {recorded_project_id} to {snapshot.project_id}; "
                    "refusing to treat a replacement board as incremental state"
                )
            self.db.execute("UPDATE project_items SET present = 0")
            previous_rows = {
                row["item_id"]: row
                for row in self.db.execute("SELECT item_id, status, present FROM project_items").fetchall()
            }
            for item in snapshot.items:
                previous = previous_rows.get(item.item_id)
                in_target = (
                    not item.archived
                    and item.content_type == "Issue"
                    and isinstance(item.status, str)
                    and item.status.casefold() == target
                )
                has_routing_label = any(label.casefold() in ROUTING_LABELS for label in item.labels)
                if has_routing_label and item.repository and item.issue_number is not None:
                    self.db.execute(
                        """
                        UPDATE transitions
                           SET delivery_status = 'delivered', delivered_at = ?, routing_outcome = 'routed',
                               outcome_reason = COALESCE(outcome_reason, 'Observed routing label during Project reconciliation'),
                               outcome_at = COALESCE(outcome_at, ?), next_attempt_at = NULL, last_error = NULL
                         WHERE repository = ? AND issue_number = ?
                           AND source_kind = ?
                           AND delivery_status IN ('pending', 'delivering')
                        """,
                        (now_text, now_text, item.repository, item.issue_number, PROJECT_EVENT_KIND),
                    )
                elif in_target:
                    if not all(
                        [
                            item.repository,
                            item.issue_number is not None,
                            item.issue_url,
                            item.issue_created_at,
                        ]
                    ):
                        raise MonitorError(f"Issue item {item.item_id} is in Todo without complete issue metadata")
                    key = transition_key(config, item)
                    cursor = self.db.execute(
                        """
                        INSERT OR IGNORE INTO transitions(
                          idempotency_key, source_kind, project_item_id, from_status, to_status,
                          status_updated_at, repository, issue_number, issue_url,
                          issue_created_at, detected_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            PROJECT_EVENT_KIND,
                            item.item_id,
                            previous["status"] if previous else None,
                            item.status,
                            item.status_updated_at or item.item_updated_at,
                            item.repository,
                            item.issue_number,
                            item.issue_url,
                            item.issue_created_at,
                            now_text,
                        ),
                    )
                    queued += cursor.rowcount
                    if cursor.rowcount == 0:
                        revived = self.db.execute(
                            """
                            UPDATE transitions
                               SET delivery_status = 'pending', delivered_at = NULL, paperclip_run_id = NULL,
                                   next_attempt_at = NULL,
                                   last_error = 'Reconciled legacy delivery without a routing outcome'
                             WHERE idempotency_key = ? AND delivery_status = 'delivered'
                               AND routing_outcome IS NULL
                            """,
                            (key,),
                        )
                        queued += revived.rowcount
                elif previous is not None:
                    self.db.execute(
                        """
                        UPDATE transitions
                           SET delivery_status = 'delivered', delivered_at = ?, routing_outcome = 'ignored',
                               outcome_reason = 'Project item left Todo before routing', outcome_at = ?,
                               next_attempt_at = NULL, last_error = NULL
                         WHERE source_kind = ? AND project_item_id = ?
                           AND delivery_status IN ('pending', 'delivering')
                        """,
                        (now_text, now_text, PROJECT_EVENT_KIND, item.item_id),
                    )

                self.db.execute(
                    """
                    INSERT INTO project_items(
                      item_id, item_updated_at, status, status_option_id, status_updated_at,
                      content_type, repository, issue_number, issue_url, issue_created_at,
                      archived, present, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                      item_updated_at = excluded.item_updated_at,
                      status = excluded.status,
                      status_option_id = excluded.status_option_id,
                      status_updated_at = excluded.status_updated_at,
                      content_type = excluded.content_type,
                      repository = excluded.repository,
                      issue_number = excluded.issue_number,
                      issue_url = excluded.issue_url,
                      issue_created_at = excluded.issue_created_at,
                      archived = excluded.archived,
                      present = excluded.present,
                      last_seen_at = excluded.last_seen_at
                    """,
                    (
                        item.item_id,
                        item.item_updated_at,
                        item.status,
                        item.status_option_id,
                        item.status_updated_at,
                        item.content_type,
                        item.repository,
                        item.issue_number,
                        item.issue_url,
                        item.issue_created_at,
                        int(item.archived),
                        int(not item.archived),
                        now_text,
                        now_text,
                    ),
                )

            if not baseline:
                self.set_metadata("baseline_complete", "1")
                self.set_metadata("baseline_created_at", now_text)
            self.set_metadata("github_project_id", snapshot.project_id)
            self.db.execute(
                """
                UPDATE monitor_status
                   SET last_poll_succeeded_at = ?,
                       last_poll_error = NULL,
                       last_project_id = ?,
                       last_project_title = ?,
                       last_item_count = ?
                 WHERE singleton = 1
                """,
                (now_text, snapshot.project_id, snapshot.project_title, len(snapshot.items)),
            )
        return queued

    def record_webhook_listener_started(self, now: dt.datetime) -> None:
        with self.db:
            self.db.execute(
                "UPDATE monitor_status SET webhook_listener_started_at = ? WHERE singleton = 1",
                (isoformat(now),),
            )

    def record_webhook_failure(self, error: object) -> None:
        with self.db:
            self.db.execute(
                "UPDATE monitor_status SET last_webhook_error = ? WHERE singleton = 1",
                (compact_error(error),),
            )

    def record_webhook_delivery(
        self,
        config: Config,
        *,
        delivery_id: str,
        hook_id: str,
        github_event: str,
        issue: Optional[WebhookIssue],
        received_at: dt.datetime,
    ) -> str:
        received_text = isoformat(received_at)
        action = issue.action if issue else None
        repository = issue.repository if issue else None
        issue_number = issue.issue_number if issue else None
        result = "queued" if issue else ("ping" if github_event == "ping" else "ignored")
        with self.db:
            receipt = self.db.execute(
                """
                INSERT OR IGNORE INTO webhook_receipts(
                  delivery_id, hook_id, github_event, github_action,
                  repository, issue_number, received_at, result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    hook_id,
                    github_event,
                    action,
                    repository,
                    issue_number,
                    received_text,
                    result,
                ),
            )
            if receipt.rowcount == 0:
                return "duplicate"
            if issue is not None:
                key = webhook_key(
                    config,
                    issue.delivery_id,
                    issue.action,
                    issue.repository,
                    issue.issue_number,
                )
                transition = self.db.execute(
                    """
                    INSERT OR IGNORE INTO transitions(
                      idempotency_key, source_kind, source_delivery_id, github_event,
                      github_action, project_item_id, from_status, to_status,
                      status_updated_at, repository, issue_number, issue_url,
                      issue_created_at, detected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        WEBHOOK_EVENT_KIND,
                        issue.delivery_id,
                        github_event,
                        issue.action,
                        f"webhook:{issue.delivery_id}",
                        None,
                        issue.status,
                        issue.issue_updated_at,
                        issue.repository,
                        issue.issue_number,
                        issue.issue_url,
                        issue.issue_created_at,
                        received_text,
                    ),
                )
                if transition.rowcount != 1:
                    raise MonitorError("Webhook receipt collided with an existing routing event")
            self.db.execute(
                """
                UPDATE monitor_status
                   SET last_webhook_received_at = ?,
                       last_webhook_delivery_id = ?,
                       last_webhook_event = ?,
                       last_webhook_action = ?,
                       last_webhook_error = NULL
                 WHERE singleton = 1
                """,
                (received_text, delivery_id, github_event, action),
            )
        return result

    def webhook_receipt_count(self) -> int:
        row = self.db.execute("SELECT COUNT(*) AS count FROM webhook_receipts").fetchone()
        return int(row["count"] if row else 0)

    def next_transition(self, now: dt.datetime) -> Optional[Transition]:
        row = self.db.execute(
            """
            SELECT idempotency_key, source_kind, source_delivery_id, github_event,
                   github_action, project_item_id, from_status, to_status,
                   status_updated_at, repository, issue_number, issue_url,
                   issue_created_at, detected_at, delivery_status, delivery_attempts,
                   attempt_started_at, next_attempt_at, paperclip_run_id,
                   routing_outcome, outcome_reason, outcome_at, last_error
             FROM transitions
             WHERE delivery_status = 'pending'
             ORDER BY issue_created_at ASC, detected_at ASC, idempotency_key ASC
             LIMIT 1
            """,
        ).fetchone()
        if row is None:
            return None
        transition = Transition.from_row(row)
        retry_at = parse_time(transition.next_attempt_at)
        return transition if retry_at is None or retry_at <= now else None

    def has_pending_transitions(self) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM transitions WHERE delivery_status = 'pending' LIMIT 1"
        ).fetchone()
        return row is not None

    def delivering_transitions(self) -> List[Transition]:
        rows = self.db.execute(
            """
            SELECT idempotency_key, source_kind, source_delivery_id, github_event,
                   github_action, project_item_id, from_status, to_status,
                   status_updated_at, repository, issue_number, issue_url,
                   issue_created_at, detected_at, delivery_status, delivery_attempts,
                   attempt_started_at, next_attempt_at, paperclip_run_id,
                   routing_outcome, outcome_reason, outcome_at, last_error
              FROM transitions
             WHERE delivery_status = 'delivering'
             ORDER BY issue_created_at ASC, detected_at ASC, idempotency_key ASC
            """
        ).fetchall()
        return [Transition.from_row(row) for row in rows]

    def mark_delivering(self, key: str, now: dt.datetime) -> bool:
        with self.db:
            cursor = self.db.execute(
                """
                UPDATE transitions
                   SET delivery_status = 'delivering',
                       delivery_attempts = delivery_attempts + 1,
                       attempt_started_at = ?,
                       next_attempt_at = NULL,
                       routing_outcome = NULL,
                       outcome_reason = NULL,
                       outcome_at = NULL,
                       last_error = NULL
                 WHERE idempotency_key = ? AND delivery_status = 'pending'
                """,
                (isoformat(now), key),
            )
        return cursor.rowcount == 1

    def record_delivery_error(self, key: str, error: object) -> None:
        message = compact_error(error)
        with self.db:
            self.db.execute(
                "UPDATE transitions SET last_error = ? WHERE idempotency_key = ?", (message, key)
            )
            self.db.execute(
                "UPDATE monitor_status SET last_dispatch_error = ? WHERE singleton = 1", (message,)
            )

    def mark_delivery_accepted(self, key: str, now: dt.datetime, run_id: str) -> None:
        with self.db:
            self.db.execute(
                """
                UPDATE transitions
                   SET paperclip_run_id = ?, last_error = NULL
                 WHERE idempotency_key = ? AND delivery_status = 'delivering'
                """,
                (run_id, key),
            )
            self.db.execute(
                "UPDATE monitor_status SET last_dispatch_at = ?, last_dispatch_error = NULL WHERE singleton = 1",
                (isoformat(now),),
            )

    def record_dispatch_failure(self, error: object) -> None:
        with self.db:
            self.db.execute(
                "UPDATE monitor_status SET last_dispatch_error = ? WHERE singleton = 1",
                (compact_error(error),),
            )

    def mark_delivered(
        self,
        key: str,
        now: dt.datetime,
        run_id: Optional[str],
        *,
        outcome: str,
        reason: str,
    ) -> None:
        if outcome not in {"routed", "ignored"}:
            raise MonitorError(f"Terminal routing outcome must be routed or ignored, got {outcome!r}")
        with self.db:
            self.db.execute(
                """
                UPDATE transitions
                   SET delivery_status = 'delivered', delivered_at = ?, paperclip_run_id = ?,
                       routing_outcome = ?, outcome_reason = ?, outcome_at = ?,
                       next_attempt_at = NULL, last_error = NULL
                 WHERE idempotency_key = ?
                """,
                (isoformat(now), run_id, outcome, compact_error(reason), isoformat(now), key),
            )
            self.db.execute(
                "UPDATE monitor_status SET last_dispatch_at = ?, last_dispatch_error = NULL WHERE singleton = 1",
                (isoformat(now),),
            )

    def mark_retry(
        self,
        transition: Transition,
        now: dt.datetime,
        reason: str,
        *,
        delay_seconds: Optional[int] = None,
    ) -> None:
        exponent = max(0, min(transition.delivery_attempts - 1, 5))
        delay_seconds = delay_seconds if delay_seconds is not None else min(300, 15 * (2 ** exponent))
        retry_at = now + dt.timedelta(seconds=delay_seconds)
        with self.db:
            self.db.execute(
                """
                UPDATE transitions
                   SET delivery_status = 'pending', attempt_started_at = NULL,
                       next_attempt_at = ?, paperclip_run_id = NULL,
                       routing_outcome = 'deferred', outcome_reason = ?, outcome_at = ?, last_error = ?
                 WHERE idempotency_key = ? AND delivery_status = 'delivering'
                """,
                (
                    isoformat(retry_at),
                    compact_error(reason),
                    isoformat(now),
                    compact_error(reason),
                    transition.idempotency_key,
                ),
            )

    def acknowledge(self, key: str, outcome: str, reason: str, now: dt.datetime, deferred_retry: int) -> str:
        if outcome not in ROUTING_OUTCOMES:
            raise MonitorError(f"Unknown routing outcome: {outcome!r}")
        if not reason.strip():
            raise MonitorError("Routing outcome requires a concise reason")
        row = self.db.execute("SELECT * FROM transitions WHERE idempotency_key = ?", (key,)).fetchone()
        if row is None:
            raise MonitorError(f"Unknown routing task key: {key}")
        transition = Transition.from_row(row)
        if transition.delivery_status == "delivered":
            if transition.routing_outcome == outcome:
                return "already_acknowledged"
            raise MonitorError(
                f"Routing task {key} was already acknowledged as {transition.routing_outcome or 'legacy-delivered'}"
            )
        if transition.delivery_status != "delivering":
            raise MonitorError(f"Routing task {key} is not awaiting an outcome")
        if outcome == "deferred":
            self.mark_retry(transition, now, reason, delay_seconds=deferred_retry)
            return "deferred"
        self.mark_delivered(
            key,
            now,
            transition.paperclip_run_id,
            outcome=outcome,
            reason=reason,
        )
        return outcome

    def transition_counts(self) -> Dict[str, int]:
        counts = {"pending": 0, "delivering": 0, "delivered": 0}
        for row in self.db.execute(
            "SELECT delivery_status, COUNT(*) AS count FROM transitions GROUP BY delivery_status"
        ).fetchall():
            counts[row["delivery_status"]] = row["count"]
        return counts

    def outcome_counts(self) -> Dict[str, int]:
        counts = {"routed": 0, "deferred": 0, "ignored": 0, "unacknowledged": 0}
        for row in self.db.execute(
            """
            SELECT COALESCE(routing_outcome, 'unacknowledged') AS outcome, COUNT(*) AS count
              FROM transitions
             GROUP BY COALESCE(routing_outcome, 'unacknowledged')
            """
        ).fetchall():
            counts[row["outcome"]] = row["count"]
        return counts

    def source_counts(self) -> Dict[str, int]:
        counts = {PROJECT_EVENT_KIND: 0, WEBHOOK_EVENT_KIND: 0}
        for row in self.db.execute(
            "SELECT source_kind, COUNT(*) AS count FROM transitions GROUP BY source_kind"
        ).fetchall():
            counts[row["source_kind"]] = row["count"]
        return counts

    def status_snapshot(self, config: Config, now: dt.datetime) -> Dict[str, object]:
        row = self.db.execute("SELECT * FROM monitor_status WHERE singleton = 1").fetchone()
        counts = self.transition_counts()
        baseline = self.baseline_complete()
        poll_time = parse_time(row["last_poll_succeeded_at"] if row else None)
        daemon_time = parse_time(row["daemon_heartbeat_at"] if row else None)
        stale_after = max(45, config.poll_interval_seconds * 3)
        poll_age = (now - poll_time).total_seconds() if poll_time else None
        daemon_age = (now - daemon_time).total_seconds() if daemon_time else None
        daemon_pid = row["daemon_pid"] if row else None
        process_alive = False
        if isinstance(daemon_pid, int) and daemon_pid > 1:
            try:
                os.kill(daemon_pid, 0)
                process_alive = True
            except OSError:
                process_alive = False
        reasons: List[str] = []
        if not baseline:
            reasons.append("baseline_missing")
        if poll_age is None or poll_age > stale_after:
            reasons.append("poll_stale")
        if daemon_age is None or daemon_age > stale_after or not process_alive:
            reasons.append("daemon_not_healthy")
        return {
            "status": "healthy" if not reasons else "unhealthy",
            "reasons": reasons,
            "baselineComplete": baseline,
            "baselineCreatedAt": self.get_metadata("baseline_created_at"),
            "project": {
                "owner": config.github_owner,
                "number": config.github_project_number,
                "id": row["last_project_id"] if row else None,
                "title": row["last_project_title"] if row else None,
                "itemCount": row["last_item_count"] if row else None,
            },
            "pollIntervalSeconds": config.poll_interval_seconds,
            "lastPollStartedAt": row["last_poll_started_at"] if row else None,
            "lastPollSucceededAt": row["last_poll_succeeded_at"] if row else None,
            "lastPollError": row["last_poll_error"] if row else None,
            "webhook": {
                "hookId": config.github_webhook_hook_id,
                "listenHost": config.webhook_listen_host,
                "listenPort": config.webhook_listen_port,
                "path": config.webhook_path,
                "healthPath": config.webhook_health_path,
                "publicUrl": config.webhook_public_url,
                "listenerStartedAt": row["webhook_listener_started_at"] if row else None,
                "lastReceivedAt": row["last_webhook_received_at"] if row else None,
                "lastDeliveryId": row["last_webhook_delivery_id"] if row else None,
                "lastEvent": row["last_webhook_event"] if row else None,
                "lastAction": row["last_webhook_action"] if row else None,
                "lastError": row["last_webhook_error"] if row else None,
                "receiptCount": self.webhook_receipt_count(),
            },
            "lastDispatchAt": row["last_dispatch_at"] if row else None,
            "lastDispatchError": row["last_dispatch_error"] if row else None,
            "periodicAudit": {
                "intervalSeconds": config.audit_interval_seconds,
                "lastDispatchedAt": self.get_metadata("last_periodic_audit_at"),
                "lastRunId": self.get_metadata("last_periodic_audit_run_id"),
            },
            "daemon": {
                "pid": daemon_pid,
                "startedAt": row["daemon_started_at"] if row else None,
                "heartbeatAt": row["daemon_heartbeat_at"] if row else None,
                "processAlive": process_alive,
            },
            "transitions": counts,
            "outcomes": self.outcome_counts(),
            "sourceCounts": self.source_counts(),
            "statePath": str(config.state_path),
            "logPath": str(config.log_path),
        }


class PaperclipClient:
    def __init__(self, config: Config, runner: Optional[JsonCommandRunner] = None) -> None:
        self.config = config
        self.runner = runner or JsonCommandRunner()
        self._api_key: Optional[str] = None
        self._agent_ids: Dict[str, str] = {}

    def _load_api_key(self) -> Optional[str]:
        if self._api_key:
            return self._api_key
        if self.config.paperclip_api_key_env_var:
            inherited = os.environ.get(self.config.paperclip_api_key_env_var, "").strip()
            if inherited:
                self._api_key = inherited
                return inherited
        if not self.config.paperclip_api_key_command:
            return None
        try:
            result = subprocess.run(
                list(self.config.paperclip_api_key_command),
                capture_output=True,
                text=True,
                timeout=self.config.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MonitorError(f"Unable to read Paperclip credential: {compact_error(error)}") from error
        token = result.stdout.strip()
        if result.returncode != 0 or not token:
            detail = compact_error(result.stderr or "credential command returned no value")
            raise MonitorError(f"Unable to read Paperclip credential: {detail}")
        self._api_key = token
        return token

    def _run(self, args: Sequence[str]) -> object:
        environment = dict(os.environ)
        environment.pop("PAPERCLIP_API_KEY", None)
        api_key = self._load_api_key()
        if api_key:
            environment["PAPERCLIP_API_KEY"] = api_key
        return self.runner.run(
            [*self.config.paperclip_command, *args, "--api-base", self.config.paperclip_api_base, "--json"],
            env=environment,
            timeout=self.config.command_timeout_seconds,
        )

    def probe_wake_command(self) -> None:
        try:
            result = subprocess.run(
                [*self.config.paperclip_command, "agent", "wake", "--help"],
                capture_output=True,
                text=True,
                timeout=self.config.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MonitorError(f"Unable to probe Paperclip wake command: {compact_error(error)}") from error
        if result.returncode != 0 or "Request a heartbeat wakeup" not in result.stdout:
            detail = compact_error(result.stderr or result.stdout or f"exit {result.returncode}")
            raise MonitorError(f"Configured Paperclip CLI does not support `agent wake`: {detail}")

    def resolve_agent_id(self, agent: Optional[str] = None) -> str:
        requested = agent or self.config.paperclip_agent
        wanted = requested.casefold()
        if wanted in self._agent_ids:
            return self._agent_ids[wanted]
        response = self._run(["agent", "list", "--company-id", self.config.paperclip_company_id])
        rows = response if isinstance(response, list) else []
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and (
                str(row.get("id", "")).casefold() == wanted
                or str(row.get("urlKey", "")).casefold() == wanted
                or str(row.get("name", "")).casefold() == wanted
            )
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
            raise MonitorError(f"Expected exactly one Paperclip agent matching {requested!r}")
        self._agent_ids[wanted] = matches[0]["id"]
        return self._agent_ids[wanted]

    def live_runs(self) -> List[Mapping[str, object]]:
        response = self._run(
            ["run", "live", "--company-id", self.config.paperclip_company_id, "--limit", "100"]
        )
        return [row for row in response if isinstance(row, dict)] if isinstance(response, list) else []

    def recent_runs(self) -> List[Mapping[str, object]]:
        response = self._run(
            [
                "run",
                "list",
                "--company-id",
                self.config.paperclip_company_id,
                "--agent-id",
                self.resolve_agent_id(),
                "--limit",
                "1000",
            ]
        )
        return [row for row in response if isinstance(row, dict)] if isinstance(response, list) else []

    @staticmethod
    def run_for_transition(
        runs: Iterable[Mapping[str, object]], key: str, run_id: Optional[str] = None
    ) -> Optional[Mapping[str, object]]:
        fallback: Optional[Mapping[str, object]] = None
        for run in runs:
            context = run.get("contextSnapshot")
            if isinstance(context, dict) and context.get("taskKey") == key:
                if run_id and run.get("id") == run_id:
                    return run
                if not run_id and fallback is None:
                    fallback = run
        return fallback

    def has_active_run(self) -> bool:
        agent_id = self.resolve_agent_id()
        return any(
            run.get("agentId") == agent_id and run.get("status") in LIVE_RUN_STATUSES for run in self.live_runs()
        )

    def coding_capacity_available(self) -> bool:
        if not self.config.paperclip_coding_agents:
            return True
        agent_ids = [self.resolve_agent_id(agent) for agent in self.config.paperclip_coding_agents]
        busy_ids = {
            str(run.get("agentId"))
            for run in self.live_runs()
            if run.get("status") in LIVE_RUN_STATUSES
        }
        return any(agent_id not in busy_ids for agent_id in agent_ids)

    def wake(self, transition: Transition) -> Mapping[str, object]:
        payload = wake_payload(self.config, transition)
        source = (
            f"issues/{transition.github_action or 'event'}"
            if transition.source_kind == WEBHOOK_EVENT_KIND
            else f"Project {self.config.github_project_number}/{self.config.target_status}"
        )
        ack_prefix = [
            str(self.config.state_path.parent / "monitorctl"),
            "ack",
            transition.idempotency_key,
        ]
        reason = (
            f"GitHub router task: handle only {transition.repository}#{transition.issue_number}; "
            f"url={transition.issue_url}; source={source}; taskKey={transition.idempotency_key}; "
            f"ackCommand={json.dumps(ack_prefix, separators=(',', ':'))} "
            "then routed|deferred|ignored --reason <concise reason>."
        )
        response = self._run(
            [
                "agent",
                "wake",
                self.config.paperclip_agent,
                "--company-id",
                self.config.paperclip_company_id,
                "--source",
                "automation",
                "--trigger",
                "callback",
                "--reason",
                reason,
                "--payload",
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                "--idempotency-key",
                f"{transition.idempotency_key}:attempt:{transition.delivery_attempts + 1}",
            ]
        )
        if not isinstance(response, dict) or not isinstance(response.get("id"), str):
            raise CommandError(f"Paperclip wake was not accepted: {compact_error(response)}")
        return response

    def wake_periodic_audit(self, now: dt.datetime) -> Mapping[str, object]:
        bucket = int(now.timestamp()) // self.config.audit_interval_seconds
        response = self._run(
            [
                "agent",
                "wake",
                self.config.paperclip_agent,
                "--company-id",
                self.config.paperclip_company_id,
                "--source",
                "timer",
                "--trigger",
                "system",
                "--reason",
                "Periodic GitHub routing and in-progress audit",
                "--payload",
                json.dumps(
                    {
                        "event": PERIODIC_AUDIT_EVENT_KIND,
                        "instruction": (
                            "Run only the Periodic Audit section of HEARTBEAT.md. Reconcile missed GitHub work, "
                            "CI failures, project placement, and stalled In Progress issues. Do not create a routing wrapper."
                        ),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "--idempotency-key",
                f"github-periodic-audit:v1:{bucket}",
            ]
        )
        if not isinstance(response, dict) or not isinstance(response.get("id"), str):
            raise CommandError(f"Paperclip periodic audit wake was not accepted: {compact_error(response)}")
        return response


class TodoMonitor:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        github: GitHubClient,
        paperclip: PaperclipClient,
        *,
        clock: Callable[[], dt.datetime] = utc_now,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.store = store
        self.github = github
        self.paperclip = paperclip
        self.clock = clock
        self.logger = logger or logging.getLogger("github_project_todo_monitor")

    def poll(self) -> int:
        started = self.clock()
        self.store.record_poll_started(started)
        try:
            snapshot = self.github.fetch_project()
            queued = self.store.apply_snapshot(self.config, snapshot, self.clock())
        except Exception as error:
            self.store.record_poll_failure(error)
            raise
        self.logger.info(
            "project poll succeeded",
            extra={"event": "poll_succeeded", "item_count": len(snapshot.items), "queued": queued},
        )
        return queued

    def reconcile_deliveries(self) -> int:
        delivering = self.store.delivering_transitions()
        if not delivering:
            return 0
        runs = self.paperclip.recent_runs()
        now = self.clock()
        reconciled = 0
        for transition in delivering:
            existing = self.paperclip.run_for_transition(
                runs,
                transition.idempotency_key,
                transition.paperclip_run_id,
            )
            if existing is not None:
                run_id = existing.get("id") if isinstance(existing.get("id"), str) else None
                if run_id and transition.paperclip_run_id != run_id:
                    self.store.mark_delivery_accepted(transition.idempotency_key, now, run_id)
                run_status = str(existing.get("status") or "")
                attempt_started = parse_time(transition.attempt_started_at)
                age = (now - attempt_started).total_seconds() if attempt_started else 0
                if run_status not in LIVE_RUN_STATUSES and age >= self.config.outcome_timeout_seconds:
                    self.store.mark_retry(
                        transition,
                        now,
                        "Dev Manager run ended without acknowledging routed, deferred, or ignored",
                        delay_seconds=self.config.deferred_retry_seconds,
                    )
                    self.logger.warning(
                        "unacknowledged outcome returned to retry queue",
                        extra={"event": "outcome_retry_scheduled", "transition_key": transition.idempotency_key},
                    )
                else:
                    reconciled += 1
                continue
            attempt_started = parse_time(transition.attempt_started_at)
            age = (now - attempt_started).total_seconds() if attempt_started else self.config.delivery_ambiguity_seconds
            if age >= self.config.delivery_ambiguity_seconds:
                self.store.mark_retry(transition, now, "No matching Paperclip run found after ambiguity window")
                self.logger.warning(
                    "delivery returned to retry queue",
                    extra={"event": "delivery_retry_scheduled", "transition_key": transition.idempotency_key},
                )
        return reconciled

    def dispatch_once(self) -> str:
        self.reconcile_deliveries()
        if self.store.delivering_transitions():
            return "awaiting_outcome"
        if self.paperclip.has_active_run():
            return "agent_busy"
        now = self.clock()
        transition = self.store.next_transition(now)
        if transition is None:
            if self.store.has_pending_transitions():
                return "retry_wait"
            if self.store.periodic_audit_due(self.config, now):
                run = self.paperclip.wake_periodic_audit(now)
                self.store.record_periodic_audit(self.clock(), str(run["id"]))
                return "periodic_audit_dispatched"
            return "idle"
        if transition.source_kind == PROJECT_EVENT_KIND and not self.paperclip.coding_capacity_available():
            return "coding_capacity_full"
        if not self.store.mark_delivering(transition.idempotency_key, now):
            return "race_lost"
        try:
            run = self.paperclip.wake(transition)
        except Exception as error:
            self.store.record_delivery_error(transition.idempotency_key, error)
            self.logger.error(
                "wake delivery failed; reconciliation required",
                extra={
                    "event": "delivery_failed",
                    "transition_key": transition.idempotency_key,
                    "repository": transition.repository,
                    "issue_number": transition.issue_number,
                    "error_text": compact_error(error),
                },
            )
            raise
        self.store.mark_delivery_accepted(transition.idempotency_key, self.clock(), str(run["id"]))
        self.logger.info(
            "Dev Manager wake accepted",
            extra={
                "event": "delivery_accepted",
                "transition_key": transition.idempotency_key,
                "repository": transition.repository,
                "issue_number": transition.issue_number,
                "paperclip_run_id": run["id"],
            },
        )
        return "awaiting_outcome"


class WebhookReceiver:
    def __init__(
        self,
        config: Config,
        logger: logging.Logger,
        *,
        secret: Optional[bytes] = None,
        clock: Callable[[], dt.datetime] = utc_now,
    ) -> None:
        self.config = config
        self.logger = logger
        self.secret = secret if secret is not None else load_webhook_secret(config)
        self.clock = clock
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return int(self.server.server_port) if self.server else self.config.webhook_listen_port

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "GitHubIssueRouter/1"
            sys_version = ""
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _respond(self, status: int, value: Mapping[str, object]) -> None:
                body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                path = self.path.split("?", 1)[0]
                if path != receiver.config.webhook_health_path:
                    self._respond(404, {"status": "not_found"})
                    return
                self._respond(
                    200,
                    {
                        "status": "ok",
                        "service": "github-issue-router",
                        "acceptedEvent": "issues",
                    },
                )

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                path = self.path.split("?", 1)[0]
                if path != receiver.config.webhook_path:
                    self._respond(404, {"status": "not_found"})
                    return
                delivery_id = self.headers.get("X-GitHub-Delivery", "").strip()
                github_event = self.headers.get("X-GitHub-Event", "").strip()
                hook_id = self.headers.get("X-GitHub-Hook-ID", "").strip()
                try:
                    content_length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self._respond(411, {"status": "error", "error": "content_length_required"})
                    return
                if content_length < 0 or content_length > receiver.config.webhook_max_body_bytes:
                    self.close_connection = True
                    self._respond(413, {"status": "error", "error": "payload_too_large"})
                    return
                body = self.rfile.read(content_length)
                if len(body) != content_length:
                    self._respond(400, {"status": "error", "error": "incomplete_payload"})
                    return
                if not verify_webhook_signature(
                    receiver.secret,
                    body,
                    self.headers.get("X-Hub-Signature-256"),
                ):
                    receiver.logger.warning(
                        "GitHub webhook signature rejected",
                        extra={
                            "event": "webhook_rejected",
                            "delivery_id": delivery_id or None,
                            "error_text": "invalid_signature",
                        },
                    )
                    self._respond(401, {"status": "error", "error": "invalid_signature"})
                    return
                if hook_id != receiver.config.github_webhook_hook_id:
                    self._respond(403, {"status": "error", "error": "unexpected_hook"})
                    return
                if not delivery_id or len(delivery_id) > 200 or not github_event or len(github_event) > 100:
                    self._respond(400, {"status": "error", "error": "invalid_github_headers"})
                    return
                content_type = self.headers.get_content_type()
                if content_type != "application/json":
                    self._respond(415, {"status": "error", "error": "json_required"})
                    return
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._respond(400, {"status": "error", "error": "invalid_json"})
                    return
                issue: Optional[WebhookIssue] = None
                if github_event == "issues":
                    try:
                        issue = parse_issue_webhook(receiver.config, delivery_id, payload)
                    except MonitorError as error:
                        status = 403 if "not allowed" in str(error) else 400
                        self._respond(status, {"status": "error", "error": compact_error(error, 200)})
                        return
                try:
                    store = StateStore(receiver.config.state_path)
                    try:
                        result = store.record_webhook_delivery(
                            receiver.config,
                            delivery_id=delivery_id,
                            hook_id=hook_id,
                            github_event=github_event,
                            issue=issue,
                            received_at=receiver.clock(),
                        )
                    finally:
                        store.close()
                except Exception as error:
                    with contextlib.suppress(Exception):
                        error_store = StateStore(receiver.config.state_path)
                        try:
                            error_store.record_webhook_failure(error)
                        finally:
                            error_store.close()
                    receiver.logger.error(
                        "GitHub webhook persistence failed",
                        extra={
                            "event": "webhook_failed",
                            "delivery_id": delivery_id,
                            "error_text": compact_error(error),
                        },
                    )
                    self._respond(500, {"status": "error", "error": "persistence_failed"})
                    return
                receiver.logger.info(
                    "GitHub webhook accepted",
                    extra={
                        "event": "webhook_accepted",
                        "delivery_id": delivery_id,
                        "github_event": github_event,
                        "github_action": issue.action if issue else None,
                        "repository": issue.repository if issue else None,
                        "issue_number": issue.issue_number if issue else None,
                        "webhook_result": result,
                    },
                )
                self._respond(202 if result == "queued" else 200, {"status": result})

        return Handler

    def start(self) -> None:
        if self.server is not None:
            raise MonitorError("Webhook receiver is already started")
        try:
            server = ThreadingHTTPServer(
                (self.config.webhook_listen_host, self.config.webhook_listen_port),
                self._handler_class(),
            )
        except OSError as error:
            raise MonitorError(
                f"Unable to listen for GitHub webhooks on "
                f"{self.config.webhook_listen_host}:{self.config.webhook_listen_port}: {compact_error(error)}"
            ) from error
        server.daemon_threads = True
        self.server = server
        self.thread = threading.Thread(
            target=server.serve_forever,
            name="github-webhook-receiver",
            daemon=True,
        )
        self.thread.start()
        try:
            store = StateStore(self.config.state_path)
            try:
                store.record_webhook_listener_started(self.clock())
            finally:
                store.close()
        except Exception:
            self.stop()
            raise
        self.logger.info(
            "GitHub webhook listener started",
            extra={"event": "webhook_listener_started", "listen_port": self.port},
        )

    def stop(self) -> None:
        server = self.server
        thread = self.thread
        self.server = None
        self.thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)


def check_local_webhook_health(config: Config) -> Dict[str, object]:
    host = "127.0.0.1" if config.webhook_listen_host in {"0.0.0.0", "::"} else config.webhook_listen_host
    connection = http.client.HTTPConnection(host, config.webhook_listen_port, timeout=2)
    try:
        connection.request("GET", config.webhook_health_path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        body = response.read(4096)
        value = json.loads(body.decode("utf-8")) if body else {}
        ok = response.status == 200 and isinstance(value, dict) and value.get("status") == "ok"
        return {"ok": ok, "statusCode": response.status}
    except (OSError, http.client.HTTPException, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {"ok": False, "error": compact_error(error)}
    finally:
        connection.close()


def check_public_webhook_health(config: Config) -> Dict[str, object]:
    health_url = f"{config.webhook_public_url}{config.webhook_health_path}"
    request = urllib_request.Request(health_url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            body = response.read(4096)
            value = json.loads(body.decode("utf-8")) if body else {}
            ok = response.status == 200 and isinstance(value, dict) and value.get("status") == "ok"
            return {"ok": ok, "statusCode": response.status, "url": health_url}
    except (OSError, urllib_error.URLError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {"ok": False, "error": compact_error(error), "url": health_url}


class JsonLogFormatter(logging.Formatter):
    EXTRA_FIELDS = (
        "event",
        "item_count",
        "queued",
        "transition_key",
        "repository",
        "issue_number",
        "paperclip_run_id",
        "error_text",
        "delivery_id",
        "github_event",
        "github_action",
        "webhook_result",
        "listen_port",
    )

    def format(self, record: logging.LogRecord) -> str:
        value: Dict[str, object] = {
            "timestamp": isoformat(utc_now()),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        for field in self.EXTRA_FIELDS:
            field_value = getattr(record, field, None)
            if field_value is not None:
                value[field] = field_value
        return json.dumps(value, separators=(",", ":"), sort_keys=True)


def configure_logging(config: Config) -> logging.Logger:
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("github_project_todo_monitor")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        config.log_path,
        maxBytes=config.max_log_bytes,
        backupCount=config.log_backups,
        encoding="utf-8",
    )
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


@contextlib.contextmanager
def singleton_lock(path: Path) -> Iterable[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MonitorError(f"Monitor is already running (lock: {path})") from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def build_monitor(config: Config, store: StateStore, logger: logging.Logger) -> TodoMonitor:
    return TodoMonitor(config, store, GitHubClient(config), PaperclipClient(config), logger=logger)


def run_daemon(config: Config) -> int:
    logger = configure_logging(config)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    with singleton_lock(config.lock_path):
        store = StateStore(config.state_path)
        started = utc_now()
        store.record_daemon_heartbeat(started, started_at=started)
        monitor = build_monitor(config, store, logger)
        receiver: Optional[WebhookReceiver] = None
        try:
            receiver = WebhookReceiver(config, logger)
            receiver.start()
            logger.info("router started", extra={"event": "daemon_started"})
            while not stop.is_set():
                loop_started = time.monotonic()
                store.record_daemon_heartbeat(utc_now())
                try:
                    monitor.poll()
                except Exception as error:
                    logger.error(
                        "project poll failed",
                        extra={"event": "poll_failed", "error_text": compact_error(error)},
                    )
                try:
                    monitor.dispatch_once()
                except Exception as error:
                    store.record_dispatch_failure(error)
                    logger.error(
                        "dispatch cycle failed",
                        extra={"event": "dispatch_failed", "error_text": compact_error(error)},
                    )
                store.record_daemon_heartbeat(utc_now())
                remaining = max(0.0, config.poll_interval_seconds - (time.monotonic() - loop_started))
                stop.wait(remaining)
        finally:
            if receiver is not None:
                receiver.stop()
            store.clear_daemon_pid()
            store.close()
            logger.info("router stopped", extra={"event": "daemon_stopped"})
    return 0


def run_once(config: Config, *, dispatch: bool) -> Dict[str, object]:
    logger = configure_logging(config)
    with singleton_lock(config.lock_path):
        store = StateStore(config.state_path)
        try:
            monitor = build_monitor(config, store, logger)
            was_baselined = store.baseline_complete()
            queued = monitor.poll()
            dispatch_result = monitor.dispatch_once() if dispatch and was_baselined else "not_requested"
            return {
                "ok": True,
                "baselineCreated": not was_baselined,
                "queuedTransitions": queued,
                "dispatch": dispatch_result,
                "counts": store.transition_counts(),
            }
        finally:
            store.close()


def run_doctor(config: Config) -> Dict[str, object]:
    checks: Dict[str, object] = {}
    healthy = True
    try:
        snapshot = GitHubClient(config).fetch_project()
        checks["github"] = {
            "ok": True,
            "projectId": snapshot.project_id,
            "projectTitle": snapshot.project_title,
            "itemCount": len(snapshot.items),
        }
    except Exception as error:
        healthy = False
        checks["github"] = {"ok": False, "error": compact_error(error)}
    try:
        paperclip = PaperclipClient(config)
        paperclip.probe_wake_command()
        agent_id = paperclip.resolve_agent_id()
        coding_agent_ids = {
            agent: paperclip.resolve_agent_id(agent) for agent in config.paperclip_coding_agents
        }
        active = [
            run
            for run in paperclip.live_runs()
            if run.get("agentId") == agent_id and run.get("status") in LIVE_RUN_STATUSES
        ]
        checks["paperclip"] = {
            "ok": True,
            "agent": config.paperclip_agent,
            "agentId": agent_id,
            "activeRunCount": len(active),
            "codingAgents": coding_agent_ids,
            "codingCapacityAvailable": paperclip.coding_capacity_available(),
        }
    except Exception as error:
        healthy = False
        checks["paperclip"] = {"ok": False, "error": compact_error(error)}
    try:
        load_webhook_secret(config)
        checks["webhook"] = {
            "ok": True,
            "hookId": config.github_webhook_hook_id,
            "repositories": list(config.github_webhook_repositories),
            "listenHost": config.webhook_listen_host,
            "listenPort": config.webhook_listen_port,
            "publicUrl": config.webhook_public_url,
            "secretAvailable": True,
        }
    except Exception as error:
        healthy = False
        checks["webhook"] = {"ok": False, "error": compact_error(error)}
    return {"status": "ready" if healthy else "not_ready", "checks": checks}


def run_status(config: Config) -> Dict[str, object]:
    store = StateStore(config.state_path)
    try:
        result = store.status_snapshot(config, utc_now())
    finally:
        store.close()
    local_health = check_local_webhook_health(config)
    webhook = result.get("webhook")
    if isinstance(webhook, dict):
        webhook["localHealth"] = local_health
    if not local_health.get("ok"):
        reasons = result.get("reasons")
        if isinstance(reasons, list) and "webhook_listener_unhealthy" not in reasons:
            reasons.append("webhook_listener_unhealthy")
        result["status"] = "unhealthy"
    return result


def run_ack(config: Config, task_key: str, outcome: str, reason: str) -> Dict[str, object]:
    store = StateStore(config.state_path)
    try:
        result = store.acknowledge(
            task_key,
            outcome,
            reason,
            utc_now(),
            config.deferred_retry_seconds,
        )
        return {"ok": True, "taskKey": task_key, "outcome": result}
    finally:
        store.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("GITHUB_PROJECT_TODO_MONITOR_CONFIG", str(DEFAULT_CONFIG_PATH))),
        help="Path to monitor JSON config",
    )
    result.add_argument(
        "command",
        choices=["daemon", "once", "baseline", "status", "health", "doctor", "public-health", "ack"],
    )
    result.add_argument("task_key", nargs="?", help="Routing task key for ack")
    result.add_argument("outcome", nargs="?", choices=sorted(ROUTING_OUTCOMES), help="Routing outcome for ack")
    result.add_argument("--reason", default="", help="Concise outcome reason")
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = Config.load(args.config.resolve())
        if args.command == "daemon":
            return run_daemon(config)
        if args.command in {"once", "baseline"}:
            result = run_once(config, dispatch=args.command == "once")
            json_output(result)
            return 0
        if args.command in {"status", "health"}:
            result = run_status(config)
            json_output(result)
            return 0 if result["status"] == "healthy" else 1
        if args.command == "doctor":
            result = run_doctor(config)
            json_output(result)
            return 0 if result["status"] == "ready" else 1
        if args.command == "public-health":
            result = check_public_webhook_health(config)
            json_output(result)
            return 0 if result.get("ok") else 1
        if args.command == "ack":
            if not args.task_key or not args.outcome:
                raise MonitorError("ack requires TASK_KEY and routed, deferred, or ignored")
            result = run_ack(config, args.task_key, args.outcome, args.reason)
            json_output(result)
            return 0
        raise AssertionError(f"Unhandled command: {args.command}")
    except MonitorError as error:
        json_output({"status": "error", "error": compact_error(error)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
