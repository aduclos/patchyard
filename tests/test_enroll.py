import base64
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from patchyard import enroll
from tests.helpers import make_config

ROOT = Path(__file__).resolve().parent.parent


class EnrollCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        shutil.copytree(ROOT / "bootstrap", root / "bootstrap")
        (root / "key.pub").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBody controller\n")
        self.cfg = make_config(paths__code=str(root), service_account__key=str(root / "key"),
                               service_account__allowed_sources=["192.0.2.5", "192.0.2.6"],
                               service_account__name="patchyard")

    def tearDown(self):
        self.tmp.cleanup()

    def test_linux_command_carries_the_exact_bootstrap(self):
        cmd = enroll.linux_command(self.cfg)
        blob = re.search(r"echo (\S+) \| base64 -d", cmd).group(1)
        self.assertEqual(base64.b64decode(blob), (ROOT / "bootstrap" / "bootstrap.sh").read_bytes())
        self.assertIn("PATCHYARD_ACCOUNT=patchyard", cmd)
        self.assertIn("PATCHYARD_ALLOWED_SOURCES=192.0.2.5,192.0.2.6", cmd)
        self.assertIn("PATCHYARD_PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBody patchyard'", cmd)
        self.assertTrue(cmd.startswith("B=$(mktemp)") and " sudo env " in cmd)

    def test_windows_command_is_encoded_powershell(self):
        cmd = enroll.windows_command(self.cfg)
        self.assertTrue(cmd.startswith("powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "))
        script = base64.b64decode(cmd.rsplit(" ", 1)[1]).decode("utf-16-le")
        first = script.splitlines()[0]
        self.assertIn("$env:PATCHYARD_ACCOUNT='patchyard'", first)
        self.assertIn("$env:PATCHYARD_ALLOWED_SOURCES='192.0.2.5,192.0.2.6'", first)
        self.assertEqual(script.splitlines()[1:], enroll._compact_ps((ROOT / "bootstrap" / "bootstrap.ps1").read_text())
                         .splitlines())
        self.assertNotIn("# ", script)                       # comments stripped (they cost 4x as base64)
        self.assertIn("OK: $Account ready", script)
        self.assertIn("Run as administrator", script)        # explicit elevation check survives compaction
        self.assertLess(len(cmd), enroll.WINDOWS_CMD_MAX - 2000)   # headroom for long account names/keys

    def test_compaction_keeps_code_and_drops_only_comments(self):
        src = "# header\n\n  $a = 'x'   # why\n$b = \"#notacomment\"\n    if ($a) { 1 }\n"
        self.assertEqual(enroll._compact_ps(src), "$a = 'x'\n$b = \"#notacomment\"\nif ($a) { 1 }\n")

    def test_short_names_get_the_site_domain(self):
        cfg = make_config(site__domain=".corp.example.com")
        self.assertEqual(enroll.normalize_host(cfg, "Web1"), "web1.corp.example.com")
        self.assertEqual(enroll.normalize_host(cfg, "web1.other.example.com"), "web1.other.example.com")


class CheckAccessTest(unittest.TestCase):
    """The Add machine wizard's checks, with the network replaced."""

    def run_check(self, os_family="linux", method="password", resolves="192.0.2.7", key=True, ssh_out=("", "", 0),
                  port=True):
        from subprocess import CompletedProcess
        from unittest import mock

        from patchyard import ssh
        cfg = make_config()
        limiter = mock.Mock()
        r = CompletedProcess([], ssh_out[2], ssh_out[0], ssh_out[1])
        with mock.patch.object(ssh, "resolves", return_value=resolves), \
             mock.patch.object(enroll, "_record_host_key",
                               side_effect=lambda c, h, say: (say("host key: 256 SHA256:abc h (ED25519)"), key)[1]), \
             mock.patch.object(enroll, "_ssh_with_password", return_value=r), \
             mock.patch.object(enroll, "_port_open", return_value=port):
            steps = enroll.check_access(cfg, limiter, "web7.example.com", os_family, method, "bob", "pw")
        return steps, limiter

    def test_all_good_linux(self):
        steps, _ = self.run_check(ssh_out=("signed-in\nsudo-ok\n", "", 0))
        self.assertEqual([(s["step"], s["ok"]) for s in steps], [("Name", True), ("SSH", True), ("Sign-in", True),
                                                                ("Rights", True)])
        self.assertIn("SHA256:abc", steps[1]["detail"])

    def test_stops_at_the_first_problem_with_a_hint(self):
        steps, _ = self.run_check(resolves=None)
        self.assertEqual([(s["step"], s["ok"]) for s in steps], [("Name", False)])
        steps, _ = self.run_check(os_family="windows", key=False)
        self.assertIn("Run a line on it instead", steps[-1]["detail"])
        steps, limiter = self.run_check(ssh_out=("", "bob@web7: Permission denied (password).", 255))
        self.assertEqual(steps[-1]["step"], "Sign-in")
        self.assertIn("refused", steps[-1]["detail"])
        limiter.fail.assert_called_once()                # counts toward the lockout guard
        steps, _ = self.run_check(ssh_out=("signed-in\n", "sudo: a password is required", 1))
        self.assertEqual((steps[-1]["step"], steps[-1]["ok"]), ("Rights", False))

    def test_a_closed_port_fails_ssh_even_with_a_known_host_key(self):
        steps, _ = self.run_check(os_family="windows", port=False)     # key on file, firewall rule removed
        self.assertEqual([(s["step"], s["ok"]) for s in steps], [("Name", True), ("SSH", False)])
        self.assertIn("nothing answers on port 22", steps[-1]["detail"])

    def test_windows_administrator(self):
        steps, _ = self.run_check(os_family="windows", ssh_out=("admin\r\n", "", 0))
        self.assertEqual((steps[-1]["step"], steps[-1]["ok"]), ("Rights", True))
        # an administrator whose remote sign-in Windows filtered (UAC on a local account): say so
        steps, _ = self.run_check(os_family="windows", ssh_out=("limited\r\n", "", 0))
        self.assertEqual((steps[-1]["step"], steps[-1]["ok"]), ("Rights", False))
        self.assertIn("UAC", steps[-1]["detail"])
        steps, _ = self.run_check(os_family="windows", ssh_out=("no\r\n", "", 0))
        self.assertIn("is not an administrator", steps[-1]["detail"])
        # the probe works under cmd and PowerShell alike: no quotes or $ for the outer shell to touch
        self.assertNotRegex(enroll.WINDOWS_ADMIN_PROBE, r"[\"'$|]")



class UnenrollTest(unittest.TestCase):
    def test_retire_hint_matches_the_machine(self):
        import io
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "host_vars").mkdir()
            (state / "inventory.ini").write_text("[managed]\npc1.example.com\nweb1.example.com\n")
            (state / "host_vars" / "pc1.example.com.yml").write_text("ansible_shell_type: powershell\n")
            cfg = make_config(paths__state=str(state))
            jobs = mock.Mock()
            win, lin = io.StringIO(), io.StringIO()
            enroll.unenroll(cfg, jobs, win, "pc1.example.com")
            enroll.unenroll(cfg, jobs, lin, "web1.example.com")
        self.assertIn("Remove-LocalUser", win.getvalue())
        self.assertNotIn("userdel", win.getvalue())
        self.assertIn("userdel", lin.getvalue())


if __name__ == "__main__":
    unittest.main()
