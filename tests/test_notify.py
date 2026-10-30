import unittest
from unittest import mock

from patchyard import notify
from tests.helpers import make_config


class NotifyTest(unittest.TestCase):
    def test_every_backend_gets_it_and_failures_are_isolated(self):
        cfg = make_config(notify__backends=["ntfy", "webhook"])
        calls = []
        def broken(c, m):
            raise OSError("down")

        backends = {"ntfy": broken, "webhook": lambda c, m: calls.append(m.title)}
        with mock.patch.dict(notify.BACKENDS, backends), self.assertLogs("patchyard.notify", "WARNING"):
            failed = notify.send(cfg, notify.Message("t", "b"))
        self.assertEqual(failed, ["ntfy"])
        self.assertEqual(calls, ["t"])

    def test_no_backends_is_fine(self):
        self.assertEqual(notify.send(make_config(), notify.Message("t", "b")), [])

    def test_bad_priority(self):
        with self.assertRaises(ValueError):
            notify.send(make_config(), notify.Message("t", "b", priority="urgent"))


if __name__ == "__main__":
    unittest.main()
