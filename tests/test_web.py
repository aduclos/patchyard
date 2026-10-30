import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from patchyard import web
from patchyard.auth import Authenticator, RateLimiter
from patchyard.config import Device
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
        cfg.devices = [Device(name="router", driver="opnsense", url="https://192.0.2.1", credentials_file="/x"),
                       Device(name="ap", driver="omada_eap", url="https://192.0.2.2", credentials_file="/x")]
        cls.app = web.App(cfg)
        cls.app.auth = Authenticator(cfg, RateLimiter(5, 900, 900),
                                     check_password=lambda u, p: p == "pw", member=lambda u, g: True)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(cls.app))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, cookie=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": "application/json", "X-Patchyard": "1"} if body is not None else {}
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
        r, _ = self.req("POST", "/api/login", {"username": "alice", "password": "pw"}, headers={"X-Patchyard": "0"})
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

    def test_the_patchyard_server_is_a_machine_like_the_others(self):
        from types import SimpleNamespace
        from unittest import mock
        c = self.login()
        with mock.patch.object(self.app.jobs, "start", return_value=SimpleNamespace(id="j1")) as start:
            r, _ = self.req("POST", "/api/run", {"action": "patch", "hosts": ["ctl.example.com"]}, cookie=c)
            self.assertEqual(r.status, 200)                         # it sits in the managed group: patchable
            self.assertEqual(start.call_args.args[:2], ("patch", ["ctl.example.com"]))
        # but it reboots only on its own, with reboot_command, and never gets a release upgrade
        for body, msg in [({"action": "reboot", "hosts": ["ctl.example.com", "web1.example.com"]}, b"on its own"),
                          ({"action": "reboot", "hosts": ["ctl.example.com"], "confirm": "yes"}, b"not configured"),
                          ({"action": "release-upgrade", "hosts": ["ctl.example.com"]}, b"Patchyard runs on")]:
            r, data = self.req("POST", "/api/run", body, cookie=c)
            self.assertEqual(r.status, 400, body)
            self.assertIn(msg, data)

    def test_enroll_is_validated(self):
        c = self.login()
        r, _ = self.req("POST", "/api/enroll", {"host": "new1.example.com", "os": "solaris"}, cookie=c)
        self.assertEqual(r.status, 400)
        r, _ = self.req("POST", "/api/enroll", {"host": "new1.example.com", "admin": "root"}, cookie=c)
        self.assertEqual(r.status, 400)          # password method without a password
        r, _ = self.req("POST", "/api/enroll", {"host": "web1.example.com", "method": "command"}, cookie=c)
        self.assertEqual(r.status, 409)          # already on the dashboard

    def test_api_token_works_instead_of_a_session(self):
        c = self.login()
        r, data = self.req("POST", "/api/tokens", {"action": "create", "name": "test"}, cookie=c)
        token = json.loads(data)["token"]
        r, data = self.req("GET", "/api/state", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual((r.status, json.loads(data)["user"]), (200, "alice"))
        r, _ = self.req("GET", "/api/state", headers={"Authorization": "Bearer pyd_00000000_" + "x" * 30})
        self.assertEqual(r.status, 401)
        r, data = self.req("GET", "/data/status.json", headers={"Authorization": "Bearer pyd_00000000_" + "x" * 30})
        self.assertEqual((r.status, json.loads(data)["login"]), (401, True))      # JSON, not a redirect

    def test_device_actions_are_validated(self):
        c = self.login()
        r, _ = self.req("POST", "/api/device", {"name": "nope", "action": "update", "confirm": "yes"}, cookie=c)
        self.assertEqual(r.status, 400)
        r, _ = self.req("POST", "/api/device", {"name": "ap", "action": "update", "confirm": "yes"}, cookie=c)
        self.assertEqual(r.status, 400)          # omada_eap cannot update
        r, _ = self.req("POST", "/api/device", {"name": "router", "action": "reboot"}, cookie=c)
        self.assertEqual(r.status, 400)          # no confirmation

    def test_controller_reboot_needs_configuration(self):
        r, _ = self.req("POST", "/api/reboot-controller", {"confirm": "yes"}, cookie=self.login())
        self.assertEqual(r.status, 400)


if __name__ == "__main__":
    unittest.main()


class ServerPatchJobTest(unittest.TestCase):
    def test_others_first_then_the_server_with_its_own_command(self):
        import io
        from unittest import mock

        from patchyard import jobs
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(paths__state=tmp)
            cfg.controller.patch_command = ["true"]
            jm = jobs.JobManager(cfg)
            calls = []
            def pb(log, name, hosts, extra):
                calls.append(("pb", hosts))
                return 0
            with mock.patch.object(jm, "playbook", side_effect=pb), \
                 mock.patch.object(jm, "patch_controller", side_effect=lambda log: calls.append(("own",)) or 0), \
                 mock.patch.object(jm, "check", return_value=0):
                rc = jm.action("patch", ["ctl.example.com", "web1.example.com"], {})(io.StringIO())
                self.assertEqual((rc, calls), (0, [("pb", ["web1.example.com"]), ("own",)]))
                calls.clear()
                jm.action("patch", ["ctl.example.com"], {})(io.StringIO())
                self.assertEqual(calls, [("own",)])                      # no playbook for it at all
                calls.clear()
                cfg.controller.patch_command = []
                jm.action("patch", ["ctl.example.com"], {})(io.StringIO())
                self.assertEqual(calls, [("pb", ["ctl.example.com"])])   # without one: the playbook, like the rest
