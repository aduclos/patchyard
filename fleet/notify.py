"""Alert delivery. Configured backends (config `notify.backends`) all receive every message.

A failing backend is logged and skipped; it never stops the fleet check.
"""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)
PRIORITIES = ("low", "default", "high")


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    priority: str = "default"      # low | default | high
    url: str = ""                  # link to the dashboard


def _secret(path: str) -> str:
    return Path(path).read_text().strip() if path else ""


def send_ntfy(cfg: Config, msg: Message) -> None:
    n = cfg.notify.ntfy
    req = urllib.request.Request(f"{n.url.rstrip('/')}/{n.topic}", data=msg.body.encode(), method="POST")
    req.add_header("Title", msg.title)
    req.add_header("Priority", msg.priority)
    req.add_header("Tags", "package")
    if msg.url:
        req.add_header("Click", msg.url)
    token = _secret(n.token_file)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    urllib.request.urlopen(req, timeout=15).read()


def send_webhook(cfg: Config, msg: Message) -> None:
    body = json.dumps({"title": msg.title, "body": msg.body, "priority": msg.priority, "url": msg.url}).encode()
    req = urllib.request.Request(cfg.notify.webhook.url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(req, timeout=15).read()


def send_smtp(cfg: Config, msg: Message) -> None:
    s = cfg.notify.smtp
    em = EmailMessage()
    em["Subject"] = f"[{cfg.site.name}] {msg.title}"
    em["From"] = s.sender or s.user
    em["To"] = ", ".join(s.recipients)
    em.set_content(msg.body + (f"\n\n{msg.url}\n" if msg.url else ""))
    with smtplib.SMTP(s.host, s.port, timeout=20) as conn:
        if s.starttls:
            conn.starttls(context=ssl.create_default_context())
        if s.user:
            conn.login(s.user, _secret(s.password_file))
        conn.send_message(em)


BACKENDS = {"ntfy": send_ntfy, "webhook": send_webhook, "smtp": send_smtp}


def send(cfg: Config, msg: Message) -> list[str]:
    """Deliver to every configured backend; returns the names that failed."""
    if msg.priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {PRIORITIES}")
    failed = []
    for name in cfg.notify.backends:
        try:
            BACKENDS[name](cfg, msg)
        except Exception as e:   # one broken backend must not stop the others
            log.warning("notify via %s failed: %s", name, e)
            failed.append(name)
    return failed
