"""Persistent event store and 3/2/1-day reminder computation."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List
from zoneinfo import ZoneInfo

from .config import data_path
from .extractor import Event

IST = ZoneInfo("Asia/Kolkata")

log = logging.getLogger(__name__)

EVENTS_FILE = data_path("events.json")


def _load(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not load %s: %s", path, e)
        return {}


def _save(path: Path, data: Dict[str, dict]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def upsert_events(new_events: Iterable[Event]) -> Dict[str, dict]:
    """Merge new events into the persistent store. Returns the full store."""
    store = _load(EVENTS_FILE)
    for ev in new_events:
        if not ev.event_id:
            ev.compute_id()
        # Keep the earliest first-seen timestamp; refresh other fields.
        existing = store.get(ev.event_id, {})
        first_seen = existing.get("first_seen") or datetime.now(IST).isoformat()
        record = asdict(ev)
        record["first_seen"] = first_seen
        record["last_seen"] = datetime.now(IST).isoformat()
        store[ev.event_id] = record
    _save(EVENTS_FILE, store)
    return store


def purge_before(floor: date) -> int:
    """Drop events whose event_date OR source_date is before `floor`.
    Returns the number of records removed.
    """
    store = _load(EVENTS_FILE)
    keep: Dict[str, dict] = {}
    removed = 0
    floor_iso = floor.isoformat()
    for eid, ev in store.items():
        ed = ev.get("event_date", "")
        sd = (ev.get("source_date") or "")[:10]
        if ed and ed < floor_iso:
            removed += 1
            continue
        if sd and sd < floor_iso:
            removed += 1
            continue
        keep[eid] = ev
    if removed:
        _save(EVENTS_FILE, keep)
    return removed


def load_all_events() -> List[dict]:
    return list(_load(EVENTS_FILE).values())


def upcoming_events(store: Iterable[dict], today: date | None = None) -> List[dict]:
    today = today or datetime.now(IST).date()
    out = []
    for ev in store:
        try:
            ed = date.fromisoformat(ev["event_date"])
        except Exception:
            continue
        if ed >= today:
            out.append(ev)
    out.sort(key=lambda e: e["event_date"])
    return out


def reminders_due(store: Iterable[dict], today: date | None = None,
                  offsets_days: tuple = (3, 2, 1)) -> List[dict]:
    """Return events whose event_date is exactly N days away for N in offsets."""
    today = today or datetime.now(IST).date()
    offset_dates = {today + timedelta(days=n): n for n in offsets_days}
    out = []
    for ev in store:
        try:
            ed = date.fromisoformat(ev["event_date"])
        except Exception:
            continue
        if ed in offset_dates:
            enriched = dict(ev)
            enriched["_days_out"] = offset_dates[ed]
            out.append(enriched)
    out.sort(key=lambda e: (e["_days_out"], e["event_date"]))
    return out
