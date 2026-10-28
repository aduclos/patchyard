import unittest

from fleet import auth
from tests.helpers import make_config


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.limiter = auth.RateLimiter(3, 900, 900, clock=self.clock)
        self.a = auth.Authenticator(make_config(), self.limiter,
                                    check_password=lambda u, p: p == "right",
                                    member=lambda u, g: u == "alice")

    def test_normalize(self):
        for raw in ("CORP\\Alice", "alice@CORP.EXAMPLE.COM", " Alice "):
            self.assertEqual(auth.normalize(raw), "alice")

    def test_success(self):
        self.assertEqual(self.a.authenticate("CORP\\alice", "right"), "alice")

    def test_wrong_password(self):
        with self.assertRaisesRegex(auth.AuthError, "wrong username or password"):
            self.a.authenticate("alice", "wrong")

    def test_not_in_group(self):
        with self.assertRaisesRegex(auth.AuthError, "not in the group"):
            self.a.authenticate("bob", "right")

    def test_lockout_after_failures_then_expiry(self):
        for _ in range(3):
            with self.assertRaises(auth.AuthError):
                self.a.authenticate("alice", "wrong")
        with self.assertRaisesRegex(auth.AuthError, "too many"):
            self.a.authenticate("alice", "right")
        self.clock.t += 901
        self.assertEqual(self.a.authenticate("alice", "right"), "alice")

    def test_method_none_needs_nothing(self):
        a = auth.Authenticator(make_config(auth__method="none"))
        self.assertFalse(a.required)
        self.assertEqual(a.authenticate("", ""), "anonymous")


if __name__ == "__main__":
    unittest.main()
