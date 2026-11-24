"""Shared senders: ntfy push and (optional) email. Settings come from .env."""
import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage

from .. import USER_AGENT


def public_url(path: str) -> str | None:
    """Link into the dashboard, if SONAR_PUBLIC_URL is set (it is once the dashboard is up)."""
    base = os.environ.get("SONAR_PUBLIC_URL", "").rstrip("/")
    return f"{base}/{path.lstrip('/')}" if base else None


def send_ntfy(title: str, message: str, click: str | None = None, tags: list | None = None,
              priority: int = 3, actions: list | None = None) -> None:
    """Publish to NTFY_TOPIC on NTFY_SERVER (default ntfy.sh). Raises on failure."""
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
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if token := os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(server, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def email_configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "DIGEST_TO"))


def send_email(subject: str, html: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ["SMTP_USER"], os.environ["DIGEST_TO"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)
