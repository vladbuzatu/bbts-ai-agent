#!/usr/bin/env bash
# Restore from a nightly backup (ops/backup.sh).
#
#   ops/restore.sh                       list the available backups
#   ops/restore.sh <YYYY-MM-DD> <what>   restore one part: workspace | data | studio | claude | env | all
#
# The agent team is stopped while volumes are restored, then started again. The current contents of
# a restored volume are first saved next to the backup as <name>.before-restore.tgz (nothing is lost).
# Databases are not restored automatically: see the printed command (they're the app's live data).
set -euo pipefail
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT=${BACKUP_DIR:-/var/backups/bbts}
IMAGE=bbts-agents:latest

if [ $# -lt 2 ]; then
  echo "Backups in $ROOT:"
  for d in $(ls -1 "$ROOT" 2>/dev/null | sort -r); do echo "  $d  $(du -sh "$ROOT/$d" | cut -f1)  $(ls "$ROOT/$d" | tr '\n' ' ')"; done
  echo; echo "Usage: $0 <YYYY-MM-DD> workspace|data|studio|claude|env|all"
  exit 0
fi
DAY=$1; WHAT=$2; SRC="$ROOT/$DAY"
[ -d "$SRC" ] || { echo "No backup $SRC"; exit 1; }
parts=("$WHAT"); [ "$WHAT" = all ] && parts=(workspace data studio claude env)

read -r -p "Restore ${parts[*]} from $DAY? The agent team will be stopped meanwhile. Type yes: " ok
[ "$ok" = yes ] || { echo "Cancelled."; exit 1; }
cd "$KIT_DIR"
docker compose stop agents
for p in "${parts[@]}"; do
  if [ "$p" = env ]; then
    cp .env "$SRC/env.before-restore" 2>/dev/null || true
    cp "$SRC/env" .env && chmod 600 .env && echo "restored .env"
    continue
  fi
  [ -f "$SRC/$p.tgz" ] || { echo "skip $p: not in this backup"; continue; }
  vol="agent-team-kit_$p"
  docker run --rm --entrypoint tar -v "$vol":/v -v "$SRC":/b "$IMAGE" czf "/b/$p.before-restore.tgz" -C /v .
  docker run --rm --entrypoint sh -v "$vol":/v -v "$SRC":/b "$IMAGE" -c \
    "find /v -mindepth 1 -delete && tar xzf /b/$p.tgz -C /v"
  echo "restored $p"
done
docker compose up -d
echo
for f in "$SRC"/db-*.sql.gz; do
  [ -f "$f" ] || continue
  env=$(basename "$f" .sql.gz); env=${env#db-}
  echo "Database dump for $env: $f"
  echo "  restore (replaces the $env data!):  gunzip -c $f | docker exec -i app-$env-db-1 psql -U app app"
done
