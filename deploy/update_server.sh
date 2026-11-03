#!/usr/bin/env bash
# Update /opt/bellwether to origin/main without losing the server's local edits.
#
#   sudo -u bellwether bash deploy/update_server.sh      (from /opt/bellwether)
#
# Arnav runs this himself, with bellwether-bot stopped. It never starts or stops the
# service and never talks to SIG. Local edits it keeps:
#   config/risk.yaml   restored as it was, unless origin/main changed the committed file
#                      (then it stops and prints the diff)
#   bot/map.yaml       origin/main's version, with every race the server had excluded
#                      set back to exclude: true (approvals from main are kept)
#   config/ladder.yaml restored as it was, unless origin/main changed it too (stops)
# Backups go to bot/state/backup-<UTC timestamp>/. On any failure after the update the
# backups are restored and the rollback commands are printed.
set -euo pipefail

APP=/opt/bellwether
SERVICE=bellwether-bot
PY=.venv/bin/python

die() { echo "update_server: $*" >&2; exit 1; }

# --- 1. where, who, and the service stopped ----------------------------------------------
[ "$(pwd -P)" = "$APP" ] || die "run this from $APP (cd $APP first)"
[ "$(id -un)" = bellwether ] || die "run as the bellwether user: sudo -u bellwether bash deploy/update_server.sh"
if systemctl is-active --quiet "$SERVICE"; then
  echo "$SERVICE is running. Stop it first (a live stop cancels every open order):"
  echo "  sudo systemctl stop $SERVICE"
  exit 1
fi
[ -x "$PY" ] || die "$PY not found"
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || die "the checkout is not on main"

KEEP_RE='^(config/risk\.yaml|bot/map\.yaml|config/ladder\.yaml)$'
OTHER=$(git status --porcelain --untracked-files=no | cut -c4- | grep -Ev "$KEEP_RE" || true)
[ -z "$OTHER" ] || die "other tracked files have local changes, resolve them first:
$OTHER"
if ! git diff --cached --quiet -- config/risk.yaml bot/map.yaml config/ladder.yaml; then
  die "staged edits to risk.yaml, map.yaml or ladder.yaml must be reviewed and unstaged first"
fi

echo "== fetching origin (a read-only token may be asked for; username oauth2)"
git fetch origin main
OLD_HEAD=$(git rev-parse HEAD)
NEW_HEAD=$(git rev-parse origin/main)
git merge-base --is-ancestor "$OLD_HEAD" "$NEW_HEAD" || die "origin/main is not a fast-forward of HEAD"
echo "   HEAD $OLD_HEAD -> origin/main $NEW_HEAD"

# --- 2. backups ---------------------------------------------------------------------------
BACKUP="bot/state/backup-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP"
cp -p config/risk.yaml "$BACKUP/risk.yaml"
cp -p bot/map.yaml "$BACKUP/map.yaml"
HAD_LADDER=false
if [ -f config/ladder.yaml ]; then
  cp -p config/ladder.yaml "$BACKUP/ladder.yaml"
  HAD_LADDER=true
fi
echo "== backups in $BACKUP"
echo "== checking that local map edits are exclusions only"
"$PY" - "$OLD_HEAD" "$BACKUP/map.yaml" <<'PYEOF' || die "bot/map.yaml has edits beyond race exclusions; nothing was updated"
from pathlib import Path
import subprocess
import sys

old_sha, server_path = sys.argv[1:]
old = subprocess.run(
    ["git", "show", f"{old_sha}:bot/map.yaml"], check=True, stdout=subprocess.PIPE
).stdout.splitlines(keepends=True)
server = Path(server_path).read_bytes().splitlines(keepends=True)
if len(old) != len(server):
    sys.exit("server map has added or removed lines")
for number, (committed, local) in enumerate(zip(old, server), start=1):
    if committed == local or (
        committed == b"  exclude: false\n" and local == b"  exclude: true\n"
    ):
        continue
    sys.exit(f"server map has another change at line {number}")
PYEOF

# --- 3. risk.yaml (and ladder.yaml): stop if main changed what the server edits ------------
if ! git diff --quiet "$OLD_HEAD" "$NEW_HEAD" -- config/risk.yaml; then
  echo "config/risk.yaml changed on origin/main. Merge it by hand; nothing was changed:"
  git --no-pager diff "$OLD_HEAD" "$NEW_HEAD" -- config/risk.yaml
  exit 1
fi
KEEP_LADDER=false
if git cat-file -e "$OLD_HEAD:config/ladder.yaml" 2>/dev/null; then
  if ! git diff --quiet "$OLD_HEAD" -- config/ladder.yaml; then
    if ! git diff --quiet "$OLD_HEAD" "$NEW_HEAD" -- config/ladder.yaml; then
      echo "config/ladder.yaml has server edits AND changed on origin/main. Nothing was changed:"
      git --no-pager diff "$OLD_HEAD" "$NEW_HEAD" -- config/ladder.yaml
      exit 1
    fi
    KEEP_LADDER=true
  fi
