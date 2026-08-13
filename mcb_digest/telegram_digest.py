"""Shared Telegram-format digest builder.

Used by both the interactive bot (`mcb_digest.channels.telegram_bot`) and the
one-shot pusher (`push_digest.py`) that is invoked by the Windows Scheduled
Task after each scrape.

Kept deliberately free of `telegram.ext` types so callers can be either the
async bot or a plain HTTP client.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from mcb_digest.agent import Agent
from mcb_digest.config import data_path
from mcb_digest.diary_store import entries_on, recent_entries
from mcb_digest.extractor import (
    _extract_dates_diary,
    extract_events,
    extract_events_from_diary,
    set_data_floor,
)
from mcb_digest.reminders import (
    reminders_due,
    upcoming_events,
    upsert_events,
)

IST = ZoneInfo("Asia/Kolkata")

TELEGRAM_MSG_LIMIT = 3800  # a little under the hard 4096 cap

# Hour-of-day (IST) used as the fallback cutoff for the morning push. The
# evening scrape runs at 19:00, so any announcement posted after this hour on
# the previous day would first be scraped the next morning — and we want the
# morning push to still surface it. Kept slightly before 19:00 so posts that
# slipped in during the evening scrape are also included.
_MORNING_LOOKBACK_FROM_HOUR = 18

# At the evening push, deadlines dated TODAY are usually already done — showing
# them adds noise. We keep an exception for things that stay actionable in the
# evening: online registrations, fee payments, form submissions with a
# midnight/EOD deadline. Match against title/context/tag.
_EVENING_KEEP_TODAY_RX = re.compile(
    r"registration|register|fee\b|fees\b|payment|pay online|last date "
    r"for payment|form submission|submit .*form",
    re.I,
)


def _is_registration_or_fee(ev: dict) -> bool:
    """Return True if an event dict looks like a registration/fee deadline
    that a parent might still act on in the evening."""
    blob = " ".join([
        str(ev.get("title", "")),
        str(ev.get("context", "")),
        str(ev.get("source_title", "")),
        str(ev.get("tag", "")),
    ])
    return bool(_EVENING_KEEP_TODAY_RX.search(blob))


# ---------- formatting helpers ----------

def esc(s: str) -> str:
    """HTML-escape user/agent-generated text so it renders safely as HTML."""
    if not s:
        return ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def pretty_date(d: date | str, today: date | None = None) -> str:
    """Format 2026-07-29 as 'Wed, 29 Jul (tomorrow)' / '(in 3d)' / '(today)'."""
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except ValueError:
            return d
    today = today or datetime.now(IST).date()
    base = d.strftime("%a, %d %b")
    days = (d - today).days
    if days == 0:
        return f"{base} (today)"
    if days == 1:
        return f"{base} (tomorrow)"
    if days == -1:
        return f"{base} (yesterday)"
    if 2 <= days <= 6:
        return f"{base} (in {days}d)"
    return base


def clip(text: str, max_len: int = 260) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[:max_len].rstrip() + "…"


def _open_link(url: str, label: str = "open") -> str:
    """Return a leading-space ` (📄 <a href=...>open</a>)` fragment if url is
    a plausible http(s) link, else empty string. Safe to concatenate onto any
    reminder / event line.
    """
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return ""
    safe = esc(url)
    return f" (📄 <a href=\"{safe}\">{label}</a>)"


def chunk_for_telegram(text: str, limit: int = TELEGRAM_MSG_LIMIT) -> List[str]:
    """Split text into <=limit-char chunks on line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


# ---------- announcement freshness window ----------

def _parse_ann_datetime(a) -> Optional[datetime]:
    """Return the announcement's posting time as an IST-aware datetime, or
    None if it can't be parsed. Accepts ISO 8601 (with or without tz) from
    `a.date` and falls back to the date-only prefix (assumed 00:00 IST)."""
    raw = getattr(a, "date", None) or getattr(a, "date_raw", None) or ""
    if not raw:
        return None
    s = str(raw).strip()
    # Try full ISO first (e.g. "2026-08-05T21:03:00+05:30").
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except ValueError:
        pass
    # Fall back to date-only prefix.
    try:
        d = date.fromisoformat(s[:10])
        return datetime(d.year, d.month, d.day, tzinfo=IST)
    except ValueError:
        return None


