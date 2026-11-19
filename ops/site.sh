#!/usr/bin/env bash
# Preview site: puts a project on SITE_DOMAIN (one shared address: public, but not indexed by search
# engines or AI crawlers). Deploying another project replaces what the address shows.
#
# Started by bbts-site.path when the bot writes $CONTROL/site-request (Telegram /site, or the PM):
#   {"id": "...", "action": "deploy"|"stop", "project": "tstauto", "commit": "abc123", "title": "..."}
# with the project's committed code (git archive) in $CONTROL/site/<id>.tar.
#
#   ops/site.sh            handle the pending request
#   ops/site.sh --heal     watchdog: the proxy has our block, the current site is running
#
# The project describes how it runs in preview/compose.yml (written by the agents, so untrusted):
# ops/site_check.py refuses anything that could reach the host (bind mounts, privileged, ports, host
# network, host env files…). Every service gets memory/CPU/process limits. A deploy builds the new
# version while the old one keeps serving, checks that 'web' answers, and only then switches the
# address; if the check fails it goes back to the previous version of that project.
set -uo pipefail
KIT_DIR="${KIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
. "$KIT_DIR/ops/lib.sh"

CONTROL=${CONTROL_DIR:-/var/lib/bbts/control}
ROOT=${SITE_ROOT:-/var/lib/bbts/site}
CADDYFILE=${SITE_CADDYFILE:-/opt/app/proxy/Caddyfile}
CADDY=${SITE_CADDY_CONTAINER:-proxy-caddy-1}
TRIES=${SITE_TRIES:-90}
SLEEP=${SITE_SLEEP:-2}
DOMAIN=$(envval SITE_DOMAIN)
MEM=$(envval SITE_MEM); MEM=${MEM:-512m}
STATUS="$CONTROL/site-status.json"
LOG="$CONTROL/site-log.txt"
CHECK="$KIT_DIR/ops/site_check.py"
BEGIN_MARK="# BEGIN bbts-site"
END_MARK="# END bbts-site"

mkdir -p "$ROOT" "$CONTROL"
chmod 700 "$ROOT"
exec 9>"$ROOT/.lock"
if [ "${1:-}" = --heal ]; then
  flock -n 9 || exit 0                       # a deploy is running: it takes care of things itself
else
  flock -w 1800 9 || { echo "another site deploy is still running"; exit 1; }
fi

