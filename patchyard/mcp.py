"""MCP server: manage a Patchyard fleet from an AI assistant (Claude Desktop, Claude Code, ...).

  python -m patchyard mcp            (stdio; configure it as an MCP server in your client)

Environment:
  PATCHYARD_URL         the dashboard, e.g. https://patch.example.com
  PATCHYARD_TOKEN       an API token (Settings > API tokens, or `python -m patchyard token create`)
  PATCHYARD_TOKEN_FILE  ...or a file containing it (preferred: keeps it out of client configs)
  PATCHYARD_CA_FILE     optional CA bundle for a private certificate authority

Everything goes through the normal web API as the token's user, so every action is a
logged job like one started in the browser. Tools that change machines need
"confirm": true; without it they return a preview of what would happen, so the
assistant shows the plan before anything runs.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__

PROTOCOL = "2025-06-18"

TOOLS: list[dict[str, Any]] = [
    {"name": "list_machines", "description": "List every machine and network device with its status (ok, "
     "reboot waiting, update available, problem), what needs attention, and how many updates are pending "
     "(an ok machine can still have pending updates its update policy has not installed yet).",
     "inputSchema": {"type": "object", "properties": {"filter": {
         "type": "string", "enum": ["all", "attention", "waiting", "pending", "devices"],
         "description": "attention = problems/warnings, waiting = reboot or update waiting, "
                        "pending = machines with pending updates"}}}},
    {"name": "machine_status", "description": "Details for one machine or device: items, policy, pending packages, "
     "containers, firmware.", "inputSchema": {"type": "object", "required": ["name"],
                                              "properties": {"name": {"type": "string"}}}},
    {"name": "check_now", "description": "Re-check every machine now (read-only; takes about a minute).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "patch", "description": "Install pending updates on machines, one at a time. Changes machines: needs "
     "confirm=true (without it, returns a preview).",
     "inputSchema": {"type": "object", "required": ["machines"], "properties": {
         "machines": {"type": "array", "items": {"type": "string"}},
         "reboot_if_needed": {"type": "boolean", "default": False},
         "confirm": {"type": "boolean", "default": False}}}},
    {"name": "reboot", "description": "Reboot one machine and wait for its containers to come back. Changes the "
     "machine: needs confirm=true.",
     "inputSchema": {"type": "object", "required": ["machine"], "properties": {
         "machine": {"type": "string"}, "ignore_blackout": {"type": "boolean", "default": False},
         "confirm": {"type": "boolean", "default": False}}}},
    {"name": "device_action", "description": "Network device: 'check' looks for firmware (read-only); 'update' "
     "installs firmware and 'reboot' restarts it (both need confirm=true).",
     "inputSchema": {"type": "object", "required": ["device", "action"], "properties": {
         "device": {"type": "string"}, "action": {"type": "string", "enum": ["check", "update", "reboot"]},
         "confirm": {"type": "boolean", "default": False}}}},
    {"name": "recent_jobs", "description": "The latest jobs (patch, reboot, checks) with who started them and results.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "job_log", "description": "Output of a job (from patch/reboot/check_now), and whether it finished.",
     "inputSchema": {"type": "object", "required": ["job_id"], "properties": {
         "job_id": {"type": "string"}, "offset": {"type": "integer", "default": 0}}}},
]


class ApiError(Exception):
    pass


class Client:
    """Thin client for the Patchyard web API, authenticated with an API token."""

    def __init__(self, url: str, token: str, ca_file: str = ""):
        if not url or not token:
            raise ApiError("set PATCHYARD_URL and PATCHYARD_TOKEN (or PATCHYARD_TOKEN_FILE)")
        self.url = url.rstrip("/")
        self.token = token
        self.ctx = ssl.create_default_context(cafile=ca_file or None)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "X-Patchyard": "1", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=60) as r:
                raw = r.read()
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                raise ApiError("the reply was not JSON: is PATCHYARD_URL the Patchyard dashboard, "
                               "and is the token valid?") from None
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", "")
            except ValueError:
                msg = ""
            finally:
                e.close()
            raise ApiError(f"HTTP {e.code}: {msg or e.reason}") from None
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(f"cannot reach {self.url}: {getattr(e, 'reason', e)}") from None


class Fleet:
    """The tools, independent of the MCP transport (and easy to test)."""

    def __init__(self, client: Client):
        self.c = client

    def _status(self) -> dict[str, Any]:
        return self.c.call("GET", "/data/status.json")

    def _find(self, name: str, hosts: list[dict[str, Any]]) -> dict[str, Any]:
        name = name.strip().lower()
        for h in hosts:
            if name in (h["host"].lower(), h["name"].lower()):
                return h
        raise ApiError(f"no machine or device called {name!r}; use list_machines")

    @staticmethod
    def _pending(h: dict[str, Any]) -> str:
        """'4 pending, update policy: ...' (status ok only means nothing is failing or waiting on a person)."""
        if not h.get("pending"):
            return ""
        how = (h.get("updates") or {}).get("policy") or ""
        return f"{h['pending']} pending, " + (f"update policy: {how}" if how not in ("", "none")
                                               else "no automatic updates (only patch installs them)")

    @classmethod
    def _line(cls, h: dict[str, Any]) -> str:
        notes = [cls._pending(h)] + [i["text"] for i in h.get("items", []) if i["sev"] != "info"]
        return f"{h['name']:16} {h['status']:7} {h.get('os', ''):28} {'; '.join(n for n in notes if n)}".rstrip()

    def list_machines(self, filter: str = "all") -> str:
        st = self._status()
        keep = {"all": lambda h: True, "attention": lambda h: h["status"] in ("fail", "warn"),
                "waiting": lambda h: h["status"] in ("reboot", "update"), "pending": lambda h: bool(h.get("pending")),
                "devices": lambda h: h["role"] == "device"}
        hosts = [h for h in st["hosts"] if keep.get(filter, keep["all"])(h)]
        s = st.get("summary", {})
        head = (f"{len(st['hosts'])} machines/devices: {s.get('ok', 0)} ok, "
                f"{s.get('reboot', 0)} reboot waiting, {s.get('update', 0)} update available, "
                f"{s.get('fail', 0) + s.get('warn', 0)} need attention.")
        pend = [h for h in st["hosts"] if h.get("pending")]
        if pend:
            head += (f" {sum(h['pending'] for h in pend)} updates pending on {len(pend)} machine(s): each "
                     "machine's update policy installs them, or patch does now.")
        return head + "\n" + ("\n".join(self._line(h) for h in hosts) or "(none in this view)")

    def machine_status(self, name: str) -> str:
        h = self._find(name, self._status()["hosts"])
        keep = ("host", "role", "status", "os", "updates", "pending", "pending_pkgs", "left_out", "reboot_since",
                "containers", "policy", "device", "release", "items")
        return json.dumps({k: h[k] for k in keep if k in h}, indent=1)

    def check_now(self) -> str:
        job = self.c.call("POST", "/api/refresh", {})["job"]
        return f"checking every machine; job {job} (use job_log to follow it)"

    def patch(self, machines: list[str], reboot_if_needed: bool = False, confirm: bool = False) -> str:
        hosts = self._status()["hosts"]
        targets = [self._find(m, hosts) for m in machines]
        bad = [t["name"] for t in targets if t["role"] != "managed"]
        if bad:
            raise ApiError(f"not patchable from Patchyard: {', '.join(bad)} (report-only or device)")
        plan = (f"Would patch {', '.join(t['name'] for t in targets)} one at a time"
                + (", rebooting if an update needs it (outside no-reboot windows)." if reboot_if_needed
                   else ", without rebooting."))
        if not confirm:
            return plan + " Call again with confirm=true to run it."
        job = self.c.call("POST", "/api/run", {"action": "patch", "hosts": [t["host"] for t in targets],
                                               "reboot": reboot_if_needed})["job"]
        return f"started: job {job}. {plan.replace('Would patch', 'Patching')}"

    def reboot(self, machine: str, ignore_blackout: bool = False, confirm: bool = False) -> str:
        t = self._find(machine, self._status()["hosts"])
        window = (t.get("policy") or {}).get("reboot_blackout")
        note = ""
        if window:
            why = ", ".join(x for x in (window.get("text"), window.get("reason")) if x)
            note = f" (it has a no-reboot window: {why}; {'overriding it' if ignore_blackout else 'refused inside it'})"
        if t.get("controller"):
            note += " It runs Patchyard: the dashboard (and this tool) are unavailable until it is back up"
        plan = f"Would reboot {t['name']}{note}."
        if not confirm:
            return plan + " Call again with confirm=true to run it."
        body = {"action": "reboot", "hosts": [t["host"]], "force": ignore_blackout, "confirm": "yes"}
        job = self.c.call("POST", "/api/run", body)["job"]
        return f"started: job {job}. Rebooting {t['name']}."

    def device_action(self, device: str, action: str, confirm: bool = False) -> str:
        t = self._find(device, self._status()["hosts"])
        if t["role"] != "device":
            raise ApiError(f"{t['name']} is not a network device")
        if action != "check" and not confirm:
            d = t.get("device") or {}
            what = f"install firmware {d.get('latest') or 'update'} on" if action == "update" else "reboot"
            return (f"Would {what} {t['name']}. {d.get('disruption') or ''} "
                    "Call again with confirm=true to run it.").replace("  ", " ")
        body = {"name": t["host"], "action": action, **({"confirm": "yes"} if action != "check" else {})}
        job = self.c.call("POST", "/api/device", body)["job"]
        return f"started: job {job} ({action} {t['name']})."

    def recent_jobs(self) -> str:
        jobs = self.c.call("GET", "/api/state").get("jobs", [])
        return "\n".join(f"{j['id']}  {j['action']:16} {','.join(j['hosts']) or '-':28} by {j.get('user', '?'):10} "
                         f"{'running' if j['rc'] is None else 'ok' if j['rc'] == 0 else 'failed'}" for j in jobs) \
            or "(no jobs since the service started)"

    def job_log(self, job_id: str, offset: int = 0) -> str:
        r = self.c.call("GET", f"/api/job/{job_id}?offset={int(offset)}")
        tail = r["text"][-20000:]
        state = "finished, rc=" + str(r["rc"]) if r["done"] else f"still running (next offset {r['offset']})"
        return f"[{state}]\n{tail}"


def handle(fleet: Fleet | None, msg: dict[str, Any], setup_error: str = "") -> dict[str, Any] | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    mid, method = msg.get("id"), msg.get("method", "")
    if mid is None:                              # notifications (e.g. notifications/initialized)
        return None

    def ok(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        return ok({"protocolVersion": version, "capabilities": {"tools": {}},
                   "serverInfo": {"name": "patchyard", "version": __version__},
                   "instructions": "Manage a Patchyard fleet. Read with list_machines/machine_status; tools that "
                                   "change machines return a preview unless confirm=true. Show the user the "
                                   "preview and get their agreement before confirming."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t["name"] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            if fleet is None:
                raise ApiError(setup_error)
            text = getattr(fleet, name)(**args)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except (ApiError, TypeError, KeyError) as e:
            return ok({"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        except Exception as e:                   # never let one tool call take the server down
            print(f"patchyard mcp: {name} failed: {e!r}", file=sys.stderr)
            return ok({"content": [{"type": "text", "text": f"internal error: {e}"}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def from_env(env: Any = os.environ) -> tuple[Fleet | None, str]:
    """The Fleet for this environment, or None and the reason (a missing token file, a bad CA bundle...)."""
    token = env.get("PATCHYARD_TOKEN", "")
    token_file, ca_file = env.get("PATCHYARD_TOKEN_FILE", ""), env.get("PATCHYARD_CA_FILE", "")
    try:
        if not token and token_file:
            try:
                token = Path(token_file).read_text().strip()
            except OSError as e:
                raise ApiError(f"cannot read PATCHYARD_TOKEN_FILE {token_file}: {e.strerror or e}") from None
        try:
            return Fleet(Client(env.get("PATCHYARD_URL", ""), token, ca_file)), ""
        except (OSError, ssl.SSLError) as e:      # ssl omits the file name from its errors
            raise ApiError(f"cannot load PATCHYARD_CA_FILE {ca_file}: {e.strerror or e}") from None
    except ApiError as e:
        return None, str(e)


def main() -> int:
    fleet, err = from_env()                      # still start on errors, so the client shows the reason per call
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None,
                                           "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle(fleet, msg, err)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0
