#!/bin/zsh
# Nightly brain refresh, run by launchd (`python -m brain install-launchd`).
#
# Ingests new items from every enabled source, summarises what changed through
# your Claude Code login (`claude -p`, subscription usage), rebuilds the tree and
# scans for secrets. If the data folder is a git repo it then commits, and pushes
# when a remote is set.
#
# launchd fires at the main slot plus 09:00, 13:00 and 19:00 (and on wake if one
# was missed). After a successful run the later slots exit at once.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

CODE="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${BRAIN_ROOT:-$HOME/brain}"
PY="${BRAIN_PYTHON:-python3}"
LOG_DIR="$HOME/Library/Logs/smoothbrain"
STAMP="$LOG_DIR/last-success"
LOCK="$LOG_DIR/lock"
TODAY="$(date +%F)"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/$TODAY.log" 2>&1

notify() {
  osascript -e "display notification \"$1\" with title \"Brain\"" || true
}

[[ "${1:-}" != "--force" && -f "$STAMP" && "$(cat "$STAMP")" == "$TODAY" ]] && exit 0
# A lock older than 6 hours is left over from a crashed run.
find "$LOCK" -maxdepth 0 -mmin +360 -exec rmdir {} \; 2>/dev/null
mkdir "$LOCK" 2>/dev/null || { echo "$(date) another run holds $LOCK"; exit 0; }
trap 'rmdir "$LOCK"' EXIT

echo "=== $(date) ==="
cd "$CODE" || exit 1
export BRAIN_ROOT="$DATA"

"$PY" -m brain run
rc=$?

if "$PY" -m brain check; then
  if git -C "$DATA" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$DATA" add -A
    if ! git -C "$DATA" diff --cached --quiet; then
      git -C "$DATA" commit -q -m "nightly: $(date '+%Y-%m-%d %H:%M')"
      if git -C "$DATA" remote | grep -q .; then
        git -C "$DATA" push -q || notify "Push failed — see $LOG_DIR"
      fi
    fi
  fi
else
  notify "Secret check failed, nothing committed — see $LOG_DIR/$TODAY.log"
  rc=4
fi

if [[ $rc -eq 0 ]]; then
  echo "$TODAY" > "$STAMP"
elif [[ $rc -eq 3 ]]; then
  echo "usage limit; will finish at the next slot"
else
  notify "Nightly run failed (exit $rc) — see $LOG_DIR/$TODAY.log"
fi
echo "=== done $(date) exit $rc ==="
find "$LOG_DIR" -name '*.log' -mtime +60 -delete
exit $rc
