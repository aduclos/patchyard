import re
import unittest
from pathlib import Path

from patchyard import devices, machines, status
from patchyard.config import Device
from patchyard.status import Item
from tests.helpers import make_config

NOW = 1_800_000_000.0


def host(**kw):
    base = {"uu_policy": "patchyard", "uu_result": "success", "uu_last": str(int(NOW - 3600)),
            "containers": [], "groups": ["managed"], "os": "Ubuntu 24.04"}
    base.update(kw)
    return base


class AssessTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def keys(self, items):
        return {i.key.split(":")[0] for i in items}

    def test_healthy_machine_has_no_items(self):
        self.assertEqual(status.assess_host(self.cfg, "a", host(), {}, NOW), [])

    def test_unreachable(self):
        items = status.assess_host(self.cfg, "a", None, {}, NOW)
        self.assertEqual(self.keys(items), {"unreach"})

    def test_reboot_waiting(self):
        items = status.assess_host(self.cfg, "a", host(reboot_since=str(int(NOW - 7200))), {}, NOW)
        self.assertEqual([i.sev for i in items], ["reboot"])
        self.assertIn("2 h", items[0].text)

    def test_stale_and_failed_auto_updates(self):
        h = host(uu_last=str(int(NOW - 3 * 86400)), uu_result="exit-code")
        items = status.assess_host(self.cfg, "a", h, {}, NOW)
        self.assertEqual(self.keys(items), {"uufail", "uustale"})

    def test_container_memory_flags_then_forgets(self):
        mem = {}
        status.assess_host(self.cfg, "a", host(containers=["db"]), mem, NOW)
        gone = status.assess_host(self.cfg, "a", host(containers=[]), mem, NOW + 60)
        self.assertEqual([i.key for i in gone], ["ctr:a:db"])
        later = NOW + self.cfg.checks.container_memory_days * 86400 + 120
        fresh = host(containers=[], uu_last=str(int(later - 60)))
        self.assertEqual(status.assess_host(self.cfg, "a", fresh, mem, later), [])
        self.assertEqual(mem, {})

    def test_container_tracking_can_be_switched_off(self):
        mem = {"db": NOW}
        h = host(containers=[], policy_vars={"patchyard_track_containers": False})
        self.assertEqual(status.assess_host(self.cfg, "a", h, mem, NOW), [])

    def test_policy_typo_is_a_warning_not_a_crash(self):
        items = status.assess_host(self.cfg, "a", host(policy_vars={"patchyard_reboot_blackout": "nights"}), {}, NOW)
        self.assertEqual(self.keys(items), {"policy"})

    def test_idle_release_upgrade_is_flagged(self):
        h = host(release_upgrading="1", relup_activity=str(int(NOW - 3600)))
        self.assertIn("relupstuck", self.keys(status.assess_host(self.cfg, "a", h, {}, NOW)))

    def test_roles(self):
        self.assertEqual(status.role_of(self.cfg, "ctl.example.com", ["managed"]), "managed")  # like the rest
        self.assertEqual(status.role_of(self.cfg, "ctl.example.com", ["report_only"]), "report_only")
        self.cfg.controller.patch_command = ["sudo", "-n", "/usr/local/sbin/self-patch"]
        self.assertEqual(status.role_of(self.cfg, "ctl.example.com", ["report_only"]), "managed")  # its own command
        self.assertEqual(status.role_of(self.cfg, "b", ["report_only"]), "report_only")
        self.assertEqual(status.role_of(self.cfg, "c", ["managed"]), "managed")


class PluginTest(unittest.TestCase):
    def test_plugin_describes_the_controllers_own_updates(self):
        cfg = make_config()
        h = host(uu_policy="none", uu_last="", uu_result="")
        merged = status.with_plugin_updates(h, {"updates": {"policy": "weekly script", "last": NOW - 5 * 86400,
                                                            "result": "success"}})
        self.assertEqual(merged["uu_policy"], "weekly script")
        # not "no automatic updates", and a weekly schedule is not "stale" after 5 days
        self.assertEqual(status.assess_host(cfg, "ctl.example.com", merged, {}, NOW), [])

    def test_plugin_failure_result_is_a_problem(self):
        merged = status.with_plugin_updates(host(), {"updates": {"policy": "weekly script", "result": "exit-code"}})
        items = status.assess_host(make_config(), "ctl.example.com", merged, {}, NOW)
        self.assertEqual([i.key.split(":")[0] for i in items], ["uufail"])

    def test_without_updates_the_row_is_unchanged(self):
        h = host()
        self.assertIs(status.with_plugin_updates(h, {"note": "x"}), h)


