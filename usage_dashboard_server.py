#!/usr/bin/env python3
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from wsgiref.simple_server import make_server


COOKIE_NAME = "sub2api_usage_session"
SESSION_MAX_AGE = 30 * 86400
DEFAULT_API_BASE = "http://sub2api:8080/api/v1"


def normalize_base_path(value):
    value = (value or "/usage").strip()
    if not value or value == "/":
        return ""
    return "/" + value.strip("/")


def b64_encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64_decode(value):
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def html_escape(value):
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )


def create_session_cookie(subject, secret, now=None, max_age=SESSION_MAX_AGE):
    now = int(now if now is not None else time.time())
    payload = {"sub": subject, "iat": now, "exp": now + max_age}
    payload_part = b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret, payload_part.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_part}.{b64_encode(sig)}"


def validate_session_cookie(cookie_value, secret, now=None):
    now = int(now if now is not None else time.time())
    try:
        payload_part, sig_part = cookie_value.split(".", 1)
        expected_sig = hmac.new(secret, payload_part.encode("ascii"), hashlib.sha256).digest()
        actual_sig = b64_decode(sig_part)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(b64_decode(payload_part).decode("utf-8"))
        if int(payload.get("exp", 0)) < now:
            return None
        return payload
    except Exception:
        return None


def load_or_create_secret(path):
    path = Path(path)
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48).encode("ascii")
    path.write_bytes(secret)
    os.chmod(path, 0o600)
    return secret


def parse_cookies(environ):
    raw = environ.get("HTTP_COOKIE", "")
    cookies = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        cookies[key] = value
    return cookies


def authenticate_with_sub2api(email, password, api_base=DEFAULT_API_BASE):
    endpoint = f"{api_base.rstrip('/')}/auth/login"
    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
            return False, payload.get("message") or "账号或密码不正确"
        except Exception:
            return False, "账号或密码不正确"
    except Exception:
        return False, "登录服务暂时不可用"

    if isinstance(payload, dict) and (payload.get("access_token") or payload.get("data") or payload.get("code") == 0):
        return True, "ok"
    return False, payload.get("message") if isinstance(payload, dict) else "账号或密码不正确"


def default_query_runner(database_url, sql):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for database refresh") from exc

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql)
            row = cur.fetchone()
    if not row:
        raise RuntimeError("usage query returned no rows")
    return row[0]


