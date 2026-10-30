import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

PLAYBOOKS = Path(__file__).resolve().parent.parent / "ansible" / "playbooks"
WINDOWS_FIRST = "ansible_facts['os_family'] != 'Windows' and "


class PlaybookConditionTest(unittest.TestCase):
    def test_linux_only_facts_are_guarded_for_windows(self):
        # Windows facts have no pkg_mgr; ansible-core fails the task (and the run) on the
        # missing key unless the condition rules Windows out first.
        for book in sorted(PLAYBOOKS.glob("*.yml")):
            for n, line in enumerate(book.read_text().splitlines(), 1):
                m = re.match(r"\s*(?:- )?when: (.*)", line)
                if m and "ansible_facts['pkg_mgr']" in m.group(1):
                    self.assertTrue(m.group(1).startswith(WINDOWS_FIRST), f"{book.name}:{n}: {line.strip()}")

    def test_windows_patch_never_installs_feature_upgrades_by_default(self):
        text = (PLAYBOOKS / "patch.yml").read_text()
        task = text[text.index("ansible.windows.win_updates:"):text.index("register: upgrade_win")]
        self.assertNotIn('"*"', task)
        self.assertNotIn("'Upgrades'", task)
        self.assertIn("skip_optional: true", task)
        self.assertIn("reboot: false", task)

    def test_windows_patch_log_names_each_update(self):
        text = (PLAYBOOKS / "patch.yml").read_text()
        summary = text[text.index("register: upgrade_win"):text.index("- name: Reboot required?")]
        for part in ("upgrade_win.updates", "upgrade_win.filtered_updates", "installed: ", "FAILED: ", "left out: "):
            self.assertIn(part, summary)


DNF5 = """Upgrades
hunspell.x86_64            1.7.6-1.fc44    updates
libsolv.x86_64             0.7.41-1.fc44   updates
"""
DNF4 = """
kernel.x86_64                     6.5.6-300.fc39     updates
a-very-long-package-name-that-wraps-here.noarch
                                  1.0-1.fc39         updates
grub2-tools.x86_64                1:2.06-100.fc39    updates
Obsoleting Packages
grub2-tools.x86_64                1:2.06-100.fc39    updates
    grub2-tools.x86_64            1:2.06-95.fc39     @updates
"""


class DnfPendingTest(unittest.TestCase):
    """Runs status.yml's own dnf pending lines against a fake dnf."""

    def pending(self, dnf_output: str) -> dict[str, str]:
        lines = (PLAYBOOKS / "status.yml").read_text().splitlines()
        start = next(i for i, x in enumerate(lines) if x.strip().startswith("pk=$(dnf "))
        end = next(i for i in range(start, len(lines)) if lines[i].strip().startswith('echo "pending_pkgs='))
        script = "\n".join(x.strip() for x in lines[start:end + 1])
        with tempfile.TemporaryDirectory() as d:
            Path(d, "out").write_text(dnf_output)
            Path(d, "dnf").write_text(f"#!/bin/sh\ncat {d}/out\n")
            os.chmod(Path(d, "dnf"), 0o755)
            r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True,
                               env={**os.environ, "PATH": f"{d}:{os.environ['PATH']}"})
        return dict(line.split("=", 1) for line in r.stdout.splitlines())

    def test_dnf5_header_is_not_a_package(self):
        self.assertEqual(self.pending(DNF5), {"pending": "2", "pending_pkgs": "hunspell.x86_64 libsolv.x86_64"})

    def test_dnf4_obsoletes_and_wrapped_names(self):
        out = self.pending(DNF4)
        self.assertEqual(out["pending"], "3")
        self.assertEqual(out["pending_pkgs"].split(),
                         ["a-very-long-package-name-that-wraps-here.noarch", "grub2-tools.x86_64", "kernel.x86_64"])

    def test_nothing_pending(self):
        self.assertEqual(self.pending(""), {"pending": "0", "pending_pkgs": ""})


if __name__ == "__main__":
    unittest.main()
