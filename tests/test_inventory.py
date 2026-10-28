import tempfile
import unittest
from pathlib import Path

from fleet import inventory

INI = """# managed by hand
[managed]
# canaries first
web1.example.com
web2.example.com

[report_only]
ctl.example.com ansible_connection=local

[managed:vars]
ansible_user=svc
"""


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "inventory.ini"
        self.path.write_text(INI)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_hosts_ignoring_comments_and_vars(self):
        self.assertEqual(inventory.hosts(self.path, "managed"), ["web1.example.com", "web2.example.com"])
        self.assertEqual(inventory.hosts(self.path, "report_only"), ["ctl.example.com"])

    def test_add_then_remove_round_trips_exactly(self):
        inventory.add(self.path, "managed", "db1.example.com")
        self.assertEqual(inventory.hosts(self.path, "managed")[-1], "db1.example.com")
        self.assertIn("[managed:vars]", self.path.read_text())
        inventory.remove(self.path, "managed", "db1.example.com")
        self.assertEqual(self.path.read_text(), INI)

    def test_add_keeps_a_backup(self):
        backup = inventory.add(self.path, "managed", "db1.example.com")
        self.assertEqual(backup.read_text(), INI)

    def test_rejects_bad_names_duplicates_and_missing(self):
        with self.assertRaises(inventory.InventoryError):
            inventory.add(self.path, "managed", "bad name")
        with self.assertRaises(inventory.InventoryError):
            inventory.add(self.path, "managed", "web1.example.com")
        with self.assertRaises(inventory.InventoryError):
            inventory.remove(self.path, "managed", "nope.example.com")
        with self.assertRaises(inventory.InventoryError):
            inventory.hosts(self.path, "nogroup")


if __name__ == "__main__":
    unittest.main()
