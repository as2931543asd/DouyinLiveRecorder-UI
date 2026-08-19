from __future__ import annotations

import asyncio
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.config_loader import Settings
from src.danmaku import (
    ChatMessage,
    DanmakuJsonlWriter,
    DanmakuRecorder,
    danmaku_jsonl_path,
    extract_room_identifier,
    parse_chat_message,
)
from src import danmaku as danmaku_module
from src.recorder import _check_subprocess, _record_once


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

    def test_missing_user_fields_are_none_and_empty_content_is_preserved(self) -> None:
        message = parse_chat_message(
            {
                "method": "WebcastChatMessage",
                "user": {},
                "content": "",
            }
        )

        self.assertEqual(message, ChatMessage(None, None, ""))

        self.assertEqual(
            parse_chat_message(
                {
                    "method": "WebcastChatMessage",
                    "content": "没有 user 对象",
                }
            ),
            ChatMessage(None, None, "没有 user 对象"),
        )

    def test_chat_without_content_is_skipped(self) -> None:
        self.assertIsNone(
            parse_chat_message(
                {
                    "method": "WebcastChatMessage",
                    "user": {"nickname": "没有正文"},
                }
            )
        )


class JsonlWriterTests(unittest.TestCase):
    def test_video_extension_and_segment_template_are_removed_from_jsonl_path(self) -> None:
        self.assertEqual(
            danmaku_jsonl_path("/tmp/主播_2026-08-19_10-20-30_%03d.ts"),
            Path("/tmp/主播_2026-08-19_10-20-30.danmaku.jsonl"),
        )
        self.assertEqual(
            danmaku_jsonl_path("/tmp/主播_2026-08-19_10-20-30.flv"),
            Path("/tmp/主播_2026-08-19_10-20-30.danmaku.jsonl"),
        )

    def test_metadata_chat_fields_utf8_controls_and_millisecond_flooring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jsonl_path = Path(directory) / "record.danmaku.jsonl"
            writer = DanmakuJsonlWriter(
                jsonl_path, room_identifier="直播间-一", started_at=1000.0009
            )
            writer.open()
            writer.write(
                ChatMessage('昵称<&"\x01', "user&1", "正文 > & < ' \"\x00"),
                received_at=1002.3459,
            )
            writer.write(ChatMessage("第二人", "2", "第二条"), received_at=999.9999)
            writer.close()

            raw = jsonl_path.read_text(encoding="utf-8")
            self.assertIn("直播间-一", raw)
            self.assertNotIn("\\u76f4", raw)
            self.assertTrue(raw.endswith("\n"))
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(len(records), 3)

            metadata, first, second = records
            self.assertEqual(
                metadata,
                {
                    "type": "metadata",
                    "schema": "douyin-live-recorder-ui.danmaku",
                    "version": 1,
                    "platform": "douyin",
                    "room_identifier": "直播间-一",
                    "recording_started_at_unix_ms": 1000000,
                    "time_units": {
                        "recording_started_at_unix_ms": "milliseconds since Unix epoch UTC",
                        "received_at_unix_ms": "milliseconds since Unix epoch UTC",
                        "offset_ms": "milliseconds from recording start",
                    },
                },
            )
            self.assertEqual(first["type"], "chat")
            self.assertEqual(first["sequence"], 1)
            self.assertEqual(first["method"], "WebcastChatMessage")
            self.assertEqual(first["offset_ms"], 2345)
            self.assertEqual(first["received_at_unix_ms"], 1002345)
            self.assertEqual(first["user"], {"id": "user&1", "nickname": '昵称<&"\x01'})
            self.assertEqual(first["content"], "正文 > & < ' \"\x00")
            self.assertNotIn("style", first)
            self.assertEqual(second["sequence"], 2)
            self.assertEqual(second["offset_ms"], 0)

    def test_nullable_user_fields_are_written_as_json_null(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nullable.danmaku.jsonl"
            writer = DanmakuJsonlWriter(path, room_identifier="123", started_at=1.0)
            writer.open()
            writer.write(ChatMessage(None, None, ""), received_at=1.0)
            writer.close()

            chat = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
            self.assertEqual(chat["user"], {"id": None, "nickname": None})
            self.assertEqual(chat["content"], "")

    def test_open_refuses_to_truncate_existing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.danmaku.jsonl"
            path.write_text("stale data", encoding="utf-8")
            writer = DanmakuJsonlWriter(path, room_identifier="123", started_at=1.0)

            with self.assertRaises(FileExistsError):
                writer.open()

            self.assertEqual(path.read_text(encoding="utf-8"), "stale data")
            self.assertIsNone(writer._file)

    def test_wall_clock_jumps_do_not_change_monotonic_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clock.danmaku.jsonl"
            writer = DanmakuJsonlWriter(
                path,
                room_identifier="123",
                started_at=1000.0,
                started_monotonic=500.0,
            )

            writer.open()
            with patch(
                "src.danmaku.time.time", side_effect=[5000.0, 900.0]
            ), patch(
                "src.danmaku.time.monotonic", side_effect=[501.0, 502.0]
            ):
                writer.write(ChatMessage("前跳", "1", "第一条"))
                writer.write(ChatMessage("后跳", "2", "第二条"))
            writer.close()

            chats = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()[1:]
            ]
            self.assertEqual(
                [chat["received_at_unix_ms"] for chat in chats],
                [5_000_000, 900_000],
            )
            self.assertEqual([chat["offset_ms"] for chat in chats], [1000, 2000])


