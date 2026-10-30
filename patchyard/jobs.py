"""Background jobs started from the web UI: playbook runs, upgrade watching, enrollment.

Jobs touching different machines run in parallel; a second job on a busy machine
is refused. Each job writes one log file (<state>/logs/jobs/<id>.log) that the UI
tails. Job history is kept in memory only (it resets when the service restarts).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from . import devices, ssh
from .config import Config

PLAYBOOKS = {
    "patch": "patch.yml",
    "reboot": "reboot.yml",
    "auto-updates": "auto-updates.yml",
    "release-upgrade": "release-upgrade.yml",
    "cancel-upgrade": "release-upgrade.yml",
}
SINGLE_HOST = {"release-upgrade", "cancel-upgrade", "watch-upgrade", "enroll", "unenroll"}
READ_ONLY = {"refresh", "watch-upgrade"}        # never block a machine

# Follow a release upgrade's log over SSH until the upgrader exits. Upgrades started by
# the tool log to patchyard-release-upgrade.log; one started by hand only has main.log.
WATCH_SCRIPT = r"""
F=/var/log/patchyard-release-upgrade.log; M=/var/log/dist-upgrade/main.log
if systemctl is-active -q patchyard-release-upgrade || { [ -e $F ] && [ $F -nt $M ]; }; then L=$F; else L=$M; fi
echo "=== following $L on $(hostname)"
tail -n {start} -F $L 2>/dev/null & T=$!
sleep 3
while pgrep -f 'ubuntu-release-upgrade[r]-' >/dev/null || systemctl is-active -q patchyard-release-upgrade
do sleep 5; done
sleep 3; kill $T 2>/dev/null
echo "=== the upgrader is no longer running"
. /etc/os-release; echo "=== now: $PRETTY_NAME"
[ -e /var/run/reboot-required ] && echo "=== REBOOT REQUIRED to finish"
true
"""


class JobError(Exception):
    """A job could not be started; the message is safe to show."""


@dataclass
class Job:
    id: str
    action: str
    hosts: list[str]
    user: str
    log: Path
    start: float = field(default_factory=time.time)
    end: float | None = None
    rc: int | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "action": self.action, "hosts": self.hosts, "user": self.user,
                "start": self.start, "end": self.end, "rc": self.rc}


class JobManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._seq = 0
        (cfg.state / "logs" / "jobs").mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- registry
    def running(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.end is None]

    def recent(self, n: int = 20) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.start, reverse=True)[:n]

    def start(self, action: str, hosts: list[str], user: str, work: Callable[[IO[str]], int]) -> Job:
        with self._lock:
            if action not in READ_ONLY:
                busy = {h for j in self.running() if j.action not in READ_ONLY for h in j.hosts}
                clash = set(hosts) & busy
                if clash:
                    raise JobError("already busy: " + ", ".join(sorted(h.split(".")[0] for h in clash)))
            elif any(j.action == action and j.hosts == sorted(hosts) for j in self.running()):
                raise JobError("that is already running (see Activity)")
            self._seq += 1
            jid = f"{time.strftime('%Y%m%d-%H%M%S')}-{action}-{self._seq}"
            job = Job(jid, action, sorted(hosts), user, self.cfg.state / "logs" / "jobs" / f"{jid}.log")
            job.log.touch(mode=0o640)
            self.jobs[jid] = job
        threading.Thread(target=self._run, args=(job, work), daemon=True).start()
        return job

    def _run(self, job: Job, work: Callable[[IO[str]], int]) -> None:
        rc = 99
        try:
            with open(job.log, "a", buffering=1) as log:
                log.write(f"started by {job.user}\n")
                rc = work(log)
                log.write(f"\n=== finished rc={rc}\n")
        except Exception as e:                  # never leave a job "running" forever
            with open(job.log, "a") as log:
                log.write(f"\n=== internal error: {e}\n")
        finally:
            job.rc, job.end = rc, time.time()

    def read(self, jid: str, offset: int, limit: int = 256 * 1024) -> dict[str, Any] | None:
        job = self.jobs.get(jid)
        if not job:
            return None
        with open(job.log, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(limit)
        return {"text": chunk.decode(errors="replace"), "offset": offset + len(chunk),
                "done": job.end is not None, "rc": job.rc}

    # ---------------------------------------------------------------- work units
    def engine_vars(self) -> list[str]:
        return ["-e", f"patchyard_managed_group={self.cfg.inventory.managed_group}",
                "-e", f"patchyard_controller={self.cfg.controller.host}"]

    def playbook(self, log: IO[str], name: str, hosts: list[str], extra: list[str]) -> int:
        ansible = self.cfg.code / "ansible"
        cmd = [str(ansible / ".venv/bin/ansible-playbook"), f"playbooks/{name}", "--limit", ",".join(hosts),
               *self.engine_vars(), *extra]
        log.write(f"\n$ ansible-playbook {name} --limit {','.join(hosts)} {' '.join(extra)}\n")
        log.flush()
        env = dict(os.environ, ANSIBLE_FORCE_COLOR="0", PYTHONUNBUFFERED="1", PATCHYARD_STATE=str(self.cfg.state))
        return subprocess.run(cmd, cwd=ansible, stdout=log, stderr=subprocess.STDOUT, env=env).returncode

    def check(self, log: IO[str], push: bool = True) -> int:
        log.write("\n$ patchyard check\n")
        log.flush()
        cmd = [sys.executable, "-m", "patchyard", "check"] + ([] if push else ["--no-push"])
        return subprocess.run(cmd, cwd=self.cfg.code, stdout=log, stderr=subprocess.STDOUT).returncode

    def watch_upgrade(self, log: IO[str], host: str, start: str = "60") -> int:
        log.write(f"\n$ follow release upgrade on {host.split('.')[0]}\n")
        log.flush()
        return subprocess.run(ssh.as_service(self.cfg, host, WATCH_SCRIPT.replace("{start}", start)),
                              stdout=log, stderr=subprocess.STDOUT).returncode

    def upgrading(self, host: str) -> bool:
        r = ssh.run_as_service(self.cfg, host, "pgrep -f 'ubuntu-release-upgrade[r]-' >/dev/null || "
                                               "systemctl is-active -q patchyard-release-upgrade", timeout=30)
        return r.returncode == 0

    def action(self, action: str, hosts: list[str], opts: dict[str, Any]) -> Callable[[IO[str]], int]:
        """The work function for a playbook action."""
        extra: list[str] = []
        if action == "release-upgrade":
            extra += ["-e", "mode=start"]
        elif action == "cancel-upgrade":
            extra += ["-e", "mode=cancel"]
        elif action == "patch":
            # Picking machines by name in the UI is the explicit opt-in for patchyard_manual_only.
            extra += ["-e", f"reboot_ok={'true' if opts.get('reboot') else 'false'}", "-e", "patchyard_explicit=true"]
        elif action == "reboot" and opts.get("force"):
            extra += ["-e", "ignore_blackout=true"]

        ctl, cmd = self.cfg.controller.host, self.cfg.controller.patch_command
        own = action == "patch" and ctl in hosts and bool(cmd)
        others = [h for h in hosts if not (own and h == ctl)]

        def work(log: IO[str]) -> int:
            rc = self.playbook(log, PLAYBOOKS[action], others, extra) if others else 0
            if own:                             # last, so it cannot interrupt the others
                rc = self.patch_controller(log) or rc
            if action == "release-upgrade" and rc == 0:
                log.write("\nUpgrade launched; it keeps running if this page or the service goes away.\n")
                rc = self.watch_upgrade(log, hosts[0], "+1")
            self.check(log)
            return rc
        return work

    def device_action(self, name: str, action: str) -> Callable[[IO[str]], int]:
        """Work function for a device: check (look for firmware now), update, reboot."""
        dev = next(d for d in self.cfg.devices if d.name == name)

        def work(log: IO[str]) -> int:
            try:
                drv = devices.driver_for(dev)          # reads the credentials file: a DeviceError too
                if action == "check":
                    st = drv.status(check=True)
                    log.write(f"{name}: {st.model} {st.version}; latest {st.latest or 'unknown'}; "
                              f"update available: {st.update_available}\n")
                    rc = 0
                elif action == "update":
                    rc = drv.update(log)
                else:
                    rc = drv.reboot(log)
            except devices.DeviceError as e:
                log.write(f"!! {name}: {e}\n")
                rc = 1
            self.check(log)
            return rc
        return work

    def patch_controller(self, log: IO[str]) -> int:
        cmd = self.cfg.controller.patch_command
        log.write(f"\n$ {' '.join(cmd)}    (patches {self.cfg.controller.host}, the Patchyard server)\n")
        log.flush()
        rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        log.write(f"{self.cfg.controller.host}: patch command finished rc={rc}\n")
        return rc

    def reboot_controller(self, log: IO[str]) -> int:
        cmd = self.cfg.controller.reboot_command
        log.write(f"controller reboot requested; running {' '.join(cmd)} in 5 s\n")
        log.flush()
        time.sleep(5)
        return subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