class FakeDriver:
    def __init__(self, st=None, error=None):
        self.st, self.error, self.checks = st, error, []

    def status(self, check=False):
        self.checks.append(check)
        if self.error:
            raise self.error
        return self.st


class DeviceCheckTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(checks__device_firmware_check_hours=24)
        self.dev = Device(name="router", driver="opnsense", url="https://192.0.2.1", credentials_file="/x")

    def test_update_available_is_an_update_item(self):
        drv = FakeDriver(devices.DeviceStatus("OPNsense", "25.1.3", "25.1.4", True))
        st, items, _err = status.check_device(self.cfg, self.dev, {}, NOW, drv)
        self.assertEqual([i.sev for i in items], ["update"])
        self.assertIn("25.1.3 -> 25.1.4", items[0].text)
        row = status.export_device(self.dev, st, items, NOW)
        got = (row["role"], row["status"], row["device"]["capabilities"]["update"])
        self.assertEqual(got, ("device", "update", True))

    def test_firmware_is_looked_up_once_per_interval(self):
        mem = {}
        drv = FakeDriver(devices.DeviceStatus("x", "1", "1", False))
        status.check_device(self.cfg, self.dev, mem, NOW, drv)
        status.check_device(self.cfg, self.dev, mem, NOW + 3600, drv)
        status.check_device(self.cfg, self.dev, mem, NOW + 25 * 3600, drv)
        self.assertEqual(drv.checks, [True, False, True])

    def test_unreachable_device_is_a_warning(self):
        drv = FakeDriver(error=devices.DeviceError("cannot reach it"))
        st, items, err = status.check_device(self.cfg, self.dev, {}, NOW, drv)
        self.assertIsNone(st)
        self.assertEqual((items[0].sev, err), ("warn", "cannot reach it"))
        self.assertFalse(status.export_device(self.dev, st, items, NOW)["reachable"])

    def test_refused_login_is_not_retried_until_credentials_change(self):
        import os
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{}")
        self.addCleanup(os.remove, f.name)
        dev = Device(name="ap", driver="asuswrt", url="https://192.0.2.2", credentials_file=f.name)
        mem = {}
        drv = FakeDriver(error=devices.DeviceAuthError("wrong username or password"))
        status.check_device(self.cfg, dev, mem, NOW, drv)
        _, items, _ = status.check_device(self.cfg, dev, mem, NOW + 900, drv)
        self.assertEqual(len(drv.checks), 1)                    # second run did not log in again
        self.assertIn("not retrying", items[0].text)
        os.utime(f.name, (NOW + 1000, NOW + 1000))              # credentials fixed
        drv.error, drv.st = None, devices.DeviceStatus("AP", "1", "1", False)
        _, items, _ = status.check_device(self.cfg, dev, mem, NOW + 1800, drv)
        self.assertEqual((len(drv.checks), items), (2, []))
        self.assertNotIn("auth_refused", mem)

    def test_driver_bug_does_not_stop_the_check(self):
        _, items, _ = status.check_device(self.cfg, self.dev, {}, NOW, FakeDriver(error=KeyError("oops")))
        self.assertIn("driver error", items[0].text)

    def test_update_alert_title(self):
        msg = status.alerts(self.cfg, {"router": [status.Item("update", "fw:router", "x")]}, {}, NOW, 8, "d")
        self.assertEqual(msg.title, "Patchyard: update available")


class SnoozeAlertTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(checks__digest_hour=9)

    def test_snoozed_items_become_info(self):
        items = status.apply_snoozes([Item("reboot", "reboot:a", "reboot needed")], {"reboot:a": NOW + 99}, NOW)
        self.assertEqual(items[0].sev, "info")
        self.assertIn("snoozed until", items[0].text)

    def test_new_item_alerts_once(self):
        state = {}
        assessed = {"a": [Item("fail", "ctr:a:db", "container db is not running")]}
        msg = status.alerts(self.cfg, assessed, state, NOW, 8, "2026-01-01")
        self.assertEqual((msg.title, msg.priority), ("Patchyard: needs attention", "high"))
        self.assertIsNone(status.alerts(self.cfg, assessed, state, NOW, 8, "2026-01-01"))

    def test_daily_digest_while_something_waits(self):
        state = {"alerted": ["reboot:a"], "digest": "2026-01-01"}
        assessed = {"a": [Item("reboot", "reboot:a", "reboot needed")]}
        self.assertIsNone(status.alerts(self.cfg, assessed, state, NOW, 10, "2026-01-01"))
        msg = status.alerts(self.cfg, assessed, state, NOW, 10, "2026-01-02")
        self.assertEqual((msg.title, msg.priority), ("Patchyard: still waiting on you", "low"))

    def test_digest_off(self):
        cfg = make_config(checks__digest_hour=-1)
        state = {"alerted": ["reboot:a"]}
        self.assertIsNone(status.alerts(cfg, {"a": [Item("reboot", "reboot:a", "x")]}, state, NOW, 23, "d"))

    def test_info_never_alerts(self):
        self.assertIsNone(status.alerts(self.cfg, {"a": [Item("info", "stock:a", "x")]}, {}, NOW, 8, "d"))


class ExportTest(unittest.TestCase):
    def test_pending_titles_with_spaces(self):
        cfg = make_config()
        mac = host(os="MacOSX 15.1", pending="2", pending_titles="macOS Sequoia 15.1.1-24B91|Safari 18.2")
        one = host(os="Microsoft Windows 11 Pro", pending="1", pending_titles="Intel Display Driver Update (32.0)",
                   left_out="Feature update to Windows 11, version 25H2 (Upgrades)|Preview (optional)")
        linux = host(pending="2", pending_pkgs="openssl tzdata")
        out = status.export_host(cfg, "m", mac, [], {})
        self.assertEqual((out["pending_pkgs"], out["pending_titles"]),
                         (["macOS Sequoia 15.1.1-24B91", "Safari 18.2"], True))
        out = status.export_host(cfg, "w", one, [], {})
        self.assertEqual(out["pending_pkgs"], ["Intel Display Driver Update (32.0)"])   # one title, not its words
        self.assertEqual(out["left_out"],
                         ["Feature update to Windows 11, version 25H2 (Upgrades)", "Preview (optional)"])
        out = status.export_host(cfg, "l", linux, [], {})
        self.assertEqual((out["pending_pkgs"], out["pending_titles"], out["left_out"]),
                         (["openssl", "tzdata"], False, []))

    def test_windows_status_searches_what_patch_installs(self):
        root = Path(__file__).resolve().parent.parent / "ansible" / "playbooks"
        pattern = r"patchyard_windows_update_categories \| default\((\[[^\]]*\])\)"
        lists = [re.search(pattern, (root / f).read_text()) for f in ("status.yml", "patch.yml")]
        found = [re.findall(r"'([^']+)'", m.group(1)) for m in lists]
        self.assertEqual(found[0], found[1])
        self.assertEqual(found[0], list(machines.WINDOWS_DEFAULT))
        self.assertIn("skip_optional: true", (root / "status.yml").read_text())

    def test_export_shapes_policy_and_role(self):
        cfg = make_config()
        raw = {"_time": NOW, "_all": ["a"], "a": host(policy_vars={
            "patchyard_reboot_blackout": {"hours_utc": [1], "reason": "backups"}})}
        out = status.export(cfg, raw, {"a": []}, {}, NOW)
        h = out["hosts"][0]
        self.assertEqual(h["role"], "managed")
        self.assertEqual(h["policy"]["reboot_blackout"]["reason"], "backups")
        self.assertEqual(out["summary"]["ok"], 1)


if __name__ == "__main__":
    unittest.main()
