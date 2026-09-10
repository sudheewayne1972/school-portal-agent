"""MCB Agent: answer questions about the kids' school announcements.

Design: context-stuff every announcement (post-floor) into a system prompt.
Cheap and simple; fine while total announcements stay under a few hundred.
Later we can swap in retrieval (FAISS/embeddings) without changing callers.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .config import AppConfig, ROOT, data_path, load_config
from .diary_parser import DiaryEntry
from .diary_store import recent_entries as recent_diary_entries
from .extractor import (
    Event,
    extract_events,
    extract_events_from_diary,
    set_data_floor,
)
from .llm import LLM
from .parser import Announcement, Attachment
from .reminders import load_all_events, upcoming_events

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

SYSTEM_PROMPT = """You are the family school-assistant for the {parent_names} family.
You have access to school announcements AND the class-diary (homework, classwork,
assignments, notes) from the CHIREC MyClassBoard portal for their children:
{children}.

Answer questions concisely and factually, grounded ONLY in the announcements,
class-diary entries, and events supplied below. If the answer is not in the
context, say so plainly — do NOT invent details. When you cite a specific item,
mention the child's name, the date, subject/sender when available.

Today's date (Asia/Kolkata): {today}.
Data floor: only announcements on or after {floor} are included. Class-diary
covers the last 7 days.

Output style — you are replying in a Telegram chat:
* Keep answers short. Two or three lines when possible; use bullets only for
  genuine lists.
* For bullets, use "• " (a real bullet character) at the start of a line.
* Use **bold** for names, dates, and deadlines — do NOT use headings, code
  fences, tables, or emoji-heavy decoration.
* For deadlines, always write the date as "Wed, 30 Jul 2026" and include how
  many days away it is: e.g. "in 3 days" or "tomorrow" or "today".
