#!/usr/bin/env bash
# One-time host setup for the agent team (run as root from the kit folder):  sudo ops/install.sh
#
# Installs systemd units that run the scripts in ops/:
#   bbts-update.path        /update in Telegram -> ops/update.sh (keeps .env, rolls back if unhealthy)
#   bbts-update-check.timer hourly: is a newer kit version on GitHub? (shown in /server)
#   bbts-watchdog.timer     every 2 min: restart what stopped, reconnect staging, memory/disk alerts
#   bbts-backup.timer       nightly 03:40: backups to /var/backups/bbts (ops/restore.sh to restore)
# Safe to run again (e.g. after the kit folder moved).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo $0"; exit 1; }
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTROL=/var/lib/bbts/control
mkdir -p "$CONTROL" /var/backups/bbts /opt/carstage
chmod 755 /var/lib/bbts "$CONTROL"; chmod 700 /var/backups/bbts
chmod +x "$KIT_DIR"/ops/*.sh

unit() { cat > "/etc/systemd/system/$1"; }

unit bbts-update.service <<EOF2
[Unit]
Description=Agent team: update to the requested kit version
[Service]
Type=oneshot
ExecStart=$KIT_DIR/ops/update-from-request.sh
EOF2
unit bbts-update.path <<EOF2
[Unit]
Description=Agent team: watch for /update requests from Telegram
[Path]
PathExists=$CONTROL/update-request
Unit=bbts-update.service
[Install]
WantedBy=multi-user.target
EOF2
unit bbts-update-check.service <<EOF2
[Unit]
Description=Agent team: check GitHub for a newer kit version
[Service]
Type=oneshot
ExecStart=$KIT_DIR/ops/update.sh --check
EOF2
unit bbts-update-check.timer <<EOF2
[Unit]
Description=Agent team: hourly update check
[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
RandomizedDelaySec=5min
[Install]
WantedBy=timers.target
EOF2
unit bbts-watchdog.service <<EOF2
[Unit]
Description=Agent team: self-healing checks
[Service]
Type=oneshot
ExecStart=$KIT_DIR/ops/watchdog.sh
EOF2
unit bbts-watchdog.timer <<EOF2
[Unit]
Description=Agent team: self-healing checks every 2 minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
EOF2
unit bbts-backup.service <<EOF2
[Unit]
Description=Agent team: nightly backup
[Service]
Type=oneshot
Nice=10
IOSchedulingClass=idle
ExecStart=$KIT_DIR/ops/backup.sh
EOF2
unit bbts-backup.timer <<EOF2
[Unit]
Description=Agent team: nightly backup at 03:40
[Timer]
OnCalendar=*-*-* 03:40:00
Persistent=true
RandomizedDelaySec=10min
[Install]
WantedBy=timers.target
EOF2

systemctl daemon-reload
systemctl enable --now bbts-update.path bbts-update-check.timer bbts-watchdog.timer bbts-backup.timer >/dev/null
rm -f "$CONTROL/update-request"
"$KIT_DIR/ops/update.sh" --check >/dev/null 2>&1 || true
echo "Installed. Timers:"
systemctl list-timers 'bbts-*' --no-pager | head -6
echo
echo "Run a first backup now (optional):  systemctl start bbts-backup.service && ls /var/backups/bbts"
