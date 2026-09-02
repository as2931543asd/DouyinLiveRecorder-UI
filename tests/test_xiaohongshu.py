from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import live_history, platforms, runtime, spider
from src.recorder import _fetch_stream_info, _ffmpeg_prologue
from src.url_config import get_configured_urls
from webui import server


def _room_html(room_info: dict, status: str = "success") -> str:
    state = {
        "liveStream": {
            "liveStatus": status,
            "roomData": {"roomInfo": room_info},
        }
    }
    return (
        "<html><script>window.__INITIAL_STATE__="
        + json.dumps(state, ensure_ascii=False)
        + ";</script></html>"
    )


class PlatformUrlTests(unittest.TestCase):
    def test_identifies_supported_xhs_urls_without_substring_false_positives(self) -> None:
        self.assertEqual(
            platforms.identify_platform(
                "https://www.xiaohongshu.com/livestream/570321613446566013"
            ),
            platforms.XIAOHONGSHU,
        )
        self.assertEqual(
            platforms.identify_platform(
                "https://www.xiaohongshu.com/livestream/"
                "dynpathBELik3RA/570435571505008498?xsec_token=secret"
            ),
            platforms.XIAOHONGSHU,
        )
        self.assertEqual(
            platforms.identify_platform("http://xhslink.com/abc123"),
            platforms.XIAOHONGSHU,
        )
        self.assertIsNone(
            platforms.identify_platform("https://xiaohongshu.com/user/profile/user-id")
        )
        self.assertIsNone(
            platforms.identify_platform("https://xiaohongshu.com.example.test/livestream/123")
        )

    def test_normalizes_room_tracking_query_but_preserves_short_link_query(self) -> None:
        self.assertEqual(
            platforms.normalize_supported_url(
                "www.xiaohongshu.com/livestream/570321613446566013/?xsec_token=secret"
            ),
            "https://www.xiaohongshu.com/livestream/570321613446566013",
        )
        self.assertEqual(
            platforms.normalize_supported_url("http://xhslink.com/abc123?source=share"),
            "http://xhslink.com/abc123?source=share",
        )


class XiaohongshuParserTests(unittest.TestCase):
    def test_parses_selected_h264_quality_from_pull_config(self) -> None:
        room_info = {
            "roomTitle": "今晚直播",
            "deeplink": "xhsdiscover://live?host_nickname=%E5%B0%8F%E7%BA%A2",
            "pullConfig": json.dumps({
                "h264": [
                    {
                        "quality_type": "HD",
                        "master_url": "https://cdn.example/live-hd.flv?token=1",
                    },
                    {
                        "quality_type": "SD",
                        "master_url": "https://cdn.example/live-sd.flv?token=2",
                    },
                    {
                        "quality_type": "SD",
                        "master_url": "https://cdn.example/live-sd.m3u8?token=3",
                    },
                ]
            }),
        }
        request = AsyncMock(return_value=_room_html(room_info))

        with patch.object(spider, "async_req", request):
            info = asyncio.run(spider.get_xhs_stream_url(
                "https://www.xiaohongshu.com/livestream/570321613446566013",
                video_quality="SD",
            ))

        self.assertTrue(info["fetch_ok"])
        self.assertTrue(info["is_live"])
        self.assertEqual(info["anchor_name"], "小红")
        self.assertEqual(info["title"], "今晚直播")
        self.assertEqual(info["record_url"], "https://cdn.example/live-sd.flv?token=2")
        self.assertEqual(info["m3u8_url"], "https://cdn.example/live-sd.m3u8?token=3")
        self.assertEqual(info["codec"], "h264")
        self.assertIn("xy-common-params", request.call_args.kwargs["headers"])

    def test_expands_short_link_and_supports_legacy_deeplink_stream(self) -> None:
        room_info = {
            "roomTitle": "旧版直播页",
            "deeplink": (
                "xhsdiscover://live?host_nickname=%E4%B8%BB%E6%92%AD"
                "&flvUrl=https%3A%2F%2Fcdn.example%2Fold.flv%3Ftoken%3Dabc"
            ),
        }
        request = AsyncMock(side_effect=[
            "https://www.xiaohongshu.com/livestream/123456?source=share",
            _room_html(room_info),
        ])

        with patch.object(spider, "async_req", request):
            info = asyncio.run(spider.get_xhs_stream_url("http://xhslink.com/short"))

        self.assertTrue(info["is_live"])
        self.assertEqual(info["anchor_name"], "主播")
        self.assertEqual(info["record_url"], "https://cdn.example/old.flv?token=abc")
        self.assertEqual(info["m3u8_url"], "https://cdn.example/old.m3u8?token=abc")
        self.assertTrue(request.call_args_list[0].kwargs["redirect_url"])

    def test_expands_current_dynamic_path_share_link(self) -> None:
        request = AsyncMock(side_effect=[
            (
                "https://www.xiaohongshu.com/livestream/"
                "dynpathBELik3RA/570435571505008498?xsec_token=secret"
            ),
            _room_html({"roomTitle": "直播已结束"}, "ended"),
        ])

        with patch.object(spider, "async_req", request):
            info = asyncio.run(spider.get_xhs_stream_url(
                "https://xhslink.com/m/7dAJAYRe1Eu"
            ))

        self.assertTrue(info["fetch_ok"])
        self.assertFalse(info["is_live"])
        self.assertEqual(info["room_id"], "570435571505008498")
        self.assertEqual(
            request.call_args_list[1].args[0],
            "https://www.xiaohongshu.com/livestream/570435571505008498",
        )

    def test_offline_room_is_a_successful_fetch_without_stream_url(self) -> None:
        request = AsyncMock(return_value=_room_html({"roomTitle": "直播已结束"}, "ended"))
        with patch.object(spider, "async_req", request):
            info = asyncio.run(spider.get_xhs_stream_url(
                "https://www.xiaohongshu.com/livestream/123456"
            ))

        self.assertTrue(info["fetch_ok"])
        self.assertFalse(info["is_live"])
        self.assertNotIn("record_url", info)


