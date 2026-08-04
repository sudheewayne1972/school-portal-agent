"""SMTP mailer (Gmail app-password friendly)."""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

from .config import EmailConfig

log = logging.getLogger(__name__)


def send_email(cfg: EmailConfig, subject: str, text_body: str, html_body: str,
               recipients: List[str] | None = None) -> None:
    recipients = recipients or cfg.recipients
    if not cfg.sender or not cfg.password:
        raise RuntimeError(
            "SMTP sender/password not configured. Set SMTP_SENDER and SMTP_PASSWORD "
            "in .env (or [Email] in mcb_config.ini)."
        )
    msg = MIMEMultipart("alternative")
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    log.info("Connecting to %s:%s as %s", cfg.smtp_host, cfg.smtp_port, cfg.sender)
    if cfg.smtp_port == 465:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as srv:
            srv.login(cfg.sender, cfg.password)
            srv.sendmail(cfg.sender, recipients, msg.as_string())
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as srv:
            srv.starttls()
            srv.login(cfg.sender, cfg.password)
            srv.sendmail(cfg.sender, recipients, msg.as_string())
    log.info("Digest sent to %s", ", ".join(recipients))
