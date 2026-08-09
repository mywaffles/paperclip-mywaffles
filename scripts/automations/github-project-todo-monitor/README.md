# GitHub issue router

This headless macOS service is the unified, no-wrapper router for Matt's GitHub
issue work. It runs two inputs through one durable queue:

1. It receives normal GitHub `issues` webhook deliveries for the configured
   repositories.
2. It polls one configured personal GitHub Project every 10 seconds because
   personal-account Projects do not emit the needed item-status webhook.

Every routed event invokes Dev Manager directly through:

```text
paperclipai agent wake dev-manager --source automation --trigger callback
```

The wake contains exactly one GitHub issue: repository, number, URL, Project
number, current status, a stable task/idempotency key, and the webhook action or
Project transition context. It instructs Dev Manager to handle only that issue,
reuse the GitHub↔Paperclip mapping, skip the paused routing routine, and never
create a `Route GitHub ToDo issues` wrapper.

## Reliability and security contract

- The public endpoint is
  `https://YOUR_TAILSCALE_HOST.ts.net:10000`. Tailscale
  Funnel gives the router a dedicated HTTPS listener and proxies it to
  `127.0.0.1:8788`; existing Paperclip and Hermes handlers are preserved.
- The receiver accepts JSON only, caps request bodies at 1 MiB, verifies the
  raw body using `X-Hub-Signature-256` and a Keychain-cached,
  1Password-sourced secret, requires the configured `X-GitHub-Hook-ID`, and
  rejects repositories outside the explicit allowlist. See [GitHub's signature validation guidance](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries).
- Every `X-GitHub-Delivery` is recorded durably in SQLite. Receipt dedupe and
  outbox insertion occur in one transaction, so a redelivery cannot wake Dev
  Manager twice. GitHub `ping` deliveries are acknowledged and deduplicated but
  never queued as work.
- Project polling is configuration-gated to 5–15 seconds, leaving margin inside
  the 30-second detection target. Pagination completes before item state
  commits, so failed or partial queries cannot advance the board snapshot.
- Every successful Project snapshot reconciles existing Todo items. An item
  without `dev-claude-max` or `dev-codex-max` is durably queued even on the
  first snapshot, so missed events and restarts cannot strand existing work.
- A Project transition key hashes the Project item ID, Status `updatedAt`,
  owner, Project number, and target status. Staying in Todo cannot enqueue
  again; leaving and later re-entering creates a new key.
- Webhook and Project events share one SQLite outbox, sorted by GitHub issue
  creation time (oldest first). Before each Project-routing wake the router
  requires Dev Manager to be idle and at least one coding agent to have an
  available slot. Pending work remains queued while both coding agents work.
- A delivery is marked `delivering` before the CLI call. Dev Manager must
  acknowledge `routed`, `deferred`, or `ignored` through `monitorctl ack`.
  `deferred` returns the same item to the durable queue; a merely existing or
  completed Paperclip run is not success. Missing acknowledgements retry after
  a bounded timeout, and each attempt has its own idempotency key.
- When the queue is empty, a direct periodic Dev Manager audit runs every 30
  minutes to reconcile missed work, CI failures, project placement, and stalled
  In Progress issues. It does not create a routing wrapper.
- Logs are JSON lines rotated at 1 MiB with three backups. launchd stdout and
  stderr go to `/dev/null`, so there is no second unbounded log.

## Credentials

Installation deliberately fails closed until all three credentials are
available on the macOS host that runs Paperclip:

1. `gh` authenticated with access to the configured Project, hook, and
   repositories.
2. A company-scoped Paperclip board CLI key stored in the configured
   1Password vault. An agent key cannot wake Dev Manager.
3. A random webhook signing secret of at least 32 bytes in the same 1Password
   vault. The same value is written to the
   GitHub webhook only after the local and public endpoint health checks pass.

The installer reads both secrets with `op` and refreshes a runtime-only macOS
Keychain cache. The background LaunchAgent can read the cache without an
interactive 1Password unlock. 1Password remains the source of truth.

Never place either secret in this repository, the JSON config, logs, issue
comments, or shell history. Credential creation and GitHub re-authentication are
security-sensitive operations and require the issue's explicit credential
approval.

The supplied config uses the wake-capable `paperclipai` executable installed in
`~/.local/bin`.

## Install and operate

From this directory:

```sh
./install.sh install
```

The installer:

1. copies the runtime to
   `~/Library/Application Support/Paperclip Automations/github-project-todo-monitor/`;
2. runs credential and integration diagnostics;
3. reconciles the current Project Todo backlog into the durable queue;
4. installs and starts a launchd LaunchAgent;
5. requires the local listener and poller to be healthy;
6. adds a dedicated Funnel listener on port 10000;
7. requires the public health URL to pass.

It does **not** update the configured GitHub webhook. Repoint the webhook only
after `public-health` succeeds. Preserve the `issues` event and JSON content
type. Use the Keychain-cached, 1Password-sourced secret.
Keep the old Paperclip routine `Route GitHub ToDo issues` paused.

Operational commands:

```sh
./install.sh doctor
./install.sh status
./install.sh health
./install.sh public-health
```

Dev Manager acknowledges each issue-scoped run before exiting:

```sh
./monitorctl ack TASK_KEY routed --reason "Mapped, linked, labeled, and assigned"
./monitorctl ack TASK_KEY deferred --reason "Both coding agents are busy"
./monitorctl ack TASK_KEY ignored --reason "Issue is closed or no longer Todo"
```

Machine-readable status reports the baseline, poll and daemon freshness, local
listener probe, public endpoint configuration, last accepted webhook metadata,
receipt count, per-source queue counts, last dispatch, and state/log paths.

Rollback is scoped and recoverable:

```sh
./install.sh uninstall
```

Uninstall stops launchd and removes only the Funnel path recorded as managed by
this installer. It preserves code, config, SQLite state, bounded logs, and all
unrelated Funnel handlers.

## Verification

Offline regression suite:

```sh
python3 -m unittest discover -s scripts/automations/github-project-todo-monitor -p 'test_*.py'
bash -n scripts/automations/github-project-todo-monitor/install.sh
```

Live acceptance sequence:

1. Confirm `doctor`, local `health`, and `public-health` pass; every existing
   unrouted Todo is pending or actively acknowledged; the legacy routine remains paused.
2. Record current Dev Manager runs and Paperclip issues named
   `Route GitHub ToDo issues`.
3. Repoint the configured webhook. Keep only `issues`, JSON content, and the
   Keychain-cached, 1Password-sourced secret. Confirm its signed `ping` is
   accepted without a wake.
4. Create a disposable issue. Verify the `issues/opened` delivery produces one
   direct callback run with the issue-scoped payload. Redeliver the same GitHub
   delivery and verify the receipt is `duplicate` and no second run appears.
5. Wait until Dev Manager is idle. Add the disposable issue to Project 4 outside
   Todo, then move it into Todo. Within 30 seconds verify one direct callback
   run with a Project-transition key. Leave it in Todo for two more polls and
   verify no repeat.
6. Verify no new Paperclip issue named `Route GitHub ToDo issues` exists and the
   paused routine has not run.
7. Move the item out of Todo, remove/restore its Project item, and close or
   delete the disposable issue. Any resulting distinct `issues` cleanup event
   is expected to route once under its own delivery ID.