class XiaohongshuIntegrationTests(unittest.TestCase):
    def test_url_config_includes_active_and_paused_xhs_rooms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "URL_config.ini"
            config_file.write_text(
                "原画,https://www.xiaohongshu.com/livestream/123?source=one\n"
                "#高清,http://xhslink.com/short?source=two\n",
                encoding="utf-8-sig",
            )

            urls = get_configured_urls(str(config_file), "原画")

        self.assertEqual(urls, [
            "https://www.xiaohongshu.com/livestream/123",
            "http://xhslink.com/short?source=two",
        ])

    def test_recorder_dispatches_xhs_cookie_and_quality_to_parser(self) -> None:
        parser = AsyncMock(return_value={
            "anchor_name": "主播",
            "is_live": False,
            "fetch_ok": True,
        })
        settings = SimpleNamespace(proxy_addr=None, xhs_cookie="xhs-cookie")

        with patch.object(spider, "get_xhs_stream_url", parser), patch.object(
            runtime,
            "run_coro",
            side_effect=asyncio.run,
        ):
            info, raw = _fetch_stream_info(
                platforms.XIAOHONGSHU,
                "https://www.xiaohongshu.com/livestream/123",
                "SD",
                settings,
            )

        self.assertTrue(info["fetch_ok"])
        self.assertEqual(raw, {})
        parser.assert_awaited_once_with(
            url="https://www.xiaohongshu.com/livestream/123",
            video_quality="SD",
            proxy_addr=None,
            cookies="xhs-cookie",
        )

    def test_webui_accepts_and_labels_xhs_room(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "URL_config.ini"
            history_file = Path(directory) / "live_history.json"
            config_file.write_text("", encoding="utf-8-sig")
            live_history.init(history_file)
            server.init(str(config_file))

            result = asyncio.run(server.add_streamer(server.StreamerAdd(
                url=(
                    "https://www.xiaohongshu.com/livestream/570321613446566013"
                    "?xsec_token=tracking"
                ),
                quality="高清",
            )))
            streamers = server._parse_url_config()

        self.assertTrue(result["ok"])
        self.assertEqual(len(streamers), 1)
        self.assertEqual(streamers[0]["platform"], platforms.XIAOHONGSHU)
        self.assertEqual(streamers[0]["platform_name"], "小红书直播")
        self.assertNotIn("xsec_token", streamers[0]["url"])

    def test_xhs_ffmpeg_headers_are_input_options(self) -> None:
        cmd = _ffmpeg_prologue(
            "https://cdn.example/live.flv",
            None,
            user_agent="xhs-ios-agent",
            referer="https://app.xhs.cn/",
        )

        input_index = cmd.index("-i")
        self.assertEqual(cmd[cmd.index("-user_agent") + 1], "xhs-ios-agent")
        self.assertLess(cmd.index("-referer"), input_index)
        self.assertEqual(cmd[cmd.index("-referer") + 1], "https://app.xhs.cn/")


if __name__ == "__main__":
    unittest.main()
