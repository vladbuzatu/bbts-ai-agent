#!/usr/bin/env bash
# One-time (and safe to re-run) setup of the server that runs staging/production, as root.
#
#   bash server-setup.sh                               # reuse saved domains (or free sslip.io names)
#   bash server-setup.sh shop.ro staging.shop.ro       # set production + staging domains
#   NEW_STAGING_PASSWORD=1 bash server-setup.sh        # also generate a new staging password
#
# What it sets up:
# - Docker, firewall (22/80/443), swap, daily production DB backups
# - /opt/app/{staging,production}: compose file + secrets (.env), root-owned
# - the deploy gate (/opt/app/bin/deploy-gate): the ONLY command GitHub's deploy key can
#   run (SSH forced command + sudo rule for that one file). The deploy user has no shell
#   and is not in the docker group.
# - an HTTPS proxy (Caddy): password on staging, /version.json readable for the bot
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
for f in deploy-gate compose.yml; do [ -f "$HERE/$f" ] || { echo "Missing $HERE/$f - run this from the kit's deploy folder"; exit 1; }; done

IP="$(curl -fsS -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
DASHED="${IP//./-}"
mkdir -p /opt/app/bin /opt/app/proxy /opt/app/staging /opt/app/production /opt/app/backups

# Domains: arguments > saved > existing Caddyfile > free sslip.io names
if [ $# -ge 2 ]; then PROD_DOMAIN="$1"; STAGING_DOMAIN="$2"
elif [ -f /opt/app/DOMAINS ]; then read -r PROD_DOMAIN STAGING_DOMAIN < /opt/app/DOMAINS
elif [ -f /opt/app/proxy/Caddyfile ]; then
  mapfile -t SITES < <(grep -E '^[A-Za-z0-9.-]+ \{$' /opt/app/proxy/Caddyfile | awk '{print $1}')
  PROD_DOMAIN="${SITES[0]:-${DASHED}.sslip.io}"; STAGING_DOMAIN="${SITES[1]:-staging.${DASHED}.sslip.io}"
else PROD_DOMAIN="${DASHED}.sslip.io"; STAGING_DOMAIN="staging.${DASHED}.sslip.io"; fi
echo "$PROD_DOMAIN $STAGING_DOMAIN" > /opt/app/DOMAINS
echo "==> Production: $PROD_DOMAIN   Staging: $STAGING_DOMAIN"

echo "==> Packages and Docker"
apt-get update -qq && apt-get install -y -qq curl openssl ufw sudo util-linux >/dev/null
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

echo "==> Swap (2 GB)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Firewall (SSH, HTTP, HTTPS)"
ufw allow OpenSSH >/dev/null && ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "==> Deploy gate"
install -o root -g root -m 755 "$HERE/deploy-gate" /opt/app/bin/deploy-gate
for ENV_NAME in staging production; do
  install -o root -g root -m 644 "$HERE/compose.yml" "/opt/app/$ENV_NAME/compose.yml"
done

echo "==> Deploy user: no shell, no docker group, key can only run the gate"
id deploy >/dev/null 2>&1 || useradd -m -s /bin/bash deploy
gpasswd -d deploy docker >/dev/null 2>&1 || true
[ -f /root/deploy_key ] || ssh-keygen -q -t ed25519 -N "" -C "github-actions-deploy" -f /root/deploy_key
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
# shellcheck disable=SC2016
echo "command=\"sudo -n /opt/app/bin/deploy-gate \\\"\$SSH_ORIGINAL_COMMAND\\\"\",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty $(cat /root/deploy_key.pub)" \
  > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys
echo "deploy ALL=(root) NOPASSWD: /opt/app/bin/deploy-gate" > /etc/sudoers.d/deploy-gate
chmod 440 /etc/sudoers.d/deploy-gate
visudo -cf /etc/sudoers.d/deploy-gate >/dev/null

echo "==> App environments"
for ENV_NAME in staging production; do
  DOMAIN="$PROD_DOMAIN"; [ "$ENV_NAME" = staging ] && DOMAIN="$STAGING_DOMAIN"
  F="/opt/app/$ENV_NAME/.env"
  if [ ! -f "$F" ]; then
    cat > "$F" <<EOF
APP_ENV=$ENV_NAME
POSTGRES_PASSWORD=$(openssl rand -hex 24)
DJANGO_SECRET_KEY=$(openssl rand -hex 32)
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=$DOMAIN,localhost,backend
DJANGO_CSRF_TRUSTED_ORIGINS=https://$DOMAIN
EOF
  else
    sed -i "s|^DJANGO_ALLOWED_HOSTS=.*|DJANGO_ALLOWED_HOSTS=$DOMAIN,localhost,backend|; s|^DJANGO_CSRF_TRUSTED_ORIGINS=.*|DJANGO_CSRF_TRUSTED_ORIGINS=https://$DOMAIN|" "$F"
  fi
done
chown -R root:root /opt/app && chmod 700 /opt/app/staging /opt/app/production /opt/app/backups
chmod 600 /opt/app/staging/.env /opt/app/production/.env

echo "==> HTTPS proxy"
docker network inspect proxy >/dev/null 2>&1 || docker network create proxy >/dev/null
STAGING_USER=staff
OLD_HASH="$(grep -oE 'staff \$2[aby]\$[^ ]+' /opt/app/proxy/Caddyfile 2>/dev/null | awk '{print $2}' || true)"
if [ -n "$OLD_HASH" ] && [ "${NEW_STAGING_PASSWORD:-0}" != 1 ]; then
  STAGING_HASH="$OLD_HASH"; STAGING_PASS="(unchanged - run with NEW_STAGING_PASSWORD=1 for a new one)"
else
  STAGING_PASS="$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 16)"
  STAGING_HASH="$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$STAGING_PASS")"