def _freshness_cutoff(slot: Optional[str], today: date) -> datetime:
    """Return the earliest posting time an announcement can have and still
    count as 'new' for this push slot.

    - morning / full: yesterday 18:00 IST — catches anything posted after the
      previous evening's 19:00 scrape.
    - evening: today 00:00 IST — only today's postings.
    - anything else (e.g. homework): today 00:00 IST.
    """
    if slot == "morning" or slot is None:
        yday = today - timedelta(days=1)
        return datetime(yday.year, yday.month, yday.day,
                        _MORNING_LOOKBACK_FROM_HOUR, 0, tzinfo=IST)
    return datetime(today.year, today.month, today.day, tzinfo=IST)


def _fresh_announcements(anns, cutoff: datetime):
    """Return announcements at or after `cutoff`, newest-first, keeping the
    original list order among items that share a timestamp / are unparseable.
    Unparseable-date items are dropped (they can't be judged 'new')."""
    out = []
    for a in anns:
        dt = _parse_ann_datetime(a)
        if dt is None:
            continue
        if dt >= cutoff:
            out.append(a)
    return out


# ---------- digest builder ----------

_MD_BOLD_RX = None


def _md_bold_to_html(text: str) -> str:
    """Convert **bold** → <b>bold</b>. HTML-escapes everything else first so
    the LLM output can't inject tags."""
    global _MD_BOLD_RX
    if _MD_BOLD_RX is None:
        import re as _re
        _MD_BOLD_RX = _re.compile(r"\*\*(.+?)\*\*", _re.S)
    return _MD_BOLD_RX.sub(r"<b>\1</b>", esc(text))


def _headline(today: date, slot: Optional[str]) -> str:
    stamp = today.strftime("%a, %d %b %Y")
    if slot == "morning":
        return f"🌅 <b>Good morning — {stamp}</b>"
    if slot == "evening":
        return f"🌇 <b>Evening summary — {stamp}</b>"
    if slot == "homework":
        return f"📔 <b>Homework — {stamp}</b>"
    return f"📅 <b>MCB Daily Digest — {stamp}</b>"


def _mode_for(slot: Optional[str]) -> str:
    """Map slot label to render mode.

    Modes:
      "full"     — reminders + announcements + homework + upcoming (default)
      "morning"  — same as full
      "evening"  — reminders + announcements + LLM note + upcoming (NO homework)
      "homework" — homework only
    """
    if slot == "evening":
        return "evening"
    if slot == "homework":
        return "homework"
    return "full"


def _render_homework(child_name: str, today_iso: str, today: date,
                      indent: str = "  ") -> List[str]:
    """Return the 📔 Homework block lines for one child (empty if none)."""
    de_today = entries_on(child_name, today_iso)
    if not de_today:
        return []
    out: List[str] = [f"{indent}📔 <b>Homework — {len(de_today)}</b>"]
    by_subject: dict[str, list] = {}
    for de in de_today:
        by_subject.setdefault(de.subject, []).append(de)
    for subj, des in sorted(by_subject.items()):
        out.append(f"{indent}  <b>{esc(subj)}</b>")
        for de in des:
            kind = esc(de.kind or "Note")
            desc = esc(clip(de.description or "", max_len=220))
            teacher = esc(de.teacher or "")
            out.append(f"{indent}    • <i>{kind}</i> — {desc}")
            active = [d for d in _extract_dates_diary(de.description or "")
                      if d >= today]
            if active:
                due = min(active)
                out.append(
                    f"{indent}      ⏰ <b>due {esc(pretty_date(due.isoformat(), today))}</b>"
                )
            if teacher:
                out.append(f"{indent}      <i>from {teacher}</i>")
    return out


