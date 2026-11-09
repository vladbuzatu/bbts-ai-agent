#!/usr/bin/env bash
# Shared helpers for the host scripts in ops/ (sourced, not run).

KIT_DIR="${KIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

envval() {  # envval NAME -> value from the kit's .env (no export, no eval)
  local line
  line=$(grep -E "^$1=" "$KIT_DIR/.env" 2>/dev/null | tail -1) || return 0
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

tg() {  # tg "message" -> Telegram, to the owner (best effort, never fails the caller)
  local token owner
  token=$(envval TELEGRAM_BOT_TOKEN); owner=$(envval TELEGRAM_OWNER_ID)
  [ -n "$token" ] && [ -n "$owner" ] || return 0
  curl -s -m 15 -o /dev/null "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${owner}" --data-urlencode "text=$1" || true
}
