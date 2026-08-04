"""Attachment summarisation via LLM.

Announcements can carry PDFs (and occasionally DOCX) that contain the real
substance — schedules, permission slips, event details, deadlines.
`parser._extract_attachment_text` already pulls the raw text at scrape-time;
this module walks announcements and uses the LLM to produce a short,
parent-friendly summary that we cache on `Attachment.summary`.

Design notes:
  * We only summarise attachments whose `content` is non-empty and whose
    `summary` is empty. So this is idempotent — re-runs won't re-bill.
  * Input to the LLM is capped to ~4000 characters. School PDFs rarely exceed
    that; long ones get their first pages (which usually carry the crux).
  * Any exception (LLM downtime, quota, network) is logged and swallowed —
    the scrape must never fail because of summarisation.
  * `Attachment.summary` round-trips through JSON because
    `_dict_of_announcements` serialises `att.__dict__`, and the
    `_load_saved_announcements` loader now reads it back.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from .parser import Announcement

log = logging.getLogger(__name__)

_MAX_INPUT_CHARS = 4000
_MAX_OUTPUT_TOKENS = 180

# Placeholder text produced by parser.py when a PDF is IRM-protected. Don't
# waste an LLM call on it.
_UNREADABLE_MARKERS = (
    "[pdf is protected",
    "[docx parse",
    "[pdf parse",
)


def _is_summarisable(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    if not t:
        return False
    for m in _UNREADABLE_MARKERS:
        if m in t:
            return False
    # Very short "text" is almost always OCR noise or a page number.
    return len(t) >= 60


def _sanitize(text: str) -> str:
    """Drop unpaired UTF-16 surrogates that survive from Word/Outlook copy-
    paste and break the OpenAI HTTP client (`'utf-8' codec can't encode ...
    surrogates not allowed`)."""
    if not text:
        return text
    # `errors='replace'` swaps any un-encodable code point (typically a lone
    # high/low surrogate) with U+FFFD.
    return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _build_prompt(child_name: str, ann_title: str, filename: str,
                   body: str) -> str:
    return (
        "You are a parent's school-assistant. A school announcement for "
        f"**{child_name}** included an attachment named **{filename}**. "
        f"The announcement title was: \"{ann_title or '(untitled)'}\".\n\n"
        "Summarise the attachment for the parent in 2–3 crisp sentences. "
        "Prioritise: any **dates, deadlines, times, venues, amounts, items to "
        "bring, or required parent actions**. Skip generic pleasantries and "
        "boilerplate. Use **bold** for key facts. If the attachment is empty "
        "or unreadable, reply with the single word: SKIP.\n\n"
        "Attachment text:\n----\n"
        + body[:_MAX_INPUT_CHARS]
        + ("\n\n[...truncated]" if len(body) > _MAX_INPUT_CHARS else "")
    )


def summarize_attachments(anns: Iterable[Announcement],
                           llm=None,
                           *,
                           child_name: str = "",
                           force: bool = False) -> int:
    """Populate `att.summary` for each attachment that has content but no
    cached summary. Mutates announcements in-place. Returns the count of
    summaries newly created.

    `llm` is expected to be an `mcb_digest.llm.LLM` instance (or anything with
    a compatible `.chat(messages, max_output_tokens, temperature)` method).
    If `llm` is None the function is a silent no-op.
    """
    if llm is None:
        return 0

    made = 0
    for ann in anns:
        atts = getattr(ann, "attachments", None) or []
        for att in atts:
            content = getattr(att, "content", "") or ""
            existing = getattr(att, "summary", "") or ""
            if existing and not force:
                continue
            if not _is_summarisable(content):
                continue

            prompt = _build_prompt(
                child_name=_sanitize(child_name),
                ann_title=_sanitize(getattr(ann, "title", "") or ""),
                filename=_sanitize(getattr(att, "filename", "") or "attachment"),
                body=_sanitize(content),
            )
            try:
                raw = llm.chat(
                    [{"role": "user", "content": prompt}],
                    max_output_tokens=_MAX_OUTPUT_TOKENS,
                    temperature=0.2,
                )
            except Exception as e:
                log.warning("LLM summary failed for %s (%s): %s",
                             getattr(att, "filename", "?"), child_name, e)
                continue

            raw = (raw or "").strip()
            if not raw or raw.upper().strip(".") == "SKIP":
                continue

            att.summary = raw
            made += 1
            log.info("  summarised %s (%d chars input → %d chars summary)",
                      getattr(att, "filename", "?"), len(content), len(raw))
    return made
