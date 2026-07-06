import unittest

from installer import proxy_patch


class ProxyPatchTest(unittest.TestCase):
    def test_patch_caddy_by_domain(self):
        original = """sub.example.com {
\treverse_proxy 127.0.0.1:8080
}

other.example.com {
\treverse_proxy 127.0.0.1:9000
}
"""

        patched = proxy_patch.patch_caddy(original, domain="sub.example.com", path="/usage", upstream="127.0.0.1:8091")

        self.assertIn("handle /usage/*", patched)
        self.assertIn("reverse_proxy 127.0.0.1:8091", patched)
        self.assertLess(patched.index("handle /usage/*"), patched.index("reverse_proxy 127.0.0.1:8080"))
        self.assertEqual(proxy_patch.patch_caddy(patched, domain="sub.example.com"), patched)

    def test_patch_caddy_by_existing_upstream(self):
        original = """a.example.com {
\treverse_proxy 127.0.0.1:7000
}

b.example.com {
\treverse_proxy 127.0.0.1:8080
}
"""

        patched = proxy_patch.patch_caddy(original, sub2api_upstream="127.0.0.1:8080")

        self.assertIn("b.example.com", patched)
        block_start = patched.index("b.example.com")
        self.assertGreater(patched.index("handle /usage/*"), block_start)

    def test_patch_nginx_by_domain(self):
        original = """server {
    server_name sub.example.com;
    location / {
        proxy_pass http://127.0.0.1:8080;
    }
}

server {
    server_name other.example.com;
    location / { proxy_pass http://127.0.0.1:9000; }
}
"""

        patched = proxy_patch.patch_nginx(original, domain="sub.example.com", path="/usage", upstream="http://127.0.0.1:8091")

        self.assertIn("location = /usage", patched)
        self.assertIn("proxy_pass http://127.0.0.1:8091;", patched)
        self.assertLess(patched.index("location = /usage"), patched.index("\n}\n\nserver"))
        self.assertEqual(proxy_patch.patch_nginx(patched, domain="sub.example.com"), patched)

    def test_patch_nginx_by_existing_upstream(self):
        original = """server {
    server_name a.example.com;
    location / { proxy_pass http://127.0.0.1:7000; }
}
server {
    server_name b.example.com;
    location / { proxy_pass http://127.0.0.1:8080; }
}
"""

        patched = proxy_patch.patch_nginx(original, sub2api_upstream="127.0.0.1:8080")

        self.assertGreater(patched.index("location = /usage"), patched.index("server_name b.example.com"))

    def test_raises_when_ambiguous(self):
        original = """a.example.com {
\treverse_proxy 127.0.0.1:7000
}
b.example.com {
\treverse_proxy 127.0.0.1:9000
}
"""

        with self.assertRaises(ValueError):
            proxy_patch.patch_caddy(original)


if __name__ == "__main__":
    unittest.main()
