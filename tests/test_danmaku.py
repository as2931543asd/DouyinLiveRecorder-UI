from __future__ import annotations

import asyncio
import datetime
import json
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.config_loader import Settings
from src.danmaku import (
    BilibiliXmlWriter,
    ChatMessage,
    DanmakuRecorder,
    danmaku_xml_path,
    extract_room_identifier,
    parse_chat_message,
)
from src.recorder import _record_once


class RoomIdentifierTests(unittest.TestCase):
    def test_prefers_owner_web_rid_over_other_fields_and_url(self) -> None:
        api_data = {
            "web_rid": "outer-room",
            "owner": {"web_rid": 123456789},
        }

        room_identifier = extract_room_identifier(
            api_data, "https://live.douyin.com/url-room?from=test"
        )

        self.assertEqual(room_identifier, "123456789")

    def test_finds_nested_web_rid(self) -> None:
        api_data = {"data": {"room": {"owner": {"web_rid": "nested-room"}}}}

        self.assertEqual(
            extract_room_identifier(api_data, "https://live.douyin.com/url-room"),
            "nested-room",
        )

    def test_falls_back_to_normalized_live_url_last_path_segment(self) -> None:
        self.assertEqual(
            extract_room_identifier(
                {}, "https://LIVE.DOUYIN.COM/first/final-room/?from=dashboard"
            ),
            "final-room",
        )

    def test_unreliable_short_url_has_no_fallback(self) -> None:
        self.assertIsNone(
            extract_room_identifier({}, "https://v.douyin.com/short-code/")
        )


class MessageParsingTests(unittest.TestCase):
    def test_filters_system_and_non_chat_messages(self) -> None:
        self.assertIsNone(
            parse_chat_message(
                {"type": "system", "method": "WebcastChatMessage", "user": {}}
            )
        )
        self.assertIsNone(
            parse_chat_message(
                {"method": "WebcastGiftMessage", "user": {"nickname": "用户"}}
            )
        )

    def test_accepts_documented_fields_and_numeric_user_id(self) -> None:
        message = parse_chat_message(
            {
                "method": "WebcastChatMessage",
                "user": {"nickname": "昵称", "id": 987654321},
                "content": "正文",
            }
        )

        self.assertEqual(message, ChatMessage("昵称", "987654321", "正文"))

    def test_accepts_nested_message_and_compatible_field_names(self) -> None:
        message = parse_chat_message(
            {
                "data": {
                    "method": "WebcastChatMessage",
                    "user": {"nick_name": "兼容昵称", "id_str": "uid-string"},
                    "default_content": "兼容正文",
                }
            }
        )

        self.assertEqual(message, ChatMessage("兼容昵称", "uid-string", "兼容正文"))


class XmlWriterTests(unittest.TestCase):
    def test_segment_template_is_removed_from_xml_path(self) -> None:
        self.assertEqual(
            danmaku_xml_path("/tmp/主播_2026-08-19_10-20-30_%03d.ts"),
            Path("/tmp/主播_2026-08-19_10-20-30.xml"),
        )
        self.assertEqual(
            danmaku_xml_path("/tmp/主播_2026-08-19_10-20-30.flv"),
            Path("/tmp/主播_2026-08-19_10-20-30.xml"),
        )

    def test_xml_escapes_text_is_parseable_and_has_bilibili_p_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            xml_path = Path(directory) / "record.xml"
            writer = BilibiliXmlWriter(xml_path, started_at=1000.0)
            writer.open()
            writer.write(
                ChatMessage('昵称<&"\x01', "user&1", "正文 > & < ' \""),
                received_at=1002.345,
            )
            writer.write(ChatMessage("第二人", "2", "第二条"), received_at=1003.0)
            writer.close()

            root = ET.parse(xml_path).getroot()
            comments = root.findall("d")
            first_fields = comments[0].attrib["p"].split(",")
            second_fields = comments[1].attrib["p"].split(",")

            self.assertEqual(root.tag, "i")
            self.assertEqual(comments[0].text, '昵称<&": 正文 > & < \' "')
            self.assertEqual(first_fields[:5], ["2.345", "1", "25", "16777215", "1002"])
            self.assertEqual(first_fields[6:], ["user&1", "1"])
            self.assertEqual(second_fields[-1], "2")


class _FakeWebSocket:
    def __init__(self, delivered: threading.Event) -> None:
        self._delivered = delivered
        self._sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._sent:
            self._sent = True
            return json.dumps(
                {
                    "method": "WebcastChatMessage",
                    "user": {"nickname": "mock 用户", "id": "mock-id"},
                    "content": "mock 正文",
                },
                ensure_ascii=False,
            )
        self._delivered.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration


class _FakeConnection:
    def __init__(self, websocket) -> None:
        self.websocket = websocket

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


