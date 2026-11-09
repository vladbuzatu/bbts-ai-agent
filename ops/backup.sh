#!/usr/bin/env bash
# Nightly backup of everything that lives only on this server (systemd timer bbts-backup.timer).
#
#   /var/backups/bbts/<YYYY-MM-DD>/
#     workspace.tgz   projects + team (without node_modules / venvs / caches: they're re-installable)
#     data.tgz        bot state, logs, git mirrors
#     studio.tgz      studio jobs, lab results, brand
#     claude.tgz      agents' conversation history
#     app.tgz         /opt/app (deploy gate, proxy config, pre-deploy DB dumps) + /opt/carstage
#     db-<env>.sql.gz Postgres dumps of staging/production
#     env             the kit's .env (secrets! the folder is root-only)
#
# Keeps 7 daily + 4 Sunday backups. Optional off-site copy: set BACKUP_REMOTE in .env to an rclone
# remote path (e.g. spaces:my-bucket/bbts) and install rclone. Restore: ops/restore.sh.
set -uo pipefail
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$KIT_DIR/ops/lib.sh"
ROOT=${BACKUP_DIR:-/var/backups/bbts}
DAY=$(date +%F)
DEST="$ROOT/$DAY"
mkdir -p "$DEST" && chmod 700 "$ROOT"
IMAGE=bbts-agents:latest
fail=()

vol_backup() {  # vol_backup <volume> <name> [extra tar excludes...]
  local vol="agent-team-kit_$1" name=$2; shift 2
  docker volume inspect "$vol" >/dev/null 2>&1 || return 0
  docker run --rm --entrypoint tar -v "$vol":/v:ro -v "$DEST":/b "$IMAGE" \
    czf "/b/$name.tgz" -C /v --exclude=node_modules --exclude=.venv --exclude=venv --exclude=.angular \
    --exclude=__pycache__ --exclude=.pytest_cache "$@" . 2>/dev/null || fail+=("$name")
}

vol_backup workspace workspace
vol_backup data data
vol_backup studio studio
vol_backup claude claude
vol_backup claude-studio claude-studio
tar czf "$DEST/app.tgz" --ignore-failed-read /opt/app /opt/carstage 2>/dev/null || [ $? -eq 1 ] || fail+=(app)
for env in staging production; do
  c="app-$env-db-1"
  docker inspect "$c" >/dev/null 2>&1 || continue
  docker exec "$c" pg_dump -U app app 2>/dev/null | gzip > "$DEST/db-$env.sql.gz" || fail+=("db-$env")
done
cp "$KIT_DIR/.env" "$DEST/env" 2>/dev/null && chmod 600 "$DEST/env"
size=$(du -sh "$DEST" | cut -f1)

# Retention: the 7 newest days + Sundays from the last 4 weeks.
days=()
for d in "$ROOT"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]; do [ -d "$d" ] && days+=("$(basename "$d")"); done
mapfile -t days < <(printf '%s\n' "${days[@]}" | sort -r)
for i in "${!days[@]}"; do
  d=${days[$i]}
  [ "$i" -lt 7 ] && continue
  if [ "$(date -d "$d" +%u)" = 7 ] && [ "$(( ( $(date +%s) - $(date -d "$d" +%s) ) / 86400 ))" -le 28 ]; then continue; fi
  rm -rf "${ROOT:?}/$d"
done

remote=$(envval BACKUP_REMOTE)
offsite=""
if [ -n "$remote" ]; then
  if command -v rclone >/dev/null && rclone copy "$DEST" "$remote/$DAY" --quiet; then
    offsite="copied off-site"
  else
    fail+=(offsite); offsite="off-site copy FAILED"
  fi
fi

python3 - "$DAY" "$size" "${#fail[@]}" "${fail[*]:-}" "$offsite" /var/lib/bbts/control/backup-status.json <<'PY'
import json, sys, time
day, size, nfail, failed, offsite, path = sys.argv[1:7]
json.dump({"day": day, "size": size, "ok": nfail == "0", "failed": failed.split(), "offsite": offsite,
           "at": int(time.time())}, open(path, "w"))
PY
if [ "${#fail[@]}" -gt 0 ]; then
  tg "⚠️ Nightly backup incomplete ($DAY): failed: ${fail[*]}. The rest is in $DEST ($size)."
  exit 1
fi
exit 0
