"""Central configuration loader.

Reads from two sources, in this order:
1. Environment variables (or a `.env` file next to this project)
2. `mcb_config.ini` (legacy, still used for child credentials)

Secrets should live in `.env`. `mcb_config.ini` is retained only for backwards
compatibility with the existing setup and is also gitignored going forward.
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "mcb_config.ini"
ENV_FILE = ROOT / ".env"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def _load_env_file(path: Path) -> None:
    """Minimal .env loader (avoids adding a dependency)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_env_file(ENV_FILE)


@dataclass
class ChildAccount:
    key: str          # e.g. "Child1"
    name: str         # display name shown in the digest
    username: str
    password: str


@dataclass
class EmailConfig:
    smtp_host: str
    smtp_port: int
    sender: str
    password: str
    recipients: List[str]


@dataclass
class AppConfig:
    children: List[ChildAccount]
    email: EmailConfig
    log_file: Path
    mcb_base: str = "https://chirec.myclassboard.com"
    sso_base: str = "https://ssolive.myclassboard.com"
    datetype: int = 3  # "last 30 days" listing on the announcements page
    timezone: str = "Asia/Kolkata"
    reminder_offsets_days: tuple = (5, 3, 2, 1)
    # Ignore any announcement or event dated before this floor. Bump this
    # forward at the start of a new academic year to purge stale data.
    data_floor_date: date = date(2026, 7, 13)


def _read_ini() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        cp.read(CONFIG_FILE, encoding="utf-8")
    return cp


def load_config() -> AppConfig:
    cp = _read_ini()

    children: List[ChildAccount] = []
    for section in cp.sections():
        if section.lower().startswith("child"):
            children.append(
                ChildAccount(
                    key=section,
                    name=cp.get(section, "name", fallback=section),
                    username=cp.get(section, "username"),
                    password=cp.get(section, "password"),
                )
            )

    log_file = Path(cp.get("Global", "log_file", fallback="mcb_scheduler.log"))
    if not log_file.is_absolute():
        log_file = ROOT / log_file

    recipients_env = os.environ.get("DIGEST_RECIPIENTS", "").strip()
    if recipients_env:
        recipients = [r.strip() for r in recipients_env.split(",") if r.strip()]
    else:
        recipients = []

    email = EmailConfig(
        smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.environ.get("SMTP_PORT", "465")),
        sender=os.environ.get("SMTP_SENDER") or cp.get("Email", "sender", fallback=""),
        password=os.environ.get("SMTP_PASSWORD") or cp.get("Email", "password", fallback=""),
        recipients=recipients,
    )

    kwargs: dict = {}
    floor_raw = os.environ.get("DATA_FLOOR_DATE", "").strip()
    if floor_raw:
        try:
            from datetime import date as _date
            kwargs["data_floor_date"] = _date.fromisoformat(floor_raw)
        except ValueError:
            pass

    # Portal tenant overrides. Defaults are set on AppConfig; env wins if set.
    mcb_base_env = os.environ.get("MCB_BASE_URL", "").strip()
    if mcb_base_env:
        kwargs["mcb_base"] = mcb_base_env.rstrip("/")
    sso_base_env = os.environ.get("MCB_SSO_URL", "").strip()
    if sso_base_env:
        kwargs["sso_base"] = sso_base_env.rstrip("/")

    return AppConfig(children=children, email=email, log_file=log_file, **kwargs)


def data_path(*parts: str) -> Path:
    p = DATA_DIR.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
