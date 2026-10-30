import json
import tempfile
import unittest
from pathlib import Path

from patchyard import accounts, auth
from tests.helpers import make_config


class PasswordTest(unittest.TestCase):
    def test_hash_and_check(self):
        h = accounts.hash_password("correct horse battery", iterations=1000)
        self.assertTrue(h.startswith("pbkdf2_sha256$1000$"))
        self.assertTrue(accounts.check_password("correct horse battery", h))
        self.assertFalse(accounts.check_password("wrong", h))
        self.assertFalse(accounts.check_password("x", "garbage"))

    def test_salts_differ(self):
        self.assertNotEqual(accounts.hash_password("same password", iterations=1000),
                            accounts.hash_password("same password", iterations=1000))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        accounts.ITERATIONS = 1000                 # keep tests fast

    def tearDown(self):
        self.tmp.cleanup()

    def test_users_lifecycle(self):
        u = accounts.Users(self.state)
        u.set("alice", "a long password", create=True)
        self.assertTrue(u.verify("alice", "a long password"))
        self.assertFalse(u.verify("alice", "nope"))
        self.assertFalse(u.verify("bob", "a long password"))
        with self.assertRaisesRegex(accounts.AccountError, "at least"):
            u.set("bob", "short", create=True)
        with self.assertRaisesRegex(accounts.AccountError, "already exists"):
            u.set("alice", "another long one", create=True)
        with self.assertRaisesRegex(accounts.AccountError, "last user"):
            u.remove("alice")
        self.assertEqual(oct((self.state / "users.json").stat().st_mode & 0o777), "0o600")
        self.assertNotIn("a long password", (self.state / "users.json").read_text())

    def test_tokens(self):
        t = accounts.Tokens(self.state)
        token = t.create("laptop MCP", "alice")
        self.assertEqual(t.user_for(token), "alice")
        self.assertIsNone(t.user_for(token[:-2] + "xx"))
        self.assertIsNone(t.user_for("not a token"))
        stored = (self.state / "tokens.json").read_text()
        self.assertNotIn(token.split("_", 2)[2], stored)        # only a hash is kept
        tid = t.public()[0]["id"]
        self.assertNotIn("hash", t.public()[0])
        t.revoke(tid)
        self.assertIsNone(t.user_for(token))


class LocalSignInTest(unittest.TestCase):
    def test_local_method(self):
        with tempfile.TemporaryDirectory() as d:
            accounts.ITERATIONS = 1000
            accounts.Users(Path(d)).set("alice", "a long password", create=True)
            cfg = make_config(auth__method="local", paths__state=d)
            a = auth.Authenticator(cfg)
            self.assertEqual(a.authenticate("Alice", "a long password"), "alice")
            with self.assertRaises(auth.AuthError):
                a.authenticate("alice", "wrong password!")
            self.assertTrue(json.loads((Path(d) / "users.json").read_text())["users"])


if __name__ == "__main__":
    unittest.main()
