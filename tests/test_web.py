import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from fleet import web
from fleet.auth import Authenticator, RateLimiter
from tests.helpers import make_config

INI = "[managed]\nweb1.example.com\nctl.example.com\n\n[report_only]\n"


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        state, code = root / "state", root / "code"
        for d in (state / "logs", state / "www", code / "www"):
            d.mkdir(parents=True)
        (state / "inventory.ini").write_text(INI)
        (state / "www" / "status.json").write_text('{"hosts": []}')
        (code / "www" / "index.html").write_text("<h1>app</h1>")
        (code / "www" / "login.html").write_text("<form>login</form>")
        (root / "secret.txt").write_text("nope")
        cfg = make_config(paths__state=str(state), paths__code=str(code))
        cls.app = web.App(cfg)
        cls.app.auth = Authenticator(cfg, RateLimiter(5, 900, 900),
                                     check_password=lambda u, p: p == "pw", member=lambda u, g: True)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(cls.app))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, cookie=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": "application/json", "X-Fleet": "1"} if body is not None else {}
        if cookie:
            h["Cookie"] = cookie
        h.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r, data

    def login(self):
        r, _ = self.req("POST", "/api/login", {"username": "alice", "password": "pw"})
        self.assertEqual(r.status, 200)
        return r.getheader("Set-Cookie").split(";")[0]

    def test_login_page_is_public(self):
        r, data = self.req("GET", "/login")
        self.assertEqual((r.status, data), (200, b"<form>login</form>"))

    def test_pages_and_data_redirect_without_session(self):
        for path in ("/", "/data/status.json"):
            r, _ = self.req("GET", path)
            self.assertEqual(r.status, 302)
            self.assertTrue(r.getheader("Location").startswith("/login?next="))

    def test_api_needs_session(self):
        r, data = self.req("GET", "/api/state")
        self.assertEqual(r.status, 401)
        self.assertTrue(json.loads(data)["login"])

    def test_forward_auth_endpoint(self):
        r, _ = self.req("GET", "/api/auth")
        self.assertEqual(r.status, 302)
        r, _ = self.req("GET", "/api/auth", cookie=self.login())
        self.assertEqual(r.status, 204)

    def test_wrong_password(self):
        r, _ = self.req("POST", "/api/login", {"username": "alice", "password": "bad"})
        self.assertEqual(r.status, 401)

    def test_csrf_guards(self):
        r, _ = self.req("POST", "/api/login", {"username": "alice", "password": "pw"}, headers={"X-Fleet": "0"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/api/login", {"username": "alice", "password": "pw"},
                        headers={"Origin": "https://evil.example.org"})
        self.assertEqual(r.status, 403)

    def test_session_serves_app_and_data(self):
        c = self.login()
        self.assertEqual(self.req("GET", "/", cookie=c)[1], b"<h1>app</h1>")
        r, _data = self.req("GET", "/data/status.json", cookie=c)
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "application/json"))

    def test_no_path_traversal(self):
        c = self.login()
        for path in ("/../secret.txt", "/data/../../secret.txt", "/%2e%2e/secret.txt"):
            _r, data = self.req("GET", path, cookie=c)
            self.assertNotEqual(data, b"nope", path)

    def test_logout_ends_session(self):
        c = self.login()
        self.assertEqual(self.req("POST", "/api/logout", {}, cookie=c)[0].status, 200)
        self.assertEqual(self.req("GET", "/api/state", cookie=c)[0].status, 401)

    def test_run_validates_targets(self):
        c = self.login()
        r, _data = self.req("POST", "/api/run", {"action": "patch", "hosts": ["nope.example.com"]}, cookie=c)
        self.assertEqual(r.status, 400)
        r, _ = self.req("POST", "/api/run", {"action": "rm -rf", "hosts": ["web1.example.com"]}, cookie=c)
        self.assertEqual(r.status, 400)
        # the controller is never a target even if it sits in the managed group
        r, _ = self.req("POST", "/api/run", {"action": "patch", "hosts": ["ctl.example.com"]}, cookie=c)
        self.assertEqual(r.status, 400)

    def test_controller_reboot_needs_configuration(self):
        r, _ = self.req("POST", "/api/reboot-controller", {"confirm": "yes"}, cookie=self.login())
        self.assertEqual(r.status, 400)


if __name__ == "__main__":
    unittest.main()
