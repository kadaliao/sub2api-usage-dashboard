#!/usr/bin/env bash
set -euo pipefail

IMAGE="ghcr.io/kadaliao/sub2api-usage-dashboard:latest"
PROXY=""
DOMAIN=""
BASE_PATH="/usage"
PORT="8091"
SUB2API_DIR=""
AUTH_MODE="sub2api"
DRY_RUN="0"
YES="0"
CADDY_CONFIG="${CADDY_CONFIG:-/etc/caddy/Caddyfile}"
NGINX_CONFIG="${NGINX_CONFIG:-}"
INSTALL_BASE_URL="${INSTALL_BASE_URL:-https://raw.githubusercontent.com/kadaliao/sub2api-usage-dashboard/main}"

usage() {
  cat <<'EOF'
Install Sub2API Usage Dashboard sidecar.

Usage:
  ./install.sh --proxy caddy --domain s2a.example.com
  ./install.sh --proxy nginx --domain s2a.example.com
  ./install.sh --proxy port --port 8091

Options:
  --proxy caddy|nginx|port     Reverse proxy integration mode.
  --domain DOMAIN              Existing Sub2API public domain to patch.
  --path PATH                  Mount path, default /usage.
  --port PORT                  Host port for sidecar, default 8091.
  --sub2api-dir DIR            Existing Sub2API compose directory.
  --auth sub2api|none          Authentication mode, default sub2api.
  --image IMAGE                Dashboard image.
  --dry-run                    Print generated changes without writing.
  --yes                        Do not prompt before changes.
  -h, --help                   Show this help.
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

log() {
  echo "==> $*"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --proxy) PROXY="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --path) BASE_PATH="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --sub2api-dir) SUB2API_DIR="${2:-}"; shift 2 ;;
    --auth) AUTH_MODE="${2:-}"; shift 2 ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN="1"; shift ;;
    --yes) YES="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

case "$AUTH_MODE" in
  sub2api|none) ;;
  *) die "--auth must be sub2api or none" ;;
esac

normalize_path() {
  local path="$1"
  path="/${path#/}"
  path="${path%/}"
  [[ "$path" == "" ]] && path="/"
  echo "$path"
}

BASE_PATH="$(normalize_path "$BASE_PATH")"
[[ "$BASE_PATH" == "/" ]] && die "--path must be a subpath such as /usage"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "--port must be numeric"

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
  else
    die "docker compose or docker-compose is required"
  fi
}

detect_proxy() {
  if [[ -n "$PROXY" ]]; then
    echo "$PROXY"
    return
  fi
  if command -v caddy >/dev/null 2>&1 && [[ -f "$CADDY_CONFIG" ]]; then
    echo "caddy"
  elif command -v nginx >/dev/null 2>&1; then
    echo "nginx"
  else
    echo "port"
  fi
}

detect_sub2api_dir() {
  if [[ -n "$SUB2API_DIR" ]]; then
    echo "$SUB2API_DIR"
    return
  fi
  if [[ -f docker-compose.yml ]] && grep -q "sub2api" docker-compose.yml; then
    pwd
    return
  fi
  if [[ -f /root/sub2api/docker-compose.yml ]]; then
    echo "/root/sub2api"
    return
  fi
  die "could not find Sub2API compose directory; pass --sub2api-dir"
}

read_env_value() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 0
  awk -F= -v key="$key" '$1 == key {value=$0; sub("^[^=]*=", "", value); print value}' "$file" | tail -1 | sed -e "s/^['\"]//" -e "s/['\"]$//"
}

urlencode() {
  python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  fi
}

detect_container() {
  local preferred="$1"
  local service="$2"
  local compose="$3"
  if docker inspect "$preferred" >/dev/null 2>&1; then
    echo "$preferred"
    return
  fi
  local found
  found="$($compose ps -q "$service" 2>/dev/null | head -1 || true)"
  [[ -n "$found" ]] || die "could not find container for service $service"
  echo "$found"
}

detect_network() {
  docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$1" | head -1
}

detect_sub2api_upstream() {
  local container="$1"
  local port_line port
  port_line="$(docker port "$container" 8080/tcp 2>/dev/null | head -1 || true)"
  if [[ -n "$port_line" ]]; then
    port="${port_line##*:}"
    echo "127.0.0.1:$port"
  else
    echo "127.0.0.1:8080"
  fi
}

