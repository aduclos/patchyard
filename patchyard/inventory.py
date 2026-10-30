"""The Ansible inventory (INI format) in the state directory.

Only the managed group (config `inventory.managed_group`) is ever edited, and only
by adding or removing whole host lines; comments, other groups and variables are
kept exactly as written. Every edit leaves a timestamped backup next to the file.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


class InventoryError(ValueError):
    pass


def valid_host(name: str) -> bool:
    return len(name) <= 253 and bool(HOST_RE.match(name))


def _section(lines: list[str], group: str) -> tuple[int, int]:
    header = f"[{group}]"
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == header)
    except StopIteration:
        raise InventoryError(f"group [{group}] not found in the inventory") from None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    return start, end


def hosts(path: Path, group: str) -> list[str]:
    lines = path.read_text().split("\n")
    start, end = _section(lines, group)
    out = []
    for line in lines[start + 1:end]:
        t = line.strip()
        if t and not t.startswith(("#", ";")):
            out.append(t.split()[0])
    return out


def _write(path: Path, lines: list[str], tag: str) -> Path:
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{tag}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(path)
    return backup


def all_hosts(path: Path) -> list[str]:
    """Every host named in a host section (not :vars or :children), in file order."""
    out: list[str] = []
    in_hosts = True                      # lines before the first header are ungrouped hosts
    for line in path.read_text().split("\n"):
        t = line.strip()
        if t.startswith("["):
            in_hosts = not t.rstrip("]").endswith((":vars", ":children"))
        elif t and in_hosts and not t.startswith(("#", ";")):
            name = t.split()[0]
            if name not in out:
                out.append(name)
    return out


def add(path: Path, group: str, host: str) -> Path:
    """Append `host` to `group` (after its last non-blank line). Returns the backup path."""
    if not valid_host(host):
        raise InventoryError(f"not a valid host name: {host!r}")
    if host in hosts(path, group):
        raise InventoryError(f"{host} is already in [{group}]")
    lines = path.read_text().split("\n")
    start, end = _section(lines, group)
    last = max(i for i in range(start, end) if lines[i].strip())
    lines.insert(last + 1, host)
    return _write(path, lines, f"add-{host}")


def remove(path: Path, group: str, host: str) -> Path:
    lines = path.read_text().split("\n")
    start, end = _section(lines, group)
    keep = [line for i, line in enumerate(lines)
            if not (start < i < end and line.strip().split()[:1] == [host])]
    if len(keep) == len(lines):
        raise InventoryError(f"{host} is not in [{group}]")
    return _write(path, keep, f"remove-{host}")
