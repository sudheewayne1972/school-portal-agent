"""Post the current MCB digest to the configured Telegram group.

Called by Windows Task Scheduler right after ``run_digest.py --dry-run``
completes its morning (06:30) and evening (19:00) scrape. Reads freshly
scraped data from disk, formats the Telegram HTML digest, and sends it via a
direct call to the Bot API. Does NOT require the polling bot to be running.

CLI flags:
  --slot morning|evening  Header wording tweak (auto-derived from local time
                          if omitted).
  --dry-run               Build the digest and print it to stdout without
                          posting anything.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from typing import Iterable

import httpx

from mcb_digest.agent import Agent
from mcb_digest.config import load_config
from mcb_digest.telegram_digest import (
    IST,
    build_html_digest,
    chunk_for_telegram,
)

log = logging.getLogger("mcb_digest.push")


def _auto_slot() -> str:
    hour = datetime.now(IST).hour
    return "morning" if hour < 12 else "evening"


def _post_chunks(token: str, chat_id: int, chunks: Iterable[str]) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with httpx.Client(timeout=30.0) as client:
        for i, chunk in enumerate(chunks, 1):
            resp = client.post(url, data={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if resp.status_code != 200 or not resp.json().get("ok"):
                log.error("sendMessage chunk %d failed: %s %s",
                          i, resp.status_code, resp.text[:400])
                resp.raise_for_status()
            log.info("posted chunk %d (%d chars)", i, len(chunk))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", choices=["morning", "evening", "homework"],
                        default=None,
                        help="Header wording. Auto-derived from local time if omitted.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build and print the digest; do not send.")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    load_config()  # populates os.environ from .env

    slot = args.slot or _auto_slot()

    agent = Agent()
    agent.reload_context()
    html = build_html_digest(agent, slot=slot)

    if not html.strip():
        log.info("digest empty for slot=%s — skipping push", slot)
        return 0

    chunks = chunk_for_telegram(html)
    log.info("digest built: %d chars in %d chunk(s), slot=%s",
             len(html), len(chunks), slot)

    if args.dry_run:
        print(html)
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env", file=sys.stderr)
        return 1
    if not chat_id_raw:
        print("ERROR: TELEGRAM_CHAT_ID not set in .env", file=sys.stderr)
        return 1
    chat_id = int(chat_id_raw)

    _post_chunks(token, chat_id, chunks)
    log.info("push complete: chat_id=%d, slot=%s", chat_id, slot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
