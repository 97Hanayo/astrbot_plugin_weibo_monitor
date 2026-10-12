import importlib.util
import ast
import hashlib
import pathlib
import sys
import types
import unittest
import asyncio
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import quote
from unittest.mock import AsyncMock, Mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "weibo_monitor_session_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
for name in ("weibo_cookies", "weibo_session"):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

session = sys.modules[f"{PACKAGE}.weibo_session"]
cookies = sys.modules[f"{PACKAGE}.weibo_cookies"]

# Exercise the actual persistence methods without requiring an AstrBot runtime.
tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
plugin_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "WeiboMonitor")
selected = [n for n in plugin_class.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in {
    "_capture_playwright_cookie_updates", "_get_cookie_source", "_log_cookie_navigation", "_log_browser_cookie_state", "_persist_refreshed_cookie", "_cookie_fingerprint", "_log_cookie_refresh",
    "_initialize_cookie_value", "_capture_weibo_cookie_updates", "_refresh_weibo_cookie_session",
    "_fetch_weibo_cards", "_prepare_desktop_mblog", "_fetch_single_weibo",
    "_extract_image_urls", "_extract_video_info", "_extract_valid_mblogs",
    "get_headers", "_check_cookie_health", "weibo_verify",
}]
for method in selected:
    if method.name == "weibo_verify":
        method.decorator_list = []
namespace = {"hashlib": hashlib, "astrbot_logger": Mock(), **{name: getattr(cookies, name) for name in (
    "normalize_cookie_text", "normalize_cookie_source", "parse_browser_cookie_source", "update_cookie_source", "serialize_cookie_header", "parse_cookie_text", "would_remove_login_cookie",
    "is_desktop_weibo_domain", "merge_set_cookie_headers",
)}}
namespace.update(List=List, Dict=Dict, Any=Any, Optional=Optional, Tuple=Tuple,
                 asyncio=asyncio, quote=quote, login_data=session.login_data,
                 WEIBO_WEB_BASE="https://weibo.com", WEIBO_COOKIE_REFRESH_URL=session.WEIBO_LOGIN_URL,
                 WEIBO_API_BASE="https://weibo.com/ajax/statuses/mymblog",
                 httpx=types.SimpleNamespace(Response=object), DEFAULT_TIMEOUT=20,
                 WEIBO_BROWSER_REFRESH_URLS=("https://weibo.com/",),
                 collect_browser_cookies=AsyncMock(), BrowserUnavailableError=type("BrowserUnavailableError", (RuntimeError,), {}), BrowserCookieRefreshError=type("BrowserCookieRefreshError", (RuntimeError,), {}), AstrMessageEvent=object)
ast_class = ast.ClassDef(name="PersistenceHarness", bases=[], keywords=[], body=selected, decorator_list=[])
exec(compile(ast.fix_missing_locations(ast.Module(body=[ast_class], type_ignores=[])), "main.py", "exec"), namespace)
PersistenceHarness = namespace["PersistenceHarness"]


def persistence_harness():
    plugin = PersistenceHarness()
    plugin.cookie = "SUB=old"
    plugin._data = {}
    plugin.plugin_logger = Mock()
    plugin.cookie_file = types.SimpleNamespace(save=Mock(return_value=True), path=ROOT / "cookies" / "weibo_cookies.txt")
    plugin._get_cookie_value = lambda: plugin.cookie
    plugin._set_config = lambda key, value: setattr(plugin, "cookie", value)
    plugin._save_data = Mock(return_value=True)
    plugin._save_plugin_config_async = AsyncMock(return_value=True)
    plugin._cookie_refresh_lock = asyncio.Lock()
    plugin._get_config = lambda key, default=None: plugin.cookie if key == "weibo_cookie" else default
    return plugin


def response(login=True, uid="123", updates=(), status=200):
    return types.SimpleNamespace(
        status_code=status,
        json=lambda: {"ok": 1, "data": {"login": login, "uid": uid}},
        headers=types.SimpleNamespace(get_list=lambda _: list(updates)),
    )


