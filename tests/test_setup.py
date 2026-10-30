import tempfile
import unittest
from pathlib import Path

from patchyard import accounts, config, setup


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        accounts.ITERATIONS = 1000

    def tearDown(self):
        self.tmp.cleanup()

    def answers(self, **kw):
        a = {"site_name": "My Fleet", "site_url": "https://patch.example.com/", "domain": "corp.example.com",
             "auth_method": "local", "admin_user": "Alice", "admin_password": "a long password",
             "notify": "ntfy", "ntfy_url": "https://ntfy.sh", "ntfy_topic": "my-fleet",
             "bind": "0.0.0.0", "port": "8790"}
        a.update(kw)
        return a

    def test_setup_code_is_created_once_and_private(self):
        code = setup.setup_token(self.state)
        self.assertEqual(setup.setup_token(self.state), code)
        self.assertEqual(oct((self.state / "setup-token").stat().st_mode & 0o777), "0o600")
        self.assertTrue(setup.code_ok(self.state, code.upper()))
        self.assertFalse(setup.code_ok(self.state, "nope"))

    def test_finish_writes_a_config_that_loads(self):
        setup.setup_token(self.state)
        path = self.state / "patchyard.toml"
        setup.finish(self.answers(), path, self.state)
        cfg = config.load(path)
        self.assertEqual((cfg.site.name, cfg.site.url, cfg.site.domain), ("My Fleet", "https://patch.example.com",
                                                                           ".corp.example.com"))
        self.assertEqual((cfg.auth.method, cfg.notify.backends, cfg.notify.ntfy.topic), ("local", ["ntfy"], "my-fleet"))
        self.assertTrue(accounts.Users(self.state).verify("alice", "a long password"))
        self.assertFalse((self.state / "setup-token").exists())
        with self.assertRaisesRegex(config.ConfigError, "already exists"):
            setup.finish(self.answers(), path, self.state)

    def test_domain_sign_in_needs_realm_and_group(self):
        with self.assertRaisesRegex(config.ConfigError, "realm"):
            setup.build(self.answers(auth_method="kerberos", group="admins"), self.state)
        with self.assertRaisesRegex(config.ConfigError, "group"):
            setup.build(self.answers(auth_method="kerberos", realm="corp.example.com"), self.state)
        cfg = setup.build(self.answers(auth_method="kerberos", realm="corp.example.com", group="admins"), self.state)
        self.assertEqual(cfg.auth.realm, "CORP.EXAMPLE.COM")

    def test_bad_answers_are_refused(self):
        with self.assertRaisesRegex(config.ConfigError, "site.url"):
            setup.build(self.answers(site_url="patch"), self.state)
        with self.assertRaisesRegex(config.ConfigError, "first admin"):
            setup.build(self.answers(admin_password=""), self.state)
        with self.assertRaisesRegex(config.ConfigError, "ntfy"):
            setup.build(self.answers(ntfy_topic=""), self.state)

    def test_toml_round_trip_with_devices_and_quotes(self):
        cfg = setup.build(self.answers(site_name='Quote "this" \\ please'), self.state)
        cfg.devices = [config.Device(name="r", driver="exec", url="", options={"status_command": 'echo "{}"'})]
        path = self.state / "x.toml"
        path.write_text(setup.toml_dumps(cfg))
        again = config.load(path)
        self.assertEqual(again.site.name, 'Quote "this" \\ please')
        self.assertEqual(again.devices[0].options["status_command"], 'echo "{}"')


if __name__ == "__main__":
    unittest.main()
