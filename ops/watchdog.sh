#!/usr/bin/env bash
# Self-healing checks, every 2 minutes (systemd timer bbts-watchdog.timer). Fixes what it safely can
# and tells you on Telegram what it fixed; warns before memory or disk run out. Quiet when all is well.
set -uo pipefail
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$KIT_DIR/ops/lib.sh"
STATE=/var/lib/bbts/control/watchdog
mkdir -p "$STATE"

once() {  # once <key> <seconds>: true at most once per period (no alert spam)
  local f="$STATE/$1"
  if [ -f "$f" ] && [ $(( $(date +%s) - $(stat -c %Y "$f") )) -lt "$2" ]; then return 1; fi
  touch "$f"; return 0
}
running() { [ "$(docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null)" = true ]; }
exists() { docker inspect "$1" >/dev/null 2>&1; }

# 1. The agent team itself.
if [ -f /var/lib/bbts/control/update.lock ] && ! flock -n /var/lib/bbts/control/update.lock true; then
  exit 0   # an update is in progress: it handles restarts itself
fi
if ! running agent-team-kit-agents-1; then
  (cd "$KIT_DIR" && docker compose up -d >/dev/null 2>&1)
  sleep 20
  if running agent-team-kit-agents-1; then
    tg "🩹 Watchdog: the agent team wasn't running. I started it again."
  elif once agents-down 1800; then
    tg "🚨 Watchdog: the agent team is down and won't start. Check: cd $KIT_DIR && docker compose logs --tail 50"
  fi
fi

# 2. The HTTPS proxy in front of staging/production.
if exists proxy-caddy-1 && ! running proxy-caddy-1; then
  (cd /opt/app/proxy && docker compose -p proxy up -d >/dev/null 2>&1)
  tg "🩹 Watchdog: the HTTPS proxy had stopped. I started it again."
fi

# 3. Staging/production web containers must be on the proxy network under their name (502 otherwise).
for env in staging production; do
  c="app-$env-web-1"
  exists "$c" && running "$c" || continue
  docker network inspect proxy >/dev/null 2>&1 || continue
  aliases=$(docker inspect --format "{{range \$k, \$v := .NetworkSettings.Networks}}{{if eq \$k \"proxy\"}}{{range \$v.Aliases}}{{.}} {{end}}{{end}}{{end}}" "$c" 2>/dev/null)
  if ! docker inspect --format '{{json .NetworkSettings.Networks}}' "$c" | grep -q '"proxy"'; then
    docker network connect --alias "web-$env" proxy "$c" && tg "🩹 Watchdog: $env had dropped off the proxy network (the site showed 502). Reconnected."
  elif ! grep -qw "web-$env" <<<"$aliases"; then
    docker network disconnect proxy "$c" && docker network connect --alias "web-$env" proxy "$c" \
      && tg "🩹 Watchdog: $env had lost its name on the proxy network (502). Fixed."
  fi
done

# 4. Memory and disk.
read -r total avail < <(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print t, a}' /proc/meminfo)
if [ "$total" -gt 0 ] && [ $(( avail * 100 / total )) -lt 8 ] && once mem 3600; then
  top=$(docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null | sort -k2 -h | tail -3 | tr '\n' ';')
  tg "⚠️ Watchdog: memory almost full ($(( avail / 1024 )) MB free). Biggest: ${top}"
fi
if dmesg 2>/dev/null | tail -200 | grep -q "Out of memory: Killed process" && once oom 21600; then
  tg "⚠️ Watchdog: Linux killed a process because memory ran out (see dmesg). Consider a bigger server or MEM_LIMIT in .env."
fi
use=$(df --output=pcent / | tail -1 | tr -dc 0-9)
if [ "${use:-0}" -ge 85 ] && once disk 21600; then
  tg "⚠️ Watchdog: disk ${use}% full. 'docker system prune -af' frees old images; backups are in /var/backups/bbts."
fi
exit 0
