import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from patchyard import machines
from tests.helpers import make_config

ROOT = Path(__file__).resolve().parent.parent
INI = "[managed]\nweb1.example.com\nwin1.example.com\n\n[report_only]\nold.example.com\n\n[managed:vars]\nx=1\n"


class MachineSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state = Path(self.tmp.name)
        (state / "inventory.ini").write_text(INI)
        self.cfg = make_config(paths__state=str(state), paths__code=str(ROOT))

    def tearDown(self):
        self.tmp.cleanup()

    def test_values_are_checked_and_normalized(self):
        c = machines.check
        self.assertEqual(c("patchyard_update_time", "04:30"), "04:30")
        self.assertEqual(c("patchyard_update_spread_minutes", "45"), 45)
        self.assertEqual(c("patchyard_manual_only", {"reason": " planned "}), {"reason": "planned"})
        self.assertIs(c("patchyard_manual_only", {"reason": ""}), True)
        self.assertIs(c("patchyard_manual_only", False), False)
        self.assertEqual(c("patchyard_reboot_blackout", {"days": ["fri", "mon"], "start": "22:00", "end": "06:00"}),
                         {"days": ["mon", "fri"], "start": "22:00", "end": "06:00"})
        self.assertEqual(c("patchyard_reboot_blackout", {"hours_utc": [9, 8], "reason": "batch"}),
                         {"hours_utc": [8, 9], "reason": "batch"})            # the older form still saves
        self.assertEqual(c("patchyard_hold_packages", {"packages": ["postgresql-", " "], "reason": "x"}),
                         {"packages": ["postgresql-"], "reason": "x"})
        for key, bad in [("patchyard_update_time", "4:30"), ("patchyard_update_spread_minutes", 999),
                         ("patchyard_update_spread_minutes", True), ("patchyard_track_containers", "yes"),
                         ("patchyard_reboot_blackout", {"start": "08:00", "end": "08:00"}),
                         ("patchyard_hold_packages", {"packages": []}), ("patchyard_windows_update_categories", []),
                         ("patchyard_windows_update_categories", ["Everything"]),
                         ("patchyard_windows_update_mode", "sometimes"), ("patchyard_nope", 1)]:
            with self.assertRaises(machines.SettingError, msg=f"{key}={bad!r}"):
                c(key, bad)

    def test_store_defaults_hosts_and_clearing(self):
        machines.ensure(self.cfg)
        self.assertEqual(machines.read(self.cfg), ({}, {}))
        diff = machines.update(self.cfg, None, {"patchyard_update_time": "03:00"})
        self.assertEqual(diff, {"patchyard_update_time": [None, "03:00"]})
        machines.update(self.cfg, "web1.example.com", {"patchyard_update_time": "05:00",
                                                       "patchyard_track_containers": False})
        self.assertEqual(machines.update(self.cfg, "web1.example.com", {"patchyard_update_time": "05:00"}), {})
        defaults, hosts = machines.read(self.cfg)
        self.assertEqual(defaults, {"patchyard_update_time": "03:00"})
        self.assertEqual(hosts["web1.example.com"]["patchyard_track_containers"], False)
        machines.update(self.cfg, "web1.example.com",
                        {"patchyard_update_time": None, "patchyard_track_containers": None})
        self.assertNotIn("web1.example.com", machines.read(self.cfg)[1])   # nothing left: the entry goes
        data = json.loads(machines.path(self.cfg).read_text())
        self.assertEqual(set(data["all"]), {"vars", "hosts"})              # Ansible's inventory format

    def test_only_inventory_machines_are_written(self):
        with self.assertRaisesRegex(machines.SettingError, "not in the inventory"):
            machines.update(self.cfg, "ghost.example.com", {"patchyard_update_time": "01:00"})
        machines.update(self.cfg, "old.example.com", {"patchyard_track_containers": False})   # report-only is fine
        machines.update(self.cfg, "web1.example.com", {"patchyard_update_time": "01:00"})
        (self.cfg.state / "inventory.ini").write_text(INI.replace("web1.example.com\n", ""))
        machines.update(self.cfg, None, {"patchyard_update_time": "02:00"})   # any write drops machines that left
        self.assertEqual(set(machines.read(self.cfg)[1]), {"old.example.com"})
        machines.forget(self.cfg, "old.example.com")
        self.assertEqual(machines.read(self.cfg)[1], {})

    def test_family_and_applicability(self):
        self.assertEqual(machines.family("Microsoft Windows 11 Pro"), "windows")
        self.assertEqual(machines.family("Ubuntu 24.04"), "apt")
        self.assertEqual(machines.family("Fedora 42"), "linux")
        self.assertEqual(machines.family(""), "unknown")
        hold = machines.BY_KEY["patchyard_hold_packages"]
        self.assertTrue(machines.applies(hold, "apt"))
        self.assertFalse(machines.applies(hold, "linux"))
        self.assertFalse(machines.applies(machines.BY_KEY["patchyard_track_containers"], "windows"))

    def test_view_without_ansible_uses_the_store(self):
        machines.update(self.cfg, None, {"patchyard_update_time": "03:00"})
        machines.update(self.cfg, "win1.example.com", {"patchyard_windows_update_mode": "download_only"})
        with mock.patch.object(machines, "effective", return_value=None):
            v = machines.view(self.cfg, "win1.example.com", "Microsoft Windows 11 Pro")
        self.assertEqual(v["family"], "windows")
        self.assertEqual(v["source"]["patchyard_windows_update_mode"], "machine")
        self.assertEqual(v["source"]["patchyard_update_time"], "default")
        self.assertEqual(v["source"]["patchyard_reboot_blackout"], "builtin")
        self.assertNotIn("patchyard_hold_packages", v["applies"])

    @unittest.skipUnless(shutil.which("ansible-inventory"), "needs ansible-inventory on PATH")
    def test_effective_values_and_sources_from_ansible(self):
        (self.cfg.state / "host_vars").mkdir()
        hv = self.cfg.state / "host_vars" / "web1.example.com.yml"
        hv.write_text('patchyard_manual_only:\n  reason: "by hand"\n')
        machines.update(self.cfg, None, {"patchyard_update_time": "03:00"})
        machines.update(self.cfg, "web1.example.com", {"patchyard_manual_only": {"reason": "ui"},
                                                       "patchyard_track_containers": False})
        code = Path(self.tmp.name) / "code"
        shutil.copytree(ROOT / "ansible", code / "ansible", ignore=shutil.ignore_patterns(".venv", "collections"))
        cfg = make_config(paths__state=str(self.cfg.state), paths__code=str(code))
        v = machines.view(cfg, "web1.example.com", "Ubuntu 24.04")
        self.assertEqual(v["effective"]["patchyard_manual_only"], {"reason": "by hand"})   # host_vars wins
        self.assertEqual(v["source"]["patchyard_manual_only"], "inventory")
        self.assertEqual(v["source"]["patchyard_track_containers"], "machine")
        self.assertEqual(v["source"]["patchyard_update_time"], "default")
        self.assertEqual(v["source"]["patchyard_reboot_blackout"], "builtin")


if __name__ == "__main__":
    unittest.main()
