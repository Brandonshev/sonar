"""Shared senders: ntfy push and (optional) email. Settings come from .env."""
import json
import os
import smtplib
import time
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from zoneinfo import ZoneInfo

from .. import USER_AGENT


QUIET: dict | None = None  # {"start": "22:00", "end": "09:00", "tz": "Australia/Sydney"}, set from pipeline.json


def configure_quiet(quiet: dict | None) -> None:
    global QUIET
    QUIET = quiet


def quiet_until(now: float, quiet: dict | None = None) -> int | None:
    """If `now` is inside quiet hours, the unix time they end; otherwise None."""
    q = quiet or QUIET
    if not q:
        return None
    tz = ZoneInfo(q.get("tz", "UTC"))
    local = datetime.fromtimestamp(now, tz)
    sh, sm = map(int, q["start"].split(":"))
    eh, em = map(int, q["end"].split(":"))
    minutes, start, end = local.hour * 60 + local.minute, sh * 60 + sm, eh * 60 + em
    inside = (start <= minutes or minutes < end) if start > end else (start <= minutes < end)
    if not inside:
        return None
    end_dt = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end_dt <= local:
        end_dt += timedelta(days=1)
    return int(end_dt.timestamp())


def public_url(path: str) -> str | None:
    """Link into the dashboard, if SONAR_PUBLIC_URL is set (it is once the dashboard is up)."""
    base = os.environ.get("SONAR_PUBLIC_URL", "").rstrip("/")
    return f"{base}/{path.lstrip('/')}" if base else None


def send_ntfy(title: str, message: str, click: str | None = None, tags: list | None = None,
              priority: int = 3, actions: list | None = None, urgent: bool = False) -> None:
    """Publish to NTFY_TOPIC on NTFY_SERVER (default ntfy.sh). Raises on failure.
    During quiet hours a non-urgent message is held by ntfy and delivered when they end."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        raise RuntimeError("NTFY_TOPIC is not set in .env")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    body = {"topic": topic, "title": title[:250], "message": message[:4000], "priority": priority,
            "tags": tags or []}
    if click:
        body["click"] = click
    if actions:
        body["actions"] = actions
    if icon := public_url("icon-192.png"):  # shown next to the notification (ntfy on Android)
        body["icon"] = icon
    if not urgent and (until := quiet_until(time.time())):
        body["delay"] = str(until)  # ntfy's scheduled delivery
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if token := os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(server, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def email_configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "DIGEST_TO"))


def send_email(subject: str, html: str | None, text: str) -> None:
    """Plain text (plus HTML if given). Always sets Date and Message-ID: mail without them looks like spam."""
    sender = os.environ["SMTP_USER"]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Sonar <{sender}>", os.environ["DIGEST_TO"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.ehlo()
        if s.has_extn("starttls"):
            s.starttls()
            s.ehlo()
        elif host not in ("127.0.0.1", "localhost", "::1"):
            # Never send the password unencrypted over a real network. Same-machine (e.g. Postline) is fine.
            raise RuntimeError(f"{host}:{port} offers no STARTTLS; refusing to send the password unencrypted")
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)