class SubprocessStartedCallbackTests(unittest.TestCase):
    @staticmethod
    def _settings() -> SimpleNamespace:
        return SimpleNamespace(
            auto_restart_on_corrupt=False,
            corrupt_error_threshold=8,
            corrupt_error_window_seconds=20,
            converts_to_mp4=False,
            split_video_by_time=False,
        )

    @staticmethod
    def _finished_process() -> Mock:
        process = Mock()
        process.stdout = io.StringIO("")
        process.returncode = 0
        process.poll.return_value = 0
        return process

    def test_popen_success_reports_paired_clocks_after_reader_start(self) -> None:
        process = self._finished_process()
        reader_started = threading.Event()
        callback_values: list[tuple[float, float]] = []
        events: list[str] = []

        def popen(*_args, **_kwargs):
            events.append("popen")
            return process

        def wall_clock() -> float:
            events.append("wall-clock")
            return 1_787_104_800.125

        def monotonic_clock() -> float:
            events.append("monotonic-clock")
            return 456.75

        def reader(_pipe, _output_queue) -> None:
            events.append("reader")
            reader_started.set()

        def on_started(started_at: float, started_monotonic: float) -> None:
            self.assertTrue(reader_started.wait(timeout=1))
            events.append("callback")
            callback_values.append((started_at, started_monotonic))

        with patch(
            "src.recorder.subprocess.Popen", side_effect=popen
        ) as popen, patch(
            "src.recorder.time.time", side_effect=wall_clock
        ) as wall_clock_mock, patch(
            "src.recorder.time.monotonic", side_effect=monotonic_clock
        ) as monotonic_clock_mock, patch(
            "src.recorder._stream_reader", side_effect=reader
        ):
            result = _check_subprocess(
                "序号1 主播",
                "https://live.douyin.com/123",
                ["ffmpeg", "-i", "source", "output.flv"],
                "FLV",
                "output.flv",
                self._settings(),
                on_started=on_started,
            )

        self.assertEqual(result, "finished")
        popen.assert_called_once()
        wall_clock_mock.assert_called_once_with()
        monotonic_clock_mock.assert_called_once_with()
        self.assertEqual(callback_values, [(1_787_104_800.125, 456.75)])
        self.assertEqual(
            events,
            ["popen", "wall-clock", "monotonic-clock", "reader", "callback"],
        )

    def test_started_callback_failure_does_not_change_ffmpeg_result(self) -> None:
        process = self._finished_process()
        callback = Mock(side_effect=RuntimeError("callback failed"))

        with patch(
            "src.recorder.subprocess.Popen", return_value=process
        ), patch("src.recorder.logger.error") as error:
            result = _check_subprocess(
                "序号1 主播",
                "https://live.douyin.com/123",
                ["ffmpeg", "output.flv"],
                "FLV",
                "output.flv",
                self._settings(),
                on_started=callback,
            )

        self.assertEqual(result, "finished")
        callback.assert_called_once()
        process.send_signal.assert_not_called()
        process.kill.assert_not_called()
        self.assertIn("回调失败", error.call_args.args[0])


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
    def test_existing_jsonl_start_failure_is_logged_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.danmaku.jsonl"
            path.write_text("source truth", encoding="utf-8")
            recorder = DanmakuRecorder(
                room_identifier="123",
                service_url="ws://127.0.0.1:1088",
                jsonl_path=path,
                started_at=1000.0,
            )

            with patch("src.danmaku.logger.error") as error:
                recorder.start()

            self.assertIsNone(recorder._thread)
            self.assertIsNone(recorder.writer._file)
            self.assertEqual(path.read_text(encoding="utf-8"), "source truth")
            self.assertIn("视频录制继续", error.call_args.args[0])

    def test_open_and_write_failures_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = DanmakuRecorder(
                room_identifier="123",
                service_url="ws://127.0.0.1:1088",
                jsonl_path=Path(directory) / "failed.danmaku.jsonl",
                started_at=1000.0,
            )
            with patch.object(
                recorder.writer, "open", side_effect=OSError("open failed")
            ), patch("src.danmaku.logger.error") as error:
                recorder.start()
            self.assertIsNone(recorder._thread)
            self.assertIsNone(recorder.writer._file)
            self.assertIn("视频录制继续", error.call_args.args[0])

            with patch.object(
                recorder.writer, "write", side_effect=OSError("write failed")
            ), patch("src.danmaku.logger.warning") as warning:
                result = recorder.record_raw_message(
                    json.dumps(
                        {
                            "method": "WebcastChatMessage",
                            "user": {"nickname": "用户", "id": "1"},
                            "content": "正文",
                        },
                        ensure_ascii=False,
                    )
                )
            self.assertTrue(result.valid_business_message)
            self.assertFalse(result.room_not_found)
            self.assertIn("视频录制继续", warning.call_args.args[0])

    def test_array_item_write_failure_does_not_skip_following_item(self) -> None:
        recorder = DanmakuRecorder(
            room_identifier="123",
            service_url="ws://127.0.0.1:1088",
            jsonl_path="unused.danmaku.jsonl",
            started_at=1000.0,
        )
        written_contents: list[str] = []

        def write_item(message: ChatMessage, received_at=None) -> None:
            if message.content == "中间失败":
                raise OSError("write failed")
            written_contents.append(message.content)

        items = [
            {
                "method": "WebcastChatMessage",
                "user": {"nickname": f"用户{index}", "id": str(index)},
                "content": content,
            }
            for index, content in enumerate(
                ("第一条", "中间失败", "最后成功"), start=1
            )
        ]
        with patch.object(
            recorder.writer, "write", side_effect=write_item
        ) as write, patch("src.danmaku.logger.warning") as warning:
            result = recorder.record_raw_message(
                json.dumps(items, ensure_ascii=False), received_at=1001.0
            )

        self.assertEqual(write.call_count, 3)
        self.assertEqual(written_contents, ["第一条", "最后成功"])
        self.assertTrue(result.valid_business_message)
        self.assertFalse(result.room_not_found)
        warning.assert_called_once()

    def test_short_connections_keep_backing_off_to_existing_cap(self) -> None:
        recorder = DanmakuRecorder(
            room_identifier="123",
            service_url="ws://127.0.0.1:1088",
            jsonl_path="unused.danmaku.jsonl",
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
            jsonl_path="unused.danmaku.jsonl",
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
            jsonl_path="unused.danmaku.jsonl",
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

    def test_room_not_found_stops_retries_but_leaves_jsonl_open_until_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jsonl_path = Path(directory) / "not-found.danmaku.jsonl"
            recorder = DanmakuRecorder(
                room_identifier="missing-room",
                service_url="ws://127.0.0.1:1088",
                jsonl_path=jsonl_path,
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
            before_stop = jsonl_path.read_text(encoding="utf-8")
            self.assertEqual(len(before_stop.splitlines()), 1)
            self.assertEqual(json.loads(before_stop)["type"], "metadata")

            recorder.stop()

            self.assertIsNone(recorder.writer._file)
            self.assertEqual(jsonl_path.read_text(encoding="utf-8"), before_stop)

    def test_mock_websocket_uses_no_proxy_and_stop_closes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jsonl_path = Path(directory) / "mock.danmaku.jsonl"
            delivered = threading.Event()
            connection = _FakeConnection(_FakeWebSocket(delivered))
            recorder = DanmakuRecorder(
                room_identifier="room/with space",
                service_url="ws://127.0.0.1:1088",
                jsonl_path=jsonl_path,
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
            records = [
                json.loads(line)
                for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([record["type"] for record in records], ["metadata", "chat"])
            self.assertEqual(records[1]["user"]["nickname"], "mock 用户")
            self.assertIsNone(recorder.writer._file)

    def test_connection_failure_still_creates_metadata_only_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jsonl_path = Path(directory) / "empty.danmaku.jsonl"
            attempted = threading.Event()

            def fail_connect(*_args, **_kwargs):
                attempted.set()
                raise OSError("service unavailable")

            recorder = DanmakuRecorder(
                room_identifier="123",
                service_url="ws://127.0.0.1:1088",
                jsonl_path=jsonl_path,
                started_at=1000.0,
            )
            with patch("src.danmaku.websockets.connect", side_effect=fail_connect):
                recorder.start()
                self.assertTrue(attempted.wait(timeout=2))
                recorder.stop(timeout=2)

            records = [
                json.loads(line)
                for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["type"], "metadata")
            self.assertIsNone(recorder.writer._file)

    def test_popen_failure_does_not_create_or_start_danmaku(self) -> None:
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
        with tempfile.TemporaryDirectory() as directory, patch(
            "src.recorder.DanmakuRecorder"
        ) as recorder_class, patch(
            "src.recorder.subprocess.Popen", side_effect=OSError("ffmpeg unavailable")
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

        recorder_class.assert_not_called()

    def test_slow_start_bootstrap_does_not_block_ffmpeg_monitoring(self) -> None:
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
        start_entered = threading.Event()
        allow_start_to_finish = threading.Event()
        start_finished = threading.Event()
        monitoring_continued = threading.Event()
        mock_recorder = Mock()
        mock_recorder.room_identifier = "123"

        def slow_start() -> None:
            start_entered.set()
            self.assertTrue(allow_start_to_finish.wait(timeout=2))
            start_finished.set()

        def stop() -> None:
            allow_start_to_finish.set()

        def monitor(*_args, on_started=None, **_kwargs):
            self.assertIsNotNone(on_started)
            on_started(1_787_104_800.125, 456.75)
            self.assertTrue(start_entered.wait(timeout=1))
            self.assertFalse(start_finished.is_set())
            monitoring_continued.set()
            return "finished"

        mock_recorder.start.side_effect = slow_start
        mock_recorder.stop.side_effect = stop
        with tempfile.TemporaryDirectory() as directory, patch(
            "src.recorder.DanmakuRecorder", return_value=mock_recorder
        ) as recorder_class, patch(
            "src.recorder._check_subprocess", side_effect=monitor
        ):
            result = _record_once(
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

        self.assertEqual(result, "finished")
        self.assertTrue(monitoring_continued.is_set())
        self.assertTrue(start_finished.wait(timeout=1))
        mock_recorder.stop.assert_called_once_with()
        parameters = recorder_class.call_args.kwargs
        self.assertEqual(parameters["started_at"], 1_787_104_800.125)
        self.assertEqual(parameters["started_monotonic"], 456.75)
        self.assertNotIn("_%03d", str(parameters["jsonl_path"]))
        self.assertTrue(str(parameters["jsonl_path"]).endswith(".danmaku.jsonl"))

    def test_finished_ffmpeg_stops_recorder_before_delayed_bootstrap(self) -> None:
        settings = SimpleNamespace(
            filename_by_title=False,
            split_video_by_time=False,
            split_time="1800",
            proxy_addr=None,
            corrupt_probe_decode=False,
            record_danmaku=True,
            danmaku_service_url="ws://127.0.0.1:1088",
            converts_to_mp4=False,
        )
        delayed_recorders: list[DanmakuRecorder] = []

        def finish_immediately(*_args, on_started=None, **_kwargs):
            self.assertIsNotNone(on_started)
            on_started(1000.0, 500.0)
            return "finished"

        with tempfile.TemporaryDirectory() as directory, patch(
            "src.recorder._check_subprocess", side_effect=finish_immediately
        ), patch(
            "src.recorder._spawn_danmaku_bootstrap",
            side_effect=delayed_recorders.append,
        ):
            result = _record_once(
                real_url="https://example.invalid/live.flv",
                anchor_name="主播",
                live_title=None,
                full_path=directory,
                record_save_type="FLV",
                record_name="序号1 主播",
                record_url="https://live.douyin.com/123",
                settings=settings,
                danmaku_room_identifier="123",
            )

            self.assertEqual(result, "finished")
            self.assertEqual(len(delayed_recorders), 1)
            recorder = delayed_recorders[0]
            self.assertTrue(recorder._stop_event.is_set())
            self.assertFalse(recorder.jsonl_path.exists())

            # 模拟 daemon bootstrap 在线程调度严重延迟后才真正执行。
            recorder.start()
            self.assertFalse(recorder.jsonl_path.exists())
            self.assertIsNone(recorder.writer._file)
            with danmaku_module._registry_lock:
                self.assertNotIn(recorder, danmaku_module._active_recorders)

    def test_record_once_ignores_danmaku_stop_failure(self) -> None:
        settings = SimpleNamespace(
            filename_by_title=False,
            split_video_by_time=False,
            split_time="1800",
            proxy_addr=None,
            corrupt_probe_decode=False,
            record_danmaku=True,
            danmaku_service_url="ws://127.0.0.1:1088",
            converts_to_mp4=False,
        )
        mock_recorder = Mock()
        mock_recorder.stop.side_effect = OSError("close failed")

        def finish(*_args, on_started=None, **_kwargs):
            self.assertIsNotNone(on_started)
            on_started(1000.0, 500.0)
            return "finished"

        with tempfile.TemporaryDirectory() as directory, patch(
            "src.recorder.DanmakuRecorder", return_value=mock_recorder
        ), patch(
            "src.recorder._check_subprocess", side_effect=finish
        ), patch(
            "src.recorder._spawn_danmaku_bootstrap",
            side_effect=lambda recorder: recorder.start(),
        ), patch("src.recorder.logger.error") as error:
            result = _record_once(
                real_url="https://example.invalid/live.flv",
                anchor_name="主播",
                live_title=None,
                full_path=directory,
                record_save_type="FLV",
                record_name="序号1 主播",
                record_url="https://live.douyin.com/123",
                settings=settings,
                danmaku_room_identifier="123",
            )

        self.assertEqual(result, "finished")
        mock_recorder.start.assert_called_once_with()
        mock_recorder.stop.assert_called_once_with()
        self.assertIn("视频录制结果不受影响", error.call_args.args[0])


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
