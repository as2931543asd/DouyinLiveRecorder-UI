from __future__ import annotations

import asyncio
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import live_history, runtime
from src.recorder import _corrupt_retry_delay, _retry_delay
from src.url_config import _apply_name_updates, get_configured_urls
from webui import server


class LiveHistoryTests(unittest.TestCase):
    def test_completed_live_is_persisted_and_reloaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "live_history.json"
            started = datetime.datetime(2026, 7, 24, 8, 0, tzinfo=datetime.timezone.utc)
            ended = datetime.datetime(2026, 7, 24, 9, 30, tzinfo=datetime.timezone.utc)

            live_history.init(history_file)
            live_history.record_completed("https://live.douyin.com/123", "新昵称", started, ended)

            raw = json.loads(history_file.read_text(encoding="utf-8"))
            self.assertEqual(raw["https://live.douyin.com/123"]["anchor_name"], "新昵称")
            self.assertEqual(raw["https://live.douyin.com/123"]["last_duration_seconds"], 5400)

            live_history.init(history_file)
            snapshot = live_history.get_snapshot()
            self.assertEqual(
                snapshot["https://live.douyin.com/123"]["last_ended_at"],
                "2026-07-24T09:30:00+00:00",
            )

    def test_initializes_only_missing_urls_at_current_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "live_history.json"
            existing_url = "https://live.douyin.com/existing"
            missing_url = "https://live.douyin.com/missing"
            ended = datetime.datetime(2026, 7, 23, 9, 30, tzinfo=datetime.timezone.utc)
            baseline = datetime.datetime(2026, 7, 24, 12, 0, tzinfo=datetime.timezone.utc)
            live_history.init(history_file)
            live_history.record_completed(
                existing_url,
                "已有历史",
                ended - datetime.timedelta(hours=1),
                ended,
            )

            added = live_history.initialize_missing(
                [existing_url, missing_url, missing_url],
                baseline,
            )
            snapshot = live_history.get_snapshot()

            self.assertEqual(added, 1)
            self.assertEqual(snapshot[existing_url]["last_ended_at"], ended.isoformat())
            self.assertFalse(snapshot[existing_url]["is_baseline"])
            self.assertEqual(snapshot[missing_url]["last_ended_at"], baseline.isoformat())
            self.assertTrue(snapshot[missing_url]["is_baseline"])
            self.assertIsNone(snapshot[missing_url]["last_duration_seconds"])


class ReconnectBackoffTests(unittest.TestCase):
    def test_disconnect_backoff_is_bounded(self) -> None:
        delays = [
            _retry_delay(15, attempt, 120, jitter_seconds=0)
            for attempt in range(7)
        ]
        self.assertEqual(delays, [15, 30, 60, 120, 120, 120, 120])

    def test_retry_jitter_never_exceeds_nominal_delay(self) -> None:
        with patch("src.recorder.random.randint", return_value=11):
            self.assertEqual(_retry_delay(15, 0, 300), 11)

    def test_corruption_uses_independent_backoff(self) -> None:
        delays = [_corrupt_retry_delay(3, attempt) for attempt in range(5)]
        self.assertEqual(delays, [3, 10, 30, 120, 120])


class AnchorNameSyncTests(unittest.TestCase):
    def test_updates_active_and_paused_lines_by_exact_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "URL_config.ini"
            config_file.write_text(
                "原画,https://live.douyin.com/123,主播: 旧昵称\n"
                "#高清,https://live.douyin.com/456,主播: 暂停旧昵称\n"
                "原画,https://live.douyin.com/1234,主播: 不应误改\n",
                encoding="utf-8-sig",
            )

            changed = _apply_name_updates(
                str(config_file),
                {
                    "https://live.douyin.com/123": "新昵称",
                    "https://live.douyin.com/456": "暂停新昵称",
                },
                "原画",
            )

            content = config_file.read_text(encoding="utf-8-sig")
            self.assertEqual(changed, 2)
            self.assertIn("原画,https://live.douyin.com/123,主播: 新昵称\n", content)
            self.assertIn("#高清,https://live.douyin.com/456,主播: 暂停新昵称\n", content)
            self.assertIn("原画,https://live.douyin.com/1234,主播: 不应误改\n", content)

    def test_reads_active_and_paused_urls_for_history_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "URL_config.ini"
            config_file.write_text(
                "原画,https://live.douyin.com/123?foo=bar,主播: 主播一\n"
                "#高清,https://live.douyin.com/456,主播: 主播二\n"
                "原画,https://example.com/not-douyin,主播: 跳过\n",
                encoding="utf-8-sig",
            )

            urls = get_configured_urls(str(config_file), "原画")

            self.assertEqual(
                urls,
                [
                    "https://live.douyin.com/123",
                    "https://live.douyin.com/456",
                ],
            )


class StreamerApiTests(unittest.TestCase):
    def tearDown(self) -> None:
        with runtime.state_lock:
            runtime.current_anchor_names.clear()
            runtime.live_urls.clear()
            runtime.recording.clear()
            runtime.recording_time_list.clear()
            runtime.running_list.clear()

    def test_exposes_elapsed_history_and_current_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "URL_config.ini"
            history_file = Path(directory) / "live_history.json"
            url = "https://live.douyin.com/123"
            config_file.write_text(
                f"原画,{url},主播: 旧昵称\n",
                encoding="utf-8-sig",
            )
            live_history.init(history_file)
            ended = datetime.datetime.now().astimezone() - datetime.timedelta(hours=2)
            live_history.record_completed(
                url,
                "新昵称",
                ended - datetime.timedelta(hours=1),
                ended,
            )
            server.init(str(config_file))
            with runtime.state_lock:
                runtime.current_anchor_names[url] = "新昵称"

            streamer = server._parse_url_config()[0]

            self.assertEqual(streamer["name"], "新昵称")
            self.assertGreaterEqual(streamer["last_live_elapsed_seconds"], 7199)
            self.assertEqual(streamer["last_live_duration_seconds"], 3600)
            self.assertFalse(streamer["last_live_is_baseline"])

    def test_live_retry_hides_stale_offline_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "URL_config.ini"
            url = "https://live.douyin.com/456"
            config_file.write_text(f"原画,{url},主播: 昵称\n", encoding="utf-8-sig")
            live_history.init(Path(directory) / "history.json")
            server.init(str(config_file))
            with runtime.state_lock:
                runtime.running_list.append(url)
                runtime.live_urls.add(url)

            streamer = server._parse_url_config()[0]

            self.assertEqual(streamer["state"], "重连中")
            self.assertTrue(streamer["is_live"])

    def test_add_streamer_initializes_history_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "URL_config.ini"
            history_file = Path(directory) / "live_history.json"
            config_file.write_text("", encoding="utf-8-sig")
            live_history.init(history_file)
            server.init(str(config_file))

            with patch.object(server, "_parse_url_config", return_value=[]):
                result = asyncio.run(
                    server.add_streamer(
                        server.StreamerAdd(
                            url="https://live.douyin.com/789?foo=bar",
                            quality="原画",
                        )
                    )
                )

            snapshot = live_history.get_snapshot()
            self.assertTrue(result["ok"])
            self.assertIn("https://live.douyin.com/789", snapshot)
            self.assertTrue(snapshot["https://live.douyin.com/789"]["is_baseline"])


if __name__ == "__main__":
    unittest.main()
