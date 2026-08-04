"""One-shot diary refresher (called by the 4:30 PM Scheduled Task Mon-Sat).

- Logs in per child, hits `_AllActivities` for the last N days
- Persists to `data/{child}_diary.json`
- Extracts any explicit due-date events and upserts into the shared event store
- Optionally pushes a lightweight "Homework updated" ping to Telegram

Usage:
    python run_diary.py                 # 7-day backfill on first run, 3-day on subsequent
    python run_diary.py --days 14       # explicit window
    python run_diary.py --push          # also send a Telegram summary
    python run_diary.py --dry-run       # scrape + parse but skip persistence
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo

import httpx

from mcb_digest.config import load_config
from mcb_digest.diary_scraper import fetch_diary
from mcb_digest.diary_store import load_diary, merge_and_save, recent_entries
from mcb_digest.extractor import extract_events_from_diary, set_data_floor
from mcb_digest.reminders import upsert_events

IST = ZoneInfo("Asia/Kolkata")

TELEGRAM_MSG_LIMIT = 3800


def _setup_logging(log_file):
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
    return logging.getLogger("run_diary")


def _default_days(child_name: str) -> int:
    """Backfill 7 days on first run, otherwise 3 days (today + prev 2)."""
    return 7 if not load_diary(child_name) else 3


def _push_summary(token: str, chat_id: str, text: str,
                  dry_run: bool = False) -> None:
    if dry_run:
        print("\n--- would push to Telegram ---\n" + text)
        return
    with httpx.Client(timeout=20) as c:
        r = c.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        r.raise_for_status()


def _summary_html(children: List[str]) -> str:
    from mcb_digest.telegram_digest import esc, pretty_date  # local import
    today = datetime.now(IST).date()
    parts = [f"📔 <b>Homework refreshed — {today.strftime('%a, %d %b %Y')}</b>"]
    for name in children:
        today_iso = today.isoformat()
        todays = [e for e in load_diary(name) if e.diary_date == today_iso]
        parts.append(f"\n<b>{esc(name)}</b> — {len(todays)} entr" +
                     ("y" if len(todays) == 1 else "ies") + " today")
        if not todays:
            parts.append("  <i>Nothing posted today yet.</i>")
            continue
        by_subject: dict[str, list] = {}
        for e in todays:
            by_subject.setdefault(e.subject, []).append(e)
        for subj, es in sorted(by_subject.items()):
            parts.append(f"  <b>{esc(subj)}</b>")
            for e in es:
                desc = " ".join(e.description.split())
                if len(desc) > 220:
                    desc = desc[:220].rstrip() + "…"
                parts.append(f"    • <i>{esc(e.kind)}</i> — {esc(desc)}")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="MCB diary refresher")
    ap.add_argument("--days", type=int, default=None,
                    help="How many days back to fetch (default: auto)")
    ap.add_argument("--push", action="store_true",
                    help="Send a Telegram summary after refresh")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not persist or send anything")
    args = ap.parse_args()

    cfg = load_config()
    log = _setup_logging(cfg.log_file)
    set_data_floor(cfg.data_floor_date)

    log.info("Diary refresh starting for %d child(ren)", len(cfg.children))
    all_new_events = []
    child_names: List[str] = []
    for child in cfg.children:
        days = args.days or _default_days(child.name)
        log.info("Fetching diary for %s (days=%d)", child.name, days)
        res = fetch_diary(cfg, child, days_back=days)
        log.info("  fetch ok=%s elapsed=%ss entries=%d error=%s",
                 res.ok, res.elapsed_s, len(res.entries), res.error)
        if not res.ok:
            log.error("  fetch failed: %s", res.error)
            continue

        if args.dry_run:
            for e in res.entries:
                log.info("  [dry] %s %s / %s / %s — %s",
                         e.diary_date, e.subject, e.kind, e.teacher,
                         e.description[:80])
        else:
            merge_and_save(child.name, res.entries)

        events = extract_events_from_diary(child.name, res.entries)
        log.info("  extracted %d events from diary", len(events))
        all_new_events.extend(events)
        child_names.append(child.name)

    if args.dry_run:
        log.info("DRY-RUN: skipping event upsert and push")
        return 0

    if all_new_events:
        store = upsert_events(all_new_events)
        log.info("Event store now has %d entries", len(store))

    if args.push:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            log.warning("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID; not pushing")
        else:
            html = _summary_html(child_names or [c.name for c in cfg.children])
            _push_summary(token, chat_id, html, dry_run=False)
            log.info("Pushed diary summary to Telegram")

    return 0


if __name__ == "__main__":
    sys.exit(main())
