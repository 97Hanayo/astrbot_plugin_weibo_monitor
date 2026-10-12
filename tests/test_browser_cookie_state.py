"""Check that keepalive restores browser state without losing manual replacements."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from test_weibo_session import PACKAGE, cookies as cookie_tools
import importlib
playwright_cookies = importlib.import_module(f"{PACKAGE}.weibo_playwright")


class TestBrowserCookieState(unittest.IsolatedAsyncioTestCase):
    def setup_browser(self, value="renewed", failure=False):
        cookies = [{"name": "SUB", "value": value, "domain": ".weibo.com", "path": "/"}]
        self.context = Mock()
        self.context.add_cookies = AsyncMock()
        self.context.cookies = AsyncMock(return_value=cookies)
        self.context.storage_state = AsyncMock(return_value={
            "cookies": cookies + [{"name": "SSO", "value": "sso-state", "domain": ".sina.com.cn", "path": "/"}],
            "origins": [{"origin": "https://weibo.com", "localStorage": [{"name": "session", "value": "state"}]}],
        })
        page = Mock()
        page.goto = AsyncMock(return_value=None, side_effect=RuntimeError("timeout") if failure else None)
        page.wait_for_load_state = AsyncMock()
        page.wait_for_url = AsyncMock()
        page.close = AsyncMock()
        self.page = page
        self.context.new_page = AsyncMock(return_value=page)
        self.context.close = AsyncMock()
        self.browser = Mock(new_context=AsyncMock(return_value=self.context), close=AsyncMock())
        runtime = AsyncMock()
        runtime.__aenter__.return_value = Mock()
        module = types.ModuleType("playwright.async_api")
        module.async_playwright = Mock(return_value=runtime)
        return patch.dict(sys.modules, {"playwright.async_api": module})

    async def collect(self, path, cookie="old", **options):
        with patch.object(playwright_cookies, "_launch_chromium", new=AsyncMock(return_value=self.browser)):
            return await playwright_cookies.collect_browser_cookies(
                cookies={"SUB": cookie}, refresh_urls=("https://weibo.com/",),
                allowed_domains=("weibo.com",), cookie_domain=".weibo.com",
                user_agent="test", timeout_ms=1000, state_path=path,
                session_cookie_names=("SUB",),
                **options,
            )

    async def test_restores_scoped_cookies_and_local_storage_across_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                await self.collect(path)
            self.context.add_cookies.assert_awaited_once()
            saved = json.loads(path.read_text(encoding="utf-8"))
            with self.setup_browser():
                await self.collect(path, cookie="renewed")
            self.context.add_cookies.assert_not_awaited()
            self.assertEqual(self.browser.new_context.call_args.kwargs["storage_state"], saved["storage_state"])
            self.assertEqual(saved["storage_state"]["cookies"][1]["domain"], ".sina.com.cn")

    async def test_manual_replacement_does_not_restore_previous_account_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                await self.collect(path)
            with self.setup_browser(value="manual"):
                await self.collect(path, cookie="manual")
            self.assertNotIn("storage_state", self.browser.new_context.call_args.kwargs)
            self.assertEqual(self.context.add_cookies.call_args.args[0][0]["value"], "manual")

    async def test_scoped_sso_export_survives_refresh_and_header_reload(self):
        seeds = [
            {"name": "SUB", "value": "old", "domain": ".weibo.com", "path": "/"},
            {"name": "SSO", "value": "sina-session", "domain": ".sina.com.cn", "path": "/sso"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                await self.collect(path, scoped_cookies=seeds)
            self.assertEqual(self.context.add_cookies.call_args.args[0], seeds)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("sina-session", saved["source_digest"])
            # Persistence writes a desktop header, but the scoped browser state
            # must remain available after the next plugin startup.
            with self.setup_browser():
                await self.collect(path, cookie="renewed")
            self.context.add_cookies.assert_not_awaited()
            self.assertEqual(self.browser.new_context.call_args.kwargs["storage_state"], saved["storage_state"])

    async def test_new_sso_export_replaces_saved_state_with_same_desktop_cookie(self):
        seed = {"name": "SSO", "value": "first", "domain": ".sina.com.cn", "path": "/"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                await self.collect(path, scoped_cookies=[seed])
            replacement = {**seed, "value": "replacement"}
            with self.setup_browser():
                await self.collect(path, scoped_cookies=[replacement])
            self.assertNotIn("storage_state", self.browser.new_context.call_args.kwargs)
            self.assertEqual(self.context.add_cookies.call_args.args[0], [replacement])

    async def test_reports_effective_cookie_jar_and_state_source(self):
        callback = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                await self.collect(path, on_cookie_state=callback)
            self.assertFalse(callback.call_args.args[1])
            self.assertEqual(callback.call_args.args[0], self.context.cookies.return_value)
            with self.setup_browser():
                await self.collect(path, cookie="renewed", on_cookie_state=callback)
            self.assertTrue(callback.call_args.args[1])

    async def test_failed_navigation_preserves_saved_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                await self.collect(path)
            saved = path.read_text(encoding="utf-8")
            with self.setup_browser(failure=True):
                with self.assertRaisesRegex(RuntimeError, "No browser keepalive page loaded"):
                    await self.collect(path)
            self.assertEqual(path.read_text(encoding="utf-8"), saved)
            self.context.close.assert_awaited_once()
            self.browser.close.assert_awaited_once()

    async def test_deleted_login_cookie_does_not_save_browser_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                self.context.cookies.return_value = []
                with self.assertRaisesRegex(RuntimeError, "removed login cookies"):
                    await self.collect(path)
            self.assertFalse(path.exists())

    async def test_reads_cookies_only_after_return_from_passport(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                returned = False
                async def finish_redirect(pattern, **kwargs):
                    nonlocal returned
                    self.assertIsNone(pattern.match("https://passport.weibo.com/sso/v2/login?alt=secret"))
                    self.assertIsNotNone(pattern.match("https://weibo.com/"))
                    returned = True
                async def cookies(*args):
                    self.assertTrue(returned)
                    self.assertEqual(args, ())
                    return [{"name": "SUB", "value": "new", "domain": ".weibo.com", "path": "/"}]
                self.page.wait_for_url.side_effect = finish_redirect
                self.context.cookies.side_effect = cookies
                result = await self.collect(path, return_url_pattern=r"^https://weibo\.com/$")
            self.assertEqual(result[0]["value"], "new")
            self.page.close.assert_awaited_once()

    async def test_unfinished_passport_redirect_does_not_save_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.setup_browser():
                self.page.wait_for_url.side_effect = TimeoutError("still on passport")
                with self.assertRaises(playwright_cookies.BrowserCookieRefreshError):
                    await self.collect(path, return_url_pattern=r"^https://weibo\.com/$")
            self.context.cookies.assert_not_awaited()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
