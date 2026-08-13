"""Extract timeline-fixed events and deadlines from announcement text.

Deterministic regex-based extractor. It scans title, content, and any
extracted PDF text for date phrases and captures a short "why" snippet.

Design choices:
- Only accept dates that include a year, OR are inside an explicit
  weekday+month phrase (e.g., "Wednesday, October 8"), for which we assume
  the nearest upcoming year.
- Reject dates more than 400 days in the future or more than 30 days in
  the past — those are almost certainly false positives (past references,
  IDs, phone numbers, etc.).
- Duplicate events (same child + same normalized title + same date) are
  collapsed to one.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

MONTH_RX = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
WEEKDAY_RX = (
    r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?"
    r"|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
)

PATTERNS = [
    # Wednesday, October 8, 2025  |  Wednesday, 8 October 2025
    re.compile(rf"\b{WEEKDAY_RX},?\s+({MONTH_RX})\s+(\d{{1,2}})(?:st|nd|rd|th)?,\s*(\d{{4}})\b", re.I),
    re.compile(rf"\b{WEEKDAY_RX},?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_RX}),?\s+(\d{{4}})\b", re.I),
    # October 8, 2025
    re.compile(rf"\b({MONTH_RX})\s+(\d{{1,2}})(?:st|nd|rd|th)?,\s*(\d{{4}})\b", re.I),
    # 8 October 2025 / 8th Oct 2025
    re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_RX}),?\s+(\d{{4}})\b", re.I),
    # Weekday + Month + day (no year): "Wednesday, October 8"
    re.compile(rf"\b{WEEKDAY_RX},?\s+({MONTH_RX})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!,\s*\d)", re.I),
    re.compile(rf"\b{WEEKDAY_RX},?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_RX})\b(?!,?\s*\d{{4}})", re.I),
    # Numeric: 30/07/2026, 30-07-2026, 30.07.2026, and 2-digit year 30/07/26
    re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b"),
    re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2})\b"),
]

# Relaxed patterns used ONLY for diary text (homework descriptions), where the
# writer routinely drops weekday and year. Assume the current academic year.
DIARY_EXTRA_PATTERNS = [
    # "3rd August" / "August 3" — no weekday, no year
    re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_RX})\b(?!,?\s*\d{{4}})", re.I),
    re.compile(rf"\b({MONTH_RX})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!,\s*\d)", re.I),
    # dd/mm without year (assume current-year-or-next via _pick_year)
    re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})\b(?![/\-.]\d)"),
]

# Diary relative-deadline patterns. These are homework-specific — a teacher
# writing "prepare X for Monday" or "submit by tomorrow" is stating a
# deadline. We require a leading trigger word ("by", "for", "on", "before",
# "due", "submit", "till", "until") to reduce false positives on stray
# weekday mentions inside topic descriptions.
_DEADLINE_TRIGGER = r"(?:by|for|on|before|due(?:\s+on)?|submit(?:\s+by|\s+on)?|till|until|due\s+date\s*:?)"
DIARY_WEEKDAY_RX = re.compile(
    rf"\b{_DEADLINE_TRIGGER}\s+(?:next\s+)?({WEEKDAY_RX})\b",
    re.I,
)
# "tomorrow" / "day after tomorrow" / "next class". "next class" is skipped —
# too ambiguous.
DIARY_RELATIVE_RX = re.compile(
    rf"\b{_DEADLINE_TRIGGER}\s+(tomorrow|day\s+after\s+tomorrow)\b",
    re.I,
)

# Map weekday tokens to isoweekday numbers (Mon=1..Sun=7).
_WEEKDAY_TO_ISO = {
    "mon": 1, "monday": 1,
    "tue": 2, "tues": 2, "tuesday": 2,
    "wed": 3, "wednesday": 3,
    "thu": 4, "thur": 4, "thurs": 4, "thursday": 4,
    "fri": 5, "friday": 5,
    "sat": 6, "saturday": 6,
    "sun": 7, "sunday": 7,
}

CONTEXT_RX = re.compile(r"[^.!?\n]*?\b{keyword}\b[^.!?\n]*[.!?]?", re.I)

# Keywords that increase confidence that a date really is an event/deadline.
EVENT_KEYWORDS = [
    "celebrate", "celebration", "celebrated", "on ", "by ",
    "held on", "scheduled", "closes", "close on", "closing", "last date",
    "deadline", "due", "submit by", "submission", "before",
    "meeting", "ptm", "assembly", "holiday", "vacation", "break",
    "exam", "test", "event", "trip", "picnic", "workshop", "webinar",
    "guest lecture", "lecture", "orientation", "session", "presentation",
    "competition", "contest", "day", "week",
]


@dataclass
class Event:
    child: str
    title: str
    event_date: str          # ISO date YYYY-MM-DD
    context: str
    source_title: str
    source_date: str         # ISO datetime of the announcement itself
    source_sender: str
    tag: str
    kind: str = "event"      # or "deadline"
    event_id: str = ""
    source_url: str = ""     # first attachment URL, if any — for "open" links

    def compute_id(self) -> str:
        norm = re.sub(r"\W+", "_", self.source_title.lower()).strip("_")
        self.event_id = hashlib.sha1(
            f"{self.child}|{norm}|{self.event_date}".encode("utf-8")
        ).hexdigest()[:16]
        return self.event_id

    def to_dict(self) -> dict:
        return asdict(self)


def _today_ist() -> date:
    return datetime.now(IST).date()


_data_floor: date | None = None


def set_data_floor(d: date | None) -> None:
    """Configure the earliest event date to accept. Called by main/agent."""
    global _data_floor
    _data_floor = d


def _valid_event_date(d: date) -> bool:
    today = _today_ist()
    floor = _data_floor if _data_floor is not None else (today - timedelta(days=30))
    if d < floor:
        return False
    if d > today + timedelta(days=400):
        return False
    return True


def _month_num(s: str) -> Optional[int]:
    return MONTHS.get(s.lower())


def _pick_year(month: int, day: int) -> int:
    """For dateless matches, pick current year if the date hasn't passed by >7 days, else next year."""
    today = _today_ist()
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return today.year
    if candidate >= today - timedelta(days=7):
        return today.year
    return today.year + 1


