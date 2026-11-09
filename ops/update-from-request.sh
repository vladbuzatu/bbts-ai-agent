#!/usr/bin/env bash
# Started by bbts-update.path when the bot writes /var/lib/bbts/control/update-request (/update).
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REQ=${CONTROL_DIR:-/var/lib/bbts/control}/update-request
ref=$(head -c 200 "$REQ" 2>/dev/null | tr -dc 'A-Za-z0-9._/-')
rm -f "$REQ"
case "$ref" in
  ""|main) exec "$KIT_DIR/ops/update.sh" ;;
  check)   exec "$KIT_DIR/ops/update.sh" --check ;;
  *)       exec "$KIT_DIR/ops/update.sh" "$ref" ;;
esac