def _todays_note_html(agent: Agent, today_iso: str,
                      cutoff: datetime) -> str:
    """Ask the LLM for a short 2-3 sentence note on the announcements that
    are 'new' as of this push (see `_freshness_cutoff`). Returns empty string
    if there are no fresh announcements or the LLM call fails for any reason
    (we never want a nudge push to be blocked by LLM downtime)."""
    lines: List[str] = []
    for child_name, anns in agent.ctx.per_child.items():
        todays = _fresh_announcements(anns, cutoff)
        for a in todays:
            body = " ".join((a.content or "").split())
            if len(body) > 800:
                body = body[:800] + "…"
            entry = (
                f"- [{child_name}] {a.tag or 'General'} — {a.title or ''}\n"
                f"  from: {a.sender or ''}\n"
                f"  body: {body}"
            )
            for att in (getattr(a, "attachments", None) or []):
                s = (getattr(att, "summary", "") or "").strip()
                if s:
                    entry += (
                        f"\n  attachment ({getattr(att, 'filename', 'file')}): {s}"
                    )
            lines.append(entry)
    if not lines:
        return ""

    prompt = (
        "You are the family school-assistant. Below are today's school "
        "announcements for one or more children. Write a concise 2–3 sentence "
        "'note of the day' the parent should keep in mind. Use **bold** for "
        "names, dates, and key actions. Do NOT invent details or repeat every "
        "item. Prefer highlighting deadlines, action items, or urgent messages.\n\n"
        "Announcements:\n" + "\n".join(lines)
    )
    try:
        raw = agent.llm.chat(
            [{"role": "user", "content": prompt}],
            max_output_tokens=220,
            temperature=0.2,
        )
    except Exception as e:
        import logging
        logging.getLogger("mcb_digest.telegram_digest").warning(
            "LLM note-of-the-day failed: %s", e)
        return ""
    raw = (raw or "").strip()
    if not raw:
        return ""
    html = _md_bold_to_html(raw)
    # Persist so the next morning's push can roll it over.
    _save_note(today_iso, html)
    return html


# ---------- note store (rollover into next morning) ----------

_NOTE_STORE = data_path("notes.json")


