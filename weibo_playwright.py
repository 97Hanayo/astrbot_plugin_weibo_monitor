"""Optional Playwright-backed refresh for the effective Weibo Cookie jar."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Callable
from .weibo_cookies import is_desktop_weibo_domain, atomic_write_cookie_text

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - standalone tests
    logger = logging.getLogger(__name__)

_INSTALL_LOCK = asyncio.Lock()
_INSTALL_ATTEMPTED = False


def _host_matches(host: str, allowed_domains: Iterable[str]) -> bool:
    normalized = str(host or "").lower().strip().rstrip(".").lstrip(".")
    if not normalized:
        return False
    return any(
        normalized == domain or normalized.endswith(f".{domain}")
        for domain in (
            str(value).lower().strip().rstrip(".").lstrip(".")
            for value in allowed_domains
        )
    )


def _browser_channel_candidates() -> list[str | None]:
    return [
        shutil.which("msedge"),
        shutil.which("chrome"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]


def _configure_browser_path(browser_path: Path | None) -> Path | None:
    if browser_path is None:
        return None
    browser_path.mkdir(parents=True, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_path)
    return browser_path


async def _ensure_chromium_installed(browser_path: Path) -> bool:
    global _INSTALL_ATTEMPTED
    async with _INSTALL_LOCK:
        if _INSTALL_ATTEMPTED:
            return False
        _INSTALL_ATTEMPTED = True

        env = os.environ.copy()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_path)
        command = [sys.executable, "-m", "playwright", "install", "chromium"]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
        except Exception as exc:
            logger.warning("Playwright Chromium 安装失败: %s", exc)
            return False
        if result.returncode == 0:
            return True
        output = (result.stdout or "").strip()
        if len(output) > 1200:
            output = output[-1200:]
        logger.warning(
            "Playwright Chromium 安装退出码 %s: %s",
            result.returncode,
            output,
        )
        return False


async def _launch_chromium(playwright, browser_path: Path | None):
    _configure_browser_path(browser_path)
    first_error: Exception | None = None
    try:
        return await playwright.chromium.launch(headless=True)
    except Exception as exc:
        first_error = exc

    if browser_path is not None and await _ensure_chromium_installed(browser_path):
        try:
            return await playwright.chromium.launch(headless=True)
        except Exception as exc:
            first_error = exc

    for executable in _browser_channel_candidates():
        if not executable or not Path(executable).exists():
            continue
        try:
            return await playwright.chromium.launch(
                headless=True,
                executable_path=executable,
            )
        except Exception:
            continue

    if first_error is not None:
        raise first_error
    raise RuntimeError("Chromium launch failed")


class BrowserCookieRefreshError(RuntimeError):
    """A browser ran, but the page did not complete the expected recovery flow."""


async def collect_browser_cookies(
    *,
    cookies: Mapping[str, str],
    refresh_urls: Sequence[str],
    allowed_domains: Iterable[str],
    user_agent: str | None,
    timeout_ms: int,
    browser_path: Path | None = None,
    viewport: Mapping[str, int] | None = None,
    cookie_domain: str | None = None,
    state_path: Path | None = None,
    session_cookie_names: Sequence[str] = (),
    return_url_pattern: str | None = None,
    on_navigation: Callable[[str], None] | None = None,
    scoped_cookies: Sequence[Mapping[str, Any]] | None = None,
    on_cookie_state: Callable[[list[dict[str, Any]], bool], None] | None = None,
) -> list[dict[str, Any]]:
    """Load cookies in Chromium and return the final same-site Cookie jar.

    Playwright exposes the browser's effective Cookie jar, which includes both
    HTTP ``Set-Cookie`` updates and cookies written by page scripts. Values are
    returned to the caller so each platform can apply its login-cookie safety
    rules before persistence.
    """
    if not cookies or not refresh_urls:
        return []

    def fingerprint(values: Mapping[str, str]) -> str:
        selected = dict(values)
        return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()

    input_digest = fingerprint(cookies)
    source_digest = (
        hashlib.sha256(json.dumps([c for c in scoped_cookies if not is_desktop_weibo_domain(c.get("domain", ""))], sort_keys=True).encode()).hexdigest()
        if scoped_cookies else None
    )
    saved_state = None
    if state_path is not None:
        try:
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            if input_digest in (saved.get("input_digest"), saved.get("output_digest")):
                candidate_state = saved.get("storage_state")
                # A new SSO export must replace stale saved state even when
                # the desktop login fields happen to be unchanged.
                if isinstance(candidate_state, dict) and (
                    not scoped_cookies or source_digest in (saved.get("source_digest"), saved.get("output_source_digest"))
                ):
                    saved_state = candidate_state
        except (OSError, ValueError, AttributeError):
            pass

    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise RuntimeError("Playwright is unavailable") from exc

    _configure_browser_path(browser_path)
    async with async_playwright() as playwright:
        browser = await _launch_chromium(playwright, browser_path)
        try:
            context_options = {
                "viewport": dict(viewport or {"width": 1280, "height": 900}),
                "locale": "zh-CN",
            }
            if user_agent:
                context_options["user_agent"] = user_agent
            if saved_state is not None:
                context_options["storage_state"] = saved_state
            context = await browser.new_context(**context_options)
            try:
                seed_cookies = list(scoped_cookies) if scoped_cookies else [
                    {
                        "name": name,
                        "value": value,
                        **({"domain": cookie_domain, "path": "/", "secure": True}
                           if cookie_domain else {"url": refresh_url}),
                    }
                    for refresh_url in (refresh_urls[:1] if cookie_domain else refresh_urls)
                    for name, value in cookies.items()
                    if name and value
                ]
                if seed_cookies and saved_state is None:
                    await context.add_cookies(seed_cookies)
                if on_cookie_state is not None:
                    on_cookie_state(await context.cookies(), saved_state is not None)

                loaded = False
                for refresh_url in refresh_urls:
                    page = await context.new_page()
                    if on_navigation is not None:
                        def navigated(frame):
                            if frame == page.main_frame:
                                on_navigation(frame.url)
                        page.on("framenavigated", navigated)
                    try:
                        try:
                            response = await page.goto(
                                refresh_url,
                                wait_until="domcontentloaded",
                                timeout=max(1000, int(timeout_ms)),
                            )
                            if state_path is not None and response is not None and response.status >= 400:
                                raise RuntimeError("Browser keepalive page failed")
                        except Exception:
                            # Continue with another first-party refresh URL if
                            # one site variant is blocked or times out.
                            pass
                        else:
                            try:
                                await page.wait_for_load_state(
                                    "networkidle",
                                    timeout=min(max(1000, int(timeout_ms)), 8000),
                                )
                            except Exception:
                                # Trackers and long-polling requests can prevent
                                # networkidle without affecting Cookie updates.
                                pass
                            if return_url_pattern is not None:
                                try:
                                    await page.wait_for_url(
                                        re.compile(return_url_pattern),
                                        wait_until="domcontentloaded",
                                        timeout=max(1000, int(timeout_ms)),
                                    )
                                except Exception as exc:
                                    raise BrowserCookieRefreshError(
                                        "Browser did not return to the expected homepage"
                                    ) from exc
                            loaded = True
                    finally:
                        await page.close()

                if (state_path is not None or return_url_pattern is not None) and not loaded:
                    raise BrowserCookieRefreshError("No browser keepalive page loaded")
                allowed = tuple(allowed_domains) + ("sina.com.cn",)
                collected = (
                    await context.cookies()
                )
                browser_cookies = [
                    cookie
                    for cookie in collected
                    if _host_matches(str(cookie.get("domain") or ""), allowed)
                ]
                if state_path is not None:
                    effective = {
                        item["name"]: item["value"]
                        for item in sorted(browser_cookies, key=lambda c: c.get("domain") != ".weibo.com")
                        if is_desktop_weibo_domain(item.get("domain", "")) and item.get("path", "/") == "/"
                    }
                    if any(
                        cookies.get(name) and not effective.get(name)
                        for name in session_cookie_names
                    ):
                        raise BrowserCookieRefreshError("Browser keepalive removed login cookies")
                    # Preserve scoped cookies and local storage, including SSO
                    # state from redirects. Never flatten that state into headers.
                    atomic_write_cookie_text(state_path, json.dumps({
                        "input_digest": input_digest,
                        "output_digest": fingerprint(effective),
                        "source_digest": source_digest,
                        "output_source_digest": hashlib.sha256(json.dumps([
                            {k: (int(c[k]) if k == "expires" else c[k]) for k in ("name", "value", "domain", "path", "secure", "httpOnly", "expires") if k in c and (k != "expires" or c[k] > 0)}
                            for c in browser_cookies if not is_desktop_weibo_domain(c.get("domain", ""))
                        ], sort_keys=True).encode()).hexdigest(),
                        "storage_state": await context.storage_state(),
                    }))
                return browser_cookies
            finally:
                await context.close()
        finally:
            await browser.close()


__all__ = ["collect_browser_cookies"]
