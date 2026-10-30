import json
import unittest

from patchyard import mcp

STATUS = {"summary": {"ok": 1, "reboot": 1, "update": 0, "fail": 0, "warn": 0}, "hosts": [
    {"host": "web1.example.com", "name": "web1", "role": "managed", "status": "reboot", "os": "Ubuntu 24.04",
     "items": [{"sev": "reboot", "key": "reboot:web1", "text": "reboot needed for 2 h"}],
     "policy": {"reboot_blackout": {"hours_utc": [8], "reason": "batch jobs"}}},
    {"host": "ctl.example.com", "name": "ctl", "role": "controller", "status": "ok", "os": "Fedora", "items": [],
     "pending": 4, "updates": {"policy": "dnf-automatic"}},
    {"host": "db1.example.com", "name": "db1", "role": "managed", "status": "ok", "os": "Debian 13", "items": [],
     "pending": 2, "updates": {"policy": "none"}},
]}


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/data/status.json":
            return STATUS
        if path in ("/api/run", "/api/refresh", "/api/device"):
            return {"job": "job-1"}
        return {}


def call(fleet, tool, **args):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    resp = mcp.handle(fleet, msg)
    return resp["result"]["content"][0]["text"], resp["result"]["isError"]


class McpTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.fleet = mcp.Fleet(self.client)

    def test_initialize_and_tool_list(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
        r = mcp.handle(None, init)
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", r["result"]["capabilities"])
        tools = mcp.handle(None, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"list_machines", "machine_status", "check_now", "patch", "reboot",
                                 "device_action", "recent_jobs", "job_log"})
        self.assertIsNone(mcp.handle(None, {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_list_and_status(self):
        text, err = call(self.fleet, "list_machines", filter="waiting")
        self.assertFalse(err)
        self.assertIn("web1", text)
        self.assertNotIn("ctl ", text)
        text, _ = call(self.fleet, "machine_status", name="WEB1")
        self.assertEqual(json.loads(text)["role"], "managed")

    def test_pending_updates_show_on_ok_machines(self):
        # "ok" only means nothing is failing or waiting on a person; queued updates must still show.
        text, err = call(self.fleet, "list_machines")
        self.assertFalse(err)
        self.assertIn("6 updates pending on 2 machine(s)", text.splitlines()[0])
        self.assertIn("4 pending, update policy: dnf-automatic", text)
        self.assertIn("2 pending, no automatic updates", text)
        text, _ = call(self.fleet, "list_machines", filter="pending")
        self.assertEqual([line.split()[0] for line in text.splitlines()[1:]], ["ctl", "db1"])

    def test_missing_token_file_is_reported_per_call_not_a_crash(self):
        fleet, err = mcp.from_env({"PATCHYARD_URL": "https://patch.example.com",
                                   "PATCHYARD_TOKEN_FILE": "/nonexistent/patchyard-token"})
        self.assertIsNone(fleet)
        self.assertIn("/nonexistent/patchyard-token", err)
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_machines"}}
        result = mcp.handle(fleet, msg, err)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("/nonexistent/patchyard-token", result["content"][0]["text"])
        _, err = mcp.from_env({"PATCHYARD_URL": "https://patch.example.com", "PATCHYARD_TOKEN": "pyd_x",
                               "PATCHYARD_CA_FILE": "/nonexistent/ca.pem"})
        self.assertIn("/nonexistent/ca.pem", err)

    def test_changes_need_confirmation(self):
        text, err = call(self.fleet, "patch", machines=["web1"])
        self.assertFalse(err)
        self.assertIn("Would patch web1", text)
        self.assertNotIn(("POST", "/api/run"), [c[:2] for c in self.client.calls])
        text, _ = call(self.fleet, "patch", machines=["web1"], confirm=True)
        self.assertIn("job-1", text)
        self.assertIn(("POST", "/api/run", {"action": "patch", "hosts": ["web1.example.com"], "reboot": False}),
                      self.client.calls)

    def test_reboot_preview_mentions_the_window(self):
        text, _ = call(self.fleet, "reboot", machine="web1")
        self.assertIn("batch jobs", text)
        self.assertIn("refused inside it", text)

    def test_errors_are_tool_errors_not_crashes(self):
        text, err = call(self.fleet, "patch", machines=["ctl"], confirm=True)
        self.assertTrue(err)
        self.assertIn("not patchable", text)
        text, err = call(self.fleet, "machine_status", name="nope")
        self.assertTrue(err)
        text, err = call(None, "list_machines")
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