find_nginx_config() {
  if [[ -n "$NGINX_CONFIG" ]]; then
    echo "$NGINX_CONFIG"
    return
  fi
  if [[ -n "$DOMAIN" ]]; then
    local hit
    hit="$(grep -Rsl "server_name .*${DOMAIN}" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | head -1 || true)"
    [[ -n "$hit" ]] && { echo "$hit"; return; }
  fi
  for candidate in /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf /etc/nginx/nginx.conf; do
    [[ -f "$candidate" ]] && { echo "$candidate"; return; }
  done
  die "could not find Nginx config; set NGINX_CONFIG=/path/to/site.conf"
}

ensure_patcher() {
  local script_dir patcher
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  patcher="$script_dir/installer/proxy_patch.py"
  if [[ -f "$patcher" ]]; then
    echo "$patcher"
    return
  fi
  patcher="/tmp/sub2api-usage-proxy-patch.py"
  curl -fsSL "$INSTALL_BASE_URL/installer/proxy_patch.py" -o "$patcher"
  chmod 0755 "$patcher"
  echo "$patcher"
}

confirm() {
  [[ "$YES" == "1" || "$DRY_RUN" == "1" ]] && return 0
  echo
  read -r -p "Proceed with installation? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || die "aborted"
}

write_file() {
  local path="$1"
  local content="$2"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "--- $path"
    printf '%s\n' "$content"
  else
    printf '%s\n' "$content" > "$path"
  fi
}

run_or_print() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

PROXY="$(detect_proxy)"
case "$PROXY" in
  caddy|nginx|port) ;;
  *) die "--proxy must be caddy, nginx, or port" ;;
esac

command -v docker >/dev/null 2>&1 || die "docker is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
COMPOSE="$(compose_cmd)"
SUB2API_DIR="$(detect_sub2api_dir)"
cd "$SUB2API_DIR"

[[ -f docker-compose.yml ]] || die "$SUB2API_DIR/docker-compose.yml not found"
POSTGRES_PASSWORD="$(read_env_value "$SUB2API_DIR/.env" "POSTGRES_PASSWORD")"
[[ -n "$POSTGRES_PASSWORD" ]] || die "POSTGRES_PASSWORD not found in $SUB2API_DIR/.env"
POSTGRES_USER="$(read_env_value "$SUB2API_DIR/.env" "POSTGRES_USER")"
POSTGRES_DB="$(read_env_value "$SUB2API_DIR/.env" "POSTGRES_DB")"
POSTGRES_USER="${POSTGRES_USER:-sub2api}"
POSTGRES_DB="${POSTGRES_DB:-sub2api}"

SUB2API_CONTAINER="$(detect_container "sub2api" "sub2api" "$COMPOSE")"
POSTGRES_CONTAINER="$(detect_container "sub2api-postgres" "postgres" "$COMPOSE")"
NETWORK="$(detect_network "$SUB2API_CONTAINER")"
[[ -n "$NETWORK" ]] || die "could not detect Docker network for $SUB2API_CONTAINER"
SUB2API_UPSTREAM="$(detect_sub2api_upstream "$SUB2API_CONTAINER")"
ENCODED_PASSWORD="$(urlencode "$POSTGRES_PASSWORD")"
SESSION_SECRET="$(random_secret)"
COOKIE_SECURE="true"
BIND_ADDR="127.0.0.1"
if [[ "$PROXY" == "port" ]]; then
  BIND_ADDR="0.0.0.0"
  COOKIE_SECURE="false"
fi

DATABASE_URL="postgres://${POSTGRES_USER}:${ENCODED_PASSWORD}@${POSTGRES_CONTAINER}:5432/${POSTGRES_DB}?sslmode=disable"
COMPOSE_FILE="$SUB2API_DIR/docker-compose.usage.yml"
COMPOSE_CONTENT="$(cat <<EOF
services:
  sub2api-usage:
    image: ${IMAGE}
    container_name: sub2api-usage
    restart: unless-stopped
    ports:
      - "${BIND_ADDR}:${PORT}:8091"
    environment:
      DATABASE_URL: "${DATABASE_URL}"
      BASE_PATH: "${BASE_PATH}"
      AUTH_MODE: "${AUTH_MODE}"
      SUB2API_API_BASE: "http://${SUB2API_CONTAINER}:8080/api/v1"
      SESSION_SECRET: "${SESSION_SECRET}"
      COOKIE_SECURE: "${COOKIE_SECURE}"
      REFRESH_INTERVAL_SECONDS: "60"
    volumes:
      - ./usage_dashboard_data:/app/data
    networks:
      - sub2api_usage_network

