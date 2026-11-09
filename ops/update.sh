#!/usr/bin/env bash
# Update the agent team to the latest kit version from GitHub, safely.
#
#   ops/update.sh            update to origin/main
#   ops/update.sh <ref>      update to a branch, tag or commit
#   ops/update.sh --check    only fetch and report whether an update is available
#
# Keeps .env (it's not in git), keeps the previous image as bbts-agents:rollback, waits for the
# bot to report healthy, and rolls back automatically if it doesn't. Reports to Telegram.
# Run on the server as root (the /update command in Telegram triggers it through systemd).
set -uo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTROL=/var/lib/bbts/control
cd "$KIT_DIR"
mkdir -p "$CONTROL"
LOCK="$CONTROL/update.lock"
exec 9>"$LOCK"
flock -n 9 || { echo "Another update is running."; exit 1; }

# shellcheck source=ops/lib.sh
. "$KIT_DIR/ops/lib.sh"

status() {  # status <state> <message>  -> /var/lib/bbts/control/update-status.json (the bot reads it)
  python3 - "$1" "$2" "$(git rev-parse --short HEAD 2>/dev/null)" "$CONTROL/update-status.json" <<'PY'
import json, sys, time
state, msg, ver, path = sys.argv[1:5]
json.dump({"state": state, "message": msg, "version": ver, "at": int(time.time())}, open(path, "w"))
PY
}

[ -d .git ] || { echo "$KIT_DIR is not a git clone of the kit repo. See README: 'Moving to the kit repo'."; exit 1; }
[ -f .env ] || { echo "No .env in $KIT_DIR - refusing to update (the bot couldn't start)."; exit 1; }

git fetch -q origin || { status failed "git fetch failed"; tg "⚠️ Update: couldn't reach GitHub."; exit 1; }

if [ "${1:-}" = "--check" ]; then
  behind=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
  python3 - "$behind" "$(git rev-parse --short HEAD)" "$(git log -1 --format=%s origin/main)" "$CONTROL/update-available.json" <<'PY'
import json, sys, time
behind, ver, subject, path = sys.argv[1:5]
json.dump({"behind": int(behind), "version": ver, "latest": subject, "checked": int(time.time())}, open(path, "w"))
PY
  echo "$behind commit(s) behind origin/main"
  exit 0
fi

TARGET="${1:-origin/main}"
PREV=$(git rev-parse HEAD)
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  status failed "local changes in the kit folder"
  tg "⚠️ Update stopped: the kit folder on the server has local changes (git status). Nothing was changed."
  exit 1
fi
NEW=$(git rev-parse "$TARGET^{commit}" 2>/dev/null) || { status failed "unknown version $TARGET"; tg "⚠️ Update: unknown version $TARGET"; exit 1; }
if [ "$NEW" = "$PREV" ] && [ "${FORCE:-0}" != 1 ]; then
  status ok "already up to date"
  tg "✅ Already up to date ($(git rev-parse --short HEAD))."
  exit 0
fi

status running "updating $(git rev-parse --short "$PREV") -> $(git rev-parse --short "$NEW")"
tg "🔄 Updating the agent team: $(git log -1 --format='%h %s' "$NEW")"
cp .env "/root/.env.agent-team-backup"
docker image inspect bbts-agents:latest >/dev/null 2>&1 && docker tag bbts-agents:latest bbts-agents:rollback

git checkout -q --detach "$NEW"
if ! docker compose build -q; then
  git checkout -q --detach "$PREV"
  status failed "build failed; still on the previous version"
  tg "❌ Update failed while building. Still running the previous version $(git rev-parse --short "$PREV")."
  exit 1
fi
docker compose up -d --remove-orphans

# Wait for the bot to report healthy (heartbeat), up to ~4 minutes.
healthy=0
for _ in $(seq 1 48); do
  sleep 5
  st=$(docker inspect --format '{{.State.Health.Status}}' agent-team-kit-agents-1 2>/dev/null || echo none)
  [ "$st" = healthy ] && { healthy=1; break; }
done

if [ "$healthy" = 1 ]; then
  git checkout -q -B main "$NEW" 2>/dev/null || true
  status ok "updated to $(git rev-parse --short "$NEW")"
  tg "✅ Updated to $(git log -1 --format='%h %s' "$NEW")."
  exit 0
fi

# Roll back: previous code + previous image, no rebuild.
docker compose logs --tail 40 agents > "$CONTROL/update-failed.log" 2>&1 || true
git checkout -q --detach "$PREV"
if docker image inspect bbts-agents:rollback >/dev/null 2>&1; then
  docker tag bbts-agents:rollback bbts-agents:latest
fi
docker compose up -d --no-build --remove-orphans
git checkout -q -B main "$PREV" 2>/dev/null || true
status rolled_back "new version didn't become healthy; back on $(git rev-parse --short "$PREV")"
tg "↩️ Update rolled back: the new version didn't start properly. Back on $(git rev-parse --short "$PREV"). The last log lines are in $CONTROL/update-failed.log."
exit 1
