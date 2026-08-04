"""Playwright-based scraper that supports both the classic MCB login form
and the newer SSO login (`ssolive.myclassboard.com`).

Selector differences:
- Classic form: #UserName, #Password, #LogID
- SSO form:    #Username, #Password, #LogID
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from .config import AppConfig, ChildAccount

log = logging.getLogger(__name__)


def _is_cloudflare_error(html: str, title: str) -> bool:
    t = (title or "").lower()
    if "bad gateway" in t or "cloudflare" in t or "just a moment" in t:
        return True
    if "cf-error-details" in (html or ""):
        return True
    return False


@dataclass
class FetchResult:
    ok: bool
    landing_url: str
    ann_url: str
    html: str
    error: Optional[str] = None
    elapsed_s: float = 0.0


def _fill_login(page: Page, username: str, password: str, timeout_ms: int = 20000) -> None:
    """Fill either the classic or SSO login form (whichever is present)."""
    # Wait until either username field is present (either capitalization).
    combined = "#Username, #UserName"
    try:
        page.wait_for_selector(combined, timeout=timeout_ms, state="visible")
    except PWTimeout:
        raise RuntimeError("Login page did not render a username field in time")

    # SSO uses #Username; classic uses #UserName. Try both.
    user_selectors = ["#Username", "#UserName"]
    pass_selectors = ["#Password"]
    login_selectors = ["#LogID", "button:has-text('LOGIN')", "button[type='submit']"]

    filled_user = False
    for sel in user_selectors:
        if page.locator(sel).count() > 0:
            page.fill(sel, username)
            filled_user = True
            break
    if not filled_user:
        raise RuntimeError("Could not find username field")

    filled_pw = False
    for sel in pass_selectors:
        if page.locator(sel).count() > 0:
            page.fill(sel, password)
            filled_pw = True
            break
    if not filled_pw:
        raise RuntimeError("Could not find password field")

    clicked = False
    for sel in login_selectors:
        try:
            if page.locator(sel).count() > 0:
                page.click(sel, timeout=5000)
                clicked = True
                break
        except PWTimeout:
            continue
    if not clicked:
        raise RuntimeError("Could not find login button")


def _looks_logged_in(url: str) -> bool:
    u = url.lower()
    if "login" in u or "signin" in u or "account/login" in u:
        return False
    return "myclassboard.com" in u


def fetch_announcements_html(cfg: AppConfig, child: ChildAccount,
                              timeout_ms: int = 45000,
                              retries: int = 3,
                              retry_wait_s: float = 30.0) -> FetchResult:
    """Log in and return the HTML of the announcements listing page.

    Retries on Cloudflare 5xx / interstitial pages.
    """
    last: Optional[FetchResult] = None
    for attempt in range(1, retries + 1):
        res = _fetch_once(cfg, child, timeout_ms=timeout_ms)
        last = res
        if res.ok:
            return res
        err = (res.error or "").lower()
        html_hint = (res.html or "")[:400].lower()
        if any(k in err for k in ("timeout", "gateway", "cloudflare")) or \
                "bad gateway" in html_hint or "cf-error-details" in html_hint or \
                "did not render" in err:
            log.warning("Fetch attempt %d failed (%s). Retrying in %ss…",
                        attempt, res.error, retry_wait_s)
            if attempt < retries:
                time.sleep(retry_wait_s)
                continue
        # Non-retryable error
        break
    return last  # type: ignore[return-value]


def _fetch_once(cfg: AppConfig, child: ChildAccount,
                timeout_ms: int = 45000) -> FetchResult:
    t0 = time.time()
    ann_page = (
        f"{cfg.mcb_base}/StudentERP/ViewMoreAnnouncementDetails_New"
        f"?AnnouncementType=&Datetype={cfg.datetype}"
    )

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

            # Detect Cloudflare / origin error page early.
            if _is_cloudflare_error(page.content(), page.title()):
                elapsed = round(time.time() - t0, 2)
                html = page.content()
                browser.close()
                return FetchResult(ok=False, landing_url=page.url, ann_url="",
                                   html=html, error="Cloudflare interstitial / 502",
                                   elapsed_s=elapsed)

            _fill_login(page, child.username, child.password)

            # Wait for either successful redirect back to tenant, or an error state.
            try:
                page.wait_for_url(lambda url: _looks_logged_in(url), timeout=timeout_ms)
            except PWTimeout:
                pass

            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PWTimeout:
                pass

            landing = page.url
            if not _looks_logged_in(landing):
                html = page.content()
                browser.close()
                return FetchResult(
                    ok=False, landing_url=landing, ann_url="", html=html,
                    error=f"Login did not complete; still at {landing}",
                    elapsed_s=round(time.time() - t0, 2),
                )

            page.goto(ann_page, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PWTimeout:
                pass
            html = page.content()
            browser.close()
            return FetchResult(
                ok=True, landing_url=landing, ann_url=page.url, html=html,
                elapsed_s=round(time.time() - t0, 2),
            )
    except Exception as e:
        return FetchResult(
            ok=False, landing_url="", ann_url="", html="",
            error=f"{type(e).__name__}: {e}",
            elapsed_s=round(time.time() - t0, 2),
        )
