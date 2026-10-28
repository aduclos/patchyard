import unittest

from fleet import policy


class PolicyTest(unittest.TestCase):
    def test_no_variables_means_no_policy(self):
        p = policy.parse({})
        self.assertIsNone(p.reboot_blackout)
        self.assertIsNone(p.manual_only_reason)
        self.assertEqual(p.hold_packages, ())
        self.assertEqual(p.problems, ())

    def test_blackout_with_reason(self):
        p = policy.parse({"fleet_reboot_blackout": {"hours_utc": [9, 8, 8], "reason": "batch jobs"}})
        self.assertEqual(p.reboot_blackout.hours_utc, (8, 9))
        self.assertTrue(p.reboot_blackout.active(8))
        self.assertFalse(p.reboot_blackout.active(10))
        self.assertEqual(p.to_dict()["reboot_blackout"]["reason"], "batch jobs")

    def test_manual_only_accepts_true_or_mapping(self):
        self.assertEqual(policy.parse({"fleet_manual_only": True}).manual_only_reason, "")
        self.assertEqual(policy.parse({"fleet_manual_only": {"reason": "planned"}}).manual_only_reason, "planned")
        self.assertIsNone(policy.parse({"fleet_manual_only": False}).manual_only_reason)

    def test_holds(self):
        p = policy.parse({"fleet_hold_packages": {"packages": ["postgresql-"], "reason": "planned"}})
        self.assertEqual(p.hold_packages, ("postgresql-",))
        self.assertEqual(p.to_dict()["hold"], {"packages": ["postgresql-"], "reason": "planned"})

    def test_malformed_values_become_problems_not_exceptions(self):
        p = policy.parse({
            "fleet_reboot_blackout": {"hours_utc": [25]},
            "fleet_manual_only": "yes",
            "fleet_hold_packages": ["postgresql-"],
        })
        self.assertIsNone(p.reboot_blackout)
        self.assertIsNone(p.manual_only_reason)
        self.assertEqual(p.hold_packages, ())
        self.assertEqual(len(p.problems), 3)


if __name__ == "__main__":
    unittest.main()
