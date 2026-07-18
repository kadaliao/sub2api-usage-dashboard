import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_dashboard_has_persistent_theme_switch(self):
        html = Path(server.__file__).with_name("index.html").read_text(encoding="utf-8")

        self.assertIn('id="themeToggle"', html)
        self.assertIn('role="switch"', html)
        self.assertIn('localStorage.setItem(themeKey, normalized)', html)
        self.assertIn(':root[data-theme="dark"]', html)

        app = server.UsageDashboardApp(Path(server.__file__).parent, Path(server.__file__).with_name("data.json"), auth_mode="none")
        login_html = app.login_html()
        self.assertIn('localStorage.getItem("sub2api-theme")', login_html)
        self.assertIn(':root[data-theme="dark"]', login_html)

    def test_session_cookie_round_trip_and_tamper_rejection(self):
        secret = b"test-secret"
        cookie = server.create_session_cookie(
            "user@example.com",
            secret,
            now=1000,
            max_age=60,
            api_tokens={"at": "access", "rt": "refresh", "at_exp": 1050},
        )

        payload = server.validate_session_cookie(cookie, secret, now=1010)

        self.assertEqual(payload["sub"], "user@example.com")
        self.assertEqual(payload["at"], "access")
        self.assertEqual(payload["rt"], "refresh")
        self.assertEqual(payload["at_exp"], 1050)
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

    def test_authenticate_with_sub2api_extracts_nested_token_pair(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "code": 0,
                    "data": {
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "expires_in": 3600,
                    },
                }).encode("utf-8")

        with mock.patch.object(server.urllib.request, "urlopen", return_value=Response()), mock.patch.object(server.time, "time", return_value=1000):
            ok, auth = server.authenticate_with_sub2api("admin@example.com", "secret")

        self.assertTrue(ok)
        self.assertEqual(auth["at"], "access-token")
        self.assertEqual(auth["rt"], "refresh-token")
        self.assertEqual(auth["at_exp"], 4600)

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

    def test_login_stores_sub2api_tokens_in_signed_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "public"
            public.mkdir()

            app = server.UsageDashboardApp(
                public,
                root / "data.json",
                secret=b"secret",
                authenticator=lambda email, password: (
                    True,
                    {"message": "ok", "at": "access-token", "rt": "refresh-token", "at_exp": int(time.time()) + 3600},
                ),
            )

            status, headers, _ = call_app(app, "/usage/login", method="POST", body=b"email=user%40example.com&password=secret")

            cookie = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
            session = server.validate_session_cookie(cookie, b"secret")
            self.assertEqual(status, "302 Found")
            self.assertEqual(session["at"], "access-token")
            self.assertEqual(session["rt"], "refresh-token")

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

    def test_codex_resets_endpoint_filters_oauth_accounts_and_returns_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "public"
            public.mkdir()
            data_file = root / "data.json"
            data_file.write_text(json.dumps({
                "accounts": [
                    {"id": 11, "platform": "openai", "type": "oauth"},
                    {"id": 12, "platform": "openai", "type": "api_key"},
                    {"id": 13, "platform": "anthropic", "type": "oauth"},
                ]
            }), encoding="utf-8")
            seen = {}

            def quota_fetcher(api_base, account_ids, access_token):
                seen.update(api_base=api_base, account_ids=account_ids, access_token=access_token)
                return {"11": {"available_count": 2, "expires_at": ["2026-07-03T04:05:06Z"]}}, {}

            app = server.UsageDashboardApp(
                public,
                data_file,
                secret=b"secret",
                api_base="http://sub2api/api/v1",
                quota_fetcher=quota_fetcher,
            )
            cookie = server.create_session_cookie("admin@example.com", b"secret", api_tokens={"at": "admin-access"})

            status, _, body = call_app(
                app,
                "/usage/codex-resets.json",
                headers={"Cookie": f"{server.COOKIE_NAME}={cookie}"},
            )

            payload = json.loads(body)
            self.assertEqual(status, "200 OK")
            self.assertEqual(payload["accounts"]["11"]["available_count"], 2)
            self.assertEqual(seen["account_ids"], [11])
            self.assertEqual(seen["access_token"], "admin-access")

    def test_codex_resets_endpoint_refreshes_expiring_access_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "public"
            public.mkdir()
            data_file = root / "data.json"
            data_file.write_text(json.dumps({"accounts": [{"id": 11, "platform": "openai", "type": "oauth"}]}), encoding="utf-8")
            seen = {}

            def token_refresher(api_base, refresh_token):
                self.assertEqual(refresh_token, "old-refresh")
                return {"at": "new-access", "rt": "new-refresh", "at_exp": int(time.time()) + 3600}

            def quota_fetcher(api_base, account_ids, access_token):
                seen["access_token"] = access_token
                return {}, {}

            app = server.UsageDashboardApp(
                public,
                data_file,
                secret=b"secret",
                quota_fetcher=quota_fetcher,
                token_refresher=token_refresher,
            )
            cookie = server.create_session_cookie(
                "admin@example.com",
                b"secret",
                api_tokens={"at": "old-access", "rt": "old-refresh", "at_exp": int(time.time()) + 30},
            )

            status, headers, _ = call_app(
                app,
                "/usage/codex-resets.json",
                headers={"Cookie": f"{server.COOKIE_NAME}={cookie}"},
            )

            refreshed_cookie = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
            refreshed_session = server.validate_session_cookie(refreshed_cookie, b"secret")
            self.assertEqual(status, "200 OK")
            self.assertEqual(seen["access_token"], "new-access")
            self.assertEqual(refreshed_session["rt"], "new-refresh")

    def test_query_codex_reset_credit_extracts_count_and_expirations(self):
        quota_payload = {
            "fetched_at": 123,
            "rate_limit_reset_credits": {
                "available_count": 2,
                "credits": [
                    {"expires_at": "2026-07-04T04:05:06Z"},
                    {"expires_at": "2026-07-03T04:05:06Z"},
                ],
            },
        }
        with mock.patch.object(server, "request_sub2api_json", return_value=quota_payload) as request:
            result = server.query_codex_reset_credit("http://sub2api/api/v1", 11, "access")

        self.assertEqual(result["available_count"], 2)
        self.assertEqual(result["expires_at"][0], "2026-07-03T04:05:06Z")
        request.assert_called_once_with(
            "http://sub2api/api/v1/admin/openai/accounts/11/quota",
            access_token="access",
        )

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
