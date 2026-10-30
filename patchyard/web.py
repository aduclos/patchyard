"""HTTP: the web app, its data, and the action API.

  python -m patchyard web

Routes
  GET  /login                  sign-in page (no session needed)
  GET  /api/auth               204 with a session, else 302 /login (for a proxy's forward_auth)
  GET  /, /app.js, ...         the web app (if web.serve_static); /data/status.json from the state dir
  GET  /api/state | /api/settings | /api/job/<id>?offset=N
  POST /api/login | /api/logout | /api/refresh | /api/snooze | /api/settings
       /api/run | /api/watch | /api/enroll | /api/unenroll | /api/reboot-controller

Every route except /login, /api/login and /api/auth needs a session (unless
auth.method = "none"). POSTs must be JSON, carry X-Patchyard: 1 and, when an Origin
header is present, come from site.url: together that stops cross-site requests.
Request bodies are never logged.
"""
from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from . import __version__, audit, devices, enroll, inventory, join, machines, notify, ssh
from . import config as configmod
from .accounts import AccountError, Tokens, Users
from .auth import Authenticator, AuthError, RateLimiter
from .config import Config
from .devices import store as devstore
from .jobs import PLAYBOOKS, SINGLE_HOST, JobError, JobManager

log = logging.getLogger("patchyard.web")
COOKIE = "patchyard_session"
JOIN_SCRIPT = re.compile(r"/join/([\w-]{1,120})/(linux|windows)")
JOIN_DONE = re.compile(r"/join/([\w-]{1,120})/done")
SNOOZE_KEY = re.compile(r"^[\w.:-]{1,200}$")


class Sessions:
    def __init__(self, ttl_s: float):
        self.ttl = ttl_s
        self._s: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def create(self, user: str) -> str:
        tok = secrets.token_urlsafe(32)
        with self._lock:
            self._s[tok] = (user, time.time() + self.ttl)
        return tok

    def user(self, tok: str | None) -> str | None:
        now = time.time()
        with self._lock:
            for t in [t for t, (_, exp) in self._s.items() if exp < now]:
                del self._s[t]
            hit = self._s.get(tok or "")
        return hit[0] if hit else None

    def drop(self, tok: str | None) -> None:
        with self._lock:
            self._s.pop(tok or "", None)


class App:
    """Everything the request handler needs; one instance per process."""

    def __init__(self, cfg: Config, config_file: str | os.PathLike[str] | None = None):
        self.cfg = cfg
        self.config_file = Path(config_file or configmod.config_path())   # where Settings saves next to
        self.auth = Authenticator(cfg)
        self.sessions = Sessions(cfg.web.session_hours * 3600)
        self.jobs = JobManager(cfg)
        self.static = cfg.code / "www"
        self.data = cfg.state / "www"
        machines.ensure(cfg)                    # an inventory source in ansible.cfg; must exist
        self.join_limiter = RateLimiter(20, 600, 600)   # bad join tokens: 20 in 10 min, then 10 min off
        self.anonymous = "anonymous"            # the user name shown when auth.method = "none"
        self._last_refresh = 0.0

    def reload(self, cfg: Config) -> None:
        """Apply a changed configuration to the running service: no restart needed."""
        self.cfg = cfg
        self.auth.cfg = cfg
        lim = self.auth.limiter
        lim.max_failures = cfg.auth.max_failures
        lim.window_s, lim.lockout_s = cfg.auth.failure_window_minutes * 60, cfg.auth.lockout_minutes * 60
        self.sessions.ttl = cfg.web.session_hours * 3600
        self.jobs.cfg = cfg

    def managed(self) -> set[str]:
        c = self.cfg
        hosts = set(inventory.hosts(c.inventory_file, c.inventory.managed_group))
        if c.controller.host and c.controller.patch_command:
            hosts.add(c.controller.host)        # patched with its own command
        return hosts


def secret_path(cfg: Config, key: str) -> Path:
    """Where a secret typed into Settings is kept: notify.smtp.password_file -> secrets/notify-smtp-password."""
    return cfg.state / "secrets" / key.removesuffix("_file").replace(".", "-")


