"""Build the daily digest (plain text + HTML) from scraped announcements + events."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List
from zoneinfo import ZoneInfo

from .extractor import Event

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class Digest:
    generated_at: datetime
    today: date
    per_child: Dict[str, dict] = field(default_factory=dict)
    reminders: List[dict] = field(default_factory=list)
    upcoming: List[dict] = field(default_factory=list)

    @property
    def subject(self) -> str:
        return f"MCB Daily Digest — {self.today.isoformat()}"


def _is_today_ist(iso: str, today: date) -> bool:
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).date() == today


def _short(text: str, limit: int = 320) -> str:
    if not text:
        return ""
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def build_digest(per_child_announcements: Dict[str, list],
                 per_child_events: Dict[str, List[Event]],
                 reminders: List[dict],
                 upcoming: List[dict]) -> Digest:
    now = datetime.now(IST)
    today = now.date()

    per_child: Dict[str, dict] = {}
    for child, anns in per_child_announcements.items():
        todays = [a for a in anns if _is_today_ist(a.date, today)]
        # Deadlines/events extracted from today's announcements
        todays_titles = {a.title for a in todays}
        todays_events = [
            e for e in per_child_events.get(child, [])
            if e.source_title in todays_titles
        ]
        per_child[child] = {
            "announcements": todays,
            "todays_events": todays_events,
            "count_all": len(anns),
        }

    return Digest(
        generated_at=now,
        today=today,
        per_child=per_child,
        reminders=reminders,
        upcoming=upcoming,
    )


# ------------------------------ text renderer ------------------------------

def render_text(d: Digest) -> str:
    lines: List[str] = []
    lines.append(f"MCB DAILY DIGEST — {d.today.isoformat()}")
    lines.append(f"Generated {d.generated_at.strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append("=" * 60)

    # Section: Reminders (T-3, T-2, T-1)
    if d.reminders:
        lines.append("")
        lines.append("⚠ REMINDERS (event in 3 / 2 / 1 day(s))")
        lines.append("-" * 60)
        for ev in d.reminders:
            days = ev.get("_days_out")
            when = f"T-{days}" if days is not None else ev["event_date"]
            lines.append(f"• [{ev['child']}] {when} · {ev['event_date']} — {_short(ev['source_title'] or ev['title'], 140)}")
            if ev.get("context"):
                lines.append(f"    “{_short(ev['context'], 220)}”")
    else:
        lines.append("")
        lines.append("⚠ REMINDERS — none in the 3/2/1-day window")

    # Section: Today's announcements per child
    lines.append("")
    lines.append("TODAY'S ANNOUNCEMENTS")
    lines.append("=" * 60)
    for child, blob in d.per_child.items():
        anns = blob["announcements"]
        lines.append("")
        lines.append(f"— {child} —  ({len(anns)} today, {blob['count_all']} in last 30d)")
        if not anns:
            lines.append("  (none received today)")
            continue
        for a in anns:
            lines.append(f"• {a.title or '(untitled)'}   [{a.tag or 'General'}]  {a.date}")
            lines.append(f"    from {a.sender or 'unknown'}")
            if a.content:
                lines.append(f"    {_short(a.content, 300)}")
            if a.attachments:
                lines.append(f"    attachments: {', '.join(x.filename for x in a.attachments)}")
        # Deadlines extracted from today's announcements
        te = blob["todays_events"]
        if te:
            lines.append("  Deadlines/dates detected in today's announcements:")
            for e in te:
                lines.append(f"    → {e.event_date} · {e.kind}: {_short(e.context, 180)}")

    # Section: Upcoming timeline-fixed events
    lines.append("")
    lines.append("UPCOMING TIMELINE-FIXED EVENTS")
    lines.append("=" * 60)
    if not d.upcoming:
        lines.append("(none)")
    else:
        for ev in d.upcoming[:40]:
            lines.append(f"• {ev['event_date']}  [{ev['child']}] {ev['kind']}: {_short(ev['source_title'] or ev['title'], 120)}")
            if ev.get("context"):
                lines.append(f"    “{_short(ev['context'], 200)}”")

    lines.append("")
    lines.append("— end of digest —")
    return "\n".join(lines)


# ------------------------------ HTML renderer ------------------------------

_STYLE = """
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; color:#222; max-width: 780px; margin: 0 auto; padding: 16px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 20px 0 8px; padding-bottom: 4px; border-bottom: 1px solid #e0e0e0; }
  h3 { font-size: 14px; margin: 14px 0 6px; color: #444; }
  .meta { color:#666; font-size:12px; margin-bottom: 12px; }
  .card { border:1px solid #eee; border-radius:8px; padding:10px 12px; margin:8px 0; background:#fafafa; }
  .warn { border-left: 4px solid #d9822b; background:#fff8ee; }
  .tag  { display:inline-block; font-size:11px; background:#eef; color:#334; padding:1px 6px; border-radius:10px; margin-right:6px; }
  .kind-deadline { background:#fde; color:#733; }
  .ctx  { color:#555; font-style:italic; margin-top:4px; }
  .muted{ color:#888; font-size:12px; }
  .none { color:#888; font-style:italic; }
  ul { padding-left: 18px; }
</style>
"""


def _esc(s: str) -> str:
    return html.escape(s or "")


def render_html(d: Digest) -> str:
    parts: List[str] = ["<html><head>", _STYLE, "</head><body>"]
    parts.append(f"<h1>MCB Daily Digest</h1>")
    parts.append(f"<div class='meta'>{_esc(d.today.isoformat())} · generated {_esc(d.generated_at.strftime('%Y-%m-%d %H:%M %Z'))}</div>")

    # Reminders
    parts.append("<h2>⚠ Reminders — event in 3 / 2 / 1 day(s)</h2>")
    if not d.reminders:
        parts.append("<div class='none'>Nothing in the 3/2/1-day window today.</div>")
    else:
        for ev in d.reminders:
            days = ev.get("_days_out")
            label = f"T-{days} day{'s' if days != 1 else ''}"
            parts.append("<div class='card warn'>")
            parts.append(f"<div><span class='tag'>{_esc(ev['child'])}</span>"
                         f"<span class='tag kind-{_esc(ev.get('kind','event'))}'>{label} · {_esc(ev['event_date'])}</span>"
                         f"<strong>{_esc(_short(ev.get('source_title') or ev.get('title',''), 160))}</strong></div>")
            if ev.get("context"):
                parts.append(f"<div class='ctx'>“{_esc(_short(ev['context'], 240))}”</div>")
            parts.append("</div>")

    # Today per child
    parts.append("<h2>Today's announcements</h2>")
    for child, blob in d.per_child.items():
        anns = blob["announcements"]
        parts.append(f"<h3>{_esc(child)} <span class='muted'>· {len(anns)} today · {blob['count_all']} in last 30d</span></h3>")
        if not anns:
            parts.append("<div class='none'>No announcements received today.</div>")
        else:
            for a in anns:
                parts.append("<div class='card'>")
                parts.append(f"<div><span class='tag'>{_esc(a.tag or 'General')}</span>"
                             f"<strong>{_esc(a.title or '(untitled)')}</strong>"
                             f" <span class='muted'>· {_esc(a.date)}</span></div>")
                parts.append(f"<div class='muted'>from {_esc(a.sender)}</div>")
                if a.content:
                    parts.append(f"<div>{_esc(_short(a.content, 480))}</div>")
                if a.attachments:
                    parts.append("<div class='muted'>attachments: " +
                                 ", ".join(_esc(x.filename) for x in a.attachments) + "</div>")
                parts.append("</div>")
        te = blob["todays_events"]
        if te:
            parts.append("<div class='muted'>Deadlines/dates detected in today's announcements:</div><ul>")
            for e in te:
                parts.append(f"<li><strong>{_esc(e.event_date)}</strong> · "
                             f"<span class='tag kind-{_esc(e.kind)}'>{_esc(e.kind)}</span> "
                             f"{_esc(_short(e.context, 240))}</li>")
            parts.append("</ul>")

    # Upcoming
    parts.append("<h2>Upcoming timeline-fixed events</h2>")
    if not d.upcoming:
        parts.append("<div class='none'>No upcoming events on file.</div>")
    else:
        parts.append("<ul>")
        for ev in d.upcoming[:60]:
            parts.append(f"<li><strong>{_esc(ev['event_date'])}</strong> "
                         f"<span class='tag'>{_esc(ev['child'])}</span> "
                         f"<span class='tag kind-{_esc(ev.get('kind','event'))}'>{_esc(ev.get('kind','event'))}</span> "
                         f"{_esc(_short(ev.get('source_title') or ev.get('title',''), 160))}")
            if ev.get("context"):
                parts.append(f"<div class='ctx'>“{_esc(_short(ev['context'], 220))}”</div>")
            parts.append("</li>")
        parts.append("</ul>")

    parts.append("</body></html>")
    return "\n".join(parts)
