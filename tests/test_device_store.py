import json
import stat
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from patchyard import config
from patchyard.devices import DeviceAuthError, DeviceStatus, Driver, FormField, store

TOML = """
[site]
url = "https://patch.example.com"
[paths]
state = "{state}"
[auth]
method = "none"
[[devices]]
name = "fw"
driver = "exec"
options = {{ status_command = "true" }}
"""


class Fake(Driver):
    title = "Fake"
    fields = (FormField("key", "Key"), FormField("secret", "Secret", secret=True),
              FormField("site", "Site", option=True, required=False, default="default"))

    def status(self, check=False):
        if self.creds["secret"] != "right":
            raise DeviceAuthError("wrong username or password")
        return DeviceStatus(model="FK-1", version="1.0", latest="1.1", update_available=True)


class DeviceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.conf = self.state / "p.toml"
        self.conf.write_text(TOML.format(state=self.state))
        classes = {**store.driver_classes(), "fake": Fake}
        for p in (mock.patch("patchyard.devices.store.driver_classes", return_value=classes),
                  mock.patch("patchyard.devices.driver_classes", return_value=classes),
                  mock.patch.object(config, "DEVICE_DRIVERS", config.DEVICE_DRIVERS | {"fake"})):
            p.start()
            self.addCleanup(p.stop)
        self.cfg = config.load(self.conf)

    def tearDown(self):
        self.tmp.cleanup()

    FORM: ClassVar[dict] = {"name": "core-sw", "driver": "fake", "url": "https://192.0.2.9/",
                            "disruption": "the office goes dark"}

    def test_add_edit_rename_remove(self):
        changes = store.save(self.cfg, self.FORM, {"key": "k1", "secret": "s1"})
        self.assertEqual(changes["login"], "(changed)")
        entry = store.read(self.cfg)[0]
        self.assertEqual((entry["url"], entry["options"]), ("https://192.0.2.9", {"site": "default"}))
        creds = store.creds_path(self.cfg, "core-sw")
        self.assertEqual(json.loads(creds.read_text()), {"key": "k1", "secret": "s1"})
        self.assertEqual(stat.S_IMODE(creds.stat().st_mode), 0o600)
        cfg = config.load(self.conf)                              # the configuration includes it
        self.assertEqual([d.name for d in cfg.devices], ["fw", "core-sw"])
        view = {d["name"]: d for d in store.view(cfg)["devices"]}
        self.assertEqual(view["core-sw"]["credentials"], {"key": "k1", "secret": True})   # never the secret
        self.assertEqual((view["core-sw"]["source"], view["fw"]["source"]), ("dashboard", "file"))
        self.assertNotIn("exec", store.view(cfg)["drivers"])
        # editing with an empty secret keeps it; renaming moves the credentials
        store.save(cfg, {**self.FORM, "name": "core"}, {"key": "k2", "secret": ""}, original="core-sw")
        self.assertFalse(creds.exists())
        self.assertEqual(json.loads(store.creds_path(cfg, "core").read_text()), {"key": "k2", "secret": "s1"})
        self.assertEqual([d["name"] for d in store.read(cfg)], ["core"])
        store.remove(cfg, "core")
        self.assertEqual(store.read(cfg), [])
        self.assertFalse(store.creds_path(cfg, "core").exists())
        with self.assertRaises(store.StoreError):
            store.remove(cfg, "fw")                               # configuration-file devices stay

    def test_form_checks(self):
        ok = {"key": "k", "secret": "s"}
        for form, creds, msg in [({**self.FORM, "driver": "exec"}, ok, "configuration file only"),
                                 ({**self.FORM, "driver": "nope"}, ok, "pick a device type"),
                                 ({**self.FORM, "name": "Bad Name"}, ok, "Name"),
                                 ({**self.FORM, "name": "fw"}, ok, "configuration file"),
                                 ({**self.FORM, "url": "192.0.2.9"}, ok, "Address"),
                                 (self.FORM, {"key": "k"}, "Secret is required")]:
            with self.assertRaisesRegex(store.StoreError, msg, msg=str(form)):
                store.check(self.cfg, form, creds)
        store.save(self.cfg, self.FORM, ok)
        with self.assertRaisesRegex(store.StoreError, "already a device"):
            store.save(self.cfg, self.FORM, ok)

    def test_connection_test_saves_nothing(self):
        self.assertEqual(store.test(self.cfg, self.FORM, {"key": "k", "secret": "wrong"}),
                         {"ok": False, "error": "wrong username or password"})
        r = store.test(self.cfg, self.FORM, {"key": "k", "secret": "right"})
        self.assertEqual((r["ok"], r["model"], r["update_available"]), (True, "FK-1", True))
        self.assertEqual(store.read(self.cfg), [])
        store.save(self.cfg, self.FORM, {"key": "k", "secret": "right"})
        r = store.test(self.cfg, self.FORM, {"key": "k", "secret": ""}, original="core-sw")   # stored secret
        self.assertTrue(r["ok"])

    def test_a_broken_devices_file_does_not_stop_startup(self):
        self.cfg.devices_file.write_text(json.dumps([{"name": "x", "driver": "fake", "bogus": 1}, "junk",
                                                     {"name": "fw", "driver": "fake", "url": "https://192.0.2.1",
                                                      "credentials_file": "/x"}]))
        with self.assertLogs("patchyard.config", "WARNING"):
            cfg = config.load(self.conf)
        self.assertEqual([(d.name, d.driver) for d in cfg.devices], [("fw", "exec")])   # the file wins a clash
        self.cfg.devices_file.write_text("{not json")
        with self.assertLogs("patchyard.config", "WARNING"):
            self.assertEqual(len(config.load(self.conf).devices), 1)


if __name__ == "__main__":
    unittest.main()