class UsageDataRefresher:
    def __init__(self, database_url, query_file, data_file, query_runner=None):
        self.database_url = database_url
        self.query_file = Path(query_file)
        self.data_file = Path(data_file)
        self.query_runner = query_runner or default_query_runner

    def refresh_once(self):
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required")
        sql = self.query_file.read_text(encoding="utf-8")
        raw = self.query_runner(self.database_url, sql)
        payload = json.loads(raw if isinstance(raw, str) else json.dumps(raw, default=str))
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.data_file.with_name(f".{self.data_file.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, self.data_file)
        return payload


class RefreshState:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_ok_at = None
        self.last_error = None

    def mark_ok(self):
        with self.lock:
            self.last_ok_at = time.time()
            self.last_error = None

    def mark_error(self, error):
        with self.lock:
            self.last_error = str(error)

    def snapshot(self):
        with self.lock:
            return {"last_ok_at": self.last_ok_at, "last_error": self.last_error}


def start_refresh_loop(refresher, interval_seconds, state):
    def run():
        while True:
            try:
                refresher.refresh_once()
                state.mark_ok()
            except Exception as exc:
                state.mark_error(exc)
                print(f"usage refresh failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(interval_seconds)

    thread = threading.Thread(target=run, name="usage-refresh", daemon=True)
    thread.start()
    return thread


class UsageDashboardApp:
    def __init__(
        self,
        public_dir,
        data_file,
        base_path="/usage",
        auth_mode="sub2api",
        secret=b"",
        cookie_secure=True,
        authenticator=None,
        username_resolver=None,
        refresh_state=None,
    ):
        self.public_dir = Path(public_dir).resolve()
        self.data_file = Path(data_file).resolve()
        self.base_path = normalize_base_path(base_path)
        self.auth_mode = auth_mode
        self.secret = secret
        self.cookie_secure = cookie_secure
        self.authenticator = authenticator or authenticate_with_sub2api
        self.username_resolver = username_resolver or (lambda username: None)
        self.refresh_state = refresh_state or RefreshState()

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/") or "/"

        if path == "/health":
            return self.respond_json(start_response, self.health_payload())
        if self.base_path and path == "/":
            return self.redirect(start_response, f"{self.base_path}/")
        if self.base_path and path == self.base_path:
            return self.redirect(start_response, f"{self.base_path}/")
        if self.base_path and not path.startswith(f"{self.base_path}/"):
            return self.not_found(start_response)

        rel_path = self.relative_path(path)
        if rel_path == "login":
            if self.auth_mode == "none":
                return self.redirect(start_response, self.url_for(""))
            if method == "POST":
                return self.handle_login(environ, start_response)
            return self.respond_html(start_response, self.login_html(), "200 OK")
        if rel_path == "logout":
            return self.logout(start_response)

        if self.auth_mode != "none" and not self.current_session(environ):
            return self.redirect(start_response, self.url_for("login"))
        return self.serve_static(rel_path, start_response)

    def health_payload(self):
        state = self.refresh_state.snapshot()
        return {
            "status": "ok" if not state["last_error"] else "degraded",
            "base_path": self.base_path or "/",
            "auth_mode": self.auth_mode,
            "last_refresh_ok_at": state["last_ok_at"],
            "last_refresh_error": state["last_error"],
        }

    def relative_path(self, path):
        if self.base_path:
            rel = path[len(self.base_path) :].lstrip("/")
        else:
            rel = path.lstrip("/")
        return rel or "index.html"

    def url_for(self, rel):
        rel = rel.strip("/")
        root = self.base_path or ""
        if not rel:
            return f"{root}/" if root else "/"
        return f"{root}/{rel}" if root else f"/{rel}"

    def current_session(self, environ):
        cookie = parse_cookies(environ).get(COOKIE_NAME)
        if not cookie:
            return None
        return validate_session_cookie(cookie, self.secret)

    def handle_login(self, environ, start_response):
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        body = environ.get("wsgi.input").read(min(length, 8192)).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(body)
        email = (form.get("email") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        if not email or not password:
            return self.respond_html(start_response, self.login_html("请输入账号和密码", email), "401 Unauthorized")

        login_identifier = self.resolve_login_identifier(email)
        ok, message = self.authenticator(login_identifier, password)
        if not ok:
            return self.respond_html(start_response, self.login_html(message or "账号或密码不正确", email), "401 Unauthorized")

        session_cookie = create_session_cookie(login_identifier, self.secret)
        headers = [
            ("Location", self.url_for("")),
            ("Set-Cookie", self.session_cookie_header(session_cookie)),
            *self.security_headers(),
        ]
        start_response("302 Found", headers)
        return [b""]

    def resolve_login_identifier(self, value):
        if "@" in value:
            return value
        try:
            resolved = self.username_resolver(value)
            if resolved:
                return resolved
        except Exception:
            pass
        try:
            users = json.loads(self.data_file.read_text(encoding="utf-8")).get("users", [])
        except Exception:
            return value
        normalized = value.strip().lower()
        for user in users:
            if str(user.get("username") or "").lower() == normalized and user.get("email"):
                return str(user["email"])
        return value

    def logout(self, start_response):
        headers = [
            ("Location", self.url_for("login")),
            ("Set-Cookie", self.clear_cookie_header()),
            *self.security_headers(),
        ]
        start_response("302 Found", headers)
        return [b""]

    def serve_static(self, rel_path, start_response):
        if rel_path == "data.json":
            candidate = self.data_file
        else:
            rel = rel_path.strip("/")
            if not rel or rel.endswith("/"):
                rel = f"{rel}index.html".lstrip("/")
            candidate = (self.public_dir / rel).resolve()
            if not str(candidate).startswith(str(self.public_dir)):
                return self.not_found(start_response)

        if not candidate.is_file():
            return self.not_found(start_response)

        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type == "text/html":
            content_type = "text/html; charset=utf-8"
        if content_type == "application/json":
            content_type = "application/json; charset=utf-8"
        headers = [
            ("Content-Type", content_type),
            ("Cache-Control", "no-store"),
            *self.security_headers(),
        ]
        start_response("200 OK", headers)
        return [candidate.read_bytes()]

    def respond_html(self, start_response, html, status):
        start_response(status, [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"), *self.security_headers()])
        return [html.encode("utf-8")]

    def respond_json(self, start_response, payload, status="200 OK"):
        start_response(status, [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store"), *self.security_headers()])
        return [json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")]

    def redirect(self, start_response, location):
        start_response("302 Found", [("Location", location), *self.security_headers()])
        return [b""]

    def not_found(self, start_response):
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8"), *self.security_headers()])
        return [b"not found"]

    def session_cookie_header(self, value):
        secure = "; Secure" if self.cookie_secure else ""
        return f"{COOKIE_NAME}={value}; Path={self.base_path or '/'}; Max-Age={SESSION_MAX_AGE}; HttpOnly{secure}; SameSite=Lax"

    def clear_cookie_header(self):
        secure = "; Secure" if self.cookie_secure else ""
        return f"{COOKIE_NAME}=; Path={self.base_path or '/'}; Max-Age=0; HttpOnly{secure}; SameSite=Lax"

    def security_headers(self):
        return [
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
            ("X-Frame-Options", "DENY"),
        ]

    def login_html(self, error="", email=""):
        error_html = f'<div class="error">{html_escape(error)}</div>' if error else ""
        return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <link rel="icon" href="data:," />
    <title>Sub2API Usage Login</title>
    <style>
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: linear-gradient(180deg, rgba(255,255,255,.94), rgba(245,247,251,.9) 52%, #f5f7fb);
        color: #152033;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      }}
      .shell {{
        width: min(420px, calc(100vw - 32px));
        padding: 28px;
        border: 1px solid #dce4ee;
        border-radius: 8px;
        background: rgba(255,255,255,.96);
        box-shadow: 0 18px 45px rgba(24,38,61,.08);
      }}
      .eyebrow {{ color: #2366d1; font-size: 12px; font-weight: 760; letter-spacing: .08em; text-transform: uppercase; }}
      h1 {{ margin: 8px 0 6px; font-size: 30px; line-height: 1.1; }}
      p {{ margin: 0 0 22px; color: #65738a; font-size: 14px; }}
      label {{ display: grid; gap: 7px; margin-top: 14px; color: #536075; font-size: 13px; font-weight: 650; }}
      input {{ width: 100%; height: 42px; border: 1px solid #dce4ee; border-radius: 8px; padding: 0 12px; font: inherit; color: #152033; outline: none; }}
      input:focus {{ border-color: rgba(35,102,209,.72); box-shadow: 0 0 0 3px rgba(35,102,209,.12); }}
      button {{ width: 100%; height: 42px; margin-top: 18px; border: 0; border-radius: 8px; background: #2366d1; color: #fff; font: inherit; font-weight: 760; cursor: pointer; }}
      .error {{ margin-top: 14px; padding: 10px 12px; border-radius: 8px; background: rgba(194,65,61,.1); color: #9f2f2b; font-size: 13px; }}
      .note {{ margin-top: 14px; color: #65738a; font-size: 12px; }}
    </style>
  </head>
  <body>
    <main class="shell">
      <div class="eyebrow">Sub2API</div>
      <h1>用量看板登录</h1>
      <p>使用 Sub2API 系统账号密码登录。登录状态保留 30 天。</p>
      <form method="post" action="{html_escape(self.url_for('login'))}">
        <label>账号或邮箱
          <input name="email" type="text" autocomplete="username" value="{html_escape(email)}" required autofocus />
        </label>
        <label>密码
          <input name="password" type="password" autocomplete="current-password" required />
        </label>
        {error_html}
        <button type="submit">登录</button>
      </form>
      <div class="note">认证由 Sub2API 原登录接口完成；此页面只保存 30 天签名登录态。</div>
    </main>
  </body>
</html>"""


def resolve_username_from_database(database_url, username):
    if not username or "@" in username:
        return None
    sql = "select email from users where deleted_at is null and lower(username)=lower(%s) order by id limit 1"
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for username lookup") from exc
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql, (username,))
            row = cur.fetchone()
    return row[0] if row else None


def getenv_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main():
    base_path = normalize_base_path(os.environ.get("BASE_PATH", "/usage"))
    auth_mode = os.environ.get("AUTH_MODE", "sub2api").strip().lower()
    if auth_mode not in {"sub2api", "none"}:
        raise SystemExit("AUTH_MODE must be sub2api or none")

    public_dir = Path(os.environ.get("PUBLIC_DIR", "/app/public"))
    data_file = Path(os.environ.get("DATA_FILE", "/app/data/data.json"))
    query_file = Path(os.environ.get("QUERY_FILE", "/app/query.sql"))
    database_url = os.environ.get("DATABASE_URL", "")
    api_base = os.environ.get("SUB2API_API_BASE", DEFAULT_API_BASE)
    host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("LISTEN_PORT", "8091"))
    interval = int(os.environ.get("REFRESH_INTERVAL_SECONDS", "60"))
    cookie_secure = getenv_bool("COOKIE_SECURE", True)
    secret_env = os.environ.get("SESSION_SECRET", "").encode("utf-8")
    secret_file = os.environ.get("SESSION_SECRET_FILE", "/app/data/session_secret")
    secret = secret_env or load_or_create_secret(secret_file)

    state = RefreshState()
    refresher = UsageDataRefresher(database_url=database_url, query_file=query_file, data_file=data_file)
    if getenv_bool("REFRESH_ON_START", True):
        try:
            refresher.refresh_once()
            state.mark_ok()
        except Exception as exc:
            state.mark_error(exc)
            print(f"initial usage refresh failed: {exc}", file=sys.stderr, flush=True)
    if interval > 0:
        start_refresh_loop(refresher, interval, state)

    def authenticator(email, password):
        return authenticate_with_sub2api(email, password, api_base=api_base)

    def username_resolver(username):
        return resolve_username_from_database(database_url, username)

    app = UsageDashboardApp(
        public_dir=public_dir,
        data_file=data_file,
        base_path=base_path,
        auth_mode=auth_mode,
        secret=secret,
        cookie_secure=cookie_secure,
        authenticator=authenticator,
        username_resolver=username_resolver,
        refresh_state=state,
    )
    print(f"serving Sub2API usage dashboard on http://{host}:{port}{base_path or '/'}", flush=True)
    with make_server(host, port, app) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
