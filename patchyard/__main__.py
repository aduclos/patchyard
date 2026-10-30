"""Command line: patchyard {web | check | config | user | token | mcp} ... (or python -m patchyard)"""
from __future__ import annotations

import sys
from pathlib import Path

from .config import ConfigError, load


def accounts_cli(cmd: str, argv: list[str]) -> int:
    """Local users and API tokens from the command line (run as the service account)."""
    import getpass

    from .accounts import AccountError, Tokens, Users
    cfg = load()
    sub, name = [*argv, "", ""][:2]
    try:
        if cmd == "user":
            users = Users(cfg.state)
            if sub == "list":
                print("\n".join(users.names()) or "(no users)")
            elif sub in ("add", "passwd") and name:
                pw = getpass.getpass(f"password for {name}: ")
                if pw != getpass.getpass("again: "):
                    print("passwords differ", file=sys.stderr)
                    return 1
                users.set(name, pw, create=(sub == "add"))
                print(f"{'added' if sub == 'add' else 'password changed for'} {name}")
            elif sub == "remove" and name:
                users.remove(name)
                print(f"removed {name}")
            else:
                print("usage: python -m patchyard user add|passwd|remove NAME | user list", file=sys.stderr)
                return 2
            return 0
        tokens = Tokens(cfg.state)
        if sub == "create" and name:
            print(tokens.create(name, "cli"))
            print("(shown once; store it in your MCP client or script)", file=sys.stderr)
        elif sub == "list":
            for t in tokens.public():
                print(f"{t['id']}  {t['name']}  user={t['user']}  last_used={t['last_used']}")
        elif sub == "revoke" and name:
            tokens.revoke(name)
            print(f"revoked {name}")
        else:
            print("usage: python -m patchyard token create NAME | token list | token revoke ID", file=sys.stderr)
            return 2
        return 0
    except AccountError as e:
        print(e, file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"
    try:
        if cmd == "web":
            from .config import config_path
            from .web import serve
            # No configuration yet: start the first-run setup wizard instead of failing.
            serve(load() if Path(config_path()).exists() else None)
            return 0
        if cmd == "check":
            from .status import main as check
            return check(argv)
        if cmd == "mcp":                         # MCP server on stdio (talks to a dashboard's API)
            from .mcp import main as mcp_main
            return mcp_main()
        if cmd in ("user", "token"):
            return accounts_cli(cmd, argv)
        if cmd == "demo":                       # the dashboard with made-up machines (no config needed)
            from .demo import main as demo_main
            return demo_main(argv)
        if cmd == "config":                     # validate and print the effective configuration
            import dataclasses
            import json
            print(json.dumps(dataclasses.asdict(load()), indent=2))
            return 0
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    print("usage: python -m patchyard {web | check [--if-due] [--no-push] | config |\n"
          "                               user add|passwd|remove|list [NAME] | token create NAME|list|revoke ID |\n"
          "                               mcp | demo [--port N]}",
          file=sys.stderr)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())