elif $HAD_LADDER && git cat-file -e "$NEW_HEAD:config/ladder.yaml" 2>/dev/null; then
  # An untracked server copy would block the update; it may go only if main's is identical.
  if git show "$NEW_HEAD:config/ladder.yaml" | cmp -s - config/ladder.yaml; then
    rm config/ladder.yaml
  else
    echo "config/ladder.yaml on the server differs from origin/main's. Nothing was changed:"
    git show "$NEW_HEAD:config/ladder.yaml" | diff -u - config/ladder.yaml || true
    exit 1
  fi
fi

restore_and_explain() {
  echo "update_server: FAILED: $1" >&2
  cp -p "$BACKUP/risk.yaml" config/risk.yaml
  cp -p "$BACKUP/map.yaml" bot/map.yaml
  if $HAD_LADDER; then cp -p "$BACKUP/ladder.yaml" config/ladder.yaml; fi
  echo "The server's risk.yaml, map.yaml and ladder.yaml (if any) are restored from $BACKUP." >&2
  echo "To roll the code back to $OLD_HEAD as well:" >&2
  echo "  git reset --hard $OLD_HEAD" >&2
  echo "  cp -p $BACKUP/risk.yaml config/risk.yaml" >&2
  echo "  cp -p $BACKUP/map.yaml bot/map.yaml" >&2
  if $HAD_LADDER; then echo "  cp -p $BACKUP/ladder.yaml config/ladder.yaml" >&2; fi
  exit 1
}

# --- 5. reset the edited files, fast-forward, restore ------------------------------------
git checkout -- config/risk.yaml bot/map.yaml
if $KEEP_LADDER; then git checkout -- config/ladder.yaml; fi
# The fetch above already brought origin/main: a fast-forward to it is `git pull --ff-only`
# without a second fetch, so the risk.yaml check above is about exactly what lands.
git merge --ff-only "$NEW_HEAD" || restore_and_explain "fast-forward failed"
cp -p "$BACKUP/risk.yaml" config/risk.yaml
if $KEEP_LADDER; then cp -p "$BACKUP/ladder.yaml" config/ladder.yaml; fi

# --- 4. map.yaml: main's file, with the server's excluded races excluded again -----------
"$PY" - "$BACKUP/map.yaml" bot/map.yaml <<'PYEOF' || restore_and_explain "map.yaml re-exclusion failed"
import sys
from pathlib import Path

import yaml

from bot.mapping import load_map

backup, target = Path(sys.argv[1]), Path(sys.argv[2])
excluded = {r["race_key"] for r in yaml.safe_load(backup.read_text())["races"] if r["exclude"]}
lines = target.read_text().splitlines(keepends=True)
current = None
found = set()
for i, line in enumerate(lines):
    if line.startswith("- race_key: "):
        current = line.split(":", 1)[1].strip()
    elif current in excluded and line.rstrip("\n") in ("  exclude: false", "  exclude: true"):
        lines[i] = "  exclude: true\n"
        found.add(current)
missing = sorted(excluded - found)
if missing:
    print(f"warning: excluded on the server but not in main's map.yaml: {', '.join(missing)}")
target.write_text("".join(lines))
contracts = load_map(target)
print(f"map.yaml: {len(excluded)} excluded race(s) re-applied; {len(contracts)} approved contracts")
PYEOF
echo "== bot/map.yaml compared with origin/main (only exclude: true lines should show)"
git --no-pager diff -- bot/map.yaml

# --- 6. checks ---------------------------------------------------------------------------
echo "== ladder check (the server's risk.yaml and ladder.yaml)"
"$PY" -m bot.ladder check || restore_and_explain "python -m bot.ladder check failed"

echo "== tests"
# The tests read the committed config, so they run on a clean copy of the new HEAD (the
# server's risk.yaml edits, e.g. the NO-order settings, would fail the repo-file tests).
TESTDIR=$(mktemp -d /tmp/bellwether-test.XXXXXX)
git worktree add --detach "$TESTDIR/src" "$NEW_HEAD" >/dev/null
TEST_OK=true
(cd "$TESTDIR/src" && PYTHONPATH="$TESTDIR/src" "$APP/$PY" -m pytest bot/tests -q -p no:cacheprovider) \
  || TEST_OK=false
git worktree remove --force "$TESTDIR/src"
rm -rf "$TESTDIR"
$TEST_OK || restore_and_explain "pytest bot/tests failed"

# --- 7. next steps -----------------------------------------------------------------------
cat <<EOF

Updated $OLD_HEAD -> $NEW_HEAD. Backups: $BACKUP
Start the bot:
  sudo systemctl start $SERVICE
Then confirm the heartbeat (ladder_step, scale and caps are the active step's):
  sqlite3 -readonly $APP/bot/state/bellwether.db "SELECT ts_utc, mode, version, ladder_step, scale, cap_total, cap_per_race, min_cash_reserve, paused FROM heartbeat"
Ladder gates at any time:
  sudo -u bellwether $APP/$PY -m bot.ladder status
EOF

