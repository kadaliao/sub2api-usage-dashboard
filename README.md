# Sub2API Usage Dashboard

Reusable sidecar dashboard for existing [Sub2API](https://github.com/weishaw/sub2api) deployments.

It does not modify the Sub2API image, binary, database schema, or business tables. The sidecar connects to the existing Postgres database in read-only transactions, periodically generates usage data, and serves a dashboard under `/usage/` or a custom path.

For OpenAI OAuth accounts, the dashboard also shows the live Codex rate-limit reset credit count. This value is not stored in Postgres, so the sidecar queries the authenticated Sub2API `/admin/openai/accounts/:id/quota` endpoint and isolates per-account failures.

The dashboard includes a persistent light/dark theme switch. The first visit follows the operating system preference; an explicit choice is stored in the browser.

## Quick Install

Install under the existing Sub2API domain path:

```bash
curl -fsSL https://raw.githubusercontent.com/kadaliao/sub2api-usage-dashboard/main/install.sh | bash -s -- \
  --proxy caddy \
  --domain s2a.example.com \
  --yes
```

Nginx:

```bash
curl -fsSL https://raw.githubusercontent.com/kadaliao/sub2api-usage-dashboard/main/install.sh | bash -s -- \
  --proxy nginx \
  --domain s2a.example.com \
  --yes
```

Standalone port, without editing reverse proxy config:

```bash
curl -fsSL https://raw.githubusercontent.com/kadaliao/sub2api-usage-dashboard/main/install.sh | bash -s -- \
  --proxy port \
  --port 8091 \
  --yes
```

Run a dry-run first:

```bash
./install.sh --proxy caddy --domain s2a.example.com --dry-run
```

## What The Installer Does

1. Detects the existing Sub2API Docker Compose directory.
2. Reads Postgres settings from `.env`.
3. Detects the existing Sub2API container, Postgres container, Docker network, and host upstream port.
4. Writes `docker-compose.usage.yml` in the Sub2API directory.
5. Starts a `sub2api-usage` sidecar attached to the same Docker network.
6. If `--proxy caddy` or `--proxy nginx` is selected, patches the site that already serves Sub2API and adds the dashboard under the selected path.
7. Validates and reloads the proxy.
8. Runs a local sidecar health check.

Default dashboard path:

```text
https://your-existing-sub2api-domain/usage/
```

If multiple proxy site blocks could match, pass `--domain` to avoid ambiguity.

## Proxy Modes

### Caddy

The installer inserts this into the matching site block:

```caddy
redir /usage /usage/
handle /usage/* {
    reverse_proxy 127.0.0.1:8091
}
```

It validates with `caddy validate` before replacing the real config and keeps a timestamped backup.

### Nginx

The installer inserts this into the matching `server {}` block:

```nginx
location = /usage { return 301 /usage/; }
location /usage/ {
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_pass http://127.0.0.1:8091;
}
```

It runs `nginx -t` and restores the backup if validation fails.

### Port

The sidecar binds:

```text
0.0.0.0:8091 -> container:8091
```

Use this mode when there is no reverse proxy or when you want to manage routing yourself.

## Installer Options

```text
--proxy caddy|nginx|port     Reverse proxy integration mode.
--domain DOMAIN              Existing Sub2API public domain to patch.
--path PATH                  Mount path, default /usage.
--port PORT                  Host port for sidecar, default 8091.
--sub2api-dir DIR            Existing Sub2API compose directory.
--auth sub2api|none          Authentication mode, default sub2api.
--image IMAGE                Dashboard image.
--dry-run                    Print generated changes without writing.
--yes                        Do not prompt before changes.
```

## Authentication

Default:

```text
AUTH_MODE=sub2api
```

Users log in with their existing Sub2API account. The sidecar forwards credentials to the Sub2API login API and never stores the password. The returned access and refresh tokens are kept in the signed, `HttpOnly` 30-day session cookie so the sidecar can query live Codex reset counts; access tokens are refreshed before expiry.

Disable authentication:

```bash
./install.sh --auth none
```

Use `--auth none` only when the route is protected by another layer or intentionally public. The dashboard displays user emails, account names, status, and usage metrics. To load Codex reset counts in this mode, set `SUB2API_ADMIN_TOKEN` to a valid Sub2API admin access token.

## Manual Docker Compose

```yaml
services:
  sub2api-usage:
    image: ghcr.io/kadaliao/sub2api-usage-dashboard:latest
    container_name: sub2api-usage
    restart: unless-stopped
    ports:
      - "127.0.0.1:8091:8091"
    environment:
      DATABASE_URL: "postgres://sub2api:POSTGRES_PASSWORD@sub2api-postgres:5432/sub2api?sslmode=disable"
      BASE_PATH: "/usage"
      AUTH_MODE: "sub2api"
      SUB2API_API_BASE: "http://sub2api:8080/api/v1"
      # Optional for AUTH_MODE=none; prefer login-based tokens otherwise.
      # SUB2API_ADMIN_TOKEN: "replace-with-admin-access-token"
      SESSION_SECRET: "replace-with-random-secret"
      COOKIE_SECURE: "true"
      REFRESH_INTERVAL_SECONDS: "60"
    volumes:
      - ./usage_dashboard_data:/app/data
    networks:
      - sub2api_usage_network

networks:
  sub2api_usage_network:
    external: true
    name: "sub2api_sub2api-network"
```

## Environment Variables

```text
DATABASE_URL                 Required Postgres connection string.
BASE_PATH                    Dashboard mount path, default /usage.
AUTH_MODE                    sub2api or none, default sub2api.
SUB2API_API_BASE             Login and admin API base, default http://sub2api:8080/api/v1.
SUB2API_ADMIN_TOKEN          Optional admin access token for Codex counts, mainly for AUTH_MODE=none.
SESSION_SECRET               Cookie signing secret.
SESSION_SECRET_FILE          Secret file fallback, default /app/data/session_secret.
COOKIE_SECURE                true for HTTPS proxy, false for plain HTTP port mode.
REFRESH_INTERVAL_SECONDS     Data refresh interval, default 60.
LISTEN_HOST                  Default 0.0.0.0.
LISTEN_PORT                  Default 8091.
```

## Development

Run tests:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
bash -n install.sh
python3 -m py_compile usage_dashboard_server.py installer/proxy_patch.py
```

Build image:

```bash
docker build -t sub2api-usage-dashboard:local .
```

## Rollback

Remove the sidecar:

```bash
cd /path/to/sub2api
docker compose -f docker-compose.usage.yml down
rm -f docker-compose.usage.yml
```

Restore proxy config from the timestamped backup generated by the installer, then reload Caddy or Nginx.
