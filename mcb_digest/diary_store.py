"""On-disk persistence for parsed diary entries (per child).

Files: `data/{child_name.lower()}_diary.json`
Layout: `[{DiaryEntry.to_dict()}, …]`
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo

from .config import ROOT, data_path
from .diary_parser import DiaryEntry

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def _diary_paths(child_name: str) -> List[Path]:
    return [
        data_path(f"{child_name.lower()}_diary.json"),
        ROOT / f"{child_name.lower()}_diary.json",
    ]


def load_diary(child_name: str) -> List[DiaryEntry]:
    path = next((p for p in _diary_paths(child_name) if p.exists()), None)
    if path is None:
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not load %s: %s", path, e)
        return []
    out: List[DiaryEntry] = []
    for r in raw:
        try:
            out.append(DiaryEntry(
                child=r.get("child", child_name),
                diary_date=r.get("diary_date", ""),
                subject=r.get("subject", ""),
                kind=r.get("kind", "Note"),
                teacher=r.get("teacher", ""),
                description=r.get("description", ""),
                staff_diary_id=r.get("staff_diary_id", ""),
                subject_id=r.get("subject_id", ""),
                entry_id=r.get("entry_id", ""),
            ))
        except Exception as e:
            log.debug("Skipping bad diary row: %s (%s)", r, e)
    return out


def save_diary(child_name: str, entries: Iterable[DiaryEntry]) -> Path:
    path = data_path(f"{child_name.lower()}_diary.json")
    data = [e.to_dict() for e in entries]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path


def merge_and_save(child_name: str, new_entries: Iterable[DiaryEntry],
                   *, retention_days: int = 60) -> List[DiaryEntry]:
    """Merge new entries with what's on disk, drop rows older than the
    retention window, and persist. Returns the merged list."""
    existing = load_diary(child_name)
    by_id: dict[str, DiaryEntry] = {}
    for e in existing:
        eid = e.entry_id or e.compute_id()
        by_id[eid] = e
    for e in new_entries:
        eid = e.entry_id or e.compute_id()
        by_id[eid] = e  # newer wins on any collision

    today = datetime.now(IST).date()
    floor = today - timedelta(days=retention_days)
    merged: List[DiaryEntry] = []
    for e in by_id.values():
        try:
            d = date.fromisoformat(e.diary_date[:10])
        except Exception:
            merged.append(e)
            continue
        if d >= floor:
            merged.append(e)

    merged.sort(key=lambda x: (x.diary_date, x.subject, x.kind))
    save_diary(child_name, merged)
    return merged


def entries_on(child_name: str, iso_date: str) -> List[DiaryEntry]:
    return [e for e in load_diary(child_name)
            if (e.diary_date or "")[:10] == iso_date]


def recent_entries(child_name: str, *, days: int = 7,
                    today: Optional[date] = None) -> List[DiaryEntry]:
    today = today or datetime.now(IST).date()
    floor = today - timedelta(days=days - 1)
    out: List[DiaryEntry] = []
    for e in load_diary(child_name):
        try:
            d = date.fromisoformat(e.diary_date[:10])
        except Exception:
            continue
        if floor <= d <= today:
            out.append(e)
    out.sort(key=lambda x: (x.diary_date, x.subject, x.kind))
    return out
