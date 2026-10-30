import json
import tempfile
import unittest
from pathlib import Path

from patchyard import config

MINIMAL = """
[site]
url = "https://patch.example.com"
[auth]
realm = "CORP.EXAMPLE.COM"
"""


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text: str) -> Path:
        p = self.dir / "patchyard.toml"
        p.write_text(text + f'\n[paths]\nstate = "{self.dir}"\n')
        return p

    def test_defaults_fill_everything_else(self):
        cfg = config.load(self.write(MINIMAL))
        self.assertEqual(cfg.service_account.name, "patchyard")
        self.assertEqual(cfg.checks.interval_minutes, 15)
        self.assertEqual(cfg.notify.backends, [])

    def test_example_file_is_valid(self):
        example = Path(__file__).parent.parent / "config" / "patchyard.example.toml"
        text = example.read_text()
        self.assertIn('state = "/var/lib/patchyard"', text)
        text = text.replace('state = "/var/lib/patchyard"', f'state = "{self.dir}"')
        p = self.dir / "example.toml"
        p.write_text(text)
        config.load(p)

    def test_unknown_key_is_an_error(self):
        with self.assertRaisesRegex(config.ConfigError, "unknown key"):
            config.load(self.write(MINIMAL + "\n[checks]\nintervall_minutes = 5\n"))

    def test_wrong_type_is_an_error(self):
        with self.assertRaisesRegex(config.ConfigError, "expected int"):
            config.load(self.write(MINIMAL + '\n[checks]\ninterval_minutes = "15"\n'))

    def test_bool_is_not_an_int(self):
        with self.assertRaisesRegex(config.ConfigError, "expected int"):
            config.load(self.write(MINIMAL + "\n[web]\nport = true\n"))

    def test_kerberos_needs_a_realm(self):
        with self.assertRaisesRegex(config.ConfigError, "realm"):
            config.load(self.write('[site]\nurl = "https://x"\n[auth]\nmethod = "kerberos"\n'))

    def test_ntfy_backend_needs_url_and_topic(self):
        with self.assertRaisesRegex(config.ConfigError, "ntfy"):
            config.load(self.write(MINIMAL + '\n[notify]\nbackends = ["ntfy"]\n'))

    def test_ui_overrides_take_precedence_and_are_limited(self):
        p = self.write(MINIMAL)
        cfg = config.load(p)
        cfg, diff = config.save_overrides(cfg, {"checks.interval_minutes": "30"}, path=p)   # form values coerce
        self.assertEqual(config.load(p).checks.interval_minutes, 30)
        self.assertEqual(diff, {"checks.interval_minutes": (15, 30)})
        _, diff = config.save_overrides(cfg, {"checks.interval_minutes": 30}, path=p)
        self.assertEqual(diff, {})                                   # unchanged values are not reported
        with self.assertRaisesRegex(config.ConfigError, "not editable"):
            config.save_overrides(cfg, {"auth.method": "none"}, path=p)
        with self.assertRaisesRegex(config.ConfigError, "5 to 1440"):
            config.save_overrides(cfg, {"checks.interval_minutes": 4000}, path=p)
        with self.assertRaisesRegex(config.ConfigError, "unknown"):
            config.save_overrides(cfg, {"notify.backends": ["ntfy", "pager"]}, path=p)

    def test_locked_settings_stay_file_only(self):
        p = self.write(MINIMAL.replace("[site]", '[web]\nsettings_locked = ["checks"]\n[site]'))
        cfg = config.load(p)
        self.assertTrue(config.locked(cfg, "checks.interval_minutes"))
        self.assertFalse(config.locked(cfg, "site.name"))
        with self.assertRaisesRegex(config.ConfigError, "configuration file"):
            config.save_overrides(cfg, {"checks.interval_minutes": 30}, path=p)
        (self.dir / "settings.json").write_text(json.dumps({"checks.interval_minutes": 30, "site.name": "X"}))
        cfg = config.load(p)                                         # a stale saved value does not win
        self.assertEqual((cfg.checks.interval_minutes, cfg.site.name), (15, "X"))

    def test_invalid_override_is_rejected_and_not_saved(self):
        p = self.write(MINIMAL)
        cfg = config.load(p)
        with self.assertRaises(config.ConfigError):
            config.save_overrides(cfg, {"checks.interval_minutes": 1}, path=p)
        self.assertFalse((self.dir / "settings.json").exists())

    def test_devices_list(self):
        dev = ('\n[[devices]]\nname = "router"\ndriver = "opnsense"\nurl = "https://192.0.2.1"\n'
               'credentials_file = "/x/router.json"\n')
        cfg = config.load(self.write(MINIMAL + dev))
        self.assertEqual((cfg.devices[0].name, cfg.devices[0].verify_tls), ("router", True))
        with self.assertRaisesRegex(config.ConfigError, "driver must be"):
            config.load(self.write(MINIMAL + dev.replace("opnsense", "cisco")))
        with self.assertRaisesRegex(config.ConfigError, "unique"):
            config.load(self.write(MINIMAL + dev + dev))
        with self.assertRaisesRegex(config.ConfigError, "unknown key"):
            config.load(self.write(MINIMAL + dev + 'colour = "red"\n'))

    def test_device_options_and_exec_without_credentials(self):
        dev = ('\n[[devices]]\nname = "x"\ndriver = "exec"\nurl = ""\n'
               '[devices.options]\nstatus_command = "true"\n')
        cfg = config.load(self.write(MINIMAL + dev))
        self.assertEqual(cfg.devices[0].options, {"status_command": "true"})
        bad = dev.replace('status_command = "true"', "status_command = 5")
        with self.assertRaisesRegex(config.ConfigError, "table of str"):
            config.load(self.write(MINIMAL + bad))
        nocreds = '\n[[devices]]\nname = "r"\ndriver = "mikrotik"\nurl = "https://192.0.2.1"\n'
        with self.assertRaisesRegex(config.ConfigError, "credentials_file is required"):
            config.load(self.write(MINIMAL + nocreds))

    def test_config_path_env_and_defaults(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {"PATCHYARD_CONFIG": "/new.toml"}, clear=True):
            self.assertEqual(config.config_path(), "/new.toml")
        no_etc = mock.patch.object(config, "DEFAULT_CONFIG", "/nonexistent.toml")
        with mock.patch.dict("os.environ", {}, clear=True), no_etc:
            self.assertEqual(config.config_path(), "/var/lib/patchyard/patchyard.toml")     # the wizard's location
        with mock.patch.dict("os.environ", {"PATCHYARD_STATE": "/srv/state"}, clear=True), no_etc:
            self.assertEqual(config.config_path(), "/srv/state/patchyard.toml")
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(config, "DEFAULT_CONFIG", __file__):
            self.assertEqual(config.config_path(), __file__)                                # /etc wins if present

    def test_stray_keys_in_settings_file_are_ignored(self):
        p = self.write(MINIMAL)
        (self.dir / "settings.json").write_text(json.dumps({"auth.method": "none"}))
        self.assertEqual(config.load(p).auth.method, "local")


if __name__ == "__main__":
    unittest.main()
