"""The fleet check: collect status, judge it, publish it, alert on what changed.

  python -m fleet check [--if-due] [--no-push]

1. collect   run playbooks/status.yml (read-only) -> <state>/logs/status.json
2. assess    turn raw facts + per-machine policy into items (problem / warning /
             reboot waiting / info), remembering containers between runs
3. export    <state>/www/status.json, the only input of the web UI
4. notify    one message when a NEW actionable item appears; one low-priority
             daily digest while anything is still waiting on a person
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import notify, policy
from .config import Config, load

SEVERITY = {"fail": 0, "warn": 1, "reboot": 2, "info": 3}
ACTIONABLE = ("fail", "warn", "reboot")


@dataclass(frozen=True)
class Item:
    sev: str        # fail | warn | reboot | info
    key: str        # stable id, e.g. "reboot:web1" (used for snoozing and "is this new?")
    text: str


def short(host: str) -> str:
    return host.split(".")[0]


def ago(ts: float, now: float) -> str:
    d = (now - ts) / 86400
    return f"{d:.0f} d" if d >= 1 else f"{(now - ts) / 3600:.0f} h"


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ collect
def collect(cfg: Config) -> dict[str, Any]:
    started = time.time()
    ansible = cfg.code / "ansible"
    env = dict(os.environ, FLEET_STATE=str(cfg.state))
    r = subprocess.run([str(ansible / ".venv/bin/ansible-playbook"), "playbooks/status.yml"],
                       cwd=ansible, capture_output=True, text=True, timeout=900, env=env)
    (cfg.state / "logs" / "status-last-run.log").write_text(r.stdout + r.stderr)
    raw = json.loads((cfg.state / "logs" / "status.json").read_text())
    # status.yml exits non-zero whenever a machine is unreachable, so its exit code says
    # nothing; output older than this run means the playbook itself broke.
    if raw.get("_time", 0) < started - 5:
        raise RuntimeError("status.yml did not produce fresh data (see logs/status-last-run.log)")
    return raw


def controller_extra(cfg: Config) -> dict[str, Any]:
    """Output of the optional controller status plugin (a JSON object), or {}."""
    plugin = cfg.controller.status_plugin
    if not plugin:
        return {}
    try:
        r = subprocess.run([plugin], capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return {"items": [{"sev": "warn", "text": f"status plugin failed: {e}"}]}


# ------------------------------------------------------------------ assess
def role_of(cfg: Config, host: str, groups: list[str]) -> str:
    if host == cfg.controller.host:
        return "controller"
    if cfg.inventory.report_only_group in groups:
        return "report_only"
    if cfg.inventory.managed_group in groups:
        return "managed"
    return "other"


def assess_host(cfg: Config, host: str, h: dict[str, Any] | None, memory: dict[str, float],
                now: float) -> list[Item]:
    """Items for one machine. `memory` (container -> last seen) is updated in place."""
    c = cfg.checks
    if h is None:
        return [Item("warn", f"unreach:{host}", "unreachable (powered off or off the network?)")]
    items: list[Item] = []
    pol = policy.parse(h.get("policy_vars") or {})
    items += [Item("warn", f"policy:{host}:{i}", f"inventory: {p}") for i, p in enumerate(pol.problems)]

    upgrading = bool(h.get("release_upgrading"))
    if upgrading:
        act = _int(h.get("relup_activity")) or 0
        when = datetime.fromtimestamp(act).strftime("%H:%M") if act else "?"
        if now - act > c.release_upgrade_idle_minutes * 60:
            items.append(Item("warn", f"relupstuck:{host}",
                              f"release upgrade idle since {when}: most likely waiting for an answer "
                              "in the terminal it was started from"))
        else:
            items.append(Item("info", f"relup:{host}", f"release upgrade running, last activity {when}"))

    result = h.get("uu_result", "")
    if result not in ("success", ""):
        items.append(Item("fail", f"uufail:{host}", f"last automatic update run: {result}"))
    last = _int(h.get("uu_last")) or 0
    automatic = h.get("uu_policy") in ("fleet", "stock", "dnf-automatic")
    if automatic and not upgrading and now - last > c.stale_auto_update_hours * 3600:
        items.append(Item("warn", f"uustale:{host}", "automatic updates have not completed in "
                                                     + (ago(last, now) if last else "ever")))
    if h.get("uu_policy") == "stock":
        items.append(Item("info", f"stock:{host}",
                          "security updates only; apply the auto-update policy for all updates"))
    if h.get("uu_policy") == "none":
        items.append(Item("info", f"noauto:{host}", "no automatic updates configured"))
    if h.get("dpkg_audit", "0") not in ("0", ""):
        items.append(Item("fail", f"dpkg:{host}", "dpkg has half-installed packages"))
    since = _int(h.get("reboot_since"))
    if since:
        pk = f" ({h['reboot_pkgs']})" if h.get("reboot_pkgs") else ""
        items.append(Item("reboot", f"reboot:{host}", f"reboot needed for {ago(since, now)}{pk}"))
    if h.get("release_upgrade"):
        items.append(Item("info", f"relavail:{host}", f"{h.get('os', '').split()[0] or 'OS'} {h['release_upgrade']} "
                                                       "available (optional; the current release is still supported)"))

    if pol.track_containers:
        running = set(h.get("containers") or [])
        for name in running:
            memory[name] = now
        window = c.container_memory_days * 86400
        for name, seen in sorted(memory.items()):
            if name not in running and now - seen < window:
                items.append(Item("fail", f"ctr:{host}:{name}", f"container {name} is not running"))
        for name in [n for n, seen in memory.items() if now - seen >= window]:
            del memory[name]
    return items


def apply_snoozes(items: list[Item], snoozes: dict[str, float], now: float) -> list[Item]:
    out = []
    for it in items:
        until = snoozes.get(it.key, 0)
        if until > now and it.sev != "info":
            day = datetime.fromtimestamp(until).strftime("%b %-d")
            out.append(Item("info", it.key, f"{it.text} (snoozed until {day})"))
        else:
            out.append(it)
    return out


def worst(items: list[Item]) -> str:
    sevs = [i.sev for i in items if i.sev != "info"]
    return min(sevs, key=SEVERITY.get) if sevs else "ok"


# ------------------------------------------------------------------ export
def export_host(cfg: Config, host: str, h: dict[str, Any] | None, items: list[Item],
                extra: dict[str, Any]) -> dict[str, Any]:
    h = h or {}
    groups = h.get("groups") or []
    return {
        "host": host, "name": short(host), "role": role_of(cfg, host, groups), "groups": groups,
        "status": worst(items), "reachable": bool(h),
        "os": h.get("os", ""),
        "updates": {"policy": h.get("uu_policy", ""), "last": _int(h.get("uu_last")), "result": h.get("uu_result", "")},
        "pending": _int(h.get("pending")), "pending_pkgs": (h.get("pending_pkgs") or "").split(),
        "reboot_since": _int(h.get("reboot_since")), "reboot_pkgs": h.get("reboot_pkgs", ""),
        "uptime_s": h.get("uptime_s"), "containers": h.get("containers") or [],
        "release": {"available": h.get("release_upgrade", ""), "upgrading": bool(h.get("release_upgrading")),
                    "activity": _int(h.get("relup_activity")), "last_line": h.get("relup_last", ""),
                    "dpkg_last": h.get("dpkg_last", "")},
        "policy": policy.parse(h.get("policy_vars") or {}).to_dict(),
        "items": [asdict(i) for i in sorted(items, key=lambda i: SEVERITY[i.sev])],
        "extra": {k: v for k, v in extra.items() if k in ("note", "report")},
    }


def export(cfg: Config, raw: dict[str, Any], assessed: dict[str, list[Item]], extra: dict[str, Any],
           now: float) -> dict[str, Any]:
    hosts = [export_host(cfg, host, raw.get(host), items, extra if host == cfg.controller.host else {})
             for host, items in assessed.items()]
    order = {"fail": 0, "warn": 1, "reboot": 2, "ok": 3}
    hosts.sort(key=lambda x: (order[x["status"]], x["name"]))
    summary = {k: sum(1 for x in hosts if x["status"] == k) for k in order}
    return {"time": raw.get("_time", now), "generated": now, "summary": summary, "hosts": hosts,
            "site": {"name": cfg.site.name, "domain": cfg.site.domain,
                     "service_account": cfg.service_account.name,
                     "controller": cfg.controller.host,
                     "controller_reboot": bool(cfg.controller.reboot_command)}}


# ------------------------------------------------------------------ notify
def alerts(cfg: Config, assessed: dict[str, list[Item]], state: dict[str, Any], now: float,
           local_hour: int, today: str) -> notify.Message | None:
    """The message to send this run (or None), updating `state` for next time."""
    actionable = {i.key: (i, short(h)) for h, items in assessed.items() for i in items if i.sev in ACTIONABLE}
    new = {k: v for k, v in actionable.items() if k not in set(state.get("alerted", []))}
    digest_hour = cfg.checks.digest_hour
    digest_due = bool(actionable) and digest_hour >= 0 and local_hour >= digest_hour and state.get("digest") != today
    state["alerted"] = sorted(actionable)
    if new or digest_due:
        state["digest"] = today
    chosen = new or (actionable if digest_due else {})
    if not chosen:
        return None
    lines = [f"{h}: {i.text}" for i, h in sorted(chosen.values(), key=lambda v: SEVERITY[v[0].sev])]
    top = min(SEVERITY[i.sev] for i, _ in chosen.values())
    if new:
        title = "Fleet: needs attention" if top < SEVERITY["reboot"] else "Fleet: reboot waiting"
        prio = "high" if top == SEVERITY["fail"] else "default"
    else:
        title, prio = "Fleet: still waiting on you", "low"
    return notify.Message(title, "\n".join(lines), prio, cfg.site.url)


# ------------------------------------------------------------------ main
def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any, mode: int = 0o640) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, mode)
    tmp.replace(path)


def run(cfg: Config, *, push: bool = True, if_due: bool = False) -> int:
    logs, www = cfg.state / "logs", cfg.state / "www"
    if if_due:
        last = _read_json(logs / "status.json", {}).get("_time", 0)
        if time.time() - last < cfg.checks.interval_minutes * 60 - 30:
            return 0
    with open(logs / "fleet-check.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)        # one run at a time: the state file is read-modify-write
        state = _read_json(logs / "fleet-state.json", {})
        now = time.time()
        try:
            raw = collect(cfg)
        except Exception as e:
            if push and state.get("broken") != str(e):
                notify.send(cfg, notify.Message("Fleet check broken", str(e), "default", cfg.site.url))
            state["broken"] = str(e)
            _write_json(logs / "fleet-state.json", state)
            print(e, file=sys.stderr)
            return 1
        state.pop("broken", None)
        snoozes = {k: v for k, v in _read_json(logs / "snoozes.json", {}).items() if v > now}
        memory = state.setdefault("containers", {})
        assessed: dict[str, list[Item]] = {}
        extra = controller_extra(cfg)
        for host in raw.get("_all", []):
            items = assess_host(cfg, host, raw.get(host), memory.setdefault(host, {}), now)
            if host == cfg.controller.host:
                items += [Item(i.get("sev", "info"), f"plugin:{host}:{n}", str(i.get("text", "")))
                          for n, i in enumerate(extra.get("items", [])) if i.get("sev") in SEVERITY]
            assessed[host] = apply_snoozes(items, snoozes, now)
        msg = alerts(cfg, assessed, state, now, datetime.now().hour, datetime.now().strftime("%F"))
        if msg and push:
            notify.send(cfg, msg)
        www.mkdir(exist_ok=True)
        os.chmod(www, 0o2775)                   # the web server's own user reads this directory
        _write_json(www / "status.json", export(cfg, raw, assessed, extra, now), mode=0o644)
        _write_json(logs / "fleet-state.json", state)
    for host, items in assessed.items():
        print(f"{short(host):12} " + ("; ".join(i.text for i in items) or "ok"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fleet check", description=__doc__.split("\n")[0])
    ap.add_argument("--if-due", action="store_true", help="skip unless checks.interval_minutes has passed")
    ap.add_argument("--no-push", action="store_true", help="update the dashboard but send no alerts")
    args = ap.parse_args(argv)
    return run(load(), push=not args.no_push, if_due=args.if_due)
