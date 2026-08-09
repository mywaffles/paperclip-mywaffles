#!/bin/bash
set -euo pipefail

LABEL="com.paperclipai.github-project-todo-monitor"
SOURCE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INSTALL_ROOT="${HOME}/Library/Application Support/Paperclip Automations/github-project-todo-monitor"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LAUNCH_TARGET="gui/${UID}/${LABEL}"
FUNNEL_MARKER="${INSTALL_ROOT}/funnel-managed.json"
KEYCHAIN_SERVICE="com.paperclipai.github-project-todo-monitor"

usage() {
  echo "Usage: $0 install|status|health|doctor|public-health|uninstall"
}

tailscale_bin() {
  if command -v tailscale >/dev/null 2>&1; then
    command -v tailscale
  elif [[ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]]; then
    echo "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
  else
    return 1
  fi
}

funnel_settings() {
  /usr/bin/python3 - "$INSTALL_ROOT/config.json" <<'PY'
import json
import sys
import urllib.parse

config = json.load(open(sys.argv[1], encoding="utf-8"))
public = urllib.parse.urlparse(config["webhookPublicUrl"])
port = public.port or 443
path = public.path.rstrip("/") or "/"
target = f"http://127.0.0.1:{int(config['webhookListenPort'])}"
print(f"{public.hostname}\t{port}\t{path}\t{target}")
PY
}

current_funnel_target() {
  local host="$1" port="$2" funnel_path="$3"
  "$(tailscale_bin)" funnel status --json | /usr/bin/python3 -c '
import json
import sys
host, port, path = sys.argv[1:]
try:
    value = json.load(sys.stdin)
except json.JSONDecodeError:
    print("invalid-status")
    raise SystemExit(0)
handler = value.get("Web", {}).get(f"{host}:{port}", {}).get("Handlers", {}).get(path)
if handler is None:
    print("absent")
elif isinstance(handler, dict) and isinstance(handler.get("Proxy"), str):
    print(handler["Proxy"])
else:
    print("non-proxy-handler")
' "$host" "$port" "$funnel_path"
}

configure_funnel() {
  local settings host port funnel_path target existing tailscale_bin
  settings="$(funnel_settings)"
  IFS=$'\t' read -r host port funnel_path target <<< "$settings"
  tailscale_bin="$(tailscale_bin || true)"
  if [[ -z "$tailscale_bin" ]]; then
    echo "tailscale CLI is required to expose the signed webhook endpoint." >&2
    return 1
  fi
  existing="$(current_funnel_target "$host" "$port" "$funnel_path")"
  if [[ "$existing" != "absent" && "$existing" != "$target" ]]; then
    echo "Refusing to replace existing Funnel handler ${host}:${port}${funnel_path}: ${existing}" >&2
    return 1
  fi
  if [[ "$existing" == "absent" ]]; then
    "$tailscale_bin" funnel --bg --yes --https="$port" --set-path="$funnel_path" "$target"
    if ! /usr/bin/python3 - "$FUNNEL_MARKER" "$host" "$port" "$funnel_path" "$target" <<'PY'
import json
import pathlib
import sys

marker = pathlib.Path(sys.argv[1])
marker.write_text(json.dumps({
    "host": sys.argv[2],
    "httpsPort": int(sys.argv[3]),
    "path": sys.argv[4],
    "target": sys.argv[5],
}, sort_keys=True) + "\n", encoding="utf-8")
PY
    then
      "$tailscale_bin" funnel --yes --https="$port" --set-path="$funnel_path" off || true
      echo "Funnel handler was rolled back because its ownership marker could not be written." >&2
      return 1
    fi
  fi
}

remove_managed_funnel() {
  local settings host port funnel_path target tailscale_bin
  [[ -f "$FUNNEL_MARKER" ]] || return 0
  settings="$(/usr/bin/python3 - "$FUNNEL_MARKER" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"{value['host']}\t{value['httpsPort']}\t{value['path']}\t{value['target']}")
PY
)"
  IFS=$'\t' read -r host port funnel_path target <<< "$settings"
  tailscale_bin="$(tailscale_bin || true)"
  if [[ -z "$tailscale_bin" ]]; then
    echo "Could not remove managed Funnel path because tailscale is unavailable: ${host}:${port}${funnel_path}" >&2
    return 1
  fi
  "$tailscale_bin" funnel --yes --https="$port" --set-path="$funnel_path" off
  /bin/rm -f "$FUNNEL_MARKER"
  echo "Removed managed Funnel handler: ${host}:${port}${funnel_path}"
}

require_macos() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This installer supports macOS launchd only." >&2
    exit 1
  fi
}

provision_runtime_secret() {
  local config_field="$1" account="$2"
  /usr/bin/python3 - "$INSTALL_ROOT/config.json" "$config_field" <<'PY' |
import json
import subprocess
import sys

config = json.load(open(sys.argv[1], encoding="utf-8"))
command = config.get(sys.argv[2])
if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
    raise SystemExit(f"Config field {sys.argv[2]!r} must be a non-empty command array")
result = subprocess.run(command, capture_output=True, check=False)
secret = result.stdout.strip()
if result.returncode != 0 or not secret:
    detail = result.stderr.decode("utf-8", errors="replace").strip() or "credential command returned no value"
    raise SystemExit(detail)
sys.stdout.buffer.write(secret + b"\n" + secret + b"\n")
PY
    /usr/bin/security add-generic-password \
      -U -s "$KEYCHAIN_SERVICE" -a "$account" -w >/dev/null
  echo "Refreshed runtime credential cache: ${account}"
}