url() { case "$DOMAIN" in http://*|https://*) printf '%s' "$DOMAIN" ;; *) printf 'https://%s' "$DOMAIN" ;; esac; }

status() {  # status <state> <message>   (uses ID PROJECT COMMIT TITLE)
  python3 - "$STATUS" "$1" "$2" "${ID:-}" "${PROJECT:-}" "${COMMIT:-}" "${TITLE:-}" "$(url)" <<'PY'
import json, os, sys, time
path, state, msg, ident, project, commit, title, url = sys.argv[1:9]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump({"state": state, "message": msg, "id": ident, "project": project, "commit": commit,
               "title": title, "url": url, "at": int(time.time())}, f)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

state_get() {  # state_get <key> [file]
  python3 -c 'import json,sys
try: print(json.load(open(sys.argv[2])).get(sys.argv[1]) or "")
except Exception: print("")' "$1" "${2:-$ROOT/state.json}"
}

state_set() {  # state_set <file> key=value...
  python3 - "$@" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    d = json.load(open(path))
except Exception:
    d = {}
for kv in sys.argv[2:]:
    k, _, v = kv.partition("=")
    d[k] = v
tmp = path + ".tmp"
json.dump(d, open(tmp, "w"))
os.replace(tmp, path)
PY
}

# ---------------------------------------------------------------- proxy (Caddy)
block() {  # block <upstream or empty>
  local site="$DOMAIN"
  echo "$BEGIN_MARK (managed by the agent team: ops/site.sh rewrites this block)"
  echo "$site {"
  printf '\theader X-Robots-Tag "noindex, nofollow, noarchive"\n'
  printf '\thandle /robots.txt {\n\t\trespond "User-agent: *\nDisallow: /\n" 200\n\t}\n'
  if [ -n "$1" ]; then
    printf '\thandle {\n\t\treverse_proxy %s\n\t}\n' "$1"
  else
    printf '\thandle {\n\t\trespond "Nothing is deployed on the preview site right now." 503\n\t}\n'
  fi
  echo "}"
  echo "$END_MARK"
}

setup_proxy() {  # a minimal HTTPS proxy when the server doesn't have one yet (no staging set up)
  [ "$CADDYFILE" = /opt/app/proxy/Caddyfile ] || return 1
  docker inspect "$CADDY" >/dev/null 2>&1 && return 1
  echo "No HTTPS proxy on this server yet: setting one up in /opt/app/proxy"
  mkdir -p /opt/app/proxy
  docker network inspect proxy >/dev/null 2>&1 || docker network create proxy >/dev/null
  : > "$CADDYFILE"
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
  return 0
}

caddy_point() {  # caddy_point <upstream or empty>: rewrite our block in place (the file is bind-mounted) and reload
  [ -n "$DOMAIN" ] || return 0
  local fresh=0
  if [ ! -f "$CADDYFILE" ]; then
    if setup_proxy; then fresh=1; else echo "Caddyfile $CADDYFILE not found"; return 1; fi
  fi
  local old new
  old=$(cat "$CADDYFILE")
  new=$(printf '%s\n' "$old" | awk -v b="$BEGIN_MARK" -v e="$END_MARK" 'index($0,b)==1{skip=1} !skip{print} index($0,e)==1{skip=0}')
  new=$(printf '%s\n\n%s\n' "$new" "$(block "$1")" | cat -s)
  [ "$new" = "$old" ] && [ "$fresh" = 0 ] && return 0
  printf '%s\n' "$new" > "$CADDYFILE"           # same inode: the running proxy sees the change
  if [ "$fresh" = 1 ]; then
    (cd /opt/app/proxy && docker compose -p proxy up -d >/dev/null) || return 1
    return 0
  fi
  if ! docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    echo "The proxy refused the new configuration; keeping the old one."
    printf '%s\n' "$old" > "$CADDYFILE"
    docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1
    return 1
  fi
}

# ---------------------------------------------------------------- compose helpers
resolver() {  # an image with just the host's Compose binary: resolves compose files in a jail
  local bin="" c tag tmp
  for c in /usr/libexec/docker/cli-plugins/docker-compose /usr/lib/docker/cli-plugins/docker-compose \
           /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose \
           "${HOME:-/root}/.docker/cli-plugins/docker-compose"; do
    [ -x "$c" ] && { bin="$c"; break; }
  done
  [ -n "$bin" ] || return 1
  tag="bbts-site-resolver:$(sha256sum "$bin" | cut -c1-12)"
  if ! docker image inspect "$tag" >/dev/null 2>&1; then
    tmp=$(mktemp -d)
    cp "$bin" "$tmp/docker-compose"
    printf 'FROM scratch\nCOPY docker-compose /docker-compose\nENTRYPOINT ["/docker-compose"]\n' > "$tmp/Dockerfile"
    docker build -q -t "$tag" "$tmp" >/dev/null || { rm -rf "$tmp"; return 1; }
    rm -rf "$tmp"
  fi
  printf '%s' "$tag"
}

resolve() {  # resolve <project> <id>: the compose file as Compose sees it, resolved where ONLY the project's
  # code and its generated env files exist (no host files, no network). env_file, include, extends or
  # anything else pointing outside the project simply isn't there, whatever the Compose version.
  local p="$1" i="$2" src="$ROOT/$1/src-$2" img
  img=$(resolver) || { echo "couldn't prepare the compose resolver (is the Compose plugin installed?)" >&2; return 1; }
  local mounts=(-v "$src:$src:ro" -v "$ROOT/$p/site.env:$ROOT/$p/site.env:ro")
  local envs=(--env-file "$ROOT/$p/site.env")
  if [ -f "$ROOT/$p/secrets.env" ]; then
    mounts+=(-v "$ROOT/$p/secrets.env:$ROOT/$p/secrets.env:ro"); envs+=(--env-file "$ROOT/$p/secrets.env")
  fi
  docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
    -e HOME=/tmp -e DOCKER_CONFIG=/tmp "${mounts[@]}" -w "$src" "$img" \
    -p "site-$p" --project-directory "$src" -f "$src/preview/compose.yml" "${envs[@]}" config --format json
}

dc_args() {  # dc_args <project> <id> [override] -> DC=(docker compose ...) on the RESOLVED file, clean environment
  local p="$1" i="$2" ov="${3:-}"
  DC=(env -i PATH="$PATH" HOME="${HOME:-/root}" docker compose --progress plain -p "site-$p"
      --project-directory "$ROOT/$p/src-$i" -f "$ROOT/$p/compose-$i.json" --env-file /dev/null)
  [ -n "$ov" ] && DC+=(-f "$ov")
  return 0
}
dc() { dc_args "$1" "$2" "${3:-}"; shift 3; "${DC[@]}" "$@"; }   # nothing from the host's environment leaks in

stop_project() {  # containers stay (volumes and data kept); just not running
  local ids
  mapfile -t ids < <(docker ps -q --filter "label=com.docker.compose.project=site-$1")
  [ "${#ids[@]}" -gt 0 ] && docker stop "${ids[@]}" >/dev/null
  return 0
}

answers() {  # answers <project> <port>: 'web' responds over the proxy network (any status below 500)
  local cid ip code _
  for _ in $(seq 1 "$TRIES"); do
    cid=$(docker ps -q --filter "label=com.docker.compose.project=site-$1" --filter "label=com.docker.compose.service=web" | head -1)
    if [ -n "$cid" ]; then
      ip=$(docker inspect --format '{{with index .NetworkSettings.Networks "proxy"}}{{.IPAddress}}{{end}}' "$cid" 2>/dev/null)
      if [ -n "$ip" ]; then
        code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://$ip:$2/" 2>/dev/null)
        [ -n "$code" ] && [ "$code" != 000 ] && [ "$code" -lt 500 ] && return 0
      fi
    fi
    sleep "$SLEEP"
  done
  return 1
}

site_env() {  # generated once per project; SITE_* refreshed every deploy
  local f="$ROOT/$1/site.env"
  if [ ! -f "$f" ]; then
    { echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"; echo "DJANGO_SECRET_KEY=$(openssl rand -hex 32)"; } > "$f"
  fi
  local host="${DOMAIN#*://}"
  sed -i '/^SITE_DOMAIN=/d; /^SITE_URL=/d; /^SITE_HOST=/d' "$f"
  { echo "SITE_DOMAIN=$host"; echo "SITE_HOST=${host%%:*}"; echo "SITE_URL=$(url)"; } >> "$f"
  chmod 600 "$f"
}

cleanup() {  # keep the running version and the one before it (for rollback); drop older code and images
  local p="$1" keep1="$2" keep2="${3:-}" d i img
  for d in "$ROOT/$p"/src-*; do
    [ -d "$d" ] || continue
    i="${d##*/src-}"
    [ "$i" = "$keep1" ] || [ "$i" = "$keep2" ] && continue
    rm -rf "$d" "$ROOT/$p/override-$i.json" "$ROOT/$p/compose-$i.json"
  done
  docker images --format '{{.Repository}}:{{.Tag}}' | grep "^bbts-site-$p-" | while read -r img; do
    i="${img##*:}"
    [ "$i" = "$keep1" ] || [ "$i" = "$keep2" ] || docker rmi "$img" >/dev/null 2>&1
  done
  docker builder prune -f --filter until=168h >/dev/null 2>&1 || true
}

fail() {  # fail <message>
  echo "FAILED: $1"
  status failed "$1"
  tg "❌ Preview site: $PROJECT wasn't deployed – $1"
  finish_log
  exit 1
}

finish_log() { local t; t=$(tail -n 400 "$LOG" 2>/dev/null); printf '%s\n' "$t" > "$LOG"; }

# ---------------------------------------------------------------- deploy / stop
deploy() {
  [ -n "$DOMAIN" ] || fail "SITE_DOMAIN isn't set in the kit's .env"
  [[ "$DOMAIN" =~ ^(https?://)?[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || fail "SITE_DOMAIN '$DOMAIN' doesn't look like a domain"
  local tar="$CONTROL/site/$ID.tar" src="$ROOT/$PROJECT/src-$ID" ov="$ROOT/$PROJECT/override-$ID.json"
  [ -f "$tar" ] || fail "the code snapshot is missing"
  mkdir -p "$src" && chmod 700 "$ROOT/$PROJECT"
  tar -xf "$tar" -C "$src" --no-same-owner || fail "couldn't unpack the code"
  rm -f "$tar"
  [ -f "$src/preview/compose.yml" ] || fail "the project has no preview/compose.yml (see the preview-site skill)"
  site_env "$PROJECT"

  local cfg verdict
  cfg=$(resolve "$PROJECT" "$ID" 2>"$ROOT/$PROJECT/config.err") \
    || fail "preview/compose.yml has errors: $(grep -v '^\s*$' "$ROOT/$PROJECT/config.err" | tail -3)"
  verdict=$(printf '%s' "$cfg" | python3 "$CHECK" check "$src") || fail "couldn't check preview/compose.yml"
  if [ "$(printf '%s' "$verdict" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ok"])')" != True ]; then
    fail "preview/compose.yml isn't allowed on this server: $(printf '%s' "$verdict" | python3 -c 'import json,sys;print("; ".join(json.load(sys.stdin)["errors"]))')"
  fi
  printf '%s\n' "$cfg" > "$ROOT/$PROJECT/compose-$ID.json"     # what runs is exactly what was checked
  local port
  port=$(printf '%s' "$verdict" | python3 -c 'import json,sys;print(json.load(sys.stdin)["port"])')
  printf '%s' "$cfg" | python3 "$CHECK" override "$PROJECT" "$ID" "site-$PROJECT" "$MEM" > "$ov" || fail "couldn't prepare the limits"

  local cur_project prev_id
  cur_project=$(state_get project)
  prev_id=$(state_get good "$ROOT/$PROJECT/state.json")

  status building "Building $PROJECT ${COMMIT:0:7}"
  echo "==> Building $PROJECT ${COMMIT:0:7}"
  dc_args "$PROJECT" "$ID" "$ov"
  timeout "${SITE_BUILD_TIMEOUT:-1500}" "${DC[@]}" build || fail "the build failed (/site log shows why)"

  status starting "Starting $PROJECT ${COMMIT:0:7}"
  echo "==> Starting"
  if ! dc "$PROJECT" "$ID" "$ov" up -d --remove-orphans || ! answers "$PROJECT" "$port"; then
    echo "==> It didn't answer. Last output of each service:"
    dc "$PROJECT" "$ID" "$ov" logs --tail 40 --no-color 2>&1 | tail -n 150
    if [ -n "$prev_id" ] && [ -d "$ROOT/$PROJECT/src-$prev_id" ]; then
      echo "==> Going back to the previous version of $PROJECT"
      dc "$PROJECT" "$prev_id" "$ROOT/$PROJECT/override-$prev_id.json" up -d --no-build --remove-orphans
      cleanup "$PROJECT" "$prev_id"
      fail "the new version didn't start, so the previous one is back (/site log shows why)"
    fi
    stop_project "$PROJECT"
    [ "$cur_project" = "$PROJECT" ] && { caddy_point ""; state_set "$ROOT/state.json" project= ; }
    cleanup "$PROJECT" "nothing"
    fail "it didn't start (/site log shows why)"
  fi

  echo "==> It answers. Pointing $DOMAIN at it"
  caddy_point "site-$PROJECT:$port" || fail "the proxy didn't take the new address"
  if [ -n "$cur_project" ] && [ "$cur_project" != "$PROJECT" ]; then
    echo "==> Stopping $cur_project (its data is kept)"
    stop_project "$cur_project"
  fi
  state_set "$ROOT/state.json" project="$PROJECT" id="$ID" commit="$COMMIT" title="$TITLE" port="$port" at="$(date +%s)"
  state_set "$ROOT/$PROJECT/state.json" good="$ID" commit="$COMMIT"
  cleanup "$PROJECT" "$ID" "$prev_id"
  status ok "$PROJECT ${COMMIT:0:7} is live"
  tg "🌐 Preview site: $PROJECT (${COMMIT:0:7}) is live at $(url)"
  finish_log
}

stop_site() {
  local cur
  cur=$(state_get project)
  [ -n "$cur" ] && stop_project "$cur"
  caddy_point ""
  state_set "$ROOT/state.json" project=
  PROJECT="$cur"
  local note=""
  [ -n "$cur" ] && note=" (the data of $cur is kept)"
  status stopped "The preview site is stopped$note"
}

heal() {  # watchdog: our proxy block survives a regenerated Caddyfile; the current site keeps running
  [ -n "$DOMAIN" ] && [ -f "$CADDYFILE" ] || return 0
  local cur id port
  cur=$(state_get project); id=$(state_get id); port=$(state_get port)
  if ! grep -q "^$BEGIN_MARK" "$CADDYFILE"; then
    caddy_point "${cur:+site-$cur:$port}" && echo "restored the preview block in the proxy"
  fi
  if [ -n "$cur" ] && [ -z "$(docker ps -q --filter "label=com.docker.compose.project=site-$cur" --filter "label=com.docker.compose.service=web")" ]; then
    dc "$cur" "$id" "$ROOT/$cur/override-$id.json" up -d --no-build >/dev/null 2>&1 && echo "restarted $cur"
  fi
}

if [ "${1:-}" = --heal ]; then heal; exit 0; fi

REQ="$CONTROL/site-request"
[ -f "$REQ" ] || exit 0
fields=$(python3 - "$REQ" <<'PY'
import json, re, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception:
    r = {}
def ok(v, rx):
    v = str(v or "")
    return v if re.fullmatch(rx, v) else ""
title = re.sub(r"[\x00-\x1f]", " ", str(r.get("title") or ""))[:120]
print("\t".join([ok(r.get("action"), r"deploy|stop"), ok(r.get("id"), r"[a-z0-9-]{6,40}"),
                 ok(r.get("project"), r"[a-z0-9][a-z0-9-]{0,29}"), ok(r.get("commit"), r"[0-9a-f]{7,40}"),
                 title or "-"]))
PY
)
rm -f "$REQ"
IFS=$'\t' read -r ACTION ID PROJECT COMMIT TITLE <<< "$fields"
[ "$TITLE" = "-" ] && TITLE=""

: > "$LOG"; chmod 600 "$LOG"
exec >>"$LOG" 2>&1
echo "==> $(date -u +%FT%TZ) $ACTION ${PROJECT:-} ${COMMIT:0:7}"
case "$ACTION" in
  deploy) [ -n "$ID" ] && [ -n "$PROJECT" ] || fail "the request was incomplete"; deploy ;;
  stop)   stop_site; finish_log ;;
  *)      PROJECT=""; status failed "unknown request"; finish_log ;;
esac
