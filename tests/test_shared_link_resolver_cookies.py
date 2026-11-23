"""Tests for sharing Weibo cookies from astrbot_plugin_link_resolver."""

import ast
import asyncio
import importlib.util
import os
import pathlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, Mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "weibo_monitor_shared_cookie_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package

for name in ("weibo_cookies", "weibo_session"):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

cookies = sys.modules[f"{PACKAGE}.weibo_cookies"]
session = sys.modules[f"{PACKAGE}.weibo_session"]

tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
plugin_class = next(
    n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "WeiboMonitor"
)
selected = [
    n
    for n in plugin_class.body
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    and n.name
    in {
        "_get_shared_cookie_path",
        "_load_shared_cookie_source",
        "_is_using_shared_cookies",
        "_get_cookie_value",
        "_get_cookie_source",
        "_initialize_cookie_value",
        "_capture_weibo_cookie_updates",
        "_refresh_weibo_cookie_session",
        "_log_cookie_refresh",
    }
]
namespace = {
    "Path": Path,
    "asyncio": asyncio,
    "astrbot_logger": Mock(),
    "Optional": Optional,
    "Tuple": Tuple,
    "Dict": Dict,
    "List": List,
    "Any": Any,
    "httpx": types.SimpleNamespace(Response=object),
    "DEFAULT_TIMEOUT": 20,
    "WEIBO_WEB_BASE": "https://weibo.com",
    "WEIBO_COOKIE_REFRESH_URL": session.WEIBO_LOGIN_URL,
    "WEIBO_BROWSER_REFRESH_URLS": ("https://weibo.com/",),
    "collect_browser_cookies": AsyncMock(),
    "BrowserUnavailableError": RuntimeError,
    "BrowserCookieRefreshError": RuntimeError,
    **{
        name: getattr(cookies, name)
        for name in (
            "normalize_cookie_text",
            "normalize_cookie_source",
            "parse_browser_cookie_source",
            "update_cookie_source",
            "serialize_cookie_header",
            "parse_cookie_text",
            "would_remove_login_cookie",
            "is_desktop_weibo_domain",
            "merge_set_cookie_headers",
        )
    },
}
ast_class = ast.ClassDef(
    name="SharedCookieHarness", bases=[], keywords=[], body=selected, decorator_list=[]
)
exec(
    compile(
        ast.fix_missing_locations(ast.Module(body=[ast_class], type_ignores=[])),
        "main.py",
        "exec",
    ),
    namespace,
)
SharedCookieHarness = namespace["SharedCookieHarness"]


def _make_harness(data_dir: Path, config: dict | None = None):
    plugin = SharedCookieHarness()
    cfg = dict(config or {})
    plugin.data_dir = data_dir
    plugin._data = {}
    plugin.plugin_logger = Mock()
    plugin._cookie_refresh_lock = asyncio.Lock()
    plugin._last_cookie_refresh_at = 0.0
    plugin._shared_cookie_cache_sig = None
    plugin._shared_cookie_cache_source = ""
    plugin._shared_cookie_skip_logged = False
    plugin._get_config = lambda key, default=None: cfg.get(key, default)
    plugin._set_config = lambda key, val: cfg.__setitem__(key, val)
    plugin._save_data = Mock(return_value=True)
    plugin.cookie_file = cookies.WeiboCookieFile(data_dir / "cookies" / "weibo_cookies.txt")
    plugin.legacy_cookie_file = cookies.WeiboCookieFile(data_dir / "cookies" / "weibo_cookie.txt")
    plugin.get_headers = lambda: {"User-Agent": "test", "Cookie": plugin._get_cookie_value()}
    plugin.client = types.SimpleNamespace(get=AsyncMock())
    plugin._request_semaphore = asyncio.Semaphore(1)
    plugin._cookie_fingerprint = lambda value: f"fp:{value}"
    return plugin, cfg


class SharedLinkResolverCookieTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_hot_reloads_and_skips_own_keepalive_when_shared_file_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_data_root = Path(tmpdir)
            monitor_dir = plugin_data_root / "astrbot_plugin_weibo_monitor"
            resolver_cookie = (
                plugin_data_root
                / "astrbot_plugin_link_resolver"
                / "cookies"
                / "weibo_cookies.txt"
            )
            resolver_cookie.parent.mkdir(parents=True, exist_ok=True)
            resolver_cookie.write_text(
                "\n".join(
                    [
                        "# Netscape HTTP Cookie File",
                        "#HttpOnly_.weibo.com\tTRUE\t/\tTRUE\t2000000000\tSUB\tfrom-resolver-v1",
                        "#HttpOnly_.login.sina.com.cn\tTRUE\t/\tTRUE\t2000000000\ttgc\tresolver-tgc",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            plugin, _ = _make_harness(monitor_dir, {"weibo_cookie": "SUB=local-old"})
            plugin._initialize_cookie_value()

            self.assertTrue(plugin._is_using_shared_cookies())
            self.assertEqual(plugin._get_cookie_value(), "SUB=from-resolver-v1")
            # Local monitor cookie file should not be written in shared mode.
            self.assertFalse(plugin.cookie_file.path.exists())

            # Keepalive & Set-Cookie capture must be skipped so plugins don't fight.
            namespace["collect_browser_cookies"].reset_mock()
            await plugin._refresh_weibo_cookie_session()
            namespace["collect_browser_cookies"].assert_not_awaited()
            plugin.client.get.assert_not_awaited()

            fake_response = types.SimpleNamespace(
                request=types.SimpleNamespace(
                    headers={"Cookie": "SUB=from-resolver-v1"},
                    url=types.SimpleNamespace(host="weibo.com"),
                ),
                url=types.SimpleNamespace(host="weibo.com", path="/ajax/statuses/mymblog"),
                status_code=200,
                headers=types.SimpleNamespace(get_list=lambda _k: ["SUB=mutated; Domain=.weibo.com"]),
            )
            await plugin._capture_weibo_cookie_updates(fake_response)
            self.assertIn("from-resolver-v1", resolver_cookie.read_text(encoding="utf-8"))

            # When link_resolver updates the shared file (e.g. after /weibo_login_scan),
            # weibo_monitor hot-reloads immediately on next read.
            resolver_cookie.write_text(
                "# Netscape HTTP Cookie File\n"
                "#HttpOnly_.weibo.com\tTRUE\t/\tTRUE\t2000000000\tSUB\tfrom-resolver-v2-updated\n",
                encoding="utf-8",
            )
            stat = resolver_cookie.stat()
            os.utime(resolver_cookie, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            self.assertEqual(plugin._get_cookie_value(), "SUB=from-resolver-v2-updated")

    async def test_falls_back_to_local_cookie_when_shared_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_data_root = Path(tmpdir)
            monitor_dir = plugin_data_root / "astrbot_plugin_weibo_monitor"
            resolver_cookie = (
                plugin_data_root
                / "astrbot_plugin_link_resolver"
                / "cookies"
                / "weibo_cookies.txt"
            )
            resolver_cookie.parent.mkdir(parents=True, exist_ok=True)
            resolver_cookie.write_text("SUB=from-resolver\n", encoding="utf-8")

            plugin, _ = _make_harness(
                monitor_dir,
                {
                    "use_link_resolver_cookies": False,
                    "weibo_cookie": "SUB=local-only",
                },
            )
            self.assertFalse(plugin._is_using_shared_cookies())
            self.assertEqual(plugin._get_cookie_value(), "SUB=local-only")


if __name__ == "__main__":
    unittest.main()
