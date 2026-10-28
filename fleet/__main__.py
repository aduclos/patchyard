"""Command line: python -m fleet {web|check|config} ..."""
from __future__ import annotations

import sys

from .config import ConfigError, load


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"
    try:
        if cmd == "web":
            from .web import serve
            serve(load())
            return 0
        if cmd == "check":
            from .status import main as check
            return check(argv)
        if cmd == "config":                     # validate and print the effective configuration
            import dataclasses
            import json
            print(json.dumps(dataclasses.asdict(load()), indent=2))
            return 0
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    print("usage: python -m fleet {web | check [--if-due] [--no-push] | config}", file=sys.stderr)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())