class _SequenceWebSocket:
    def __init__(self, messages=()) -> None:
        self._messages = iter(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._messages)
        except StopIteration:
            raise StopAsyncIteration from None


class RecorderLifecycleTests(unittest.TestCase):
    def test_short_connections_keep_backing_off_to_existing_cap(self) -> None:
        recorder = DanmakuRecorder(
            room_identifier="123",
            service_url="ws://127.0.0.1:1088",
            xml_path="unused.xml",
            started_at=1000.0,
        )
        delays: list[float] = []

        async def record_retry(seconds: float) -> None:
            delays.append(seconds)
            if len(delays) == 6:
                recorder._stop_event.set()

        with patch(
            "src.danmaku.websockets.connect",
            side_effect=lambda *_args, **_kwargs: _FakeConnection(
                _SequenceWebSocket()
            ),
        ) as connect, patch.object(
            recorder, "_monotonic", return_value=1000.0
        ), patch.object(
            recorder, "_retry_sleep", side_effect=record_retry
        ):
            asyncio.run(recorder._consume())

        self.assertEqual(connect.call_count, 6)
        self.assertEqual(delays, [1, 2, 5, 10, 15, 15])

    def test_business_message_resets_accumulated_connection_failures(self) -> None:
        recorder = DanmakuRecorder(
            room_identifier="123",
            service_url="ws://127.0.0.1:1088",
            xml_path="unused.xml",
            started_at=1000.0,
        )
        delays: list[float] = []
        connections = iter(
            [
                _FakeConnection(_SequenceWebSocket()),
                _FakeConnection(_SequenceWebSocket()),
                _FakeConnection(
                    _SequenceWebSocket(
                        [json.dumps({"method": "WebcastGiftMessage"})]
                    )
                ),
            ]
        )

        async def record_retry(seconds: float) -> None:
            delays.append(seconds)
            if len(delays) == 3:
                recorder._stop_event.set()

        with patch(
            "src.danmaku.websockets.connect",
            side_effect=lambda *_args, **_kwargs: next(connections),
        ), patch.object(
            recorder, "_monotonic", return_value=1000.0
        ), patch.object(
            recorder, "_retry_sleep", side_effect=record_retry
        ):
            asyncio.run(recorder._consume())

        self.assertEqual(delays, [1, 2, 1])

    def test_healthy_connection_resets_accumulated_connection_failures(self) -> None:
        recorder = DanmakuRecorder(
            room_identifier="123",
            service_url="ws://127.0.0.1:1088",
            xml_path="unused.xml",
            started_at=1000.0,
        )
        delays: list[float] = []

        async def record_retry(seconds: float) -> None:
            delays.append(seconds)
            if len(delays) == 2:
                recorder._stop_event.set()

        with patch(
            "src.danmaku.websockets.connect",
            side_effect=lambda *_args, **_kwargs: _FakeConnection(
                _SequenceWebSocket()
            ),
        ), patch.object(
            recorder,
            "_monotonic",
            side_effect=[1000.0, 1000.0, 2000.0, 2030.0],
        ), patch.object(
            recorder, "_retry_sleep", side_effect=record_retry
        ):
            asyncio.run(recorder._consume())

        self.assertEqual(delays, [1, 1])

    def test_room_not_found_stops_retries_but_leaves_xml_open_until_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            xml_path = Path(directory) / "not-found.xml"
            recorder = DanmakuRecorder(
                room_identifier="missing-room",
                service_url="ws://127.0.0.1:1088",
                xml_path=xml_path,
                started_at=1000.0,
            )
            status_message = json.dumps(
                {
                    "type": "system",
                    "event": "live_status",
                    "code": "ROOM_NOT_FOUND",
                    "valid": False,
                    "live": False,
                }
            )
            connection = _FakeConnection(_SequenceWebSocket([status_message]))

            with patch(
                "src.danmaku.websockets.connect", return_value=connection
            ) as connect, patch.object(
                recorder, "_retry_sleep", new_callable=AsyncMock
            ) as retry_sleep:
                recorder.start()
                self.assertIsNotNone(recorder._thread)
                recorder._thread.join(timeout=2)

            connect.assert_called_once()
            retry_sleep.assert_not_awaited()
            self.assertFalse(recorder._thread.is_alive())
            self.assertIsNotNone(recorder.writer._file)
            self.assertFalse(
                xml_path.read_text(encoding="utf-8").endswith("</i>\n")
            )

            recorder.stop()

            self.assertIsNone(recorder.writer._file)
            self.assertTrue(
                xml_path.read_text(encoding="utf-8").endswith("</i>\n")
            )
            self.assertEqual(ET.parse(xml_path).getroot().tag, "i")

    def test_mock_websocket_uses_no_proxy_and_stop_closes_xml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            xml_path = Path(directory) / "mock.xml"
            delivered = threading.Event()
            connection = _FakeConnection(_FakeWebSocket(delivered))
            recorder = DanmakuRecorder(
                room_identifier="room/with space",
                service_url="ws://127.0.0.1:1088",
                xml_path=xml_path,
                started_at=1000.0,
            )

            with patch("src.danmaku.websockets.connect", return_value=connection) as connect:
                recorder.start()
                self.assertTrue(delivered.wait(timeout=2), "mock 弹幕未及时写入")
                recorder.stop(timeout=2)

            _, kwargs = connect.call_args
            self.assertEqual(connect.call_args.args[0], "ws://127.0.0.1:1088/ws/room%2Fwith%20space")
            self.assertIsNone(kwargs["proxy"])
            self.assertFalse(recorder._thread and recorder._thread.is_alive())
            root = ET.parse(xml_path).getroot()
            self.assertEqual(len(root.findall("d")), 1)
            self.assertTrue(xml_path.read_text(encoding="utf-8").endswith("</i>\n"))

    def test_connection_failure_still_creates_parseable_empty_xml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            xml_path = Path(directory) / "empty.xml"
            attempted = threading.Event()

            def fail_connect(*_args, **_kwargs):
                attempted.set()
                raise OSError("service unavailable")

            recorder = DanmakuRecorder(
                room_identifier="123",
                service_url="ws://127.0.0.1:1088",
                xml_path=xml_path,
                started_at=1000.0,
            )
            with patch("src.danmaku.websockets.connect", side_effect=fail_connect):
                recorder.start()
                self.assertTrue(attempted.wait(timeout=2))
                recorder.stop(timeout=2)

            root = ET.parse(xml_path).getroot()
            self.assertEqual(root.tag, "i")
            self.assertEqual(root.findall("d"), [])
            self.assertTrue(xml_path.read_text(encoding="utf-8").endswith("</i>\n"))

    def test_record_once_stops_danmaku_when_ffmpeg_start_raises(self) -> None:
        settings = SimpleNamespace(
            filename_by_title=False,
            split_video_by_time=True,
            split_time="1800",
            proxy_addr=None,
            corrupt_probe_decode=False,
            record_danmaku=True,
            danmaku_service_url="ws://127.0.0.1:1088",
            converts_to_mp4=False,
        )
        mock_recorder = Mock()

        with tempfile.TemporaryDirectory() as directory, patch(
            "src.recorder.DanmakuRecorder", return_value=mock_recorder
        ) as recorder_class, patch(
            "src.recorder._check_subprocess", side_effect=OSError("ffmpeg unavailable")
        ):
            with self.assertRaisesRegex(OSError, "ffmpeg unavailable"):
                _record_once(
                    real_url="https://example.invalid/live.flv",
                    anchor_name="主播",
                    live_title=None,
                    full_path=directory,
                    record_save_type="TS",
                    record_name="序号1 主播",
                    record_url="https://live.douyin.com/123",
                    settings=settings,
                    danmaku_room_identifier="123",
                )

        mock_recorder.start.assert_called_once_with()
        mock_recorder.stop.assert_called_once_with()
        xml_path = recorder_class.call_args.kwargs["xml_path"]
        started_at = recorder_class.call_args.kwargs["started_at"]
        filename_timestamp = datetime.datetime.fromtimestamp(started_at).strftime(
            "%Y-%m-%d_%H-%M-%S"
        )
        self.assertNotIn("_%03d", str(xml_path))
        self.assertIn(filename_timestamp, Path(xml_path).name)


class SettingsTests(unittest.TestCase):
    def test_danmaku_settings_default_and_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.ini"
            config_path.write_text(
                "[录制设置]\n是否录制弹幕 = 否\n"
                "弹幕服务地址 = ws://127.0.0.1:1088\n"
                "[Cookie]\n抖音cookie = keep-this-cookie\n",
                encoding="utf-8-sig",
            )
            settings = Settings(str(config_path))
            self.assertFalse(settings.record_danmaku)
            self.assertEqual(settings.danmaku_service_url, "ws://127.0.0.1:1088")

            content = config_path.read_text(encoding="utf-8-sig")
            config_path.write_text(
                content.replace("是否录制弹幕 = 否", "是否录制弹幕 = 是").replace(
                    "弹幕服务地址 = ws://127.0.0.1:1088",
                    "弹幕服务地址 = ws://127.0.0.1:2088",
                ),
                encoding="utf-8-sig",
            )
            settings.reload()

            self.assertTrue(settings.record_danmaku)
            self.assertEqual(settings.danmaku_service_url, "ws://127.0.0.1:2088")
            self.assertEqual(settings.dy_cookie, "keep-this-cookie")


if __name__ == "__main__":
    unittest.main()
