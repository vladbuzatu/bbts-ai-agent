#!/usr/bin/env bash
# End-to-end test of the preview site (ops/site.sh) with real Docker, as root:
#   sudo bash tests/site_e2e.sh
# Runs a proxy like the server's (Caddy on the "proxy" network, with a staging site already in its
# Caddyfile), then: deploy project alpha, switch to beta, a broken beta rolls back, an unsafe compose
# file is refused, stop, and the watchdog puts our block back after the Caddyfile is regenerated.
# Uses only the caddy:2-alpine image (every test service is a tiny Caddy).
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T=$(mktemp -d /tmp/site-e2e.XXXXXX)
PORT=${E2E_PORT:-18080}
CADDY=bbts-e2e-caddy
made_net=0
pass=0

cleanup() {
  for p in alpha beta evil; do
    mapfile -t ids < <(docker ps -aq --filter "label=com.docker.compose.project=site-$p")
    [ "${#ids[@]}" -gt 0 ] && docker rm -f "${ids[@]}" >/dev/null 2>&1
    docker volume ls -q --filter "label=com.docker.compose.project=site-$p" | xargs -r docker volume rm >/dev/null 2>&1
    docker network rm "site-${p}_default" >/dev/null 2>&1
  done
  docker rm -f "$CADDY" >/dev/null 2>&1
  docker images --format '{{.Repository}}:{{.Tag}}' | grep '^bbts-site-' | xargs -r docker rmi -f >/dev/null 2>&1
  [ "$made_net" = 1 ] && docker network rm proxy >/dev/null 2>&1
  rm -rf "$T"
  return 0
}
trap cleanup EXIT

ok() { echo "  ✓ $1"; pass=$((pass + 1)); }
die() { echo "  ✗ $1"; echo "----- site log"; cat "$T/control/site-log.txt" 2>/dev/null | tail -60; echo "----- status"; cat "$T/control/site-status.json" 2>/dev/null; exit 1; }
get() { curl -s -m 5 -H "Host: site.localhost" "http://127.0.0.1:$PORT$1"; }
status_is() { python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); sys.exit(0 if s["state"]==sys.argv[2] else 1)' "$T/control/site-status.json" "$1"; }

# --- the kit on the "server"
mkdir -p "$T/kit" "$T/control/site" "$T/proxy"
cp -r "$KIT/ops" "$T/kit/ops"
printf 'SITE_DOMAIN=http://site.localhost\nSITE_MEM=256m\n' > "$T/kit/.env"
export KIT_DIR="$T/kit" CONTROL_DIR="$T/control" SITE_ROOT="$T/site" SITE_CADDYFILE="$T/proxy/Caddyfile" \
       SITE_CADDY_CONTAINER="$CADDY" SITE_TRIES=40 SITE_SLEEP=1

# --- the proxy, with staging already configured
docker network inspect proxy >/dev/null 2>&1 || { docker network create proxy >/dev/null; made_net=1; }
printf 'http://staging.localhost {\n\trespond "staging ok"\n}\n' > "$T/proxy/Caddyfile"
docker run -d --name "$CADDY" --network proxy -p "127.0.0.1:$PORT:80" \
  -v "$T/proxy/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2-alpine >/dev/null
sleep 2

project() {  # project <name> <text> [broken]: a small web + backend + db app in a git repo
  local d="$T/repos/$1"
  rm -rf "$d"; mkdir -p "$d/preview" "$d/web" "$d/backend"
  cat > "$d/preview/compose.yml" <<'EOF'
services:
  db:
    image: caddy:2-alpine
    volumes: [dbdata:/data]
  backend:
    build: ./backend
    environment:
      SECRET: ${DJANGO_SECRET_KEY}
      SITE_URL: ${SITE_URL}
    depends_on: [db]
  web:
    build: ./web
    depends_on: [backend]
volumes:
  dbdata:
EOF
  printf 'FROM caddy:2-alpine\nCOPY Caddyfile /etc/caddy/Caddyfile\n' > "$d/web/Dockerfile"
  printf 'FROM caddy:2-alpine\nCOPY Caddyfile /etc/caddy/Caddyfile\n' > "$d/backend/Dockerfile"
  printf ':8000 {\n\trespond "api {env.SITE_URL} secret-set-{env.SECRET}"\n}\n' > "$d/backend/Caddyfile"
  if [ "${3:-}" = broken ]; then
    printf ':80 {\n\tthis is not valid\n}\n' > "$d/web/Caddyfile"
  else
    printf ':80 {\n\thandle /api/* {\n\t\treverse_proxy backend:8000\n\t}\n\trespond "%s"\n}\n' "$2" > "$d/web/Caddyfile"
  fi
  git -C "$d" init -q -b main && git -C "$d" add -A
  git -C "$d" -c user.email=t@t -c user.name=t commit -qm "$2"
}

