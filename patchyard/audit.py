"""Who changed what in the web UI: settings, machine settings and network devices.

One JSON object per line in <state>/logs/audit.log: {"time", "user", "what", "changes"}.
Secrets are never written; a changed password or token appears as "(changed)".
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _path(state: Path) -> Path:
    return state / "logs" / "audit.log"


def record(state: Path, user: str, what: str, changes: dict[str, Any]) -> None:
    path = _path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"time": time.time(), "user": user, "what": what, "changes": changes}, sort_keys=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    with os.fdopen(fd, "a") as fh:
        fh.write(line + "\n")


def recent(state: Path, n: int = 100) -> list[dict[str, Any]]:
    """The newest `n` entries, newest first."""
    try:
        lines = _path(state).read_text().splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
