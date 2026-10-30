"""A demo dashboard with made-up machines, for trying the UI and taking screenshots.

  python -m patchyard demo [--port 8790] [--bind 127.0.0.1]     (from a checkout)

Serves the real web app with sample status data that goes through the same code as a real
check (status.assess_host / check_device / export), plus one finished job for the terminal.
Nothing is contacted and nothing changes: every action answers "demo mode". The data lives
in a temporary directory that is removed on exit.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
import time
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any

from . import config as configmod
from . import machines as msettings
from . import status, web
from .config import Auth, Config, Controller, Device, Paths, Site, Web
from .devices import DeviceStatus
from .jobs import Job, JobError, JobManager
from .policy import VARS as POLICY_VARS
from .setup import toml_dumps

DOMAIN = ".example.com"
SERVER = "patchyard" + DOMAIN
DAY = 86400

DEVICES = [
    (Device(name="router", driver="mikrotik", url="https://192.0.2.1", credentials_file="/nonexistent/router.json",
            disruption="everyone loses internet access for about 2 minutes"),
     DeviceStatus(model="RB5009UG+S+", version="7.16.1", latest="7.16.2", update_available=True)),
    (Device(name="office-ap", driver="unifi", url="https://192.0.2.2", credentials_file="/nonexistent/ap.json",
            options={"mac": "00:00:5e:00:53:01"},
            disruption="office Wi-Fi drops for about 2 minutes"),
     DeviceStatus(model="U7 Pro", version="8.0.21", latest="8.0.21", update_available=False)),
    (Device(name="nas", driver="truenas", url="https://192.0.2.3", credentials_file="/nonexistent/nas.json",
            disruption="file shares are offline for 5 minutes"),
     DeviceStatus(model="TrueNAS SCALE", version="25.04.2", latest="25.04.2", update_available=False)),
]

PATCH_LOG = """started by demo

$ ansible-playbook patch.yml --limit web2.example.com -e reboot_ok=false -e patchyard_explicit=true

PLAY [Patch managed machines] **************************************************

TASK [Gathering Facts] *********************************************************
ok: [web2.example.com]

TASK [Containers running before] ***********************************************
ok: [web2.example.com]

TASK [Upgrade packages (apt)] **************************************************
changed: [web2.example.com]

TASK [Reboot required?] ********************************************************
ok: [web2.example.com]

TASK [Result] ******************************************************************
ok: [web2.example.com] => {
    "msg": "upgraded=True |\\nreboot_needed=yes | REBOOT SKIPPED (reboot_ok=false) |\\ncontainers back: 2"
}

PLAY RECAP *********************************************************************
web2.example.com           : ok=5    changed=1    unreachable=0    failed=0    skipped=9    rescued=0    ignored=0


$ patchyard check
web2         reboot needed for 2 d (linux-image-6.8.0-88-generic)
router       firmware update available: 7.16.1 -> 7.16.2

