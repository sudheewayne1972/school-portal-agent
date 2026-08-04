"""Parse the CHIREC announcements listing HTML into structured records."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

try:
    from PyPDF2 import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None  # type: ignore

try:
    import docx as _docx  # python-docx
except ImportError:  # pragma: no cover
    _docx = None  # type: ignore

log = logging.getLogger(__name__)

DATE_RX = re.compile(
    r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4},\s+\d{1,2}:\d{2}\s+[AP]M)", re.I
)


@dataclass
class Attachment:
    url: str
    filename: str
    content: str = ""
    summary: str = ""


@dataclass
class Announcement:
    sender: str = ""
    tag: str = ""
    date: str = ""            # ISO 8601 with +05:30 offset if parsed
    date_raw: str = ""
    title: str = ""
    content: str = ""
    attachments: List[Attachment] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _normalize_ist(raw: str) -> Optional[str]:
    if not raw:
        return None
    raw = raw.replace("\u00a0", " ")
    raw = re.sub(r"\s+", " ", raw.strip())
    for fmt in ("%d %b %Y, %I:%M %p", "%d %B %Y, %I:%M %p"):
        try:
            dt = datetime.strptime(raw, fmt)
            try:
                from zoneinfo import ZoneInfo
                dt = dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            except Exception:
                pass
            return dt.isoformat()
        except ValueError:
            continue
    return None


def _download_attachment(url: str, dest_dir: Path) -> Optional[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = os.path.basename(url.split("?", 1)[0])
    if not filename:
        return None
    dest = dest_dir / filename
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        r = requests.get(url, timeout=45)
        if r.status_code == 200:
            dest.write_bytes(r.content)
            return dest
        log.warning("Attachment fetch %s -> %s", url, r.status_code)
    except Exception as e:  # pragma: no cover
        log.warning("Attachment fetch error %s: %s", url, e)
    return None


def _extract_pdf_text(path: Path) -> str:
    if PdfReader is None or not path.exists():
        return ""
    if path.suffix.lower() != ".pdf":
        return ""
    try:
        reader = PdfReader(str(path))
        parts = []
        for pg in reader.pages:
            t = pg.extract_text() or ""
            if t.strip():
                parts.append(t.strip())
        return "\n".join(parts).strip()
    except Exception as e:
        msg = str(e).lower()
        if "encrypted" in msg or "protected" in msg:
            return "[PDF is protected by Microsoft IRM; contents not extractable]"
        log.warning("PDF parse failed %s: %s", path, e)
        return ""


def _extract_docx_text(path: Path) -> str:
    if _docx is None or not path.exists():
        return ""
    if path.suffix.lower() != ".docx":
        return ""
    try:
        doc = _docx.Document(str(path))
        parts = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
        for tbl in getattr(doc, "tables", []) or []:
            for row in tbl.rows:
                for cell in row.cells:
                    if cell.text and cell.text.strip():
                        parts.append(cell.text.strip())
        return "\n".join(parts).strip()
    except Exception as e:
        log.warning("DOCX parse failed %s: %s", path, e)
        return ""


def _extract_attachment_text(path: Path) -> str:
    """Dispatcher for supported attachment formats. Silently skips unsupported."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(path)
    if suffix == ".docx":
        return _extract_docx_text(path)
    return ""


def parse_announcements(html: str, attachments_dir: Path,
                        download: bool = True) -> List[Announcement]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Announcement] = []

    for card in soup.select(".searchdata"):
        ann = Announcement()
        header = card.select_one(".card-header")
        body = card.select_one(".card-body")

        if header:
            sender_el = header.select_one("label")
            if sender_el:
                ann.sender = sender_el.get_text(strip=True)
            cat_el = header.select_one(".label")
            if cat_el:
                ann.tag = cat_el.get_text(strip=True)
            for sp in header.find_all("span"):
                t = sp.get_text(" ", strip=True)
                m = DATE_RX.search(t)
                if m:
                    ann.date_raw = m.group(1)
                    iso = _normalize_ist(ann.date_raw)
                    ann.date = iso or ann.date_raw
                    break

        if body:
            title_el = body.select_one("h4")
            if title_el:
                ann.title = title_el.get_text(strip=True)

            rich = body.select("div.x_elementToProof")
            if rich:
                ann.content = " ".join(b.get_text(" ", strip=True) for b in rich)
            else:
                ann.content = " ".join(
                    x.get_text(" ", strip=True)
                    for x in body.select("p, div") if x.get_text(strip=True)
                )

            for node in body.select("[onclick^=ViewFile]"):
                onclick = node.get("onclick", "")
                m = re.search(r"'(https?://[^']+)'", onclick)
                if not m:
                    continue
                url = m.group(1)
                filename = os.path.basename(url.split("?", 1)[0])
                content_text = ""
                if download:
                    path = _download_attachment(url, attachments_dir)
                    if path is not None:
                        content_text = _extract_attachment_text(path)
                ann.attachments.append(Attachment(url=url, filename=filename, content=content_text))

        out.append(ann)
    return out