def write_private(path: Path, text: str) -> None:
    """Write a file only the service account can read (its directory is created if needed)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    def cfg() -> Config:
        return app.cfg

    class Handler(BaseHTTPRequestHandler):
        server_version = "patchyard"

        # ------------------------------------------------------------ plumbing
        def log_message(self, fmt: str, *args: Any) -> None:          # request line only
            log.info("%s %s", self.headers.get("X-Forwarded-For", self.client_address[0]), fmt % args)

        def send(self, code: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def json(self, code: int, obj: Any, headers: dict[str, str] | None = None) -> None:
            self.send(code, json.dumps(obj).encode(), "application/json", headers)

        def token(self) -> str | None:
            c = SimpleCookie()
            try:
                c.load(self.headers.get("Cookie", ""))
            except Exception:
                return None
            m = c.get(COOKIE)
            return m.value if m else None

        def user(self) -> str | None:
            if not app.auth.required:
                return app.anonymous
            authz = self.headers.get("Authorization", "")
            if authz.startswith("Bearer "):                 # API token (scripts, the MCP server)
                return Tokens(cfg().state).user_for(authz[7:].strip())
            return app.sessions.user(self.token())

        def cookie(self, tok: str, max_age: int) -> dict[str, str]:
            secure = "; Secure" if cfg().site.url.startswith("https://") else ""
            return {"Set-Cookie": f"{COOKIE}={tok}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}"}

        def to_login(self, path: str) -> None:
            nxt = path if path.startswith("/") and not path.startswith("//") else "/"
            self.send(302, b"", "text/plain", {"Location": "/login?next=" + quote(nxt, safe="/")})

        def file(self, root: Path, rel: str) -> None:
            target = (root / rel.lstrip("/")).resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                return self.json(404, {"error": "not found"})
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send(200, target.read_bytes(), ctype)

        # ------------------------------------------------------------ GET
        def text(self, code: int, body: str) -> None:
            self.send(code, body.encode(), "text/plain; charset=utf-8")

        def source(self) -> str:
            """Who is calling, for the audit log (the proxy's forwarded address when there is one)."""
            fwd = self.headers.get("X-Forwarded-For", "")
            return f"{self.client_address[0]} (forwarded for {fwd})" if fwd else self.client_address[0]

        # ------------------------------------------------------------ join (no sign-in: the token is the credential)
        def join_script(self, token: str, os_family: str) -> None:
            rec = None if app.join_limiter.locked_for() else join.JoinTokens(cfg().state).check(token)
            if not rec:
                app.join_limiter.fail()
                return self.text(200, join.refusal_script(os_family))
            make = join.windows_script if os_family == "windows" else join.linux_script
            return self.text(200, make(cfg(), token, rec))

        def join_done(self, token: str) -> None:
            if app.join_limiter.locked_for():
                return self.text(429, "too many failed join attempts; try again in a few minutes\n")
            tokens = join.JoinTokens(cfg().state)
            if not tokens.check(token):
                app.join_limiter.fail()
                return self.text(403, "this join command has expired, been used up or been revoked\n")
            try:
                req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self.text(400, "expected JSON: {\"host\": ..., \"os\": ...}\n")
            host = enroll.normalize_host(cfg(), str(req.get("host", "")))
            os_family = req.get("os")
            if os_family not in ("linux", "windows"):
                return self.text(400, "os must be linux or windows\n")
            if not inventory.valid_host(host) or host.split(".")[0] in ("localhost", "localhost6"):
                return self.text(400, f"this machine calls itself {host or 'nothing'}; give it a proper host name "
                                      "(or add it by name on the dashboard)\n")
            if host.split(".")[0] in {h.split(".")[0] for h in app.managed() | {cfg().controller.host}}:
                return self.text(409, f"{host.split('.')[0]} is already on the dashboard\n")
            try:
                rec = tokens.use(token, host, self.source())
            except join.JoinError as e:
                return self.text(403, f"{e}\n")
            who = f"join:{rec['name']}"
            try:
                job = app.jobs.start("enroll", [host], who, lambda log: enroll.enroll(
                    cfg(), app.jobs, app.auth.limiter, log, host, os_family=os_family, method="join"))
            except JobError as e:
                tokens.release(rec["id"], host)             # not added: the same line can run again
                return self.text(409, f"{e}; run the same line again in a minute\n")
            tokens.attach_job(rec["id"], host, job.id)      # the Add machine wizard follows it
            audit.record(cfg().state, who, f"{host.split('.')[0]} joined", {"host": host, "join command": rec["id"],
                                                                           "from": self.source()})
            return self.text(200, f"Patchyard is adding {host.split('.')[0]}: it appears on the dashboard in a "
                                  f"minute or two (job {job.id}).\n")

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            m = JOIN_SCRIPT.fullmatch(path)
            if m:
                return self.join_script(m.group(1), m.group(2))
            if path == "/login":
                return self.file(app.static, "login.html")
            if path == "/api/auth":
                if self.user():
                    return self.send(204, b"", "text/plain")
                return self.to_login(self.headers.get("X-Forwarded-Uri", "/"))
            user = self.user()
            if not user:
                # API clients (a token, or /api/*) get JSON; only browsers are sent to the sign-in page.
                if path.startswith("/api/") or self.headers.get("Authorization"):
                    return self.json(401, {"error": "sign in first (or the API token is invalid)", "login": True})
                return self.to_login(self.path)
            if not path.startswith("/api/"):
                if not cfg().web.serve_static:
                    return self.json(404, {"error": "static files are served by the web server"})
                if path.startswith("/data/"):
                    return self.file(app.data, path[len("/data/"):])
                return self.file(app.static, "index.html" if path == "/" else path)
            if path == "/api/state":
                return self.json(200, {"running": [j.id for j in app.jobs.running()],
                                       "jobs": [j.public() for j in app.jobs.recent()],
                                       "locked_s": int(app.auth.limiter.locked_for()), "user": user})
            if path == "/api/settings":
                return self.json(200, self.settings_view())
            if path == "/api/users":
                return self.json(200, {"method": cfg().auth.method, "users": Users(cfg().state).names()
                                       if cfg().auth.method == "local" else []})
            if path == "/api/tokens":
                return self.json(200, {"tokens": Tokens(cfg().state).public()})
            if path == "/api/devices":
                return self.json(200, devstore.view(cfg()))
            if path == "/api/join":
                return self.json(200, {"tokens": join.JoinTokens(cfg().state).public(), "url": join.base_url(cfg())})
            if path == "/api/audit":
                return self.json(200, {"entries": audit.recent(cfg().state)})
            if path == "/api/machine-settings":
                host = (parse_qs(urlsplit(self.path).query).get("host") or [None])[0]
                if host is None:
                    return self.json(200, machines.view(cfg()))
                if host not in inventory.all_hosts(cfg().inventory_file):
                    return self.json(404, {"error": f"{host} is not in the inventory"})
                return self.json(200, machines.view(cfg(), host, self.os_of(host)))
            m = re.fullmatch(r"/api/job/([\w-]+)", path)
            q = re.search(r"offset=(\d+)", urlsplit(self.path).query or "")
            if m:
                out = app.jobs.read(m.group(1), int(q.group(1)) if q else 0)
                return self.json(200, out) if out else self.json(404, {"error": "no such job"})
            return self.json(404, {"error": "not found"})

        def os_of(self, host: str) -> str:
            """The OS name the last check reported for `host` ("" if unknown)."""
            try:
                data = json.loads((app.data / "status.json").read_text())
            except (OSError, ValueError):
                return ""
            return next((h.get("os", "") for h in data.get("hosts", []) if h.get("host") == host), "")

        def settings_view(self) -> dict[str, Any]:
            c = cfg()
            try:
                fp = subprocess.run(["ssh-keygen", "-lf", str(c.ssh_key.with_suffix(".pub"))],
                                    capture_output=True, text=True).stdout.split()[1]
            except (OSError, IndexError):
                fp = "?"
            fields = []
            for st in configmod.SETTINGS:
                value = configmod.get(c, st.key)
                fields.append({"key": st.key, "label": st.label, "section": st.section, "kind": st.kind,
                               "help": st.help, "lo": st.lo, "hi": st.hi, "choices": list(st.choices),
                               "show_if": st.show_if, "locked": configmod.locked(c, st.key),
                               # a secret's value never leaves the server: only whether one is set
                               "value": bool(value) if st.kind == "secret" else value})
            return {"sections": configmod.SECTIONS, "fields": fields, "locked": c.web.settings_locked,
                    "service_account": c.service_account.name, "key_fingerprint": fp, "site": c.site.name,
                    "auth_method": c.auth.method, "version": __version__, "config_file": str(app.config_file)}

        # ------------------------------------------------------------ POST
        def do_POST(self) -> None:
            m = JOIN_DONE.fullmatch(urlsplit(self.path).path)
            if m:                                   # a machine reporting in: no session, the token decides
                return self.join_done(m.group(1))
            origin = self.headers.get("Origin")
            if (self.headers.get("X-Patchyard") != "1"
                    or not self.headers.get("Content-Type", "").startswith("application/json")
                    or (origin and origin != cfg().site.url.rstrip("/"))):
                return self.json(403, {"error": "forbidden"})
            try:
                req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 65536)) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self.json(400, {"error": "bad json"})
            path = urlsplit(self.path).path

            if path == "/api/login":
                try:
                    user = app.auth.authenticate(str(req.get("username", "")), str(req.get("password", "")))
                except AuthError as e:
                    return self.json(401, {"error": str(e)})
                ttl = cfg().web.session_hours * 3600
                return self.json(200, {"ok": True}, self.cookie(app.sessions.create(user), ttl))

            user = self.user()
            if not user:
                return self.json(401, {"error": "sign in first", "login": True})
            try:
                handler = ROUTES.get(path)
                if not handler:
                    return self.json(404, {"error": "not found"})
                code, body, headers = handler(self, user, req)
                return self.json(code, body, headers)
            except (JobError, inventory.InventoryError, configmod.ConfigError, AccountError) as e:
                return self.json(409, {"error": str(e)})
            except Exception:                   # a bug: say so instead of dropping the connection
                log.exception("%s failed", path)
                return self.json(500, {"error": "internal error; the service log has the details"})

        # ------------------------------------------------------------ actions
        def r_logout(self, user: str, req: dict[str, Any]) -> tuple[int, Any, dict[str, str] | None]:
            app.sessions.drop(self.token())
            return 200, {"ok": True}, self.cookie("", 0)

        def r_refresh(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            if time.time() - app._last_refresh < 60:
                return 429, {"error": "refreshed less than a minute ago"}, None
            app._last_refresh = time.time()
            job = app.jobs.start("refresh", [], user, app.jobs.check)
            return 200, {"job": job.id}, None

        def r_snooze(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            key, days = str(req.get("key", "")), int(req.get("days", 7))
            if not SNOOZE_KEY.match(key) or not 0 <= days <= 90:
                return 400, {"error": "bad snooze request"}, None
            f = cfg().state / "logs" / "snoozes.json"
            now = time.time()
            try:
                sn = {k: v for k, v in json.loads(f.read_text()).items() if v > now}
            except (OSError, ValueError):
                sn = {}
            if days:
                sn[key] = now + days * 86400
            else:
                sn.pop(key, None)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(sn))
            tmp.replace(f)
            app.jobs.start("refresh-snooze", [], user, lambda log: app.jobs.check(log, push=False))
            return 200, {"ok": True}, None

        def r_settings(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            c = cfg()
            changes: dict[str, Any] = {}
            secrets_new: dict[str, str | None] = {}      # key -> new secret, or None to remove it
            for key, value in req.items():
                st = configmod.SETTING.get(key)
                if st is None:
                    return 400, {"error": f"unknown setting: {key}"}, None
                if st.kind != "secret":
                    changes[key] = value
                elif value is None or (isinstance(value, str) and value):
                    secrets_new[key] = value              # "" leaves the stored secret as it is
                    changes[key] = "" if value is None else str(secret_path(c, key))
            group = changes.get("auth.group")
            if group and app.auth.required and not app.auth.is_member(user, group):
                return 400, {"error": f"you ({user}) are not in {group}; that would lock you out"}, None
            for key, value in secrets_new.items():        # staged next to the target; moved in once valid
                if value is not None:
                    write_private(secret_path(c, key).with_suffix(".new"), value + "\n")
            try:
                new, diff = configmod.save_overrides(c, changes, app.config_file)
            except configmod.ConfigError:
                for key in secrets_new:
                    secret_path(c, key).with_suffix(".new").unlink(missing_ok=True)
                raise
            for key, value in secrets_new.items():
                if value is None:
                    secret_path(c, key).unlink(missing_ok=True)
                else:
                    secret_path(c, key).with_suffix(".new").replace(secret_path(c, key))
            app.reload(new)
            changed: dict[str, Any] = {k: list(v) for k, v in diff.items()}
            changed.update({k: "(removed)" if v is None else "(changed)" for k, v in secrets_new.items()})
            if changed:
                audit.record(c.state, user, "settings", changed)
            return 200, {"ok": True, "changed": sorted(changed)}, None

        def r_machine_settings(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            c = cfg()
            host, values = req.get("host") or None, req.get("values")
            if not isinstance(values, dict) or (host is not None and not isinstance(host, str)):
                return 400, {"error": "expected {host, values}"}, None
            try:
                diff = machines.update(c, host, values)
            except machines.SettingError as e:
                return 400, {"error": str(e)}, None
            apply_to: list[str] = []
            if diff:
                audit.record(c.state, user, f"settings of {host.split('.')[0]}" if host else "machine defaults", diff)
                with contextlib.suppress(JobError):          # the dashboard's badges follow the new values
                    app.jobs.start("refresh-snooze", [], user, lambda log: app.jobs.check(log, push=False))
                if any(machines.BY_KEY[k].policy for k in diff):
                    apply_to = sorted(({host} if host else app.managed()) & app.managed())
            return 200, {"changed": sorted(diff), "apply_policy_to": apply_to}, None

        def r_devices(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            c, action, original = cfg(), req.get("action"), req.get("original") or None
            try:
                if action == "test":            # one sign-in, nothing saved
                    return 200, devstore.test(c, req.get("device"), req.get("credentials"), original), None
                if action == "save":
                    changes = devstore.save(c, req.get("device"), req.get("credentials"), original)
                    name = str(req["device"]["name"]).strip().lower()
                elif action == "remove":
                    name = str(req.get("name", ""))
                    devstore.remove(c, name)
                    changes = {"removed": name}
                else:
                    return 400, {"error": "unknown action"}, None
            except devstore.StoreError as e:
                return 400, {"error": str(e)}, None
            app.reload(configmod.load(app.config_file))
            audit.record(c.state, user, f"device {name}", changes)
            with contextlib.suppress(JobError):             # show it (or its absence) on the dashboard now
                if action == "save":
                    app.jobs.start("device-check", [name], user, app.jobs.device_action(name, "check"))
                else:
                    app.jobs.start("refresh-snooze", [], user, lambda log: app.jobs.check(log, push=False))
            return 200, {"ok": True, "name": name}, None

        def r_notify_test(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            c = cfg()
            if not c.notify.backends:
                return 400, {"error": "no alert destination is set up yet"}, None
            results = notify.send_each(c, notify.Message(
                f"{c.site.name}: test alert", f"{user} sent this from Settings. Real alerts look like this.",
                "default", c.site.url))
            return 200, {"results": results}, None

        def r_run(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            action, hosts = req.get("action"), sorted(set(req.get("hosts") or []))
            if action not in PLAYBOOKS:
                return 400, {"error": "unknown action"}, None
            if not hosts or not set(hosts) <= app.managed():
                return 400, {"error": "pick one or more managed machines"}, None
            if action in SINGLE_HOST and len(hosts) != 1:
                return 400, {"error": "one machine at a time for this action"}, None
            ctl = cfg().controller
            if ctl.host in hosts:                       # the machine this service runs on
                if action == "reboot":
                    if len(hosts) > 1:
                        return 400, {"error": f"reboot {ctl.host.split('.')[0]} on its own: it runs Patchyard"}, None
                    return self.r_reboot_controller(user, req)
                if action in ("release-upgrade", "cancel-upgrade"):
                    return 400, {"error": "not on the machine Patchyard runs on"}, None
                if action == "auto-updates" and ctl.patch_command:
                    hosts = [h for h in hosts if h != ctl.host]
                    if not hosts:
                        return 400, {"error": f"{ctl.host.split('.')[0]} keeps its own update schedule "
                                              "(controller.patch_command)"}, None
            if action != "cancel-upgrade":
                busy = [h.split(".")[0] for h in hosts if h != ctl.host and app.jobs.upgrading(h)]
                if busy:
                    return 409, {"error": "release upgrade in progress on " + ", ".join(busy)}, None
            work = app.jobs.action(action, hosts, {"reboot": bool(req.get("reboot")), "force": bool(req.get("force"))})
            return 200, {"job": app.jobs.start(action, hosts, user, work).id}, None

        def r_watch(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            hosts = sorted(set(req.get("hosts") or []))
            if len(hosts) != 1 or not set(hosts) <= app.managed():
                return 400, {"error": "pick one managed machine"}, None
            job = app.jobs.start("watch-upgrade", hosts, user, lambda log: app.jobs.watch_upgrade(log, hosts[0]))
            return 200, {"job": job.id}, None

        def enroll_request(self, req: dict[str, Any]) -> tuple[str, str, str, str, str] | tuple[int, Any, None]:
            """(host, os, method, admin, password) from an Add machine request, or an error reply."""
            host = enroll.normalize_host(cfg(), str(req.get("host", "")))
            os_family, method = req.get("os", "linux"), req.get("method", "password")
            admin, password = str(req.get("admin", "")).strip(), str(req.get("password", ""))
            if os_family not in ("linux", "windows", "macos") or method not in ("password", "command"):
                return 400, {"error": "os must be linux|windows|macos, method password|command"}, None
            if not inventory.valid_host(host):
                return 400, {"error": "that does not look like a host name"}, None
            if method == "password":
                if not re.fullmatch(r"[A-Za-z0-9_.\\@-]{1,64}", admin):
                    return 400, {"error": "enter the admin account (user, user@domain or DOMAIN\\user)"}, None
                if not password:
                    return 400, {"error": "the admin account's password is needed once"}, None
                wait = app.auth.limiter.locked_for()
                if wait:
                    return 401, {"error": f"too many refused passwords; wait {int(wait // 60) + 1} min"}, None
            if host.split(".")[0] in {h.split(".")[0] for h in app.managed() | {cfg().controller.host}}:
                return 409, {"error": f"{host.split('.')[0]} is already on the dashboard"}, None
            return host, os_family, method, admin, password

        def r_enroll_test(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            """The wizard's checks before adding: nothing on the machine changes."""
            got = self.enroll_request(req)
            if isinstance(got[0], int):
                return got                                      # type: ignore[return-value]
            host, os_family, method, admin, password = got     # type: ignore[misc]
            steps = enroll.check_access(cfg(), app.auth.limiter, host, os_family, method, admin, password)
            # the outcome goes to the service log (never the password) so a failed test can be looked into
            log.info("enroll test %s (%s, %s%s) by %s: %s", host, os_family, method, f" as {admin}" if admin else "",
                     user, "; ".join(f"{s['step']} {'ok' if s['ok'] else 'FAILED: ' + s['detail']}" for s in steps))
            return 200, {"host": host, "steps": steps, "ok": bool(steps) and all(s["ok"] for s in steps)}, None

        def r_enroll(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            """Add one machine."""
            got = self.enroll_request(req)
            if isinstance(got[0], int):
                return got                                      # type: ignore[return-value]
            host, os_family, method, admin, password = got     # type: ignore[misc]
            fam = "windows" if os_family == "windows" else "linux"     # macOS takes the same setup script
            job = app.jobs.start("enroll", [host], user, lambda log: enroll.enroll(
                cfg(), app.jobs, app.auth.limiter, log, host, os_family=fam, method=method,
                admin=admin, password=password))
            return 200, {"job": job.id, "host": host}, None

        def r_join(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            tokens = join.JoinTokens(cfg().state)
            try:
                if req.get("action") == "create":
                    token, rec = tokens.create(str(req.get("name", "")), int(req.get("hours", 1)),
                                               int(req.get("uses", 1)), user)
                    audit.record(cfg().state, user, "join command created",
                                 {"name": rec["name"], "valid until": time.strftime("%Y-%m-%d %H:%M", time.localtime(
                                     rec["expires"])), "machines": rec["max_uses"] or "no limit"})
                    return 200, {"commands": join.commands(cfg(), token), "expires": rec["expires"],
                                 "name": rec["name"], "max_uses": rec["max_uses"], "id": rec["id"]}, None
                if req.get("action") == "revoke":
                    tokens.revoke(str(req.get("id", "")))
                    audit.record(cfg().state, user, "join command revoked", {"id": str(req.get("id", ""))})
                    return 200, {"ok": True}, None
            except (join.JoinError, ValueError) as e:
                return 400, {"error": str(e)}, None
            return 400, {"error": "action: create | revoke"}, None

        def r_unenroll(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            host = str(req.get("host", ""))
            if host not in app.managed():
                return 400, {"error": "not an enrolled machine"}, None
            job = app.jobs.start("unenroll", [host], user, lambda log: enroll.unenroll(cfg(), app.jobs, log, host))
            return 200, {"job": job.id}, None

        def r_device(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            name, action = str(req.get("name", "")), req.get("action")
            dev = next((d for d in cfg().devices if d.name == name), None)
            if not dev:
                return 400, {"error": "no such device"}, None
            if action not in ("check", "update", "reboot"):
                return 400, {"error": "unknown action"}, None
            if action != "check":
                if not devices.capabilities(dev)[action]:
                    return 400, {"error": f"the {dev.driver} driver cannot {action} this device"}, None
                if req.get("confirm") != "yes":
                    return 400, {"error": "confirmation missing"}, None
            job = app.jobs.start(f"device-{action}", [name], user, app.jobs.device_action(name, action))
            return 200, {"job": job.id}, None

        def r_users(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            if cfg().auth.method != "local":
                return 400, {"error": "users are managed here only with auth.method = \"local\""}, None
            action, name, pw = req.get("action"), str(req.get("name", "")).strip().lower(), str(req.get("password", ""))
            users = Users(cfg().state)
            if action == "add":
                users.set(name, pw, create=True)
            elif action == "password":
                users.set(name, pw, create=False)
            elif action == "remove":
                if name == user:
                    return 400, {"error": "you cannot remove your own account while signed in with it"}, None
                users.remove(name)
            else:
                return 400, {"error": "action: add | password | remove"}, None
            return 200, {"users": users.names()}, None

        def r_tokens(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            tokens = Tokens(cfg().state)
            if req.get("action") == "create":
                token = tokens.create(str(req.get("name", "")).strip(), user)
                return 200, {"token": token, "note": "shown once; store it now"}, None
            if req.get("action") == "revoke":
                tokens.revoke(str(req.get("id", "")))
                return 200, {"tokens": tokens.public()}, None
            return 400, {"error": "action: create | revoke"}, None

        def r_reboot_controller(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            if not cfg().controller.reboot_command:
                return 400, {"error": "controller reboot is not configured"}, None
            if req.get("confirm") != "yes":
                return 400, {"error": "confirmation missing"}, None
            if [j for j in app.jobs.running() if j.action not in ("refresh", "refresh-snooze", "watch-upgrade")]:
                return 409, {"error": "jobs are running; rebooting the controller would cut them off"}, None
            job = app.jobs.start("reboot-controller", [cfg().controller.host], user, app.jobs.reboot_controller)
            return 200, {"job": job.id}, None

    ROUTES = {
        "/api/logout": Handler.r_logout, "/api/refresh": Handler.r_refresh, "/api/snooze": Handler.r_snooze,
        "/api/settings": Handler.r_settings, "/api/notify-test": Handler.r_notify_test,
        "/api/machine-settings": Handler.r_machine_settings, "/api/devices": Handler.r_devices,
        "/api/join": Handler.r_join,
        "/api/run": Handler.r_run, "/api/watch": Handler.r_watch,
        "/api/enroll": Handler.r_enroll, "/api/enroll-test": Handler.r_enroll_test,
        "/api/unenroll": Handler.r_unenroll,
        "/api/users": Handler.r_users, "/api/tokens": Handler.r_tokens,
        "/api/reboot-controller": Handler.r_reboot_controller, "/api/device": Handler.r_device,
    }
    return Handler


def make_setup_handler(server_ref: list[ThreadingHTTPServer], config_path: Path,
                       state: Path) -> type[BaseHTTPRequestHandler]:
    """Setup mode: only the first-run wizard, every call guarded by the one-time setup code."""
    from . import auth as authmod
    from . import notify as notifymod
    from . import setup as setupmod
    static = Path(__file__).resolve().parent.parent / "www"
    limiter = authmod.RateLimiter(5, 900, 900)
    token = setupmod.setup_token(state)
    log.info("SETUP MODE: open http://<this machine>:%s/ ; setup code: %s (also in %s)",
             server_ref[0].server_address[1] if server_ref else "?", token, state / setupmod.TOKEN_FILE)

    class SetupHandler(BaseHTTPRequestHandler):
        server_version = "patchyard"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s %s", self.client_address[0], fmt % args)

        def send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def json(self, code: int, obj: Any) -> None:
            self.send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/setup/info":
                return self.json(200, {"setup": True, "hostname": socket.gethostname(),
                                       "addresses": ssh.controller_ipv4(), "config_path": str(config_path)})
            name = {"/app.css": "app.css", "/setup.js": "setup.js"}.get(path, "setup.html")
            f = static / name
            self.send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "text/html")

        def do_POST(self) -> None:
            if (self.headers.get("X-Patchyard") != "1"
                    or not self.headers.get("Content-Type", "").startswith("application/json")):
                return self.json(403, {"error": "forbidden"})
            try:
                req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 65536)) or b"{}")
            except ValueError:
                return self.json(400, {"error": "bad json"})
            wait = limiter.locked_for()
            if wait:
                return self.json(429, {"error": f"too many wrong codes; wait {int(wait // 60) + 1} min"})
            if not setupmod.code_ok(state, str(req.get("code", ""))):
                limiter.fail()
                return self.json(401, {"error": "wrong setup code"})
            path = urlsplit(self.path).path
            answers = req.get("answers") or {}
            try:
                if path == "/api/setup/code":
                    return self.json(200, {"ok": True})
                if path == "/api/setup/test-notify":
                    cfg = setupmod.build({**answers, "auth_method": "none"}, state)
                    msg = notifymod.Message("Patchyard test", "Alerts from Patchyard arrive here.", "default",
                                            cfg.site.url)
                    failed = notifymod.send(cfg, msg)
                    err = "delivery failed (see the service log)" if failed else ""
                    return self.json(200 if not failed else 502, {"ok": not failed, "error": err})
                if path == "/api/setup/test-kerberos":
                    cfg = setupmod.build({**answers, "notify": "none"}, state)
                    a = authmod.Authenticator(cfg, limiter)
                    user = a.authenticate(str(req.get("username", "")), str(req.get("password", "")))
                    return self.json(200, {"ok": True, "user": user})
                if path == "/api/setup/finish":
                    cfg = setupmod.finish(answers, config_path, state)
                    app = App(cfg)
                    server_ref[0].RequestHandlerClass = make_handler(app)       # switch to the dashboard in place
                    listening = server_ref[0].server_address
                    restart = (cfg.web.bind, cfg.web.port) != (listening[0], listening[1])
                    return self.json(200, {"ok": True, "config": str(config_path), "restart_needed": restart,
                                           "listen": f"{cfg.web.bind}:{cfg.web.port}"})
            except authmod.AuthError as e:
                return self.json(401, {"ok": False, "error": str(e)})
            except configmod.ConfigError as e:
                return self.json(400, {"ok": False, "error": str(e)})
            return self.json(404, {"error": "not found"})

    return SetupHandler


def _bind(addr: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    for _ in range(60):                         # the bind address may appear late at boot
        try:
            return ThreadingHTTPServer(addr, handler)
        except OSError:
            time.sleep(5)
    raise SystemExit(f"cannot bind {addr[0]}:{addr[1]}")


def serve(cfg: Config | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if cfg is None:                              # no configuration yet: first-run wizard
        from . import setup as setupmod
        state = setupmod.state_dir()
        addr = (os.environ.get("PATCHYARD_SETUP_BIND", "0.0.0.0"),
                int(os.environ.get("PATCHYARD_SETUP_PORT", "8790")))
        ref: list[ThreadingHTTPServer] = []
        server = _bind(addr, BaseHTTPRequestHandler)
        ref.append(server)
        server.RequestHandlerClass = make_setup_handler(ref, Path(configmod.config_path()), state)
        server.serve_forever()
        return
    app = App(cfg)
    addr = (cfg.web.bind, cfg.web.port)
    server = _bind(addr, make_handler(app))
    log.info("patchyard web on %s:%s", *addr)
    server.serve_forever()