networks:
  sub2api_usage_network:
    external: true
    name: "${NETWORK}"
EOF
)"

log "Sub2API directory: $SUB2API_DIR"
log "Proxy mode: $PROXY"
log "Dashboard path: $BASE_PATH"
log "Sidecar host port: $PORT"
log "Docker network: $NETWORK"
log "Existing Sub2API upstream: $SUB2API_UPSTREAM"
confirm

write_file "$COMPOSE_FILE" "$COMPOSE_CONTENT"
run_or_print $COMPOSE -f "$COMPOSE_FILE" up -d

PATCHER=""
if [[ "$PROXY" == "caddy" ]]; then
  PATCHER="$(ensure_patcher)"
  [[ -f "$CADDY_CONFIG" ]] || die "Caddy config not found: $CADDY_CONFIG"
  if [[ "$DRY_RUN" == "1" ]]; then
    python3 "$PATCHER" --kind caddy --config "$CADDY_CONFIG" --domain "$DOMAIN" --path "$BASE_PATH" --upstream "127.0.0.1:$PORT" --sub2api-upstream "$SUB2API_UPSTREAM" --dry-run
  else
    tmp="$(mktemp)"
    cp "$CADDY_CONFIG" "$tmp"
    python3 "$PATCHER" --kind caddy --config "$tmp" --domain "$DOMAIN" --path "$BASE_PATH" --upstream "127.0.0.1:$PORT" --sub2api-upstream "$SUB2API_UPSTREAM" --write
    caddy validate --config "$tmp"
    backup="${CADDY_CONFIG}.before-sub2api-usage-$(date +%Y%m%d%H%M%S)"
    cp "$CADDY_CONFIG" "$backup"
    cp "$tmp" "$CADDY_CONFIG"
    caddy fmt --overwrite "$CADDY_CONFIG" >/dev/null 2>&1 || true
    caddy reload --config "$CADDY_CONFIG" || systemctl reload caddy
    rm -f "$tmp"
    log "Caddy config patched; backup: $backup"
  fi
elif [[ "$PROXY" == "nginx" ]]; then
  PATCHER="$(ensure_patcher)"
  NGINX_CONFIG="$(find_nginx_config)"
  if [[ "$DRY_RUN" == "1" ]]; then
    python3 "$PATCHER" --kind nginx --config "$NGINX_CONFIG" --domain "$DOMAIN" --path "$BASE_PATH" --upstream "http://127.0.0.1:$PORT" --sub2api-upstream "$SUB2API_UPSTREAM" --dry-run
  else
    backup="${NGINX_CONFIG}.before-sub2api-usage-$(date +%Y%m%d%H%M%S)"
    cp "$NGINX_CONFIG" "$backup"
    if ! python3 "$PATCHER" --kind nginx --config "$NGINX_CONFIG" --domain "$DOMAIN" --path "$BASE_PATH" --upstream "http://127.0.0.1:$PORT" --sub2api-upstream "$SUB2API_UPSTREAM" --write; then
      cp "$backup" "$NGINX_CONFIG"
      exit 1
    fi
    if ! nginx -t; then
      cp "$backup" "$NGINX_CONFIG"
      die "nginx -t failed; restored $backup"
    fi
    systemctl reload nginx || nginx -s reload
    log "Nginx config patched; backup: $backup"
  fi
fi

if [[ "$DRY_RUN" == "1" ]]; then
  log "dry-run complete"
  exit 0
fi

sleep 2
if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null; then
  log "sidecar health check passed"
else
  die "sidecar health check failed"
fi

if [[ "$PROXY" == "port" ]]; then
  log "dashboard available at http://SERVER:${PORT}${BASE_PATH}/"
elif [[ -n "$DOMAIN" ]]; then
  log "dashboard available at https://${DOMAIN}${BASE_PATH}/"
else
  log "dashboard route installed at ${BASE_PATH}/ on the detected Sub2API site"
fi