* When you don't have enough info, say "Not in the announcements I have."
"""


@dataclass
class AgentContext:
    per_child: Dict[str, List[Announcement]]
    events: List[dict]
    upcoming: List[dict]
    per_child_diary: Dict[str, List[DiaryEntry]] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.per_child_diary is None:
            self.per_child_diary = {}


def _load_announcements_for(child_name: str, floor_iso: str) -> List[Announcement]:
    """Load cached announcements for one child from the usual places, floor-filtered."""
    candidates = [
        data_path(f"{child_name.lower()}_announcements.json"),
        ROOT / f"{child_name.lower()}_announcements.json",
        ROOT / "attachments" / f"{child_name.lower()}_announcements.json",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not load %s: %s", path, e)
        return []
    out: List[Announcement] = []
    for r in raw:
        sd = (r.get("date") or r.get("date_raw") or "")[:10]
        if sd and sd < floor_iso:
            continue
        a = Announcement(
            sender=r.get("sender", ""), tag=r.get("tag", ""),
            date=r.get("date", ""), date_raw=r.get("date_raw", ""),
            title=r.get("title", ""), content=r.get("content", ""),
        )
        for att in r.get("attachments", []) or []:
            a.attachments.append(Attachment(
                url=att.get("url", ""),
                filename=att.get("filename") or att.get("file", ""),
                content=att.get("content") or att.get("extracted_text", ""),
            ))
        out.append(a)
    return out


def load_context(cfg: Optional[AppConfig] = None) -> AgentContext:
    cfg = cfg or load_config()
    set_data_floor(cfg.data_floor_date)
    floor_iso = cfg.data_floor_date.isoformat()

    per_child: Dict[str, List[Announcement]] = {}
    per_child_diary: Dict[str, List[DiaryEntry]] = {}
    for child in cfg.children:
        per_child[child.name] = _load_announcements_for(child.name, floor_iso)
        per_child_diary[child.name] = recent_diary_entries(child.name, days=7)

    events = load_all_events()
    upcoming = upcoming_events(events)
    return AgentContext(per_child=per_child, events=events,
                        upcoming=upcoming, per_child_diary=per_child_diary)


def _render_announcement(a: Announcement, i: int) -> str:
    date_str = (a.date or a.date_raw or "unknown date")[:10]
    parts = [f"[{i}] ({date_str}) [{a.tag or 'General'}] {a.title}"]
    if a.sender:
        parts.append(f"    from: {a.sender}")
    if a.content:
        body = " ".join(a.content.split())
        if len(body) > 800:
            body = body[:800] + "…"
        parts.append(f"    {body}")
    for att in a.attachments or []:
        if att.content:
            snippet = " ".join(att.content.split())
            if len(snippet) > 600:
                snippet = snippet[:600] + "…"
            parts.append(f"    attachment {att.filename}: {snippet}")
        elif att.filename:
            parts.append(f"    attachment: {att.filename}")
    return "\n".join(parts)


def _render_events(events: List[dict]) -> str:
    if not events:
        return "(no upcoming events tracked yet)"
    lines = []
    for ev in events[:60]:
        lines.append(
            f"- {ev.get('event_date')} — [{ev.get('child')}] {ev.get('title')} "
            f"({ev.get('kind', 'event')})"
        )
    return "\n".join(lines)


def _render_diary(entries: List[DiaryEntry]) -> str:
    if not entries:
        return "(no diary entries in the current window)"
    lines: List[str] = []
    by_date: Dict[str, List[DiaryEntry]] = {}
    for e in entries:
        by_date.setdefault(e.diary_date, []).append(e)
    for iso in sorted(by_date.keys(), reverse=True):
        lines.append(f"- {iso}:")
        for e in by_date[iso]:
            desc = " ".join((e.description or "").split())
            if len(desc) > 400:
                desc = desc[:400] + "…"
            teacher = f" (from {e.teacher})" if e.teacher else ""
            lines.append(f"    * [{e.subject}] {e.kind}{teacher}: {desc}")
    return "\n".join(lines)


def build_context_prompt(ctx: AgentContext, cfg: AppConfig) -> str:
    today = datetime.now(IST).date().isoformat()
    parts = [f"# CONTEXT (assembled {today} IST)\n"]

    parts.append("## Upcoming timeline events (from event store)\n")
    parts.append(_render_events(ctx.upcoming))
    parts.append("")

    for child_name, anns in ctx.per_child.items():
        parts.append(f"## Announcements for {child_name} ({len(anns)} items)\n")
        if not anns:
            parts.append("(none in the current window)\n")
        else:
            for i, a in enumerate(anns, 1):
                parts.append(_render_announcement(a, i))
        parts.append("")

        diary = (ctx.per_child_diary or {}).get(child_name, [])
        parts.append(
            f"## Class-diary / Homework for {child_name} "
            f"(last 7 days, {len(diary)} entries)\n"
        )
        parts.append(_render_diary(diary))
        parts.append("")

    return "\n".join(parts)


class Agent:
    def __init__(self, cfg: Optional[AppConfig] = None, llm: Optional[LLM] = None) -> None:
        self.cfg = cfg or load_config()
        self.llm = llm or LLM()
        self.ctx = load_context(self.cfg)
        self.history: List[dict] = []

    @property
    def system_prompt(self) -> str:
        children = ", ".join(c.name for c in self.cfg.children) or "(none)"
        today = datetime.now(IST).date().isoformat()
        floor = self.cfg.data_floor_date.isoformat()
        parents = os.environ.get("PARENT_NAMES", "the parents")
        prelude = SYSTEM_PROMPT.format(
            parent_names=parents, children=children, today=today, floor=floor
        )
        return prelude + "\n\n" + build_context_prompt(self.ctx, self.cfg)

    def reload_context(self) -> None:
        self.ctx = load_context(self.cfg)

    def ask(self, question: str, requester: Optional[str] = None) -> str:
        self.reload_context()
        user = question.strip()
        if requester:
            user = f"(from {requester}) {user}"
        messages: List[dict] = [{"role": "system", "content": self.system_prompt}]
        # Include short-window history (last 6 turns) for follow-ups.
        messages.extend(self.history[-6:])
        messages.append({"role": "user", "content": user})
        answer = self.llm.chat(messages)
        self.history.append({"role": "user", "content": user})
        self.history.append({"role": "assistant", "content": answer})
        return answer

    def reset(self) -> None:
        self.history.clear()