def _extract_dates(text: str) -> List[date]:
    if not text:
        return []
    out: List[date] = []

    for rx in PATTERNS[:2]:
        for m in rx.finditer(text):
            groups = m.groups()
            try:
                if groups[0].isdigit():
                    day = int(groups[0]); month = _month_num(groups[1]); year = int(groups[2])
                else:
                    month = _month_num(groups[0]); day = int(groups[1]); year = int(groups[2])
                if month:
                    out.append(date(year, month, day))
            except (ValueError, TypeError):
                continue

    for m in PATTERNS[2].finditer(text):
        try:
            month = _month_num(m.group(1)); day = int(m.group(2)); year = int(m.group(3))
            if month:
                out.append(date(year, month, day))
        except (ValueError, TypeError):
            continue
    for m in PATTERNS[3].finditer(text):
        try:
            day = int(m.group(1)); month = _month_num(m.group(2)); year = int(m.group(3))
            if month:
                out.append(date(year, month, day))
        except (ValueError, TypeError):
            continue

    for rx in PATTERNS[4:6]:
        for m in rx.finditer(text):
            groups = m.groups()
            try:
                if groups[0].isdigit():
                    day = int(groups[0]); month = _month_num(groups[1])
                else:
                    month = _month_num(groups[0]); day = int(groups[1])
                if month:
                    year = _pick_year(month, day)
                    out.append(date(year, month, day))
            except (ValueError, TypeError):
                continue

    # Numeric dd/mm/yyyy
    for m in PATTERNS[6].finditer(text):
        try:
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                out.append(date(year, month, day))
        except ValueError:
            continue

    # Numeric dd/mm/yy  (assume 20xx)
    for m in PATTERNS[7].finditer(text):
        try:
            day, month, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            year = 2000 + yy
            if 1 <= month <= 12 and 1 <= day <= 31:
                out.append(date(year, month, day))
        except ValueError:
            continue

    # Dedup, filter to plausible window
    return sorted({d for d in out if _valid_event_date(d)})


def _next_weekday(from_day: date, iso_weekday: int) -> date:
    """Return the next date on/after `from_day+1` whose ISO weekday matches.
    Never returns `from_day` itself — if a teacher writes "by Monday" while
    posting on a Monday, they mean the following Monday."""
    delta = (iso_weekday - from_day.isoweekday()) % 7
    if delta == 0:
        delta = 7
    return from_day + timedelta(days=delta)


