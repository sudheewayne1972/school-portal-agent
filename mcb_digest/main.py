"""Orchestrator for the daily digest.

Usage:
  python -m mcb_digest.main                # run full pipeline and send email
  python -m mcb_digest.main --dry-run      # do everything except send email
  python -m mcb_digest.main --skip-scrape  # reuse the last saved announcements
  python -m mcb_digest.main --smoke        # just log in for each child and report
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from .config import AppConfig, ROOT, data_path, load_config
from .digest import build_digest, render_html, render_text
from .diary_store import recent_entries as recent_diary_entries
from .emailer import send_email
from .extractor import (
    Event,
    extract_events,
    extract_events_from_diary,
    set_data_floor,
)
from .parser import Announcement, parse_announcements
from .attachments import summarize_attachments
from .reminders import (
    load_all_events,
    purge_before,
    reminders_due,
    upcoming_events,
    upsert_events,
)
from .scraper import fetch_announcements_html


def _setup_logging(cfg: AppConfig) -> logging.Logger:
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    # Ensure UTF-8 stdout on Windows so emoji/★/⚠ don't crash the console.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(cfg.log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    return logging.getLogger("mcb_digest")


def _dict_of_announcements(anns: List[Announcement]) -> List[dict]:
    out = []
    for a in anns:
        d = a.to_dict()
        d["attachments"] = [x.__dict__ for x in a.attachments]
        out.append(d)
    return out


def _load_saved_announcements(child_name: str) -> List[Announcement]:
    # Prefer data/ (new home); fall back to the project root JSONs written by
    # the legacy scraper.
    candidates = [
        data_path(f"{child_name.lower()}_announcements.json"),
        ROOT / f"{child_name.lower()}_announcements.json",
        ROOT / "attachments" / f"{child_name.lower()}_announcements.json",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    anns: List[Announcement] = []
    for r in raw:
        a = Announcement(
            sender=r.get("sender", ""), tag=r.get("tag", ""),
            date=r.get("date", ""), date_raw=r.get("date_raw", ""),
            title=r.get("title", ""), content=r.get("content", ""),
        )
        for att in r.get("attachments", []) or []:
            from .parser import Attachment
            a.attachments.append(Attachment(url=att.get("url",""),
                                            filename=att.get("filename") or att.get("file",""),
                                            content=att.get("content") or att.get("extracted_text",""),
                                            summary=att.get("summary", "")))
        anns.append(a)
    return anns


def scrape_all(cfg: AppConfig, log: logging.Logger,
               download_attachments: bool = True) -> Dict[str, List[Announcement]]:
    per_child: Dict[str, List[Announcement]] = {}
    for child in cfg.children:
        log.info("Scraping for %s (%s)", child.name, child.username)
        res = fetch_announcements_html(cfg, child)
        log.info("  fetch ok=%s elapsed=%ss url=%s", res.ok, res.elapsed_s, res.ann_url)
        if not res.ok:
            log.error("  fetch failed: %s", res.error)
            per_child[child.name] = _load_saved_announcements(child.name)
            log.info("  falling back to %d saved announcements", len(per_child[child.name]))
            continue

        html_path = data_path(f"{child.name.lower()}_announcements.html")
        html_path.write_text(res.html, encoding="utf-8", errors="replace")

        att_dir = data_path(f"{child.name.lower()}_attachments")
        anns = parse_announcements(res.html, att_dir, download=download_attachments)
        log.info("  parsed %d announcements", len(anns))

        # Preserve previously-cached attachment summaries so we don't re-bill
        # the LLM on every scrape. Match by (filename, url).
        cached = _load_saved_announcements(child.name)
        if cached:
            cache_map: Dict[str, str] = {}
            for c_ann in cached:
                for c_att in c_ann.attachments:
                    key = (c_att.filename or "") + "|" + (c_att.url or "")
                    if c_att.summary:
                        cache_map[key] = c_att.summary
            if cache_map:
                for ann in anns:
                    for att in ann.attachments:
                        if att.summary:
                            continue
                        key = (att.filename or "") + "|" + (att.url or "")
                        cached_sum = cache_map.get(key)
                        if cached_sum:
                            att.summary = cached_sum

        # Summarise any attachment we haven't seen before. Best-effort — a
        # failure here must not abort the scrape.
        try:
            from .llm import LLM
            llm = LLM()
            made = summarize_attachments(anns, llm, child_name=child.name)
            if made:
                log.info("  attachment summaries: %d new", made)
        except Exception as e:
            log.warning("  attachment summarisation skipped: %s", e)

        per_child[child.name] = anns

        json_path = data_path(f"{child.name.lower()}_announcements.json")
        # `ensure_ascii=True` (JSON default) escapes every non-ASCII character
        # to \uXXXX, which sidesteps unpaired-surrogate `UnicodeEncodeError`
        # crashes when announcement bodies contain broken Unicode copy-pasted
        # from Word/Outlook. A crash here would leave a 0-byte JSON and abort
        # the whole scrape loop, so keep this defensive.
        try:
            payload = json.dumps(
                _dict_of_announcements(anns), indent=2, ensure_ascii=True
            )
            json_path.write_text(payload, encoding="utf-8")
        except Exception as e:
            log.exception("  failed to write %s: %s", json_path, e)
            # never leave a truncated file behind
            if json_path.exists() and json_path.stat().st_size == 0:
                try:
                    json_path.unlink()
                except Exception:
                    pass
    return per_child


def smoke_only(cfg: AppConfig, log: logging.Logger) -> int:
    ok_all = True
    for child in cfg.children:
        res = fetch_announcements_html(cfg, child)
        status = "PASS" if res.ok else "FAIL"
        log.info("[SMOKE %s] %s login ok=%s at %s (%ss)%s",
                 status, child.name, res.ok, res.ann_url or res.landing_url,
                 res.elapsed_s, f" err={res.error}" if res.error else "")
        if not res.ok:
            ok_all = False
    return 0 if ok_all else 1


def _filter_by_floor(anns: List[Announcement], floor_iso: str) -> List[Announcement]:
    """Keep only announcements whose source date is on/after the floor."""
    out: List[Announcement] = []
    for a in anns:
        sd = (a.date or a.date_raw or "")[:10]
        if sd and sd < floor_iso:
            continue
        out.append(a)
    return out


def run(cfg: AppConfig, log: logging.Logger, dry_run: bool = False,
        skip_scrape: bool = False, download_attachments: bool = True) -> int:
    set_data_floor(cfg.data_floor_date)
    removed = purge_before(cfg.data_floor_date)
    if removed:
        log.info("Purged %d stale event(s) before %s", removed, cfg.data_floor_date)

    if skip_scrape:
        per_child = {c.name: _load_saved_announcements(c.name) for c in cfg.children}
        log.info("Skipped scrape; loaded %s",
                 {k: len(v) for k, v in per_child.items()})
    else:
        per_child = scrape_all(cfg, log, download_attachments=download_attachments)

    floor_iso = cfg.data_floor_date.isoformat()
    per_child = {name: _filter_by_floor(anns, floor_iso) for name, anns in per_child.items()}
    log.info("After floor %s: %s", floor_iso, {k: len(v) for k, v in per_child.items()})

    per_child_events: Dict[str, List[Event]] = {}
    all_new: List[Event] = []
    for child_name, anns in per_child.items():
        evs = extract_events(child_name, anns)
        per_child_events[child_name] = evs
        all_new.extend(evs)
        log.info("Extracted %d events for %s", len(evs), child_name)

    # Also fold in any deadlines derived from the diary (populated by run_diary.py)
    for child_name in per_child.keys():
        diary_entries = recent_diary_entries(child_name, days=7)
        d_events = extract_events_from_diary(child_name, diary_entries)
        if d_events:
            log.info("Extracted %d diary events for %s", len(d_events), child_name)
            all_new.extend(d_events)

    store = upsert_events(all_new)
    upcoming = upcoming_events(store.values())
    reminders = reminders_due(store.values(), offsets_days=cfg.reminder_offsets_days)
    log.info("Store size=%d, upcoming=%d, reminders_due=%d",
             len(store), len(upcoming), len(reminders))

    digest = build_digest(per_child, per_child_events, reminders, upcoming)
    text_body = render_text(digest)
    html_body = render_html(digest)

    # Always persist the last built digest for inspection.
    out_txt = data_path("last_digest.txt")
    out_html = data_path("last_digest.html")
    out_txt.write_text(text_body, encoding="utf-8")
    out_html.write_text(html_body, encoding="utf-8")
    log.info("Wrote %s and %s", out_txt, out_html)

    if dry_run:
        log.info("DRY-RUN: not sending email. Subject would be: %s", digest.subject)
        print("\n" + text_body)
        return 0

    send_email(cfg.email, digest.subject, text_body, html_body)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MCB daily digest")
    parser.add_argument("--dry-run", action="store_true", help="Skip sending email")
    parser.add_argument("--skip-scrape", action="store_true", help="Reuse last saved JSON")
    parser.add_argument("--no-attachments", action="store_true",
                        help="Don't download PDF attachments during this run")
    parser.add_argument("--smoke", action="store_true",
                        help="Just log in per child and report; no parsing")
    args = parser.parse_args()

    cfg = load_config()
    log = _setup_logging(cfg)
    log.info("Config loaded: %d child(ren), recipients=%s", len(cfg.children), cfg.email.recipients)

    if args.smoke:
        return smoke_only(cfg, log)
    return run(cfg, log, dry_run=args.dry_run, skip_scrape=args.skip_scrape,
               download_attachments=not args.no_attachments)


if __name__ == "__main__":
    sys.exit(main())
