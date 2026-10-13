"""Exercise real plugin methods with lightweight AstrBot message adapters."""
import ast
import asyncio
import json
import pathlib
import re
import types
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, Mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
plugin_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WeiboMonitor")
method_names = {
    "_get_delivery_options", "_extract_post_content", "_extract_image_urls",
    "_extract_video_info", "_post_delivery_variant", "_format_post_text",
    "_send_post_to_targets", "_send_post_variant_to_targets", "_collect_new_posts",
    "save_subscription_mappings", "_save_subscription_backup", "_prepare_desktop_mblog",
}
methods = [node for node in plugin_class.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in method_names]


class MessageChain:
    def __init__(self):
        self.chain = []

    def message(self, text):
        self.chain.append(text)
        return self


namespace = dict(
    Any=Any, Dict=Dict, List=List, Optional=Optional, Tuple=Tuple, asyncio=asyncio,
    re=re, MessageChain=MessageChain, TARGET_ID_FAILURE_GUIDANCE="guidance",
    WEIBO_WEB_BASE="https://weibo.com", json_response=lambda value: value,
    error_response=lambda message, status_code: {"error": message, "status": status_code},
    request=types.SimpleNamespace(json=AsyncMock()),
    Comp=types.SimpleNamespace(
        Image=lambda file: ("image", file),
        Video=types.SimpleNamespace(fromFileSystem=lambda path: ("video", path)),
    ),
)
harness_class = ast.ClassDef(name="Harness", bases=[], keywords=[], body=methods, decorator_list=[])
exec(compile(ast.fix_missing_locations(ast.Module(body=[harness_class], type_ignores=[])), "main.py", "exec"), namespace)


def harness():
    plugin = namespace["Harness"]()
    plugin.config = {}
    plugin.clean_text = lambda text: re.sub("<[^>]+>", "", text)
    plugin._get_config = lambda key, default=None: default
    plugin.plugin_logger = Mock()
    plugin._data = {}
    plugin._save_data = Mock(return_value=True)
    plugin._get_utc8_now = Mock(return_value=Mock(strftime=Mock(return_value="2026-10-13 12:00:00")))
    plugin._log_to_daily_file = Mock()
    plugin._download_post_images = AsyncMock(side_effect=lambda post: post["image_urls"])
    plugin._download_post_video = AsyncMock(side_effect=lambda post: post["video_info"]["url"])
    plugin.context = types.SimpleNamespace(send_message=AsyncMock())
    plugin._send_message_with_timeout = AsyncMock()
    return plugin


def status():
    return {
        "id": "200", "bid": "Rmv9VFk2V", "text": "博主评论", "user": {"screen_name": "订阅博主"},
        "pics": [{"large": {"url": "https://img/comment"}}],
        "retweeted_status": {
            "id": "100", "bid": "source", "text": "<p>被转发正文</p>",
            "user": {"id": "42", "screen_name": "原作者"},
            "pic_ids": ["b", "a"], "pic_infos": {
                "a": {"original": {"url": "https://img/a"}},
                "b": {"large": {"url": "//img/b"}},
            },
            "page_info": {"type": "video", "media_info": {"stream_url_hd": "https://video/source"}},
        },
    }


def post(plugin, mblog=None):
    return {
        **plugin._extract_post_content(mblog or status()),
        "link": "https://weibo.com/5075479643/Rmv9VFk2V", "username": "订阅博主",
    }


