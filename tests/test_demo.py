import json
import tempfile
import unittest
from pathlib import Path

from patchyard import demo, inventory
from patchyard.jobs import JobError


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = demo.make_app(Path(self.tmp.name), port=18790)

    def tearDown(self):
        self.tmp.cleanup()

    def test_data_shows_every_state_with_made_up_names(self):
        data = json.loads((self.app.data / "status.json").read_text())
        self.assertEqual(set(data["summary"]), {"fail", "warn", "update", "reboot", "ok"})
        self.assertTrue(all(n > 0 for n in data["summary"].values()), data["summary"])
        roles = {h["role"] for h in data["hosts"]}
        self.assertEqual(roles, {"managed", "report_only", "device"})
        for h in data["hosts"]:
            if h["role"] != "device":
                self.assertTrue(h["host"].endswith(".example.com"), h["host"])

    def test_inventory_matches_the_data(self):
        cfg = self.app.cfg
        managed = inventory.hosts(cfg.inventory_file, cfg.inventory.managed_group)
        self.assertIn("web2.example.com", managed)
        self.assertNotIn("legacy.example.com", managed)

    def test_nothing_runs(self):
        with self.assertRaisesRegex(JobError, "demo mode"):
            self.app.jobs.start("patch", ["web1.example.com"], "demo", lambda log: 0)

    def test_sample_job_is_readable(self):
        (job,) = self.app.jobs.recent()
        out = self.app.jobs.read(job.id, 0)
        self.assertTrue(out["done"])
        self.assertIn("finished rc=0", out["text"])
        self.assertEqual(self.app.anonymous, "demo")


if __name__ == "__main__":
    unittest.main()
