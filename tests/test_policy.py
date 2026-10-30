import unittest

from patchyard import policy


class PolicyTest(unittest.TestCase):
    def test_no_variables_means_no_policy(self):
        p = policy.parse({})
        self.assertIsNone(p.reboot_blackout)
        self.assertIsNone(p.manual_only_reason)
        self.assertEqual(p.hold_packages, ())
        self.assertEqual(p.problems, ())

    def test_blackout_with_reason(self):
        p = policy.parse({"patchyard_reboot_blackout": {"hours_utc": [9, 8, 8], "reason": "batch jobs"}})
        self.assertEqual(p.reboot_blackout.hours_utc, (8, 9))
        self.assertEqual(p.to_dict()["reboot_blackout"]["text"], "UTC hours 08:00, 09:00")
        self.assertEqual(p.to_dict()["reboot_blackout"]["reason"], "batch jobs")

    def test_blackout_in_local_time(self):
        p = policy.parse({"patchyard_reboot_blackout": {"days": ["fri", "mon", "tue", "wed", "thu"],
                                                        "start": "08:00", "end": "18:00", "reason": "office hours"}})
        b = p.reboot_blackout
        self.assertEqual((b.days, b.start, b.end), (("mon", "tue", "wed", "thu", "fri"), "08:00", "18:00"))
        self.assertEqual(b.text, "Mon\u2013Fri 08:00\u201318:00")
        overnight = policy.parse({"patchyard_reboot_blackout": {"start": "22:00", "end": "06:00"}}).reboot_blackout
        self.assertEqual(overnight.text, "every day 22:00\u201306:00")
        self.assertEqual(policy.days_text(["sat", "sun"]), "Sat, Sun")
        self.assertIsNone(policy.parse({"patchyard_reboot_blackout": {}}).reboot_blackout)
        for bad in ({"start": "8:00", "end": "18:00"}, {"start": "08:00", "end": "08:00"},
                    {"start": "08:00", "end": "18:00", "days": ["monday"]},
                    {"start": "08:00", "end": "18:00", "days": []}):
            p = policy.parse({"patchyard_reboot_blackout": bad})
            self.assertIsNone(p.reboot_blackout, bad)
            self.assertTrue(p.problems, bad)

    def test_manual_only_accepts_true_or_mapping(self):
        self.assertEqual(policy.parse({"patchyard_manual_only": True}).manual_only_reason, "")
        self.assertEqual(policy.parse({"patchyard_manual_only": {"reason": "planned"}}).manual_only_reason, "planned")
        self.assertIsNone(policy.parse({"patchyard_manual_only": False}).manual_only_reason)

    def test_holds(self):
        p = policy.parse({"patchyard_hold_packages": {"packages": ["postgresql-"], "reason": "planned"}})
        self.assertEqual(p.hold_packages, ("postgresql-",))
        self.assertEqual(p.to_dict()["hold"], {"packages": ["postgresql-"], "reason": "planned"})

    def test_malformed_values_become_problems_not_exceptions(self):
        p = policy.parse({
            "patchyard_reboot_blackout": {"hours_utc": [25]},
            "patchyard_manual_only": "yes",
            "patchyard_hold_packages": ["postgresql-"],
        })
        self.assertIsNone(p.reboot_blackout)
        self.assertIsNone(p.manual_only_reason)
        self.assertEqual(p.hold_packages, ())
        self.assertEqual(len(p.problems), 3)


if __name__ == "__main__":
    unittest.main()
