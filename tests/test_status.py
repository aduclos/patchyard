import unittest

from fleet import status
from fleet.status import Item
from tests.helpers import make_config

NOW = 1_800_000_000.0


def host(**kw):
    base = {"uu_policy": "fleet", "uu_result": "success", "uu_last": str(int(NOW - 3600)),
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
        h = host(containers=[], policy_vars={"fleet_track_containers": False})
        self.assertEqual(status.assess_host(self.cfg, "a", h, mem, NOW), [])

    def test_policy_typo_is_a_warning_not_a_crash(self):
        items = status.assess_host(self.cfg, "a", host(policy_vars={"fleet_reboot_blackout": "nights"}), {}, NOW)
        self.assertEqual(self.keys(items), {"policy"})

    def test_idle_release_upgrade_is_flagged(self):
        h = host(release_upgrading="1", relup_activity=str(int(NOW - 3600)))
        self.assertIn("relupstuck", self.keys(status.assess_host(self.cfg, "a", h, {}, NOW)))

    def test_roles(self):
        self.assertEqual(status.role_of(self.cfg, "ctl.example.com", ["managed"]), "controller")
        self.assertEqual(status.role_of(self.cfg, "b", ["report_only"]), "report_only")
        self.assertEqual(status.role_of(self.cfg, "c", ["managed"]), "managed")


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
        self.assertEqual((msg.title, msg.priority), ("Fleet: needs attention", "high"))
        self.assertIsNone(status.alerts(self.cfg, assessed, state, NOW, 8, "2026-01-01"))

    def test_daily_digest_while_something_waits(self):
        state = {"alerted": ["reboot:a"], "digest": "2026-01-01"}
        assessed = {"a": [Item("reboot", "reboot:a", "reboot needed")]}
        self.assertIsNone(status.alerts(self.cfg, assessed, state, NOW, 10, "2026-01-01"))
        msg = status.alerts(self.cfg, assessed, state, NOW, 10, "2026-01-02")
        self.assertEqual((msg.title, msg.priority), ("Fleet: still waiting on you", "low"))

    def test_digest_off(self):
        cfg = make_config(checks__digest_hour=-1)
        state = {"alerted": ["reboot:a"]}
        self.assertIsNone(status.alerts(cfg, {"a": [Item("reboot", "reboot:a", "x")]}, state, NOW, 23, "d"))

    def test_info_never_alerts(self):
        self.assertIsNone(status.alerts(self.cfg, {"a": [Item("info", "stock:a", "x")]}, {}, NOW, 8, "d"))


class ExportTest(unittest.TestCase):
    def test_export_shapes_policy_and_role(self):
        cfg = make_config()
        raw = {"_time": NOW, "_all": ["a"], "a": host(policy_vars={
            "fleet_reboot_blackout": {"hours_utc": [1], "reason": "backups"}})}
        out = status.export(cfg, raw, {"a": []}, {}, NOW)
        h = out["hosts"][0]
        self.assertEqual(h["role"], "managed")
        self.assertEqual(h["policy"]["reboot_blackout"]["reason"], "backups")
        self.assertEqual(out["summary"]["ok"], 1)


if __name__ == "__main__":
    unittest.main()
