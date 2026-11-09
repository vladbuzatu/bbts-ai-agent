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
CONTROL=${CONTROL_DIR:-/var/lib/bbts/control}
cd "$KIT_DIR" || exit 1
mkdir -p "$CONTROL"
LOCK="$CONTROL/update.lock"
exec 9>"$LOCK"
if [ "${1:-}" = "--check" ]; then
  flock -n 9 || exit 0                          # an update is running: skip this check quietly
else
  flock -w 600 9 || { echo "Another update is running."; exit 1; }
fi

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
cp .env "${ENV_BACKUP:-/root/.env.agent-team-backup}"
docker image inspect bbts-agents:latest >/dev/null 2>&1 && docker tag bbts-agents:latest bbts-agents:rollback

# Snapshot of the bot's data and workspace: a new version may migrate them (layout, state), and a
# rollback must hand the previous version the data it understands.
SNAP="${PREUPDATE_DIR:-/var/backups/bbts-preupdate}"
rm -rf "$SNAP" && mkdir -p "$SNAP" && chmod 700 "$SNAP"
for vol in data workspace; do
  docker run --rm --entrypoint tar -v "agent-team-kit_$vol":/v:ro -v "$SNAP":/b bbts-agents:latest \
    czf "/b/$vol.tgz" -C /v --exclude=node_modules --exclude=.venv --exclude=.angular . >/dev/null 2>&1 || true
done

if ! git checkout -q --detach "$NEW"; then
  status failed "couldn't check out the new version (local files in the way?)"
  tg "❌ Update stopped: couldn't switch the kit folder to the new version (git checkout failed). Nothing changed."
  exit 1
fi
if ! docker compose build -q; then
  git checkout -q --detach "$PREV"
  status failed "build failed; still on the previous version"
  tg "❌ Update failed while building. Still running the previous version $(git rev-parse --short "$PREV")."
  exit 1
fi
NEW_IMAGE=$(docker image inspect --format '{{.Id}}' bbts-agents:latest 2>/dev/null)
STARTED=$(date +%s)
up_ok=1
docker compose up -d --remove-orphans || up_ok=0

# Healthy = the container runs the NEW image, and the heartbeat comes from a bot process that started
# after this update began (the heartbeat file starts with the bot's start time).
healthy=0
if [ "$up_ok" = 1 ]; then
  for _ in $(seq 1 48); do
    sleep 5
    st=$(docker inspect --format '{{.State.Health.Status}}' agent-team-kit-agents-1 2>/dev/null || echo none)
    img=$(docker inspect --format '{{.Image}}' agent-team-kit-agents-1 2>/dev/null || echo none)
    boot=$(docker exec agent-team-kit-agents-1 cat /data/heartbeat 2>/dev/null | cut -d' ' -f1 | tr -dc 0-9)
    if [ "$st" = healthy ] && { [ -z "$NEW_IMAGE" ] || [ "$img" = "$NEW_IMAGE" ]; } && [ "${boot:-0}" -ge "$STARTED" ]; then
      healthy=1; break
    fi
  done
fi

if [ "$healthy" = 1 ]; then
  git checkout -q -B main "$NEW" 2>/dev/null || true
  status ok "updated to $(git rev-parse --short "$NEW")"
  tg "✅ Updated to $(git log -1 --format='%h %s' "$NEW")."
  exit 0
fi

# Roll back: previous code + previous image + the data as it was before the update.
docker compose logs --tail 40 agents > "$CONTROL/update-failed.log" 2>&1 || true
docker compose stop agents >/dev/null 2>&1 || true
git checkout -q --detach "$PREV"
if docker image inspect bbts-agents:rollback >/dev/null 2>&1; then
  docker tag bbts-agents:rollback bbts-agents:latest
fi
for vol in data workspace; do
  [ -s "$SNAP/$vol.tgz" ] || continue
  docker run --rm --entrypoint sh -v "agent-team-kit_$vol":/v -v "$SNAP":/b bbts-agents:latest -c \
    "find /v -mindepth 1 -delete && tar xzf /b/$vol.tgz -C /v" >/dev/null 2>&1 || true
done
docker compose up -d --no-build --remove-orphans
git checkout -q -B main "$PREV" 2>/dev/null || true
status rolled_back "new version didn't become healthy; back on $(git rev-parse --short "$PREV")"
tg "↩️ Update rolled back: the new version didn't start properly. Back on $(git rev-parse --short "$PREV") with the data as it was before. The last log lines are in $CONTROL/update-failed.log."
exit 1
