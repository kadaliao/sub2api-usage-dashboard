import io
import json
import tempfile
import time
import unittest
from pathlib import Path

import usage_dashboard_server as server


def call_app(app, path="/usage/", method="GET", body=b"", headers=None):
    headers = headers or {}
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = response_headers

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "wsgi.input": io.BytesIO(body),
        "CONTENT_LENGTH": str(len(body)),
    }
    for key, value in headers.items():
        environ[f"HTTP_{key.upper().replace('-', '_')}"] = value
    chunks = list(app(environ, start_response))
    return captured["status"], dict(captured["headers"]), b"".join(chunks)


class UsageDashboardServerTest(unittest.TestCase):
    def test_normalize_base_path(self):
        self.assertEqual(server.normalize_base_path("usage"), "/usage")
        self.assertEqual(server.normalize_base_path("/usage/"), "/usage")
        self.assertEqual(server.normalize_base_path("/"), "")

    def test_session_cookie_round_trip_and_tamper_rejection(self):
        secret = b"test-secret"
        cookie = server.create_session_cookie("user@example.com", secret, now=1000, max_age=60)

        payload = server.validate_session_cookie(cookie, secret, now=1010)

        self.assertEqual(payload["sub"], "user@example.com")
        self.assertIsNone(server.validate_session_cookie(cookie + "x", secret, now=1010))
        self.assertIsNone(server.validate_session_cookie(cookie, secret, now=2000))

    def test_no_auth_serves_index_under_base_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "public"
            public.mkdir()
            (public / "index.html").write_text("dashboard", encoding="utf-8")
            data_file = root / "data.json"
            data_file.write_text("{}", encoding="utf-8")
            app = server.UsageDashboardApp(public, data_file, auth_mode="none", secret=b"secret")

            status, headers, body = call_app(app, "/usage/")

            self.assertEqual(status, "200 OK")
            self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
            self.assertEqual(body, b"dashboard")

    def test_auth_redirects_to_base_path_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "public"
            public.mkdir()
            (public / "index.html").write_text("dashboard", encoding="utf-8")
            app = server.UsageDashboardApp(public, root / "data.json", base_path="/observe", secret=b"secret")

            status, headers, _ = call_app(app, "/observe/")

            self.assertEqual(status, "302 Found")
            self.assertEqual(headers["Location"], "/observe/login")

    def test_valid_cookie_serves_protected_static(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "public"
            public.mkdir()
            (public / "index.html").write_text("dashboard", encoding="utf-8")
            app = server.UsageDashboardApp(public, root / "data.json", secret=b"secret")
            cookie = server.create_session_cookie("user@example.com", b"secret", now=int(time.time()))

            status, _, body = call_app(app, "/usage/", headers={"Cookie": f"{server.COOKIE_NAME}={cookie}"})

            self.assertEqual(status, "200 OK")
            self.assertEqual(body, b"dashboard")

    def test_login_success_uses_base_path_and_cookie_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "public"
            public.mkdir()
            seen = {}

            def authenticator(email, password):
                seen["email"] = email
                seen["password"] = password
                return True, "ok"

            app = server.UsageDashboardApp(
                public,
                root / "data.json",
                base_path="/usage",
                secret=b"secret",
                cookie_secure=False,
                authenticator=authenticator,
            )

            status, headers, body = call_app(app, "/usage/login", method="POST", body=b"email=user%40example.com&password=secret")

            self.assertEqual(status, "302 Found")
            self.assertEqual(headers["Location"], "/usage/")
            self.assertIn("Path=/usage", headers["Set-Cookie"])
            self.assertNotIn("Secure", headers["Set-Cookie"])
            self.assertEqual(seen, {"email": "user@example.com", "password": "secret"})
            self.assertEqual(body, b"")

    def test_login_resolves_username_from_injected_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "public"
            public.mkdir()
            seen = {}

            def authenticator(email, password):
                seen["email"] = email
                return True, "ok"

            app = server.UsageDashboardApp(
                public,
                root / "data.json",
                secret=b"secret",
                authenticator=authenticator,
                username_resolver=lambda username: "live@example.com" if username == "liaoxingyi" else None,
            )

            status, _, _ = call_app(app, "/usage/login", method="POST", body=b"email=liaoxingyi&password=secret")

            self.assertEqual(status, "302 Found")
            self.assertEqual(seen["email"], "live@example.com")

    def test_refresher_writes_valid_json_from_query_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = root / "query.sql"
            data = root / "data.json"
            query.write_text("select payload", encoding="utf-8")

            def query_runner(database_url, sql):
                self.assertEqual(database_url, "postgres://example")
                self.assertEqual(sql, "select payload")
                return json.dumps({"generated_at": "now", "users": []})

            refresher = server.UsageDataRefresher("postgres://example", query, data, query_runner=query_runner)

            payload = refresher.refresh_once()

            self.assertEqual(payload["generated_at"], "now")
            self.assertEqual(json.loads(data.read_text(encoding="utf-8"))["users"], [])


if __name__ == "__main__":
    unittest.main()
