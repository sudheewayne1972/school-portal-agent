"""Parse MCB "Staff Diary" (class-diary / homework) HTML fragments.

The MCB backend returns raw HTML (no `<html>` wrapper) for
`/StudentERP/StaffDiaryToStudent_CalenderView_AllActivities`.

Fragment shape (per subject):

    <div class="row">
      <div class="col-lg-12">
        <div class="row w-100">
          <div class="col-12 col-md-12">
            <h6><span style="font-weight:bold">SUBJECT</span></h6>
          </div>
        </div>
      </div>
      <div class="col-lg-12 form-group">
        <div class="card">
          <div class="card-body p-2">
            <span class="badge badge-danger">Homework</span>
            <span style="font-weight:bold;color:darkgray">TEACHER NAME</span>
            <div class="summery-class ...">DESCRIPTION</div>
            <span onclick="fnUploadStudentHomeWorkSubmission(HW_ID, SUBJECT_ID)">…</span>
          </div>
        </div>
      </div>
    </div>

Multiple subjects can be present in a single response.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

_ONCLICK_RX = re.compile(
    r"fnUpload\w*Submission\w*\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)",
    re.I,
)


@dataclass
class DiaryEntry:
    child: str
    diary_date: str          # ISO YYYY-MM-DD — the date this diary was posted
    subject: str
    kind: str                # "Homework", "Classwork", "Assignment", "Project", "Note"
    teacher: str
    description: str
    staff_diary_id: str = ""
    subject_id: str = ""
    entry_id: str = ""

    def compute_id(self) -> str:
        if self.staff_diary_id and self.subject_id:
            key = f"{self.child}|{self.staff_diary_id}|{self.subject_id}"
        else:
            desc_norm = re.sub(r"\W+", "_", (self.description or "").lower()).strip("_")[:80]
            key = f"{self.child}|{self.diary_date}|{self.subject}|{self.kind}|{desc_norm}"
        self.entry_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self.entry_id

    def to_dict(self) -> dict:
        return asdict(self)


def _text(el: Optional[Tag]) -> str:
    if el is None:
        return ""
    return " ".join(el.get_text(" ", strip=True).split())


def _kind_from_badge(card: Tag) -> str:
    for badge in card.select(".badge"):
        t = _text(badge)
        if not t:
            continue
        # Common labels seen: Homework, Classwork, Assignment, Project, Note
        return t
    return "Note"


def _teacher_from_card(card: Tag) -> str:
    """The teacher is inside a bold darkgray span."""
    for span in card.find_all("span"):
        style = (span.get("style") or "").lower().replace(" ", "")
        if "darkgray" in style and "bold" in style:
            t = _text(span)
            if t:
                return t
    return ""


def _description_from_card(card: Tag) -> str:
    el = card.select_one(".summery-class")
    return _text(el)


def _ids_from_card(card: Tag) -> tuple[str, str]:
    """Extract (staff_diary_id, subject_id) from the onclick handler."""
    for el in card.find_all(attrs={"onclick": True}):
        m = _ONCLICK_RX.search(el.get("onclick") or "")
        if m:
            return m.group(1), m.group(2)
    return "", ""


def _find_subject(card: Tag) -> str:
    """Walk back through the DOM to find the nearest preceding subject header."""
    prev = card.find_previous("h6")
    if prev is not None:
        # The subject name is inside a bold span
        bold = prev.find("span", style=re.compile(r"font-weight\s*:\s*bold", re.I))
        if bold is not None:
            t = _text(bold)
            if t:
                return t
        t = _text(prev)
        if t:
            return t
    return "Unknown"


def parse_diary_html(html: str, child: str, diary_date_iso: str) -> List[DiaryEntry]:
    """Parse one `_AllActivities` response into DiaryEntry rows."""
    if not html or not html.strip():
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: List[DiaryEntry] = []
    for card in soup.select(".card"):
        # Skip nested/inner cards that aren't real entries
        if not card.select(".summery-class") and not card.select(".badge"):
            continue
        subject = _find_subject(card)
        kind = _kind_from_badge(card) or "Note"
        teacher = _teacher_from_card(card)
        desc = _description_from_card(card)
        if not desc:
            # No description → not an actual entry
            continue
        sd_id, sub_id = _ids_from_card(card)
        entry = DiaryEntry(
            child=child,
            diary_date=diary_date_iso,
            subject=subject,
            kind=kind,
            teacher=teacher,
            description=desc,
            staff_diary_id=sd_id,
            subject_id=sub_id,
        )
        entry.compute_id()
        out.append(entry)
    return out


def dedupe_entries(entries: List[DiaryEntry]) -> List[DiaryEntry]:
    """Keep the last-seen entry per entry_id (or content hash if id missing)."""
    seen: dict[str, DiaryEntry] = {}
    for e in entries:
        eid = e.entry_id or e.compute_id()
        seen[eid] = e
    return list(seen.values())