class SessionTests(unittest.IsolatedAsyncioTestCase):

    def test_netscape_ignores_mobile_and_subdomain_credentials(self):
        jar = "\n".join(f"{domain}\tTRUE\t/\tTRUE\t0\tSUB\t{value}" for domain, value in (
            (".weibo.com", "desktop"), (".weibo.cn", "mobile"),
            ("passport.weibo.com", "passport"), ("s.weibo.com", "search"),
        ))
        self.assertEqual(cookies.normalize_cookie_text(jar), "SUB=desktop")


    async def test_desktop_list_preserves_ids_pinning_and_long_text(self):
        plugin = persistence_harness()
        plugin._request_semaphore = asyncio.Semaphore(1)
        items = [{"idstr": "101", "mblogid": "abc", "text": "short", "isLongText": True,
                  "user": {"screen_name": "tester"}}, {"idstr": "102", "mblogid": "def", "isTop": True}]
        plugin.client = types.SimpleNamespace(get=AsyncMock(side_effect=[
            types.SimpleNamespace(status_code=200, json=lambda: {"ok": 1, "data": {"list": items}}),
            types.SimpleNamespace(status_code=200, json=lambda: {"ok": 1, "data": {"longTextContent": "full text"}}),
        ]))
        cards = await plugin._fetch_weibo_cards("123")
        statuses, username = plugin._extract_valid_mblogs(cards)
        self.assertEqual(len(statuses), 1)
        self.assertEqual((statuses[0]["id"], statuses[0]["bid"], statuses[0]["text"]), ("101", "abc", "full text"))
        self.assertEqual(username, "tester")
        self.assertEqual(plugin.client.get.call_args_list[0].args[0], "https://weibo.com/ajax/statuses/mymblog?uid=123&page=1&feature=0")
        self.assertEqual(plugin.client.get.call_args_list[1].kwargs["params"], {"id": "101"})

    async def test_desktop_detail_accepts_direct_and_wrapped_responses(self):
        status = {"idstr": "101", "mblogid": "abc", "text": "body", "user": {"id": "123"}}
        for payload in (status, {"ok": 1, "data": status}):
            plugin = persistence_harness()
            plugin._request_semaphore = asyncio.Semaphore(1)
            plugin.clean_text = lambda text: text
            plugin._parse_weibo_time = lambda value: value
            plugin.client = types.SimpleNamespace(get=AsyncMock(return_value=types.SimpleNamespace(status_code=200, json=lambda: payload)))
            post = await plugin._fetch_single_weibo("abc")
            self.assertEqual(post["link"], "https://weibo.com/123/abc")
            self.assertEqual(post["text"], "body")
            self.assertEqual(plugin.client.get.call_args.args[0], "https://weibo.com/ajax/statuses/show?id=abc")

    def test_desktop_media_formats(self):
        plugin = persistence_harness()
        self.assertEqual(plugin._extract_image_urls({"pic_ids": ["b", "a"], "pic_infos": {
            "a": {"large": {"url": "//img/a"}}, "b": {"original": {"url": "https://img/b"}},
        }}), ["https://img/b", "https://img/a"])
        video = plugin._extract_video_info({"page_info": {"type": "video", "media_info": {"playback_list": [
            {"play_info": {"bitrate": 100, "url": "https://video/low"}},
            {"play_info": {"bitrate": 200, "url": "https://video/high"}},
        ]}}})
        self.assertEqual(video["url"], "https://video/high")



    def test_backend_new_value_overrides_old_file(self):
        for recorded in (None, "SUB=old"):
            plugin = persistence_harness()
            plugin.cookie = "SUB=manual"
            plugin.cookie_file.load = Mock(return_value="SUB=old")
            plugin._data["_backup_weibo_cookie"] = "SUB=old"
            if recorded:
                plugin._data["_cookie_config_fingerprint"] = plugin._cookie_fingerprint(recorded)
            plugin._initialize_cookie_value()
            self.assertEqual(plugin.cookie, "SUB=manual")
            plugin.cookie_file.save.assert_called_once_with("SUB=manual")
            self.assertEqual(plugin._data["_backup_weibo_cookie"], "SUB=manual")

    def test_unchanged_backend_retains_renewed_file_across_reloads(self):
        plugin = persistence_harness()
        plugin.cookie_file.load = Mock(return_value="SUB=renewed")
        plugin._data["_cookie_config_fingerprint"] = plugin._cookie_fingerprint("SUB=old")
        for _ in range(2):
            plugin.cookie = "SUB=old"
            plugin._initialize_cookie_value()
            self.assertEqual(plugin.cookie, "SUB=renewed")
            self.assertEqual(plugin._data["_cookie_config_fingerprint"], plugin._cookie_fingerprint("SUB=old"))
        plugin.cookie_file.save.assert_not_called()

    async def test_config_save_failure_keeps_previous_backend_fingerprint(self):
        plugin = persistence_harness()
        fingerprint = plugin._cookie_fingerprint("SUB=old")
        plugin._data["_cookie_config_fingerprint"] = fingerprint
        plugin._save_plugin_config_async.return_value = False
        self.assertTrue(await plugin._persist_refreshed_cookie("SUB=old", "SUB=new", "test"))
        self.assertEqual(plugin._data["_cookie_config_fingerprint"], fingerprint)










    async def test_false_or_missing_login_does_not_reject_cookie_request(self):
        for payload in ({"ok": 1, "data": {"login": False, "uid": 0}}, {"ok": 1, "data": {}}, {}):
            plugin = persistence_harness()
            plugin.client = types.SimpleNamespace(get=AsyncMock(return_value=types.SimpleNamespace(status_code=200, json=lambda: payload)))
            self.assertEqual(await plugin._check_cookie_health(), "valid")
            self.assertEqual(plugin.client.get.call_args.kwargs["headers"]["Cookie"], "SUB=old")

    async def test_response_updates_persist_with_no_uid_requirement(self):
        plugin = persistence_harness()
        reply = response(login=True, uid=0, updates=["SUB=new; Domain=.weibo.com"])
        reply.url = types.SimpleNamespace(host="weibo.com", path="/ajax/config/get_config")
        reply.request = types.SimpleNamespace(headers={"Cookie": "SUB=old"})
        reply.json = Mock(return_value={"ok": 1, "data": {"login": True}})
        await plugin._capture_weibo_cookie_updates(reply)
        plugin.cookie_file.save.assert_called_once_with("SUB=new")
        self.assertEqual(plugin.cookie, "SUB=new")
        reply.json.assert_called_once()

    async def test_verify_accepts_false_login_and_non_json_account_info(self):
        for parse in (lambda: {"data": {"login": False, "uid": 0}}, Mock(side_effect=ValueError("not JSON"))):
            plugin = persistence_harness()
            plugin._set_cookie_health_status = Mock(return_value=True)
            plugin.client = types.SimpleNamespace(get=AsyncMock(return_value=types.SimpleNamespace(status_code=200, json=parse)))
            event = types.SimpleNamespace(plain_result=lambda value: value)
            messages = [message async for message in plugin.weibo_verify(event)]
            self.assertIn("请求成功", messages[-1])
            plugin._set_cookie_health_status.assert_called_once_with("valid")

    async def test_stale_response_and_deleted_credentials_do_not_overwrite_cookie(self):
        plugin = persistence_harness()
        plugin.cookie = "SUB=manual"
        self.assertFalse(await plugin._persist_refreshed_cookie("SUB=old", "SUB=new", "test"))
        self.assertFalse(await plugin._persist_refreshed_cookie("SUB=manual", "XSRF-TOKEN=value", "test"))
        plugin.cookie_file.save.assert_not_called()
        self.assertEqual(plugin.cookie, "SUB=manual")

    async def test_keepalive_uses_cookie_and_falls_back_then_obeys_interval(self):
        plugin = persistence_harness()
        plugin._last_cookie_refresh_at = 0
        plugin.data_dir = ROOT
        plugin._request_semaphore = asyncio.Semaphore(1)
        plugin.client = types.SimpleNamespace(get=AsyncMock(return_value=response(login=False, uid=0)))
        browser = namespace["collect_browser_cookies"]
        browser.reset_mock()
        browser.side_effect = RuntimeError("browser unavailable")
        await plugin._refresh_weibo_cookie_session()
        self.assertEqual(browser.call_args.kwargs["cookies"], {"SUB": "old"})
        self.assertNotIn("login_check_url", browser.call_args.kwargs)
        plugin.client.get.assert_awaited_once()
        self.assertEqual(plugin.client.get.call_args.kwargs["headers"]["Cookie"], "SUB=old")
        await plugin._refresh_weibo_cookie_session()
        self.assertEqual(browser.await_count, 1)
        self.assertEqual(plugin.client.get.await_count, 1)

    async def test_keepalive_browser_success_and_disabled_setting(self):
        plugin = persistence_harness()
        plugin._last_cookie_refresh_at = 0
        plugin.data_dir = ROOT
        plugin.client = types.SimpleNamespace(get=AsyncMock())
        plugin._capture_playwright_cookie_updates = AsyncMock()
        browser = namespace["collect_browser_cookies"]
        browser.reset_mock()
        browser.side_effect = None
        browser.return_value = [{"name": "SUB", "value": "new", "domain": ".weibo.com"}]
        await plugin._refresh_weibo_cookie_session()
        plugin._capture_playwright_cookie_updates.assert_awaited_once_with(browser.return_value, "SUB=old")
        plugin.client.get.assert_not_awaited()
        plugin._get_config = lambda key, default=None: False if key == "auto_refresh_cookies" else default
        plugin._last_cookie_refresh_at = 0
        await plugin._refresh_weibo_cookie_session()
        self.assertEqual(browser.await_count, 1)


    def test_flattened_netscape_preserves_sina_scope_and_header_isolation(self):
        source = "# Netscape HTTP Cookie File\n.weibo.com\tTRUE\t/\tTRUE\t0\tSUB\tdesktop\n#HttpOnly_.login.sina.com.cn\tTRUE\t/sso\tTRUE\t0\ttgc\tsso"
        for text in (source, source.replace("\n", " "), source.replace("\n", " ").replace("\t", " "), source.replace("\n", r"\n").replace("\t", r"\t")):
            seeds = cookies.parse_browser_cookie_source(text)
            self.assertEqual(len(seeds), 2)
            self.assertEqual(seeds[1]["domain"], ".login.sina.com.cn")
            self.assertEqual(seeds[1]["path"], "/sso")
            self.assertTrue(seeds[1]["httpOnly"])
            self.assertEqual(cookies.parse_cookie_text(text), {"SUB": "desktop"})

    async def test_sso_only_manual_change_is_detected_at_startup(self):
        original = ".weibo.com\tTRUE\t/\tTRUE\t0\tSUB\told\n.login.sina.com.cn\tTRUE\t/\tTRUE\t0\ttgc\tfirst"
        plugin = persistence_harness()
        plugin.cookie = original.replace("first", "second")
        plugin.cookie_file.load = Mock(return_value=original)
        plugin._data["_cookie_config_fingerprint"] = plugin._cookie_fingerprint(original)
        plugin._initialize_cookie_value()
        self.assertIn("second", plugin.cookie_file.save.call_args.args[0])

    async def test_http_renewal_retains_sina_cookie_in_file(self):
        plugin = persistence_harness()
        plugin.cookie = ".weibo.com\tTRUE\t/\tTRUE\t0\tSUB\told\n.login.sina.com.cn\tTRUE\t/\tTRUE\t0\ttgc\tsso"
        plugin._get_cookie_value = lambda: cookies.normalize_cookie_text(plugin.cookie)
        await plugin._persist_refreshed_cookie("SUB=old", "SUB=new", "test")
        saved = plugin.cookie_file.save.call_args.args[0]
        self.assertEqual(cookies.parse_cookie_text(saved), {"SUB": "new"})
        self.assertEqual(cookies.parse_browser_cookie_source(saved)[0]["name"], "tgc")

    async def test_login_false_config_does_not_overwrite_cookie(self):
        plugin = persistence_harness()
        reply = response(login=False, updates=["SUB=visitor; Domain=.weibo.com"])
        reply.url = types.SimpleNamespace(host="weibo.com", path="/ajax/config/get_config")
        reply.request = types.SimpleNamespace(headers={"Cookie": "SUB=old"})
        await plugin._capture_weibo_cookie_updates(reply)
        plugin.cookie_file.save.assert_not_called()

    async def test_failed_recovery_does_not_fall_back_to_http(self):
        plugin = persistence_harness()
        plugin._last_cookie_refresh_at = 0
        plugin.data_dir = ROOT
        plugin.client = types.SimpleNamespace(get=AsyncMock())
        browser = namespace["collect_browser_cookies"]
        browser.side_effect = namespace["BrowserCookieRefreshError"]("still on login")
        await plugin._refresh_weibo_cookie_session()
        plugin.client.get.assert_not_awaited()
        plugin.cookie_file.save.assert_not_called()

    async def test_sso_replaced_during_browser_refresh_discards_old_result(self):
        plugin = persistence_harness()
        plugin._last_cookie_refresh_at = 0
        plugin.data_dir = ROOT
        plugin._capture_playwright_cookie_updates = AsyncMock()
        source = ".weibo.com\tTRUE\t/\tTRUE\t0\tSUB\told\n.login.sina.com.cn\tTRUE\t/\tTRUE\t0\ttgc\tfirst"
        plugin.cookie = source
        plugin._get_cookie_value = lambda: cookies.normalize_cookie_text(plugin.cookie)
        async def replace(**kwargs):
            plugin.cookie = source.replace("first", "second")
            return [{"name": "SUB", "value": "new", "domain": ".weibo.com"}]
        namespace["collect_browser_cookies"].side_effect = replace
        await plugin._refresh_weibo_cookie_session()
        plugin._capture_playwright_cookie_updates.assert_not_awaited()


    async def test_browser_capture_preserves_full_jar_and_host_cookie_precedence(self):
        plugin = persistence_harness()
        plugin._get_cookie_value = lambda: cookies.normalize_cookie_text(plugin.cookie)
        jar = [
            {"name": "SUB", "value": "host", "domain": "weibo.com", "path": "/"},
            {"name": "SUB", "value": "parent", "domain": ".weibo.com", "path": "/"},
            {"name": "tgc", "value": "sso", "domain": ".login.sina.com.cn", "path": "/"},
        ]
        await plugin._capture_playwright_cookie_updates(jar, "SUB=old")
        self.assertEqual(plugin._get_cookie_value(), "SUB=host")
        self.assertEqual(len(cookies.parse_browser_cookie_source(plugin.cookie)), 3)

    async def test_sso_only_browser_update_is_persisted(self):
        plugin = persistence_harness()
        plugin._get_cookie_value = lambda: cookies.normalize_cookie_text(plugin.cookie)
        jar = [
            {"name": "SUB", "value": "old", "domain": ".weibo.com", "path": "/"},
            {"name": "tgc", "value": "new-sso", "domain": ".login.sina.com.cn", "path": "/"},
        ]
        await plugin._capture_playwright_cookie_updates(jar, "SUB=old")
        plugin.cookie_file.save.assert_called_once()
        self.assertEqual(cookies.parse_browser_cookie_source(plugin.cookie)[1]["value"], "new-sso")


    def test_long_header_round_trip_preserves_all_fields(self):
        values = {"SUB": "one", "SUBP": "two", "ALF": "three", "WBPSESS": "four",
                  "SINAGLOBAL": "five", "ULV": "six", "XSRF-TOKEN": "seven", "_s_tentry": "eight=tail"}
        header = cookies.serialize_cookie_header(values)
        for text in (header, "Cookie: " + header):
            self.assertEqual(cookies.parse_cookie_text(text), values)
            self.assertEqual(cookies.normalize_cookie_text(cookies.normalize_cookie_text(text)), header)
        netscape = "\n".join(f".weibo.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}" for name, value in values.items())
        self.assertEqual(cookies.parse_cookie_text(cookies.normalize_cookie_text(netscape)), values)

    async def test_full_plugin_keepalive_passes_large_desktop_header_and_scoped_sso(self):
        plugin = persistence_harness()
        values = {f"C{i}": f"value{i}" for i in range(9)}
        values.update(SUB="old", SUBP="oldp", WBPSESS="oldw")
        source = "\n".join(f".weibo.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}" for name, value in values.items())
        source += "\n#HttpOnly_.login.sina.com.cn\tTRUE\t/\tTRUE\t0\ttgc\tsso"
        plugin.cookie = source
        plugin._get_cookie_value = lambda: cookies.normalize_cookie_text(plugin.cookie)
        plugin._last_cookie_refresh_at = 0
        plugin.data_dir = ROOT
        plugin.client = types.SimpleNamespace(get=AsyncMock())
        jar = cookies.parse_browser_cookie_source(source)
        jar[0]["value"] = "renewed"
        browser = namespace["collect_browser_cookies"]
        browser.reset_mock()
        browser.side_effect = None
        browser.return_value = jar
        await plugin._refresh_weibo_cookie_session()
        self.assertEqual(browser.call_args.kwargs["cookies"], values)
        self.assertEqual(browser.call_args.kwargs["scoped_cookies"][-1]["name"], "tgc")
        plugin.client.get.assert_not_awaited()
        plugin.cookie_file.save.assert_called_once()
        self.assertEqual(cookies.parse_cookie_text(plugin.cookie)["C0"], "renewed")


if __name__ == "__main__":
    unittest.main()
