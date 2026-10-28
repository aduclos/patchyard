import json
import tempfile
import unittest
from pathlib import Path

from fleet import config

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
        p = self.dir / "fleet.toml"
        p.write_text(text + f'\n[paths]\nstate = "{self.dir}"\n')
        return p

    def test_defaults_fill_everything_else(self):
        cfg = config.load(self.write(MINIMAL))
        self.assertEqual(cfg.service_account.name, "svc_patch")
        self.assertEqual(cfg.checks.interval_minutes, 15)
        self.assertEqual(cfg.notify.backends, [])

    def test_example_file_is_valid(self):
        example = Path(__file__).parent.parent / "config" / "fleet.example.toml"
        text = example.read_text().replace('state = "/var/lib/fleet"', f'state = "{self.dir}"')
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
            config.load(self.write('[site]\nurl = "https://x"\n'))

    def test_ntfy_backend_needs_url_and_topic(self):
        with self.assertRaisesRegex(config.ConfigError, "ntfy"):
            config.load(self.write(MINIMAL + '\n[notify]\nbackends = ["ntfy"]\n'))

    def test_ui_overrides_take_precedence_and_are_limited(self):
        p = self.write(MINIMAL)
        cfg = config.load(p)
        cfg = config.save_overrides(cfg, {"checks.interval_minutes": 30}, path=p)
        self.assertEqual(config.load(p).checks.interval_minutes, 30)
        with self.assertRaisesRegex(config.ConfigError, "not editable"):
            config.save_overrides(cfg, {"auth.method": "none"}, path=p)

    def test_invalid_override_is_rejected_and_not_saved(self):
        p = self.write(MINIMAL)
        cfg = config.load(p)
        with self.assertRaises(config.ConfigError):
            config.save_overrides(cfg, {"checks.interval_minutes": 1}, path=p)
        self.assertFalse((self.dir / "settings.json").exists())

    def test_stray_keys_in_settings_file_are_ignored(self):
        p = self.write(MINIMAL)
        (self.dir / "settings.json").write_text(json.dumps({"auth.method": "none"}))
        self.assertEqual(config.load(p).auth.method, "kerberos")


if __name__ == "__main__":
    unittest.main()
