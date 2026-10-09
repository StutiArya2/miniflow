"""Notify: email, log, and wait."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from miniflow.blocks import Setting, block

log = logging.getLogger("miniflow.flow")


@block(
    "email",
    settings=[
        Setting("to", (str, list), help="One address or a list"),
        Setting("subject", str),
        Setting("body", str, default=""),
        Setting("sender", str, default="miniflow@localhost"),
        Setting("host", str, default="localhost", help="SMTP server"),
        Setting("port", int, default=25),
        Setting("username", str, default=""),
        Setting("password", str, default="", secret=True, help="Use {{ env.SMTP_PASSWORD }}"),
        Setting("tls", bool, default=False, help="Use STARTTLS (port 587 usually)"),
    ],
)
def email(
    to: str | list[str],
    subject: str,
    body: str = "",
    sender: str = "miniflow@localhost",
    host: str = "localhost",
    port: int = 25,
    username: str = "",
    password: str = "",
    tls: bool = False,
) -> dict[str, Any]:
    """Send an email through an SMTP server."""
    recipients = [to] if isinstance(to, str) else list(to)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(recipients), subject
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if tls:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)
    return {"sent_to": recipients, "subject": subject}


@block(
    "log",
    settings=[
        Setting("message", str, help="e.g. Saved {{ save.rows }} rows"),
        Setting("level", str, default="info", choices=("info", "warning", "error")),
    ],
)
def log_message(message: str, level: str = "info") -> dict[str, str]:
    """Write a message to the run's log; it is also kept as the step's result."""
    log.log(logging.getLevelName(level.upper()), message)
    return {"level": level, "message": message}


@block("wait", settings=[Setting("seconds", float, help="How long to pause")], kind="async")
async def wait(seconds: float) -> dict[str, float]:
    """Pause before the next steps, e.g. to respect an API's rate limit."""
    await asyncio.sleep(seconds)
    return {"waited": seconds}