def _extract_dates_diary(text: str, ref_date: date | None = None) -> List[date]:
    """Extract dates from diary/homework text using both the standard patterns
    and the more relaxed diary-only patterns (assume current year when the
    writer omits it).

    `ref_date` — the diary entry's own date. Used to resolve relative refs
    like "by tomorrow" or "for Monday". Defaults to today (IST) if omitted so
    behavior stays backwards-compatible for older callers.
    """
    if not text:
        return []
    ref = ref_date or _today_ist()
    out: List[date] = list(_extract_dates(text))

    # Relaxed: "3rd August" / "August 3rd" — no weekday, no year
    for m in DIARY_EXTRA_PATTERNS[0].finditer(text):
        try:
            day = int(m.group(1))
            month = _month_num(m.group(2))
            if month and 1 <= day <= 31:
                year = _pick_year(month, day)
                out.append(date(year, month, day))
        except (ValueError, TypeError):
            continue
    for m in DIARY_EXTRA_PATTERNS[1].finditer(text):
        try:
            month = _month_num(m.group(1))
            day = int(m.group(2))
            if month and 1 <= day <= 31:
                year = _pick_year(month, day)
                out.append(date(year, month, day))
        except (ValueError, TypeError):
            continue
    # Relaxed: dd/mm without year
    for m in DIARY_EXTRA_PATTERNS[2].finditer(text):
        try:
            day, month = int(m.group(1)), int(m.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                year = _pick_year(month, day)
                out.append(date(year, month, day))
        except ValueError:
            continue

    # Relative refs — only when preceded by an explicit deadline trigger
    # ("by tomorrow", "for Monday", "submit by Friday", etc.).
    for m in DIARY_RELATIVE_RX.finditer(text):
        token = m.group(1).lower().replace("  ", " ").strip()
        if token == "tomorrow":
            out.append(ref + timedelta(days=1))
        elif token.startswith("day after"):
            out.append(ref + timedelta(days=2))
    for m in DIARY_WEEKDAY_RX.finditer(text):
        wd = _WEEKDAY_TO_ISO.get(m.group(1).lower())
        if wd:
            out.append(_next_weekday(ref, wd))

    return sorted({d for d in out if _valid_event_date(d)})


def _snippet_for(text: str, event_date: date) -> str:
    """Return a short sentence containing the date phrase, if we can find one."""
    if not text:
        return ""
    day = event_date.day
    month = event_date.strftime("%B")
    month_abbr = event_date.strftime("%b")
    year = event_date.year
    keys = [f"{month} {day}", f"{month_abbr} {day}", f"{day} {month}", f"{day} {month_abbr}",
            f"{day:02d}/{event_date.month:02d}/{year}", f"{day:02d}/{event_date.month:02d}/{year%100:02d}"]
    for k in keys:
        m = re.search(rf"([^.!?\n]{{0,120}}{re.escape(k)}[^.!?\n]{{0,120}})", text, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _classify(context: str) -> str:
    ctx = (context or "").lower()
    for kw in ["deadline", "last date", "due", "submit by", "closes", "close on", "closing", "before "]:
        if kw in ctx:
            return "deadline"
    return "event"


def extract_events(child: str, announcements) -> List[Event]:
    """Given a list of Announcement objects, produce a deduped list of Event."""
    seen = {}
    for ann in announcements:
        blob_parts = [ann.title or "", ann.content or ""]
        for att in getattr(ann, "attachments", []) or []:
            if getattr(att, "content", ""):
                blob_parts.append(att.content)
        blob = "\n".join(blob_parts)

        # First attachment URL becomes the "open the announcement" link:
        # the PDF is where the deadline typically lives and it opens directly
        # from Telegram without needing to log in to the portal.
        first_url = ""
        for att in getattr(ann, "attachments", []) or []:
            u = getattr(att, "url", "") or ""
            if u:
                first_url = u
                break

        for d in _extract_dates(blob):
            snippet = _snippet_for(blob, d) or (ann.title or "")
            ev = Event(
                child=child,
                title=(ann.title or snippet)[:180],
                event_date=d.isoformat(),
                context=snippet[:400],
                source_title=ann.title or "",
                source_date=ann.date or "",
                source_sender=ann.sender or "",
                tag=ann.tag or "",
                kind=_classify(snippet),
                source_url=first_url,
            )
            eid = ev.compute_id()
            # Keep the richest context if we see duplicates.
            prev = seen.get(eid)
            if prev is None or len(ev.context) > len(prev.context):
                seen[eid] = ev

    events = list(seen.values())
    events.sort(key=lambda e: e.event_date)
    return events


def extract_events_from_diary(child: str, diary_entries) -> List[Event]:
    """Extract deadline/event records from parsed DiaryEntry rows.

    We reuse the same date-phrase scanner as `extract_events`, but source
    text is the diary description and source_date is the diary_date.
    Homework/Classwork entries default to `kind="deadline"` unless the
    context clearly points at an event.
    """
    seen = {}
    for de in diary_entries or []:
        text = de.description or ""
        if not text:
            continue
        # Use the diary entry's own date as the reference for relative refs
        # like "by tomorrow" or "for Monday" — otherwise a stale scrape would
        # resolve them against today instead of the day the teacher wrote them.
        ref_date: date | None = None
        try:
            if de.diary_date:
                ref_date = date.fromisoformat(de.diary_date[:10])
        except ValueError:
            pass
        found = _extract_dates_diary(text, ref_date=ref_date)
        for d in found:
            snippet = _snippet_for(text, d) or text[:200]
            base_kind = _classify(snippet)
            # Homework/Classwork/Assignment default to deadline
            if base_kind == "event" and (de.kind or "").lower() in (
                    "homework", "classwork", "assignment", "project"):
                base_kind = "deadline"
            title = f"[{de.subject}] {de.kind}: " + " ".join(text.split())[:120]
            ev = Event(
                child=child,
                title=title[:180],
                event_date=d.isoformat(),
                context=snippet[:400],
                source_title=f"{de.subject} — {de.kind}",
                source_date=de.diary_date,
                source_sender=de.teacher or "",
                tag=f"Diary:{de.subject}",
                kind=base_kind,
            )
            eid = ev.compute_id()
            prev = seen.get(eid)
            if prev is None or len(ev.context) > len(prev.context):
                seen[eid] = ev

    events = list(seen.values())
    events.sort(key=lambda e: e.event_date)
    return events
