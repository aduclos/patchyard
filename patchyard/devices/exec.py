"""Anything else: your own commands. The escape hatch for gear without a built-in driver.

Options:
  status_command   REQUIRED. Prints one JSON object on stdout:
                   {"model": "...", "version": "...", "latest": "..." | null,
                    "update_available": true | false | null, "needs_reboot": false, "notes": ["..."]}
  update_command   optional; enables Update. Its output goes to the job log; exit 0 = success.
  reboot_command   optional; enables Reboot.
Commands are run with a shell, as the service account, with PATCHYARD_DEVICE (the device
name), PATCHYARD_URL (url) and PATCHYARD_CREDENTIALS (the credentials file path, if any)
in the environment. They come from the site configuration only, never from the web.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import IO, Any

from . import DeviceError, DeviceStatus, Driver


class Exec(Driver):
    supports_update = True          # narrowed per device by capabilities()
    supports_reboot = True
    title = "Your own commands"
    fields = ()
    web_ok = False                  # runs commands: configuration file only

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        if not self.dev.options.get("status_command"):
            raise DeviceError("exec: options.status_command is required")

    def _env(self) -> dict[str, str]:
        return dict(os.environ, PATCHYARD_DEVICE=self.dev.name, PATCHYARD_URL=self.dev.url,
                    PATCHYARD_CREDENTIALS=self.dev.credentials_file)

    def status(self, check: bool = False) -> DeviceStatus:
        r = subprocess.run(self.dev.options["status_command"], shell=True, capture_output=True, text=True,
                           timeout=120, env=self._env())
        if r.returncode != 0:
            raise DeviceError(f"status_command failed (exit {r.returncode}): {r.stderr.strip()[:200]}")
        try:
            d = json.loads(r.stdout)
        except ValueError:
            raise DeviceError("status_command did not print a JSON object") from None
        return DeviceStatus(model=str(d.get("model", "")), version=str(d.get("version", "")),
                            latest=d.get("latest"), update_available=d.get("update_available"),
                            needs_reboot=bool(d.get("needs_reboot")), notes=[str(n) for n in d.get("notes", [])])

    def _run(self, key: str, log: IO[str]) -> int:
        cmd = self.dev.options.get(key)
        if not cmd:
            raise DeviceError(f"exec: options.{key} is not configured")
        log.write(f"$ {key}\n")
        log.flush()
        with subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              env=self._env()) as proc:
            assert proc.stdout is not None
            for line in proc.stdout:            # stream into the job log as it happens
                log.write(line)
                log.flush()
            return proc.wait(timeout=7200)

    def update(self, log: IO[str]) -> int:
        return self._run("update_command", log)

    def reboot(self, log: IO[str]) -> int:
        return self._run("reboot_command", log)