fi
cat > /opt/app/proxy/Caddyfile <<EOF
$PROD_DOMAIN {
	reverse_proxy web-production:80
}

$STAGING_DOMAIN {
	@version path /version.json
	handle @version {
		reverse_proxy web-staging:80
	}
	handle {
		basic_auth {
			$STAGING_USER $STAGING_HASH
		}
		reverse_proxy web-staging:80
	}
}
EOF
cat > /opt/app/proxy/compose.yml <<'EOF'
services:
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    networks: [proxy]
networks:
  proxy:
    external: true
volumes:
  caddy_data:
  caddy_config:
EOF
(cd /opt/app/proxy && docker compose -p proxy up -d --force-recreate >/dev/null)

echo "==> Daily production database backup (kept 14 days)"
cat > /etc/cron.daily/app-db-backup <<'EOF'
#!/bin/sh
cd /opt/app/production 2>/dev/null || exit 0
[ -f DEPLOYED_TAG ] || exit 0
TAG=$(cat DEPLOYED_TAG) IMAGE_REPO="ghcr.io/$(tr 'A-Z' 'a-z' < /opt/app/REPO 2>/dev/null)" \
  docker compose -p app-production exec -T db pg_dump -U app app 2>/dev/null | gzip > /opt/app/backups/daily-production-$(date +%F).sql.gz
find /opt/app/backups -name 'daily-*.sql.gz' -mtime +14 -delete
EOF
chmod +x /etc/cron.daily/app-db-backup

KNOWN_HOSTS="$IP $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"

cat <<EOF

=====================================================================
 Done. GitHub repo -> Settings -> Secrets and variables -> Actions:

   DEPLOY_HOST          $IP
   DEPLOY_KNOWN_HOSTS   $KNOWN_HOSTS
   DEPLOY_SSH_KEY       (everything below, including BEGIN/END lines)
---------------------------------------------------------------------
$(cat /root/deploy_key)
---------------------------------------------------------------------
 (If DEPLOY_HOST and DEPLOY_SSH_KEY are already set, only add DEPLOY_KNOWN_HOSTS.)

 Production: https://$PROD_DOMAIN
 Staging:    https://$STAGING_DOMAIN   user: $STAGING_USER   password: $STAGING_PASS

 Agent server .env:
   STAGING_URL=https://$STAGING_DOMAIN
   PRODUCTION_URL=https://$PROD_DOMAIN
=====================================================================
EOF
