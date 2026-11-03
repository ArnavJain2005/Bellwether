#!/usr/bin/env bash
# One-command, fail-closed operator entry point for the election-week server bot.
# Arnav installs/updates the systemd unit separately. This never changes risk limits,
# removes STOP/PAUSED, reads the key, or launches the bot directly in a terminal.
set -euo pipefail

APP=/opt/bellwether
SERVICE=bellwether-bot.service
PY="$APP/.venv/bin/python"
STATE="$APP/bot/state"

usage() {
  echo 'Usage: bash deploy/botctl.sh {start|status|logs}' >&2
  exit 2
}

fail() { echo "botctl: $*" >&2; exit 1; }

show_status() {
  local active enabled
  active=$(systemctl is-active "$SERVICE" 2>/dev/null || true)
  enabled=$(systemctl is-enabled "$SERVICE" 2>/dev/null || true)
  echo "Service: ${active:-unknown}; boot: ${enabled:-unknown}"
  if [[ ! -x "$PY" || ! -f "$STATE/bellwether.db" ]]; then
    echo 'Heartbeat: unavailable (environment or database missing)'
    return 1
  fi
  "$PY" - "$STATE/bellwether.db" "$APP/config/risk.yaml" "$STATE" <<'PYCODE' || return 1
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

db, config, state = sys.argv[1:]
try:
    with sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
        row = conn.execute(
            "SELECT ts_utc, mode, paused, pause_reason FROM heartbeat WHERE id = 1"
        ).fetchone()
    if row is None:
        raise ValueError("no heartbeat")
    ts, mode, paused, reason = row
    from bot.limits import load_limits

    limits = load_limits(Path(config))
    loop_seconds = limits.loop_seconds
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
    fresh = 0 <= age <= max(45, loop_seconds * 3)
    print(f"Heartbeat: {ts} ({age:.0f}s old); mode={mode}; paused={bool(paused)}")
    if paused:
        print(f"Pause reason: {reason or 'see bot/state/PAUSED'}")
    if Path(state, "STOP").exists():
        print("STOP is present; starting requires Arnav to review and remove it")
    if not fresh:
        print("Heartbeat is stale or in the future")
    if mode != "dry_run" or limits.mode != "dry_run":
        print("Dry-run mode mismatch; inspect the service and config manually")
    if not (fresh and mode == limits.mode == "dry_run" and not paused
            and not Path(state, "STOP").exists()):
        sys.exit(1)
except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
    print(f"Heartbeat: unavailable ({type(exc).__name__})")
    sys.exit(1)
PYCODE
  [[ "$active" == active && "$enabled" == enabled ]] || return 1
}

[[ $# == 1 ]] || usage
case "$1" in
  status) show_status ;;
  logs) exec journalctl -u "$SERVICE" --since '15 min ago' --no-pager -n 100 ;;
  start)
    [[ -d "$APP" && -x "$PY" ]] || fail "install the bot at $APP first (see deploy/BOT_RUNBOOK.md)"
    [[ ! -e "$STATE/STOP" ]] || fail "STOP is present; inspect why it stopped before removing it manually"
    [[ ! -e "$STATE/PAUSED" ]] || fail "PAUSED is present; inspect the cause before removing it manually"
    cd "$APP"
    # The live mode is never started via the convenience command. The bot itself
    # also requires --live, but this explicit check prevents confusing outcomes.
    "$PY" - <<'PYCODE' || fail "config check failed; dry_run is required"
from bot.limits import load_limits
from bot.mapping import load_map

if load_limits().mode != "dry_run":
    raise SystemExit("botctl start requires mode: dry_run")
if not load_map():
    raise SystemExit("no approved contracts")
PYCODE
    if systemctl is-active --quiet "$SERVICE"; then
      echo 'Service is already running; no restart was requested.'
    fi
    # enable --now is idempotent and does not restart an already active service.
    sudo systemctl enable --now "$SERVICE"
    # Startup includes SIG reads; allow a few seconds for the first heartbeat.
    for _ in {1..10}; do
      if show_status; then exit 0; fi
      sleep 2
    done
    fail "service has no healthy dry-run heartbeat; run: bash deploy/botctl.sh logs"
    ;;
  *) usage ;;
esac
