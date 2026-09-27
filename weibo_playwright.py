"""Optional Playwright-backed refresh for the effective Weibo Cookie jar."""

from __future__ import annotations

import asyncio
import json
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


async def collect_browser_cookies(
    *,
    cookies: Mapping[str, str],
    refresh_urls: Sequence[str],
    allowed_domains: Iterable[str],
    user_agent: str,
    timeout_ms: int,
    browser_path: Path | None = None,
    login_check_url: str | None = None,
) -> list[dict[str, Any]]:
    """Load seeded cookies in Chromium and return the effective Cookie jar.

    The optional login check prevents a logged-out page from replacing a valid
    user Cookie with a visitor session before the caller persists the result.
    """
    if not cookies or not refresh_urls:
        return []

    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise RuntimeError("Playwright is unavailable") from exc

    async with async_playwright() as playwright:
        browser = await _launch_chromium(playwright, browser_path)
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=user_agent,
                locale="zh-CN",
                extra_http_headers={
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                    "MWeibo-Pwa": "1",
                },
            )
            try:
                seed_cookies = []
                for refresh_url in refresh_urls:
                    host = (urlparse(refresh_url).hostname or "").lower()
                    if not host:
                        continue
                    root_domain = (
                        ".weibo.cn"
                        if host == "weibo.cn" or host.endswith(".weibo.cn")
                        else ".weibo.com"
                    )
                    seed_cookies.extend(
                        {
                            "name": name,
                            "value": value,
                            "domain": root_domain,
                            "path": "/",
                            "secure": True,
                        }
                        for name, value in cookies.items()
                        if name and value
                    )
                if seed_cookies:
                    await context.add_cookies(seed_cookies)

                for refresh_url in refresh_urls:
                    page = await context.new_page()
                    try:
                        try:
                            await page.goto(
                                refresh_url,
                                wait_until="domcontentloaded",
                                timeout=max(1000, int(timeout_ms)),
                            )
                        except Exception:
                            pass
                        else:
                            try:
                                await page.wait_for_load_state(
                                    "networkidle",
                                    timeout=min(max(1000, int(timeout_ms)), 8000),
                                )
                            except Exception:
                                pass
                    finally:
                        await page.close()

                if login_check_url:
                    page = await context.new_page()
                    try:
                        response = await page.goto(
                            login_check_url,
                            wait_until="domcontentloaded",
                            timeout=max(1000, int(timeout_ms)),
                        )
                        if response is None or response.status != 200:
                            status = response.status if response is not None else "unknown"
                            raise RuntimeError(
                                f"Playwright Cookie 验证状态码异常: {status}"
                            )
                        try:
                            payload = json.loads(await page.locator("body").inner_text())
                        except json.JSONDecodeError as exc:
                            raise RuntimeError(
                                "Playwright Cookie 验证响应不是 JSON"
                            ) from exc
                        login = (payload.get("data") or {}).get("login")
                        if login is not True:
                            raise RuntimeError(
                                f"Playwright Cookie 验证未登录: login={login!r}"
                            )
                    finally:
                        await page.close()

                return [
                    cookie
                    for cookie in await context.cookies()
                    if _host_matches(cookie.get("domain", ""), allowed_domains)
                ]
            finally:
                await context.close()
        finally:
            await browser.close()


__all__ = ["collect_browser_cookies"]
