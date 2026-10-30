"""Per-machine policy, read from inventory variables (host_vars / group_vars).

All optional; a machine with none of them gets the defaults (patch and reboot
whenever asked, hold nothing back). Each policy carries a human `reason` that the
web UI shows where the policy applies, so the tool itself never needs to know
why a particular machine is special.

  patchyard_reboot_blackout:  # no reboots in this window (the machine's local time) unless overridden
    days: [mon, tue, wed, thu, fri]   # optional; default every day
    start: "08:00"
    end: "18:00"                      # an end before the start runs past midnight
    reason: "office hours"
    # (older form, still accepted: hours_utc: [7, 8, 9] for whole UTC hours)

  patchyard_manual_only:          # never included by "patch all"; only when picked by name
    reason: "database primary; patch only in a planned window"

  patchyard_hold_packages:        # never installed by automatic updates (regex prefixes, apt style)
    packages: ["postgresql-", "libpq"]
    reason: "major database upgrades are planned by hand"

  patchyard_track_containers: false   # do not alert on containers that stop (default true);
                                  # e.g. where the service account cannot list them
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

VARS = ("patchyard_reboot_blackout", "patchyard_manual_only", "patchyard_hold_packages", "patchyard_track_containers")


class PolicyError(ValueError):
    """A patchyard_* inventory variable is malformed."""


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class RebootBlackout:
    days: tuple[str, ...] = DAYS
    start: str = ""                # "HH:MM", the machine's local time
    end: str = ""
    hours_utc: tuple[int, ...] = ()  # the older form: whole UTC hours
    reason: str = ""

    @property
    def text(self) -> str:
        if self.hours_utc:
            return "UTC hours " + ", ".join(f"{h:02d}:00" for h in self.hours_utc)
        return f"{days_text(self.days)} {self.start}\u2013{self.end}"


def days_text(days: tuple[str, ...] | list[str]) -> str:
    names = [d.capitalize() for d in DAYS]
    picked = [i for i, d in enumerate(DAYS) if d in days]
    if len(picked) == 7:
        return "every day"
    if picked and picked == list(range(picked[0], picked[-1] + 1)) and len(picked) > 2:
        return f"{names[picked[0]]}\u2013{names[picked[-1]]}"
    return ", ".join(names[i] for i in picked)


@dataclass(frozen=True)
class Policy:
    reboot_blackout: RebootBlackout | None = None
    manual_only_reason: str | None = None
    hold_packages: tuple[str, ...] = ()
    hold_reason: str = ""
    track_containers: bool = True
    problems: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        """Shape exported to the web UI (status.json)."""
        return {
            "reboot_blackout": None if not self.reboot_blackout else {
                "days": list(self.reboot_blackout.days), "start": self.reboot_blackout.start,
                "end": self.reboot_blackout.end, "hours_utc": list(self.reboot_blackout.hours_utc),
                "reason": self.reboot_blackout.reason, "text": self.reboot_blackout.text},
            "manual_only": self.manual_only_reason,
            "hold": None if not self.hold_packages else {
                "packages": list(self.hold_packages), "reason": self.hold_reason},
            "track_containers": self.track_containers,
            "problems": list(self.problems),
        }


def _reason(value: dict[str, Any], name: str) -> str:
    r = value.get("reason", "")
    if not isinstance(r, str):
        raise PolicyError(f"{name}.reason must be text")
    return r


def parse_blackout(raw: Any) -> RebootBlackout | None:
    """A patchyard_reboot_blackout value; {} or null means no window. PolicyError when malformed."""
    name = "patchyard_reboot_blackout"
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise PolicyError(f"{name} must be a mapping")
    reason = _reason(raw, name)
    if "hours_utc" in raw:
        hours = raw["hours_utc"]
        if not isinstance(hours, list) or not all(isinstance(h, int) and 0 <= h <= 23 for h in hours):
            raise PolicyError(f"{name}.hours_utc must be a list of hours 0-23")
        return RebootBlackout(hours_utc=tuple(sorted(set(hours))), reason=reason) if hours else None
    start, end, days = raw.get("start"), raw.get("end"), raw.get("days", list(DAYS))
    if not (isinstance(start, str) and TIME_RE.match(start) and isinstance(end, str) and TIME_RE.match(end)):
        raise PolicyError(f"{name}: start and end are times like 08:00")
    if start == end:
        raise PolicyError(f"{name}: start and end are the same")
    if not isinstance(days, list) or not days or not set(days) <= set(DAYS):
        raise PolicyError(f"{name}.days: some of {', '.join(DAYS)}")
    return RebootBlackout(tuple(d for d in DAYS if d in days), start, end, (), reason)


def parse(hostvars: dict[str, Any]) -> Policy:
    """Build a Policy from one machine's inventory variables.

    Malformed values do not raise: they are reported in `problems` (shown on the
    dashboard) and the policy is treated as absent, so one typo in host_vars cannot
    stop the whole check.
    """
    problems: list[str] = []
    blackout = manual = None
    holds: tuple[str, ...] = ()
    hold_reason = ""

    raw = hostvars.get("patchyard_reboot_blackout")
    if raw is not None:
        try:
            blackout = parse_blackout(raw)
        except PolicyError as e:
            problems.append(str(e))

    raw = hostvars.get("patchyard_manual_only")
    if raw is not None:
        try:
            if raw is True:
                manual = ""
            elif isinstance(raw, dict):
                manual = _reason(raw, "patchyard_manual_only")
            elif raw is not False:
                raise PolicyError("patchyard_manual_only must be a mapping with a reason (or true)")
        except PolicyError as e:
            problems.append(str(e))

    raw = hostvars.get("patchyard_hold_packages")
    if raw is not None:
        try:
            if not isinstance(raw, dict):
                raise PolicyError("patchyard_hold_packages must be a mapping")
            pkgs = raw.get("packages", [])
            if not isinstance(pkgs, list) or not all(isinstance(p, str) and p for p in pkgs):
                raise PolicyError("patchyard_hold_packages.packages must be a list of package name patterns")
            holds, hold_reason = tuple(pkgs), _reason(raw, "patchyard_hold_packages")
        except PolicyError as e:
            problems.append(str(e))

    track = hostvars.get("patchyard_track_containers", True)
    if not isinstance(track, bool):
        problems.append("patchyard_track_containers must be true or false")
        track = True

    return Policy(blackout, manual, holds, hold_reason, track, tuple(problems))