def _load_notes() -> dict:
    try:
        return json.loads(_NOTE_STORE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.getLogger("mcb_digest.telegram_digest").warning(
            "note store unreadable: %s", e)
        return {}


def _save_note(iso_date: str, html: str) -> None:
    if not iso_date or not html:
        return
    store = _load_notes()
    store[iso_date] = html
    # Keep the store small — only the last 14 days.
    try:
        cutoff = (datetime.now(IST).date() - timedelta(days=14)).isoformat()
        store = {k: v for k, v in store.items() if k >= cutoff}
    except Exception:
        pass
    try:
        _NOTE_STORE.parent.mkdir(parents=True, exist_ok=True)
        _NOTE_STORE.write_text(
            json.dumps(store, indent=2, ensure_ascii=True), encoding="utf-8"
        )
    except Exception as e:
        logging.getLogger("mcb_digest.telegram_digest").warning(
            "note store write failed: %s", e)


def _load_yesterdays_note(today_iso: str) -> str:
    """Return the stored HTML note for the day before `today_iso`, or ""."""
    try:
        yday = (date.fromisoformat(today_iso) - timedelta(days=1)).isoformat()
    except ValueError:
        return ""
    return _load_notes().get(yday, "")


def build_html_digest(agent: Optional[Agent] = None, *,
                      slot: Optional[str] = None,
                      mode: Optional[str] = None) -> str:
    """Return a Telegram-HTML digest string.

    `slot` picks the header wording ("morning" / "evening" / "homework").
    `mode` (if given) overrides the section mix; otherwise derived from slot.
    Returns "" for slot="homework" when no child has homework today, so the
    caller can skip the push.
    """
    agent = agent or Agent()
    cfg = agent.cfg
    set_data_floor(cfg.data_floor_date)
    per_child = agent.ctx.per_child

    render_mode = mode or _mode_for(slot)

    per_child_events = {name: extract_events(name, anns)
                        for name, anns in per_child.items()}

    # Diary-derived events (homework deadlines)
    per_child_diary = {c.name: recent_entries(c.name, days=7)
                       for c in cfg.children}
    diary_events = []
    for name, entries in per_child_diary.items():
        diary_events.extend(extract_events_from_diary(name, entries))

    all_events: list = []
    for evs in per_child_events.values():
        all_events.extend(evs)
    all_events.extend(diary_events)
    store = upsert_events(all_events)
    today = datetime.now(IST).date()
    today_iso = today.isoformat()
    ups = upcoming_events(store.values(), today=today)
    reminds = reminders_due(store.values(), today=today,
                            offsets_days=cfg.reminder_offsets_days)

    parts: List[str] = [_headline(today, slot)]

    # ---- homework-only mode ----
    if render_mode == "homework":
        had_content = False
        for child_name in per_child.keys():
            block = _render_homework(child_name, today_iso, today, indent="")
            if block:
                had_content = True
                parts.append(f"\n<b>{esc(child_name)}</b>")
                parts.extend(block)
        if not had_content:
            return ""  # signal caller to skip
        return "\n".join(parts)

    # ---- full / evening modes ----
    # Evening cleanup: hide today-dated items from Reminders/Upcoming unless
    # they look like registration/fee deadlines (still actionable in the
    # evening). Morning/full push keeps today's items — that's the point.
    def _hide_today(ev: dict) -> bool:
        if render_mode != "evening":
            return False
        if (ev.get("event_date") or "")[:10] != today_iso:
            return False
        return not _is_registration_or_fee(ev)

    if reminds:
        visible_reminds = [ev for ev in reminds if not _hide_today(ev)]
        if visible_reminds:
            parts.append("\n⏰ <b>Reminders</b>")
            for ev in visible_reminds:
                days = ev.get("_days_out", "?")
                when = "today" if days == 0 else f"in {days}d"
                child = esc(ev.get("child", ""))
                title = esc(ev.get("title", ""))
                ed = pretty_date(ev.get("event_date", ""), today)
                link = _open_link(ev.get("source_url", ""))
                parts.append(
                    f"  • <b>{child}</b> — {title}{link}\n    <i>{esc(ed)} · {when}</i>"
                )

    ann_cutoff = _freshness_cutoff(slot, today)
    ann_label = "new since yesterday evening" if slot == "morning" else "today"

    # Evening: LLM "Note of the day" spans across children, shown once up front
    if render_mode == "evening":
        note = _todays_note_html(agent, today_iso, ann_cutoff)
        if note:
            parts.append(f"\n📝 <b>Note on today's announcements</b>\n  {note}")

    # Morning (full): roll over yesterday's evening note so the parent sees
    # yesterday's summary again fresh in the morning.
    if render_mode == "full":
        yday_note = _load_yesterdays_note(today_iso)
        if yday_note:
            parts.append(
                f"\n📝 <b>Yesterday's note (recap)</b>\n  {yday_note}"
            )

    # Today's / new-since-last-push announcements per child (+ homework if full)
    for child_name, anns in per_child.items():
        todays = _fresh_announcements(anns, ann_cutoff)
        recent_count = len(anns)
        parts.append(f"\n📢 <b>{esc(child_name)}</b> — {len(todays)} {ann_label} · {recent_count} in last 30d")
        if not todays:
            parts.append("  <i>Nothing new.</i>")
        else:
            for a in todays[:5]:
                sender = esc(a.sender or "")
                title = esc(a.title or "(untitled)")
                tag = esc(a.tag or "")
                body = esc(clip(a.content or ""))
                head = f"  • <b>{title}</b>"
                if tag:
                    head += f"  <i>[{tag}]</i>"
                parts.append(head)
                if sender:
                    parts.append(f"    <i>from {sender}</i>")
                if body:
                    parts.append(f"    {body}")
                # Attachment summaries (LLM-derived, cached on the Attachment)
                for att in (getattr(a, "attachments", None) or []):
                    s = (getattr(att, "summary", "") or "").strip()
                    if not s:
                        continue
                    fname = esc(getattr(att, "filename", "") or "attachment")
                    parts.append(
                        f"    📎 <b>{fname}</b>: {_md_bold_to_html(s)}"
                    )

        # Homework only in "full" mode (morning push). Evening push omits it.
        if render_mode == "full":
            parts.extend(_render_homework(child_name, today_iso, today))

    # Upcoming
    if ups:
        visible_ups = [ev for ev in ups if not _hide_today(ev)]
        if visible_ups:
            parts.append("\n🗓 <b>Upcoming</b>")
            for ev in visible_ups[:8]:
                ed = pretty_date(ev.get("event_date", ""), today)
                child = esc(ev.get("child", ""))
                title = esc(ev.get("title", ""))
                kind_glyph = "⏰" if ev.get("kind") == "deadline" else "•"
                link = _open_link(ev.get("source_url", ""))
                parts.append(
                    f"  {kind_glyph} {esc(ed)} — <b>{child}</b>: {title}{link}"
                )

    return "\n".join(parts)
