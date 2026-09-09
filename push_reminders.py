"""One-shot reminder nudger.

Fired by a dedicated Windows Scheduled Task each day at 6:10 PM local. Loads
the event store, finds every event whose deadline is exactly 5, 4, 3, 2 or 1
days out, and posts a single combined Telegram message. If there is nothing
due, no message is sent (avoids empty pings).

Usage:
    python push_reminders.py                    # normal use (scheduled task)
    python push_reminders.py --dry-run          # print instead of send
    python push_reminders.py --offsets 5 4 3 2 1  # override offsets
    python push_reminders.py --force-empty      # send even if no reminders due
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List
from zoneinfo import ZoneInfo

import httpx

from mcb_digest.config import load_config
from mcb_digest.reminders import load_all_events, reminders_due
from mcb_digest.telegram_digest import (
    TELEGRAM_MSG_LIMIT,
    chunk_for_telegram,
    esc,
    pretty_date,
)

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("mcb_digest.reminders_push")


def _setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def _build_message(due: Iterable[dict], today) -> str:
    due = list(due)
    if not due:
        return ""
    stamp = today.strftime("%a, %d %b %Y")
    parts: List[str] = [f"⏰ <b>Deadline nudge — {stamp}</b>"]

    # Group by days_out (already sorted by (_days_out, event_date))
    by_days: dict[int, list[dict]] = {}
    for ev in due:
        by_days.setdefault(ev["_days_out"], []).append(ev)

    for days in sorted(by_days.keys()):
        when = "tomorrow" if days == 1 else f"in {days} days"
        parts.append(f"\n<b>Due {when}</b>")
        for ev in by_days[days]:
            child = esc(ev.get("child", ""))
            title = esc(ev.get("title", ""))
            ed = esc(pretty_date(ev.get("event_date", ""), today))
            # Truncate very long homework titles
            if len(title) > 200:
                title = title[:200].rstrip() + "…"
            parts.append(f"  • <b>{child}</b> — {title}\n    <i>{ed}</i>")
    return "\n".join(parts)


def _post_chunks(token: str, chat_id: str, chunks: List[str],
                 dry_run: bool) -> None:
    if dry_run:
        for c in chunks:
            print(c)
            print("---")
        return
    with httpx.Client(timeout=20) as c:
        for chunk in chunks:
            r = c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )
            r.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser(description="MCB deadline reminder nudge")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print message instead of sending to Telegram")
    ap.add_argument("--offsets", type=int, nargs="+", default=None,
                    help="Override reminder offsets (default: config)")
    ap.add_argument("--force-empty", action="store_true",
                    help="Send a 'no reminders today' message even if empty")
    args = ap.parse_args()

    cfg = load_config()
    _setup_logging(cfg.log_file)

    offsets = tuple(args.offsets) if args.offsets else cfg.reminder_offsets_days
    log.info("Reminder nudge — offsets=%s", offsets)

    today = datetime.now(IST).date()
    due = reminders_due(load_all_events(), today=today, offsets_days=offsets)
    log.info("%d event(s) due at offsets %s", len(due), offsets)

    if not due and not args.force_empty:
        log.info("Nothing due — skipping push")
        return 0

    text = _build_message(due, today) if due else (
        f"✅ <b>No deadlines coming up in the next "
        f"{max(offsets) if offsets else '?'} days.</b>"
    )
    chunks = chunk_for_telegram(text, limit=TELEGRAM_MSG_LIMIT)
    log.info("Prepared %d chunk(s), total %d chars", len(chunks), len(text))

    if args.dry_run:
        _post_chunks("", "", chunks, dry_run=True)
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is not set in the environment")
        return 2
    if not chat_id:
        log.error("TELEGRAM_CHAT_ID is not set in the environment")
        return 2

    _post_chunks(token, chat_id, chunks, dry_run=False)
    log.info("Pushed reminder nudge to chat %s", chat_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
