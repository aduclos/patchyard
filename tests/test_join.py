import http.client
import io
import json
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from patchyard import enroll, join, web
from patchyard.auth import Authenticator, RateLimiter
from tests.helpers import make_config

ROOT = Path(__file__).resolve().parent.parent
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBody controller\n"


def setup_dirs(root: Path):
    state, code = root / "state", root / "code"
    (state / "logs").mkdir(parents=True)
    (state / "www").mkdir()
    (state / "www" / "status.json").write_text('{"hosts": []}')
    (state / "inventory.ini").write_text("[managed]\nweb1.example.com\n\n[report_only]\n")
    shutil.copytree(ROOT / "bootstrap", code / "bootstrap")
    (code / "www").mkdir(parents=True)
    (root / "key.pub").write_text(KEY)
    return make_config(paths__state=str(state), paths__code=str(code), service_account__key=str(root / "key"),
                       service_account__allowed_sources=["192.0.2.5"], site__domain=".example.com")


class JoinTokensTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tokens = join.JoinTokens(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_lifecycle(self):
        token, _rec = self.tokens.create("lab batch", 1, 2, "alice")
        self.assertRegex(token, join.TOKEN_RE)
        raw = self.tokens.path.read_text()
        self.assertNotIn(token.split("_")[2], raw)                      # only the hash is kept
        self.assertEqual(stat.S_IMODE(self.tokens.path.stat().st_mode), 0o600)
        self.assertIsNotNone(self.tokens.check(token))
        self.tokens.use(token, "a.example.com", "192.0.2.7")
        self.tokens.use(token, "b.example.com", "192.0.2.8")
        self.assertIsNone(self.tokens.check(token))                     # two machines: used up
        with self.assertRaises(join.JoinError):
            self.tokens.use(token, "c.example.com", "192.0.2.9")
        pub = self.tokens.public()[0]
        self.assertEqual((pub["used"], pub["left"], pub["active"]), (["a.example.com", "b.example.com"], 0, False))
        self.assertNotIn("hash", pub)

    def test_expiry_revocation_and_forgery(self):
        token, rec = self.tokens.create("", 24, 0, "alice")
        self.assertTrue(rec["name"].startswith("join "))
        self.assertIsNone(self.tokens.check(token[:-1] + ("A" if token[-1] != "A" else "B")))   # wrong secret
        self.assertIsNone(self.tokens.check("pyj_zzzz_nope"))
        with mock.patch("time.time", return_value=time.time() + 25 * 3600):
            self.assertIsNone(self.tokens.check(token))                 # expired
        self.tokens.revoke(rec["id"])
        self.assertIsNone(self.tokens.check(token))
        for hours, uses in [(5, 1), (1, -1), (1, 501)]:
            with self.assertRaises(join.JoinError):
                self.tokens.create("x", hours, uses, "alice")


class JoinScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = setup_dirs(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_linux_script(self):
        token, rec = join.JoinTokens(self.cfg.state).create("x", 1, 1, "alice")
        script = join.linux_script(self.cfg, token, rec)
        self.assertEqual(subprocess.run(["sh", "-n"], input=script, text=True).returncode, 0)
        for part in ("PATCHYARD_ACCOUNT=patchyard", "PATCHYARD_ALLOWED_SOURCES=192.0.2.5", "ExampleKeyBody",
                     f"https://patch.example.com/join/{token}/done", '"os\\": \\"linux\\"'):
            self.assertIn(part, script)
        cmds = join.commands(self.cfg, token)
        self.assertEqual(cmds["linux"], f"curl -fsSL https://patch.example.com/join/{token}/linux | sudo sh")
        self.assertIn(f"irm https://patch.example.com/join/{token}/windows | iex", cmds["windows"])
        self.assertIn("Invoke-WebRequest", join.windows_script(self.cfg, token, rec))
        self.assertIn("exit 1", join.refusal_script("linux"))


class JoinWebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = setup_dirs(Path(self.tmp.name))
        self.app = web.App(self.cfg)
        self.app.auth = Authenticator(self.cfg, RateLimiter(5, 900, 900), check_password=lambda u, p: p == "pw",
                                      member=lambda u, g: True)
        self.started = []
        self.app.jobs.start = lambda action, hosts, user, work: self.started.append((action, hosts, user)) or \
            SimpleNamespace(id="job-1")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        h = {**({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})}
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        r = conn.getresponse()
        data = r.read().decode()
        conn.close()
        return r.status, data

    def test_join_flow_needs_no_session(self):
        token, _ = join.JoinTokens(self.cfg.state).create("batch", 1, 1, "alice")
        code, script = self.req("GET", f"/join/{token}/linux")
        self.assertEqual(code, 200)
        self.assertIn("PATCHYARD_PUBKEY", script)
        self.assertEqual(self.req("POST", f"/join/{token}/done", {"host": "localhost", "os": "linux"})[0], 400)
        self.assertEqual(self.req("POST", f"/join/{token}/done", {"host": "web1", "os": "linux"})[0], 409)
        code, msg = self.req("POST", f"/join/{token}/done", {"host": "web7", "os": "linux"})
        self.assertEqual(code, 200)
        self.assertIn("adding web7", msg)
        self.assertEqual(self.started, [("enroll", ["web7.example.com"], "join:batch")])
        self.assertEqual(join.JoinTokens(self.cfg.state).public()[0]["jobs"], {"web7.example.com": "job-1"})
        self.assertEqual(self.req("POST", f"/join/{token}/done", {"host": "web8", "os": "linux"})[0], 403)   # used up
        code, script = self.req("GET", f"/join/{token}/linux")
        self.assertIn("expired, been used up", script)
        log = (self.cfg.state / "logs" / "audit.log").read_text()
        self.assertIn("web7 joined", log)
        self.assertNotIn(token.split("_")[2], log)

    def test_a_use_is_given_back_when_adding_cannot_start(self):
        token, _ = join.JoinTokens(self.cfg.state).create("one", 1, 1, "alice")

        def busy(*a):
            raise web.JobError("a job is already running on web7")
        self.app.jobs.start = busy
        code, msg = self.req("POST", f"/join/{token}/done", {"host": "web7", "os": "linux"})
        self.assertEqual(code, 409)
        self.assertIn("run the same line again", msg)
        pub = join.JoinTokens(self.cfg.state).public()[0]
        self.assertEqual((pub["used"], pub["active"]), ([], True))      # still good for that one machine

    def test_bad_tokens_are_rate_limited(self):
        for _ in range(20):
            self.req("GET", "/join/pyj_00000000_bogusbogusbogusbogus/linux")
        token, _ = join.JoinTokens(self.cfg.state).create("batch", 1, 1, "alice")
        self.assertEqual(self.req("POST", f"/join/{token}/done", {"host": "web7", "os": "linux"})[0], 429)

    def test_add_one_machine_test_first_and_join_api(self):
        cookie = {"Cookie": f"patchyard_session={self.app.sessions.create('alice')}", "X-Patchyard": "1"}
        steps = [{"step": "Name", "ok": True, "detail": "web7.example.com is 192.0.2.7"},
                 {"step": "SSH", "ok": True, "detail": "the SSH server answers"}]
        with mock.patch.object(enroll, "check_access", return_value=steps) as check:
            code, data = self.req("POST", "/api/enroll-test", {"host": "web7", "os": "macos", "method": "password",
                                                               "admin": "bob", "password": "pw"}, cookie)
        self.assertEqual((code, json.loads(data)["ok"]), (200, True))
        self.assertEqual(check.call_args.args[2:6], ("web7.example.com", "macos", "password", "bob"))
        code, data = self.req("POST", "/api/enroll", {"host": "web7", "os": "macos", "method": "command"}, cookie)
        self.assertEqual((code, json.loads(data)["host"]), (200, "web7.example.com"))
        self.assertEqual(self.started[-1], ("enroll", ["web7.example.com"], "alice"))
        # one machine at a time: a list is not taken
        two = {"hosts": [{"host": "a"}, {"host": "b"}], "method": "command"}
        self.assertEqual(self.req("POST", "/api/enroll", two, cookie)[0], 400)
        self.assertEqual(self.req("POST", "/api/enroll", {"host": "web1", "method": "command"}, cookie)[0], 409)
        code, data = self.req("POST", "/api/join", {"action": "create", "name": "lab", "hours": 24, "uses": 1}, cookie)
        out = json.loads(data)
        self.assertEqual((code, out["max_uses"]), (200, 1))
        self.assertIn("/join/pyj_", out["commands"]["linux"])
        code, data = self.req("GET", "/api/join", None, cookie)
        tid = json.loads(data)["tokens"][0]["id"]
        self.assertEqual(self.req("POST", "/api/join", {"action": "revoke", "id": tid}, cookie)[0], 200)


class EnrollManyTest(unittest.TestCase):
    def test_a_refused_password_stops_the_batch(self):
        cfg = make_config()
        jobs = mock.Mock()
        jobs.playbook.return_value = 0
        outcomes = iter(["added", "refused"])
        with mock.patch.object(enroll, "_add_one", side_effect=lambda *a, **k: next(outcomes)) as add:
            log = io.StringIO()
            rc = enroll.enroll_many(cfg, jobs, None, log, [("a.example.com", "linux"), ("b.example.com", "linux"),
                                                           ("c.example.com", "linux")], method="password")
        self.assertEqual((rc, add.call_count), (1, 2))                   # c was never tried
        jobs.playbook.assert_called_once_with(log, "auto-updates.yml", ["a.example.com"], [])
        self.assertIn("not tried on c.example.com", log.getvalue())
        self.assertIn("added: a | not added: b, c", log.getvalue())


if __name__ == "__main__":
    unittest.main()