class RetweetTests(unittest.IsolatedAsyncioTestCase):
    def test_variants_keep_source_separate_and_survive_queue_serialization(self):
        plugin = harness()
        original = json.loads(json.dumps(post(plugin)))
        own = plugin._post_delivery_variant(original, False)
        self.assertEqual(own["text"], "博主评论")
        self.assertEqual(own["image_urls"], ["https://img/comment"])
        self.assertIsNone(own["video_info"])
        full = plugin._post_delivery_variant(original, True)
        self.assertIn("转发 @原作者：\n被转发正文", full["text"])
        self.assertIn("https://weibo.com/42/source", full["text"])
        self.assertEqual(full["image_urls"], ["https://img/comment", "https://img/b", "https://img/a"])
        self.assertEqual(full["video_info"]["url"], "https://video/source")
        self.assertEqual(original["text"], "博主评论")
        self.assertIsNone(original["video_info"])

    def test_defaults_and_nested_deleted_retweets(self):
        plugin = harness()
        self.assertTrue(plugin._get_delivery_options("old")["include_retweet"])
        self.assertFalse(plugin._get_delivery_options("old")["receive_hotsearch"])
        self.assertTrue(plugin._get_delivery_options("old", True)["receive_hotsearch"])
        mblog = status()
        mblog["retweeted_status"]["retweeted_status"] = {"text": "微博已删除"}
        full = plugin._post_delivery_variant(post(plugin, mblog), True)
        self.assertIn("未知用户", full["text"])
        self.assertIn("微博已删除", full["text"])
        self.assertEqual(plugin._post_delivery_variant(post(plugin, {"text": "原创"}), True)["text"], "原创")

    async def test_mixed_targets_receive_selected_text_images_and_video(self):
        plugin = harness()
        plugin.config = {"subscription_delivery_options": {"own": {"include_retweet": False}, "full": {"include_retweet": True}}}
        original = post(plugin)
        result = await plugin._send_post_to_targets(original, "{weibo}", ["own", "full", "own"])
        messages = [(call.args[0], call.args[1].chain) for call in plugin._send_message_with_timeout.call_args_list]
        self.assertEqual(messages[0], ("own", ["博主评论"]))
        self.assertEqual(messages[1], ("own", [("image", "https://img/comment")]))
        self.assertIn("被转发正文", messages[2][1][0])
        self.assertEqual(len(messages[3][1]), 3)
        plugin.context.send_message.assert_awaited_once()
        self.assertEqual(plugin.context.send_message.call_args.args[0], "full")
        self.assertEqual(result["successful_text_targets"], ["own", "full"])
        self.assertEqual(result["successful_video_targets"], ["full"])
        self.assertTrue(original["_video_sent"])
        plugin._log_to_daily_file.assert_called_once_with(original, delivery_count=2)

    async def test_original_and_retweet_videos_both_sent_and_failure_reported(self):
        plugin = harness()
        mblog = status()
        mblog["page_info"] = {"type": "video", "urls": {"mp4_hd_mp4": "https://video/comment"}}
        plugin.context.send_message.side_effect = [None, RuntimeError("failed")]
        result = await plugin._send_post_to_targets(post(plugin, mblog), "{weibo}", ["full"])
        self.assertEqual([call.args[0]["video_info"]["url"] for call in plugin._download_post_video.call_args_list], ["https://video/comment", "https://video/source"])
        self.assertEqual(result["successful_text_targets"], ["full"])
        self.assertEqual(result["video_failed_targets"], ["full"])
        self.assertTrue(result["media_partial_failure"])

    async def test_disabled_retweet_never_downloads_source_media(self):
        plugin = harness()
        plugin.config = {"subscription_delivery_options": {"own": {"include_retweet": False}}}
        result = await plugin._send_post_to_targets(post(plugin), "{weibo}", ["own"], skip_log=True)
        plugin._download_post_video.assert_not_awaited()
        self.assertEqual(plugin._download_post_images.call_args.args[0]["image_urls"], ["https://img/comment"])
        self.assertFalse(result["video_available"])
        plugin._log_to_daily_file.assert_not_called()

    async def test_global_media_switches_still_apply(self):
        plugin = harness()
        plugin._get_config = lambda key, default=None: False if key in {"enable_image_download", "enable_video_download"} else default
        await plugin._send_post_to_targets(post(plugin), "{weibo}", ["full"])
        plugin._download_post_video.assert_not_awaited()
        plugin._download_post_images.assert_not_awaited()

    async def test_text_failure_in_one_group_preserves_other_success(self):
        plugin = harness()
        plugin.config = {"subscription_delivery_options": {"own": {"include_retweet": False}}}
        plugin._send_message_with_timeout.side_effect = [RuntimeError("failed"), None, None]
        result = await plugin._send_post_to_targets(post(plugin), "{weibo}", ["own", "full"])
        self.assertEqual(result["successful_text_targets"], ["full"])
        self.assertEqual(result["failed_text_targets"], ["own"])

    async def test_retweet_long_text_hydrated_before_content_extraction(self):
        plugin = harness()
        mblog = status()
        mblog["retweeted_status"]["isLongText"] = True
        plugin._request_semaphore = asyncio.Semaphore(1)
        plugin.get_headers = lambda: {}
        plugin.client = types.SimpleNamespace(get=AsyncMock(return_value=types.SimpleNamespace(
            status_code=200, json=lambda: {"ok": 1, "data": {"longTextContent": "完整转发长微博"}},
        )))
        prepared = await plugin._prepare_desktop_mblog(mblog)
        full = plugin._post_delivery_variant(post(plugin, prepared), True)
        self.assertIn("完整转发长微博", full["text"])

    def test_monitor_keeps_retweet_for_delivery_and_global_forward_filter(self):
        plugin = harness()
        plugin._has_filter_keyword = lambda *args: False
        plugin._should_skip_by_whitelist = lambda *args: False
        plugin._parse_weibo_time = lambda value: value
        posts = plugin._collect_new_posts("5075479643", [status()], 0, False, "订阅博主")
        self.assertIn("retweeted_post", posts[0])
        plugin._get_config = lambda key, default=None: False if key == "send_forward" else default
        self.assertEqual(plugin._collect_new_posts("5075479643", [status()], 0, False, "订阅博主"), [])

    async def test_api_validates_retweet_option_before_mutation(self):
        plugin = harness()
        plugin._normalize_monitored_urls = lambda values: values
        namespace["request"].json.return_value = {"monitor_urls": [], "rows": [{
            "session_id": "target", "mode": "all", "uids": [],
            "receive_hotsearch": False, "receive_daily_summary": False, "include_retweet": "false",
        }]}
        result = await plugin.save_subscription_mappings()
        self.assertEqual(result["status"], 400)
        self.assertIn("转发", result["error"])
        self.assertEqual(plugin.config, {})

    async def test_api_saves_retweet_selection_and_backup_with_legacy_default(self):
        for selection in (False, True, None):
            with self.subTest(selection=selection):
                plugin = harness()
                plugin._normalize_monitored_urls = lambda values: values
                plugin._set_config = lambda key, value: plugin.config.update({key: value})
                plugin._save_plugin_config_async = AsyncMock(return_value=True)
                plugin._get_runtime_status_for_page = lambda: {}
                row = {"session_id": "target", "mode": "all", "uids": [],
                       "receive_hotsearch": False, "receive_daily_summary": False}
                if selection is not None:
                    row["include_retweet"] = selection
                namespace["request"].json.return_value = {"monitor_urls": ["5075479643"], "rows": [row]}
                result = await plugin.save_subscription_mappings()
                self.assertTrue(result["saved"])
                expected = selection if selection is not None else True
                self.assertEqual(plugin._get_delivery_options("target")["include_retweet"], expected)
                backup = json.loads(json.dumps(plugin._data["_subscription_page_backup"]))
                self.assertEqual(backup["subscription_delivery_options"]["target"]["include_retweet"], expected)

    async def test_partial_video_download_failure_is_reported(self):
        plugin = harness()
        mblog = status()
        mblog["page_info"] = {"type": "video", "urls": {"mp4_hd_mp4": "https://video/comment"}}
        plugin._download_post_video.side_effect = ["local.mp4", None]
        result = await plugin._send_post_to_targets(post(plugin, mblog), "{weibo}", ["full"])
        self.assertEqual(result["successful_text_targets"], ["full"])
        self.assertEqual(result["successful_video_targets"], [])
        self.assertEqual(result["video_failed_targets"], ["full"])
        self.assertTrue(result["media_partial_failure"])


if __name__ == "__main__":
    unittest.main()
