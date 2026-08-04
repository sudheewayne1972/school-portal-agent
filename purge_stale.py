"""One-shot data purge to enforce the configured `data_floor_date`.

Removes stale entries from:
  * data/events.json
  * data/{child}_announcements.json (in-place filter)
  * legacy {child}_announcements.json at the repo root (in-place filter)

Safe to re-run — idempotent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from mcb_digest.config import ROOT, data_path, load_config
from mcb_digest.reminders import purge_before


def _filter_json_list(path: Path, floor_iso: str) -> tuple[int, int]:
    if not path.exists():
        return (0, 0)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return (0, 0)
    if not isinstance(raw, list):
        return (0, 0)
    before = len(raw)
    kept = []
    for r in raw:
        sd = (r.get("date") or r.get("date_raw") or "")[:10]
        if sd and sd < floor_iso:
            continue
        kept.append(r)
    if len(kept) != before:
        path.write_text(json.dumps(kept, indent=2, ensure_ascii=False), encoding="utf-8")
    return (before, len(kept))


def main() -> int:
    cfg = load_config()
    floor = cfg.data_floor_date
    floor_iso = floor.isoformat()
    print(f"Data floor date: {floor_iso}")

    removed = purge_before(floor)
    print(f"Events store: removed {removed} record(s) before {floor_iso}")

    for child in cfg.children:
        name = child.name.lower()
        for path in [
            data_path(f"{name}_announcements.json"),
            ROOT / f"{name}_announcements.json",
            ROOT / "attachments" / f"{name}_announcements.json",
        ]:
            before, after = _filter_json_list(path, floor_iso)
            if before:
                print(f"  {path.relative_to(ROOT)}: {before} -> {after}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
