import http.client
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from patchyard import config, web

TOML = """
[site]
url = "http://127.0.0.1:1"
[paths]
state = "{state}"
[auth]
method = "none"
[web]
settings_locked = ["checks.digest_hour"]
"""


class SettingsWebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state"
        (self.state / "logs").mkdir(parents=True)
        (self.state / "inventory.ini").write_text("[managed]\nweb1.example.com\nwin1.example.com\n\n[report_only]\n")
        self.conf = root / "patchyard.toml"
        self.conf.write_text(TOML.format(state=self.state))
        env = mock.patch.dict(os.environ, {"PATCHYARD_CONFIG": str(self.conf)})
        env.start()
        self.addCleanup(env.stop)
        self.app = web.App(config.load())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        h = {"Content-Type": "application/json", "X-Patchyard": "1"} if body is not None else {}
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        r = conn.getresponse()
        data = json.loads(r.read() or b"{}")
        conn.close()
        return r.status, data

    def field(self, key):
        _, view = self.req("GET", "/api/settings")
        return next(f for f in view["fields"] if f["key"] == key)

    def test_save_applies_at_once_and_is_audited(self):
        code, out = self.req("POST", "/api/settings", {"checks.interval_minutes": "30", "auth.max_failures": 3})
        self.assertEqual((code, out["changed"]), (200, ["auth.max_failures", "checks.interval_minutes"]))
        self.assertEqual(self.app.cfg.checks.interval_minutes, 30)            # no restart
        self.assertEqual(self.app.auth.limiter.max_failures, 3)
        self.assertEqual(config.load().checks.interval_minutes, 30)          # and persisted
        _, log = self.req("GET", "/api/audit")
        self.assertEqual(log["entries"][0]["changes"]["checks.interval_minutes"], [15, 30])
        self.assertEqual(log["entries"][0]["user"], "anonymous")

    def test_secrets_are_write_only(self):
        code, _ = self.req("POST", "/api/settings", {
            "notify.backends": ["smtp"], "notify.smtp.host": "mail.example.com",
            "notify.smtp.recipients": "ops@example.com\nme@example.com", "notify.smtp.password_file": "s3cret-pw"})
        self.assertEqual(code, 200)
        f = self.state / "secrets" / "notify-smtp-password"
        self.assertEqual(f.read_text(), "s3cret-pw\n")
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual(self.app.cfg.notify.smtp.recipients, ["ops@example.com", "me@example.com"])
        self.assertIs(self.field("notify.smtp.password_file")["value"], True)
        for p in (self.state / "settings.json", self.state / "logs" / "audit.log"):
            self.assertNotIn("s3cret", p.read_text())
        self.req("POST", "/api/settings", {"notify.smtp.password_file": ""})   # empty: unchanged
        self.assertTrue(f.exists())
        self.req("POST", "/api/settings", {"notify.smtp.password_file": None})  # null: removed
        self.assertFalse(f.exists())
        self.assertIs(self.field("notify.smtp.password_file")["value"], False)

    def test_invalid_change_saves_nothing(self):
        code, out = self.req("POST", "/api/settings", {"notify.backends": ["ntfy"], "notify.ntfy.token_file": "tok"})
        self.assertEqual(code, 409)
        self.assertIn("ntfy", out["error"])
        self.assertFalse((self.state / "settings.json").exists())
        self.assertEqual(list((self.state / "secrets").glob("*")), [])       # the staged secret is gone

    def test_readonly_locked_and_unknown(self):
        self.assertEqual(self.req("POST", "/api/settings", {"site.url": "http://x"})[0], 409)
        code, out = self.req("POST", "/api/settings", {"checks.digest_hour": 7})
        self.assertEqual(code, 409)
        self.assertIn("configuration file", out["error"])
        self.assertTrue(self.field("checks.digest_hour")["locked"])
        self.assertEqual(self.req("POST", "/api/settings", {"nope": 1})[0], 400)

    def test_test_alert(self):
        self.assertEqual(self.req("POST", "/api/notify-test", {})[0], 400)    # nowhere to send yet
        got = []

        class Hook(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass
        hook = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=hook.serve_forever, daemon=True).start()
        self.addCleanup(hook.server_close)
        self.addCleanup(hook.shutdown)
        url = f"http://127.0.0.1:{hook.server_address[1]}/hook"
        self.req("POST", "/api/settings", {"notify.backends": ["webhook"], "notify.webhook.url": url})
        code, out = self.req("POST", "/api/notify-test", {})
        self.assertEqual((code, out["results"]), (200, {"webhook": ""}))
        self.assertEqual(got[0]["title"], "Patchyard: test alert")

    def test_machine_settings(self):
        code, out = self.req("POST", "/api/machine-settings",
                             {"host": None, "values": {"patchyard_update_time": "04:00"}})
        self.assertEqual((code, out["changed"]), (200, ["patchyard_update_time"]))
        self.assertEqual(out["apply_policy_to"], ["web1.example.com", "win1.example.com"])   # runs the policy again
        code, out = self.req("POST", "/api/machine-settings",
                             {"host": "web1.example.com", "values": {"patchyard_manual_only": {"reason": "db"}}})
        self.assertEqual((code, out["apply_policy_to"]), (200, []))                          # takes effect as is
        code, view = self.req("GET", "/api/machine-settings?host=web1.example.com")
        self.assertEqual(view["values"], {"patchyard_manual_only": {"reason": "db"}})
        self.assertEqual(view["defaults"], {"patchyard_update_time": "04:00"})
        self.assertEqual(self.req("GET", "/api/machine-settings?host=ghost.example.com")[0], 404)
        self.assertEqual(self.req("POST", "/api/machine-settings",
                                  {"host": "web1.example.com", "values": {"patchyard_update_time": "25:00"}})[0], 400)
        self.assertEqual(self.req("POST", "/api/machine-settings",
                                  {"host": "ghost.example.com", "values": {"patchyard_update_time": "01:00"}})[0], 400)
        _, log = self.req("GET", "/api/audit")
        self.assertEqual([e["what"] for e in log["entries"][:2]], ["settings of web1", "machine defaults"])
        self.assertTrue(any(j.action == "refresh-snooze" for j in self.app.jobs.jobs.values()))

    def test_devices(self):
        form = {"name": "nas", "driver": "truenas", "url": "http://127.0.0.1:1", "disruption": "shares go away"}
        code, out = self.req("POST", "/api/devices",
                             {"action": "test", "device": form, "credentials": {"api_key": "k"}})
        self.assertEqual((code, out["ok"]), (200, False))
        self.assertIn("cannot reach", out["error"])
        self.assertEqual(self.req("POST", "/api/devices", {"action": "save", "device": {**form, "driver": "exec"},
                                                           "credentials": {}})[0], 400)
        code, out = self.req("POST", "/api/devices", {"action": "save", "device": form,
                                                      "credentials": {"api_key": "s3cret-key"}})
        self.assertEqual(code, 200)
        self.assertEqual([d.name for d in self.app.cfg.devices], ["nas"])      # live, no restart
        _, view = self.req("GET", "/api/devices")
        self.assertEqual(view["devices"][0]["credentials"], {"api_key": True})
        self.assertNotIn("s3cret", json.dumps(view))
        self.assertNotIn("s3cret", (self.state / "logs" / "audit.log").read_text())
        self.assertEqual(self.req("POST", "/api/devices", {"action": "remove", "name": "nas"})[0], 200)
        self.assertEqual(self.app.cfg.devices, [])
        _, log = self.req("GET", "/api/audit")
        self.assertEqual([e["what"] for e in log["entries"][:2]], ["device nas", "device nas"])


if __name__ == "__main__":
    unittest.main()