=== finished rc=0
"""


def machines(now: float) -> dict[str, dict[str, Any]]:
    """Per-machine records in the shape status.yml writes."""
    def host(os_: str, policy: str = "patchyard", last: float = now - 5 * 3600, **kw: Any) -> dict[str, Any]:
        h = {"os": os_, "uu_policy": policy, "uu_last": str(int(last)), "uu_result": "success",
             "pending": "0", "pending_pkgs": "", "reboot_since": "", "reboot_pkgs": "", "dpkg_audit": "0",
             "uptime_s": 12 * DAY, "containers": [], "groups": ["managed"], "policy_vars": {}}
        return {**h, **kw}
    return {
        "web1" + DOMAIN: host("Ubuntu 24.04", containers=["nginx", "app", "redis"]),
        "web2" + DOMAIN: host("Ubuntu 24.04", reboot_since=str(int(now - 2 * DAY)),
                              reboot_pkgs="linux-image-6.8.0-88-generic", containers=["nginx", "app"]),
        "db1" + DOMAIN: host("Debian 12", pending="3", pending_pkgs="postgresql-16 libpq5 tzdata"),
        "files1" + DOMAIN: host("Rocky 9.6", policy="dnf-automatic"),
        "build1" + DOMAIN: host("Fedora 42", policy="dnf-automatic", containers=["registry"]),
        "reception" + DOMAIN: host("Microsoft Windows 11 Pro", policy="windows-update", pending="1",
                                   pending_titles="2026-10 Cumulative Update for Windows 11 Version 25H2 for x64-based "
                                                  "Systems (KB5070112)",
                                   left_out="Contoso Display Driver Update (31.0.101.5590) (Drivers)"),
        "lab-pi" + DOMAIN: host("Debian 12", last=now - 4 * DAY),
        "backup" + DOMAIN: host("Ubuntu 22.04", release_upgrade="24.04"),
        "legacy" + DOMAIN: host("CentOS 7", policy="none", groups=["report_only"]),
        SERVER: host("Ubuntu 24.04"),
    }


class _Fixed:
    """A device driver stand-in that reports a fixed status."""

    def __init__(self, st: DeviceStatus):
        self.st = st

    def status(self, check: bool = False) -> DeviceStatus:
        return self.st


class DemoJobs(JobManager):
    def start(self, action: str, hosts: list[str], user: str, work: Callable[[IO[str]], int]) -> Job:
        raise JobError("demo mode: nothing is changed")

    def upgrading(self, host: str) -> bool:
        return False


def build(state: Path, bind: str = "127.0.0.1", port: int = 8790) -> Config:
    return Config(site=Site(name="Example Co.", url=f"http://{bind}:{port}", domain=DOMAIN),
                  paths=Paths(code=str(Path(__file__).resolve().parent.parent), state=str(state)),
                  web=Web(bind=bind, port=port), auth=Auth(method="none"),
                  controller=Controller(host=SERVER), devices=[d for d, _ in DEVICES])


# Machine settings, as if set in the dashboard: a default install time and a few machines with their own.
DEFAULTS = {"patchyard_update_time": "03:00"}
MACHINE_SETTINGS = {
    "web1" + DOMAIN: {"patchyard_reboot_blackout": {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "08:00",
                                                    "end": "18:00", "reason": "office hours"}},
    "db1" + DOMAIN: {"patchyard_manual_only": {"reason": "database primary; patched in the Sunday window"},
                     "patchyard_hold_packages": {"packages": ["postgresql-"], "reason": "major upgrades are planned"}},
}


def write_data(cfg: Config, now: float) -> dict[str, Any]:
    """The inventory, machine settings and status.json, as enrollment, the dashboard and a check
    would leave them."""
    raw = machines(now)
    managed = [h for h, r in raw.items() if "managed" in r["groups"]]
    report = [h for h, r in raw.items() if "report_only" in r["groups"]]
    cfg.inventory_file.write_text("[managed]\n" + "\n".join(managed) + "\n\n[report_only]\n" + "\n".join(report) + "\n")
    msettings.update(cfg, None, DEFAULTS)
    for host, values in MACHINE_SETTINGS.items():
        msettings.update(cfg, host, values)
    for host, r in raw.items():                  # what status.yml would read back from the inventory
        merged = {**DEFAULTS, **MACHINE_SETTINGS.get(host, {})}
        r["policy_vars"] = {k: v for k, v in merged.items() if k in POLICY_VARS}
    memory = {"build1" + DOMAIN: {"registry": now, "ci-runner": now - 3600}}   # ci-runner has stopped
    assessed = {h: status.assess_host(cfg, h, r, memory.get(h, {}), now) for h, r in raw.items()}
    rows = []
    for dev, st in DEVICES:
        st, items, _ = status.check_device(cfg, dev, {}, now, driver=_Fixed(st))
        assessed[dev.name] = items
        rows.append(status.export_device(dev, st, items, now))
    data = status.export(cfg, {**raw, "_time": now}, assessed, {}, now, rows)
    (cfg.state / "www").mkdir(parents=True, exist_ok=True)
    (cfg.state / "www" / "status.json").write_text(json.dumps(data, indent=1))
    return data


def add_sample_job(jobs: JobManager, now: float) -> Job:
    jid = time.strftime("%Y%m%d-%H%M%S", time.localtime(now - 600)) + "-patch-1"
    log = jobs.cfg.state / "logs" / "jobs" / f"{jid}.log"
    log.write_text(PATCH_LOG)
    job = Job(jid, "patch", ["web2" + DOMAIN], "demo", log, start=now - 600, end=now - 540, rc=0)
    jobs.jobs[jid] = job
    return job


def make_app(state: Path, bind: str = "127.0.0.1", port: int = 8790) -> web.App:
    # A real configuration file in the temporary directory, so Settings can save next to it.
    conf = state / "patchyard.toml"
    conf.write_text(toml_dumps(build(state, bind, port)))
    cfg = configmod.load(conf)
    now = time.time()
    write_data(cfg, now)
    app = web.App(cfg, conf)
    app.jobs = DemoJobs(cfg)
    app.anonymous = "demo"
    add_sample_job(app.jobs, now)
    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="patchyard demo", description="A demo dashboard with made-up machines.")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="patchyard-demo-") as tmp:
        app = make_app(Path(tmp), a.bind, a.port)
        try:
            server = ThreadingHTTPServer((a.bind, a.port), web.make_handler(app))
        except OSError as e:
            print(f"cannot listen on {a.bind}:{a.port}: {e} (try --port)", file=sys.stderr)
            return 1
        print(f"Patchyard demo with made-up machines: http://{a.bind}:{a.port}/   (Ctrl+C stops it)")
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
    return 0