request() {  # request <action> <project>: what the bot does (snapshot + request file), then the systemd unit
  local id d commit
  id="t$(date +%s%N | tail -c 10)"
  d="$T/repos/${2:-x}"
  commit=0000000
  if [ "$1" = deploy ]; then
    commit=$(git -C "$d" rev-parse HEAD)
    git -C "$d" archive --format=tar HEAD > "$T/control/site/$id.tar"
  fi
  printf '{"id":"%s","action":"%s","project":"%s","commit":"%s","title":"e2e"}' "$id" "$1" "${2:-}" "$commit" \
    > "$T/control/site-request"
  bash "$T/kit/ops/site.sh" || true
}

echo "== deploy alpha"
project alpha "alpha v1"
request deploy alpha
status_is ok || die "alpha deploy failed"
[ "$(get /)" = "alpha v1" ] || die "site doesn't show alpha: $(get /)"
ok "alpha is live behind the proxy"
get /api/x | grep -q "api http://site.localhost secret-set-[0-9a-f]\{64\}" || die "env not passed: $(get /api/x)"
ok "web reaches its backend; SITE_URL and a generated secret are set"
get /robots.txt | grep -q "Disallow: /" || die "robots.txt: $(get /robots.txt)"
curl -s -I -m 5 -H "Host: site.localhost" "http://127.0.0.1:$PORT/" | grep -qi "x-robots-tag: noindex" || die "no X-Robots-Tag"
ok "not indexed: robots.txt disallows all, X-Robots-Tag noindex"
[ "$(curl -s -m 5 -H 'Host: staging.localhost' "http://127.0.0.1:$PORT/")" = "staging ok" ] || die "staging broken"
ok "the existing staging site still works"

echo "== switch to beta"
project beta "beta v1"
request deploy beta
status_is ok || die "beta deploy failed"
[ "$(get /)" = "beta v1" ] || die "site doesn't show beta"
[ -z "$(docker ps -q --filter label=com.docker.compose.project=site-alpha)" ] || die "alpha still running"
[ -n "$(docker volume ls -q --filter label=com.docker.compose.project=site-alpha)" ] || die "alpha's data was deleted"
ok "beta replaced alpha; alpha stopped, its data kept"

echo "== a broken beta rolls back"
project beta "beta v2" broken
request deploy beta
status_is failed || die "broken deploy reported success"
[ "$(get /)" = "beta v1" ] || die "rollback failed: $(get /)"
grep -q "previous one is back" "$T/control/site-status.json" || die "status doesn't say it rolled back"
ok "failed to start -> previous beta is back"

echo "== an unsafe compose file is refused"
project evil "evil"
cat > "$T/repos/evil/preview/compose.yml" <<'EOF'
services:
  web:
    image: caddy:2-alpine
    privileged: true
    ports: ["2222:22"]
    volumes: ["/:/host"]
    env_file: [/etc/hostname]
EOF
git -C "$T/repos/evil" -c user.email=t@t -c user.name=t commit -qam evil
request deploy evil
status_is failed || die "unsafe compose was accepted"
for w in privileged ports "bind mount" env_file; do grep -q "$w" "$T/control/site-status.json" || die "not reported: $w"; done
[ -z "$(docker ps -q --filter label=com.docker.compose.project=site-evil)" ] || die "evil is running"
[ "$(get /)" = "beta v1" ] || die "site changed"
ok "refused (privileged, ports, bind mount, host env file); beta still live"

echo "== watchdog puts our block back"
printf 'http://staging.localhost {\n\trespond "staging ok"\n}\n' > "$T/proxy/Caddyfile.new"
cat "$T/proxy/Caddyfile.new" > "$T/proxy/Caddyfile"
docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1
[ "$(get /)" != "beta v1" ] || die "test setup: block still active"
bash "$T/kit/ops/site.sh" --heal
[ "$(get /)" = "beta v1" ] || die "heal didn't restore: $(get /)"
ok "proxy block restored after the Caddyfile was regenerated"
docker stop "$(docker ps -q --filter label=com.docker.compose.project=site-beta --filter label=com.docker.compose.service=web)" >/dev/null
bash "$T/kit/ops/site.sh" --heal
sleep 2
[ "$(get /)" = "beta v1" ] || die "heal didn't restart web"
ok "a stopped site is started again"

echo "== stop"
request stop
status_is stopped || die "stop failed"
get / | grep -q "Nothing is deployed" || die "placeholder missing: $(get /)"
ok "stopped: the address shows a placeholder"

echo "All $pass preview-site checks passed."