write_plist() {
  local temp_dir plist_temp
  temp_dir="$(mktemp -d "${TMPDIR:-/private/tmp}/github-project-todo-monitor.XXXXXX")"
  plist_temp="${temp_dir}/${LABEL}.plist"
  /usr/bin/python3 - "$plist_temp" "$LABEL" "$INSTALL_ROOT" <<'PY'
import pathlib
import plistlib
import sys

destination = pathlib.Path(sys.argv[1])
label = sys.argv[2]
install_root = pathlib.Path(sys.argv[3])
payload = {
    "Label": label,
    "ProgramArguments": [
        "/usr/bin/python3",
        str(install_root / "github_project_todo_monitor.py"),
        "--config",
        str(install_root / "config.json"),
        "daemon",
    ],
    "WorkingDirectory": str(install_root),
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "ThrottleInterval": 10,
    "LowPriorityIO": True,
    "StandardOutPath": "/dev/null",
    "StandardErrorPath": "/dev/null",
    "EnvironmentVariables": {
        "PATH": f"{pathlib.Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "PYTHONUNBUFFERED": "1",
    },
}
with destination.open("wb") as handle:
    plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=True)
PY
  /usr/bin/install -m 0644 "$plist_temp" "$PLIST_PATH"
  /bin/rm -r "$temp_dir"
}

install_monitor() {
  require_macos
  /bin/mkdir -p "$INSTALL_ROOT" "$(dirname -- "$PLIST_PATH")"
  if /bin/launchctl print "$LAUNCH_TARGET" >/dev/null 2>&1; then
    /bin/launchctl bootout "gui/${UID}" "$PLIST_PATH"
  fi
  /usr/bin/install -m 0755 "$SOURCE_DIR/github_project_todo_monitor.py" "$INSTALL_ROOT/github_project_todo_monitor.py"
  /usr/bin/install -m 0755 "$SOURCE_DIR/monitorctl" "$INSTALL_ROOT/monitorctl"
  /usr/bin/install -m 0755 "$SOURCE_DIR/install.sh" "$INSTALL_ROOT/install.sh"
  if [[ ! -f "$INSTALL_ROOT/config.json" ]]; then
    /usr/bin/install -m 0600 "$SOURCE_DIR/config.example.json" "$INSTALL_ROOT/config.json"
  fi
  /bin/chmod 0600 "$INSTALL_ROOT/config.json"

  echo "Refreshing the background-safe Keychain cache from 1Password..."
  provision_runtime_secret "paperclipApiKeyProvisionCommand" "paperclip-board-api-key"
  provision_runtime_secret "webhookSecretProvisionCommand" "github-webhook-secret"

  echo "Checking GitHub and Paperclip credentials without mutating either service..."
  "$INSTALL_ROOT/monitorctl" doctor
  echo "Reconciling the current Project Todo backlog into the durable queue..."
  "$INSTALL_ROOT/monitorctl" baseline

  write_plist
  /bin/launchctl bootstrap "gui/${UID}" "$PLIST_PATH"
  /bin/launchctl enable "$LAUNCH_TARGET"
  /bin/launchctl kickstart -k "$LAUNCH_TARGET"

  local attempt=0
  while (( attempt < 20 )); do
    if "$INSTALL_ROOT/monitorctl" status >/dev/null 2>&1; then
      "$INSTALL_ROOT/monitorctl" status
      break
    fi
    sleep 1
    attempt=$((attempt + 1))
  done
  if (( attempt >= 20 )); then
    echo "LaunchAgent started but did not become healthy within 20 seconds." >&2
    "$INSTALL_ROOT/monitorctl" status || true
    return 1
  fi

  echo "Publishing the isolated webhook path through the existing Tailscale Funnel..."
  configure_funnel
  attempt=0
  while (( attempt < 20 )); do
    if "$INSTALL_ROOT/monitorctl" public-health >/dev/null 2>&1; then
      "$INSTALL_ROOT/monitorctl" public-health
      echo "Installed and healthy: $LABEL"
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done
  echo "Public webhook endpoint did not become healthy within 20 seconds; removing only this install's Funnel handler." >&2
  remove_managed_funnel || true
  "$INSTALL_ROOT/monitorctl" public-health || true
  return 1
}

uninstall_monitor() {
  require_macos
  if /bin/launchctl print "$LAUNCH_TARGET" >/dev/null 2>&1; then
    /bin/launchctl bootout "gui/${UID}" "$PLIST_PATH"
  fi
  remove_managed_funnel
  if [[ -f "$PLIST_PATH" ]]; then
    local disabled_path
    disabled_path="$INSTALL_ROOT/${LABEL}.plist.disabled.$(date -u +%Y%m%dT%H%M%SZ)"
    /bin/mv "$PLIST_PATH" "$disabled_path"
    echo "LaunchAgent definition moved to: $disabled_path"
  fi
  echo "Monitor stopped. Code, state, config, and bounded logs were retained at: $INSTALL_ROOT"
}

action="${1:-}"
case "$action" in
  install)
    install_monitor
    ;;
  status|health|doctor|public-health)
    exec "$INSTALL_ROOT/monitorctl" "$action"
    ;;
  uninstall)
    uninstall_monitor
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
