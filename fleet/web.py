"""HTTP: the web app, its data, and the action API.

  python -m fleet web

Routes
  GET  /login                  sign-in page (no session needed)
  GET  /api/auth               204 with a session, else 302 /login (for a proxy's forward_auth)
  GET  /, /app.js, ...         the web app (if web.serve_static); /data/status.json from the state dir
  GET  /api/state | /api/settings | /api/job/<id>?offset=N
  POST /api/login | /api/logout | /api/refresh | /api/snooze | /api/settings
       /api/run | /api/watch | /api/enroll | /api/unenroll | /api/reboot-controller

Every route except /login, /api/login and /api/auth needs a session (unless
auth.method = "none"). POSTs must be JSON, carry X-Fleet: 1 and, when an Origin
header is present, come from site.url: together that stops cross-site requests.
Request bodies are never logged.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import secrets
import subprocess
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from . import config as configmod
from . import enroll, inventory
from .auth import Authenticator, AuthError
from .config import Config
from .jobs import PLAYBOOKS, SINGLE_HOST, JobError, JobManager

log = logging.getLogger("fleet.web")
COOKIE = "fleet_session"
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

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.auth = Authenticator(cfg)
        self.sessions = Sessions(cfg.web.session_hours * 3600)
        self.jobs = JobManager(cfg)
        self.static = cfg.code / "www"
        self.data = cfg.state / "www"
        self._last_refresh = 0.0

    def managed(self) -> set[str]:
        c = self.cfg
        return set(inventory.hosts(c.inventory_file, c.inventory.managed_group)) - {c.controller.host}


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    def cfg() -> Config:
        return app.cfg

    class Handler(BaseHTTPRequestHandler):
        server_version = "ansiblefleet"

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
                return "anonymous"
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
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/login":
                return self.file(app.static, "login.html")
            if path == "/api/auth":
                if self.user():
                    return self.send(204, b"", "text/plain")
                return self.to_login(self.headers.get("X-Forwarded-Uri", "/"))
            user = self.user()
            if not user:
                if path.startswith("/api/"):
                    return self.json(401, {"error": "sign in first", "login": True})
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
            m = re.fullmatch(r"/api/job/([\w-]+)", path)
            q = re.search(r"offset=(\d+)", urlsplit(self.path).query or "")
            if m:
                out = app.jobs.read(m.group(1), int(q.group(1)) if q else 0)
                return self.json(200, out) if out else self.json(404, {"error": "no such job"})
            return self.json(404, {"error": "not found"})

        def settings_view(self) -> dict[str, Any]:
            try:
                fp = subprocess.run(["ssh-keygen", "-lf", str(cfg().ssh_key.with_suffix(".pub"))],
                                    capture_output=True, text=True).stdout.split()[1]
            except (OSError, IndexError):
                fp = "?"
            return {"editable": {k: {"label": label, "value": configmod.get(cfg(), k)}
                                 for k, label in configmod.EDITABLE.items()},
                    "service_account": cfg().service_account.name, "key_fingerprint": fp,
                    "site": cfg().site.name, "auth_method": cfg().auth.method}

        # ------------------------------------------------------------ POST
        def do_POST(self) -> None:
            origin = self.headers.get("Origin")
            if (self.headers.get("X-Fleet") != "1"
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
            except (JobError, inventory.InventoryError, configmod.ConfigError) as e:
                return self.json(409, {"error": str(e)})

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
            changes = {k: v for k, v in req.items() if k in configmod.EDITABLE}
            group = changes.get("auth.group")
            if group and app.auth.required and not app.auth.is_member(user, group):
                return 400, {"error": f"you ({user}) are not in {group}; that would lock you out"}, None
            app.cfg = configmod.save_overrides(cfg, changes)
            return 200, {"ok": True, "restart_needed": True}, None

        def r_run(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            action, hosts = req.get("action"), sorted(set(req.get("hosts") or []))
            if action not in PLAYBOOKS:
                return 400, {"error": "unknown action"}, None
            if not hosts or not set(hosts) <= app.managed():
                return 400, {"error": "pick one or more managed machines"}, None
            if action in SINGLE_HOST and len(hosts) != 1:
                return 400, {"error": "one machine at a time for this action"}, None
            if action != "cancel-upgrade":
                busy = [h.split(".")[0] for h in hosts if app.jobs.upgrading(h)]
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

        def r_enroll(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            host = enroll.normalize_host(cfg(), str(req.get("host", "")))
            admin, password = str(req.get("admin", "")).strip(), str(req.get("password", ""))
            if not inventory.valid_host(host) or not re.fullmatch(r"[A-Za-z0-9_.\\@-]{1,64}", admin):
                return 400, {"error": "that does not look like a host name and account"}, None
            if host.split(".")[0] in {h.split(".")[0] for h in app.managed() | {cfg().controller.host}}:
                return 409, {"error": f"{host.split('.')[0]} is already on the dashboard"}, None
            if not password:
                return 400, {"error": "the admin account's password is needed once"}, None
            wait = app.auth.limiter.locked_for()
            if wait:
                return 401, {"error": f"too many failures; wait {int(wait // 60) + 1} min"}, None
            job = app.jobs.start("enroll", [host], user, lambda log: enroll.enroll(
                cfg(), app.jobs, app.auth.limiter, log, host, admin, password))
            return 200, {"job": job.id}, None

        def r_unenroll(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            host = str(req.get("host", ""))
            if host not in app.managed():
                return 400, {"error": "not an enrolled machine"}, None
            job = app.jobs.start("unenroll", [host], user, lambda log: enroll.unenroll(cfg(), app.jobs, log, host))
            return 200, {"job": job.id}, None

        def r_reboot_controller(self, user: str, req: dict[str, Any]) -> tuple[int, Any, None]:
            if not cfg().controller.reboot_command:
                return 400, {"error": "controller reboot is not configured"}, None
            if req.get("confirm") != "yes":
                return 400, {"error": "confirmation missing"}, None
            if [j for j in app.jobs.running() if j.action not in ("refresh", "watch-upgrade")]:
                return 409, {"error": "jobs are running; rebooting the controller would cut them off"}, None
            job = app.jobs.start("reboot-controller", [cfg().controller.host], user, app.jobs.reboot_controller)
            return 200, {"job": job.id}, None

    ROUTES = {
        "/api/logout": Handler.r_logout, "/api/refresh": Handler.r_refresh, "/api/snooze": Handler.r_snooze,
        "/api/settings": Handler.r_settings, "/api/run": Handler.r_run, "/api/watch": Handler.r_watch,
        "/api/enroll": Handler.r_enroll, "/api/unenroll": Handler.r_unenroll,
        "/api/reboot-controller": Handler.r_reboot_controller,
    }
    return Handler


def serve(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = App(cfg)
    addr = (cfg.web.bind, cfg.web.port)
    for _ in range(60):                         # the bind address may appear late at boot
        try:
            server = ThreadingHTTPServer(addr, make_handler(app))
            break
        except OSError:
            time.sleep(5)
    else:
        raise SystemExit(f"cannot bind {addr[0]}:{addr[1]}")
    log.info("ansiblefleet web on %s:%s", *addr)
    server.serve_forever()
