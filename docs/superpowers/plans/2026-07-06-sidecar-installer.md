# Sub2API Usage Sidecar Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable Docker sidecar and installer that exposes the Sub2API usage dashboard under an existing Sub2API domain path or on a standalone port.

**Architecture:** The Docker image runs a small Python HTTP service that serves the dashboard, authenticates through the existing Sub2API login API when enabled, and periodically refreshes `data.json` by connecting directly to Postgres. The installer detects the existing Docker Compose deployment, writes an external-network compose file for the sidecar, and optionally patches Caddy or Nginx to proxy `/usage/` to the sidecar.

**Tech Stack:** Python 3.12, stdlib WSGI server, psycopg binary package, Docker Compose, Bash installer, pure Python proxy config patcher, unittest.

---

### Task 1: Container Service

**Files:**
- Create: `usage_dashboard_server.py`
- Create: `requirements.txt`
- Create: `Dockerfile`
- Modify: `query.sql`
- Test: `tests/test_usage_dashboard_server.py`

- [ ] Create a server module that normalizes `BASE_PATH`, loads a session secret, supports `AUTH_MODE=sub2api|none`, serves static dashboard files, refreshes data through `DATABASE_URL`, and exposes `/health`.
- [ ] Write unit tests for base-path routing, protected redirects, no-auth static serving, cookie validation, username resolution through an injected DB query function, and data refresh with a fake DB row.
- [ ] Run `python3 -m unittest tests/test_usage_dashboard_server.py -v`.

### Task 2: Proxy Patcher

**Files:**
- Create: `installer/proxy_patch.py`
- Test: `tests/test_proxy_patch.py`

- [ ] Implement Caddy patching that finds a site block by domain or by existing Sub2API upstream and inserts `redir` plus `handle_path`.
- [ ] Implement Nginx patching that finds a `server {}` block by `server_name` or existing Sub2API upstream and inserts `location` blocks.
- [ ] Make the patcher idempotent when the path is already configured.
- [ ] Run `python3 -m unittest tests/test_proxy_patch.py -v`.

### Task 3: Installer

**Files:**
- Create: `install.sh`
- Test: shell syntax plus dry-run output

- [ ] Implement flags: `--proxy caddy|nginx|port`, `--domain`, `--path`, `--port`, `--sub2api-dir`, `--auth sub2api|none`, `--image`, `--dry-run`, `--yes`.
- [ ] Detect Docker Compose command, Sub2API compose directory, `.env` password, running network, and local upstream port.
- [ ] Generate `docker-compose.usage.yml` with the sidecar attached to the external Sub2API network.
- [ ] In Caddy/Nginx modes, call `installer/proxy_patch.py`, validate config, reload proxy, and smoke-test the configured URL.
- [ ] In port mode, expose `0.0.0.0:${PORT}:8091` and print the direct URL.
- [ ] Run `bash -n install.sh`.

### Task 4: Packaging Docs

**Files:**
- Create: `README.md`
- Create: `.dockerignore`
- Modify: `index.html`

- [ ] Document one-command install, explicit proxy modes, environment variables, security model, rollback, and manual compose usage.
- [ ] Ensure Docker build context excludes screenshots, sample data, pycache, and local test artifacts.
- [ ] Run all unit tests, shell syntax checks, and an optional local Docker build if Docker is available.
