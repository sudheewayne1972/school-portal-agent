"""Playwright scraper for the MCB class-diary / homework module.

The diary UI is loaded via `ERPURLRedirection?urlres=…StaffDiaryToStudent_CalenderView`.
Behind the scenes it POSTs to `_AllActivities` with `DiaryDate=YYYY-MM-DD` to
render a specific day's entries. We reuse the existing login flow, then hit
that endpoint directly for each date in the requested window.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from .config import AppConfig, ChildAccount
from .diary_parser import DiaryEntry, parse_diary_html
from .scraper import _fill_login, _looks_logged_in

log = logging.getLogger(__name__)

ALL_ACTIVITIES_PATH = (
    "/StudentERP/StaffDiaryToStudent_CalenderView_AllActivities"
)
LANDING_URL_RES = "/StudentERP/StaffDiaryToStudent_CalenderView?type=1"


@dataclass
class DiaryFetchResult:
    ok: bool
    child: str
    entries: List[DiaryEntry] = field(default_factory=list)
    error: Optional[str] = None
    elapsed_s: float = 0.0
    dates_probed: int = 0


def _iter_dates(days_back: int, today: Optional[date] = None) -> List[date]:
    """Return the last `days_back` dates ending today (inclusive)."""
    today = today or datetime.now().date()
    out: List[date] = []
    for i in range(days_back - 1, -1, -1):
        out.append(today - timedelta(days=i))
    return out


def _open_diary_landing(page: Page, cfg: AppConfig, timeout_ms: int) -> None:
    """Navigate to the diary landing so that the session picks up any
    diary-specific cookies / view-state the AJAX endpoint expects."""
    enc = quote(LANDING_URL_RES, safe="")
    page.goto(f"{cfg.mcb_base}/StudentERP/ERPURLRedirection?urlres={enc}",
              wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWTimeout:
        pass


def _fetch_day(page: Page, cfg: AppConfig, iso: str,
               timeout_ms: int = 30000) -> str:
    """POST to _AllActivities for the given date and return the HTML body."""
    url = f"{cfg.mcb_base}{ALL_ACTIVITIES_PATH}"
    try:
        resp = page.request.post(
            url,
            form={"SubjectID": "", "type": "1", "DiaryDate": iso},
            timeout=timeout_ms,
        )
    except Exception as e:
        log.warning("Diary POST failed for %s: %s", iso, e)
        return ""
    if resp.status >= 400:
        log.warning("Diary POST %s returned status %d", iso, resp.status)
        return ""
    try:
        return resp.text()
    except Exception as e:
        log.warning("Diary POST %s body decode failed: %s", iso, e)
        return ""


def fetch_diary(cfg: AppConfig, child: ChildAccount, *,
                days_back: int = 7,
                timeout_ms: int = 45000) -> DiaryFetchResult:
    """Log in, hit `_AllActivities` for each of the last `days_back` dates,
    and return parsed `DiaryEntry` rows."""
    t0 = time.time()
    dates = _iter_dates(days_back)
    entries: List[DiaryEntry] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"),
            )
            page = ctx.new_page()

            page.goto(f"{cfg.mcb_base}/Login", wait_until="load", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PWTimeout:
                pass
            _fill_login(page, child.username, child.password)
            try:
                page.wait_for_url(lambda url: _looks_logged_in(url), timeout=timeout_ms)
            except PWTimeout:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PWTimeout:
                pass
            if not _looks_logged_in(page.url):
                browser.close()
                return DiaryFetchResult(
                    ok=False, child=child.name,
                    error=f"Login did not complete; at {page.url}",
                    elapsed_s=round(time.time() - t0, 2),
                )

            _open_diary_landing(page, cfg, timeout_ms)

            for d in dates:
                iso = d.isoformat()
                html = _fetch_day(page, cfg, iso)
                if not html:
                    continue
                day_entries = parse_diary_html(html, child.name, iso)
                entries.extend(day_entries)
                log.info("  diary %s %s: %d entries", child.name, iso, len(day_entries))

            browser.close()
    except Exception as e:
        return DiaryFetchResult(
            ok=False, child=child.name,
            error=f"{type(e).__name__}: {e}",
            elapsed_s=round(time.time() - t0, 2),
            dates_probed=len(dates),
        )

    return DiaryFetchResult(
        ok=True, child=child.name, entries=entries,
        elapsed_s=round(time.time() - t0, 2),
        dates_probed=len(dates),
    )
