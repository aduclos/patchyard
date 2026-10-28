"""Per-machine policy, read from inventory variables (host_vars / group_vars).

All optional; a machine with none of them gets the defaults (patch and reboot
whenever asked, hold nothing back). Each policy carries a human `reason` that the
web UI shows where the policy applies, so the tool itself never needs to know
why a particular machine is special.

  fleet_reboot_blackout:      # no reboots during these hours (UTC) unless overridden
    hours_utc: [7, 8, 9, 10, 11]
    reason: "nightly batch jobs"

  fleet_manual_only:          # never included by "patch all"; only when picked by name
    reason: "database primary; patch only in a planned window"

  fleet_hold_packages:        # never installed by automatic updates (regex prefixes, apt style)
    packages: ["postgresql-", "libpq"]
    reason: "major database upgrades are planned by hand"

  fleet_track_containers: false   # do not alert on containers that stop (default true);
                                  # e.g. where the service account cannot list them
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VARS = ("fleet_reboot_blackout", "fleet_manual_only", "fleet_hold_packages", "fleet_track_containers")


class PolicyError(ValueError):
    """A fleet_* inventory variable is malformed."""


@dataclass(frozen=True)
class RebootBlackout:
    hours_utc: tuple[int, ...] = ()
    reason: str = ""

    def active(self, hour_utc: int) -> bool:
        return hour_utc in self.hours_utc


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
                "hours_utc": list(self.reboot_blackout.hours_utc), "reason": self.reboot_blackout.reason},
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


def parse(hostvars: dict[str, Any]) -> Policy:
    """Build a Policy from one machine's inventory variables.

    Malformed values do not raise: they are reported in `problems` (shown on the
    dashboard) and the policy is treated as absent, so one typo in host_vars cannot
    stop the whole fleet check.
    """
    problems: list[str] = []
    blackout = manual = None
    holds: tuple[str, ...] = ()
    hold_reason = ""

    raw = hostvars.get("fleet_reboot_blackout")
    if raw is not None:
        try:
            if not isinstance(raw, dict):
                raise PolicyError("fleet_reboot_blackout must be a mapping")
            hours = raw.get("hours_utc", [])
            if not isinstance(hours, list) or not all(isinstance(h, int) and 0 <= h <= 23 for h in hours):
                raise PolicyError("fleet_reboot_blackout.hours_utc must be a list of hours 0-23")
            blackout = RebootBlackout(tuple(sorted(set(hours))), _reason(raw, "fleet_reboot_blackout"))
        except PolicyError as e:
            problems.append(str(e))

    raw = hostvars.get("fleet_manual_only")
    if raw is not None:
        try:
            if raw is True:
                manual = ""
            elif isinstance(raw, dict):
                manual = _reason(raw, "fleet_manual_only")
            elif raw is not False:
                raise PolicyError("fleet_manual_only must be a mapping with a reason (or true)")
        except PolicyError as e:
            problems.append(str(e))

    raw = hostvars.get("fleet_hold_packages")
    if raw is not None:
        try:
            if not isinstance(raw, dict):
                raise PolicyError("fleet_hold_packages must be a mapping")
            pkgs = raw.get("packages", [])
            if not isinstance(pkgs, list) or not all(isinstance(p, str) and p for p in pkgs):
                raise PolicyError("fleet_hold_packages.packages must be a list of package name patterns")
            holds, hold_reason = tuple(pkgs), _reason(raw, "fleet_hold_packages")
        except PolicyError as e:
            problems.append(str(e))

    track = hostvars.get("fleet_track_containers", True)
    if not isinstance(track, bool):
        problems.append("fleet_track_containers must be true or false")
        track = True

    return Policy(blackout, manual, holds, hold_reason, track, tuple(problems))
