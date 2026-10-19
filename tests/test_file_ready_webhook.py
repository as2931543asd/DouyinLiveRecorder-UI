from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import recorder
from src.config_loader import Settings, text_encoding


class NotifyTests(unittest.TestCase):
    def serve(self, status=204):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/file", received

    def test_posts_absolute_path(self):
        url, received = self.serve()
        recorder.notify_file_ready("/srv/nas/Record/抖音直播/a/a_000.ts", SimpleNamespace(file_ready_webhook=url))
        self.assertEqual(received, [{"path": "/srv/nas/Record/抖音直播/a/a_000.ts"}])

    def test_empty_url_sends_nothing(self):
        with patch.object(recorder.urllib.request, "build_opener") as opener:
            recorder.notify_file_ready("/x.ts", SimpleNamespace(file_ready_webhook=""))
        opener.assert_not_called()

    def test_failure_retries_then_only_logs(self):
        url, received = self.serve(status=500)
        with patch.object(recorder.time, "sleep"), patch.object(recorder.logger, "warning") as warning:
            recorder.notify_file_ready("/x.ts", SimpleNamespace(file_ready_webhook=url))
        self.assertEqual(len(received), 3)
        warning.assert_called_once()


class ConvertTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ts = Path(temporary.name) / "a_000.ts"
        self.ts.write_bytes(b"video")

    def convert(self, delete_origin=False, error=None):
        settings = SimpleNamespace(converts_to_h264=False, delete_origin_file=delete_origin,
                                   file_ready_webhook="http://127.0.0.1:1/file")

        def ffmpeg(argv, **kwargs):
            Path(argv[-1]).write_bytes(b"mp4")  # 和真 ffmpeg 一样先写出部分输出
            if error:
                raise error

        with patch.object(recorder.subprocess, "check_output", side_effect=ffmpeg), \
                patch.object(recorder.time, "sleep"), \
                patch.object(recorder, "notify_file_ready") as notify:
            recorder.converts_mp4(str(self.ts), settings)
        return notify, settings

    def test_success_notifies_with_original_path(self):
        notify, settings = self.convert()
        notify.assert_called_once_with(str(self.ts), settings)

    def test_deleted_origin_or_failed_conversion_does_not_notify(self):
        notify, _ = self.convert(delete_origin=True)
        notify.assert_not_called()
        self.ts.write_bytes(b"video")
        notify, _ = self.convert(error=subprocess.CalledProcessError(1, "ffmpeg"))
        notify.assert_not_called()
        self.assertEqual(list(self.ts.parent.glob(".*.part")), [])


class TempRecTests(unittest.TestCase):
    """录制中文件在 平台/TempRec/主播/，成品移回 平台/主播/。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "抖音直播"
        self.recording = self.root / "TempRec" / "主播"
        self.recording.mkdir(parents=True)

    def test_published_path_drops_temp_directory(self):
        self.assertEqual(recorder.published_path(self.recording / "a.mp4"), self.root / "主播" / "a.mp4")
        self.assertEqual(recorder.published_path(self.root / "主播" / "a.mp4"), self.root / "主播" / "a.mp4")

    def test_mp4_is_written_hidden_then_moved_out_of_temp_directory(self):
        ts = self.recording / "a_000.ts"
        ts.write_bytes(b"video")
        outputs = []

        def ffmpeg(argv, **kwargs):
            outputs.append(Path(argv[-1]))
            Path(argv[-1]).write_bytes(b"mp4")

        settings = SimpleNamespace(converts_to_h264=False, delete_origin_file=False, file_ready_webhook="")
        with patch.object(recorder.subprocess, "check_output", side_effect=ffmpeg), \
                patch.object(recorder, "notify_file_ready") as notify:
            recorder.converts_mp4(str(ts), settings)
        self.assertTrue(outputs[0].name.startswith("."))
        self.assertEqual(outputs[0].parent, self.root / "主播")
        self.assertEqual(sorted(p.name for p in (self.root / "主播").iterdir()), ["a_000.mp4"])
        self.assertTrue(ts.exists())
        notify.assert_called_once_with(str(ts), settings)

    def test_sidecars_follow_the_recording_out(self):
        template = self.recording / "a_%03d.ts"
        (self.recording / "a.danmaku.jsonl").write_text("{}")
        (self.recording / "a.segments.csv").write_text("a_000.ts,0,1")
        recorder.publish_sidecars(str(template))
        self.assertEqual(sorted(p.name for p in (self.root / "主播").iterdir()),
                         ["a.danmaku.jsonl", "a.segments.csv"])
        self.assertEqual(list(self.recording.iterdir()), [])

    def test_build_full_path_inserts_temp_directory_after_platform(self):
        settings = SimpleNamespace(video_save_path=str(self.root.parent), folder_by_author=True,
                                   folder_by_time=False, folder_by_title=False, record_to_temp_dir=True)
        path = recorder._build_full_path("/unused", "抖音直播", "主播", None, settings)
        self.assertEqual(Path(path), self.recording)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config.ini"

    def load(self, value):
        self.path.write_text(f"[录制设置]\n转码完成回调地址 = {value}\n", encoding=text_encoding)
        return Settings(str(self.path))

    def test_url_is_optional_and_must_be_http(self):
        self.assertEqual(self.load("").file_ready_webhook, "")
        self.assertEqual(self.load("http://127.0.0.1:8791/file").file_ready_webhook, "http://127.0.0.1:8791/file")
        with self.assertRaisesRegex(ValueError, "转码完成回调地址"):
            self.load("file:///etc/passwd")

    def test_temp_directory_requires_mp4_conversion(self):
        base = "[录制设置]\n录制中文件放到TempRec子目录 = 是\n视频保存格式ts|mkv|flv|mp4 = ts\n"
        self.path.write_text(base + "录制完成后自动转为mp4格式 = 是\n", encoding=text_encoding)
        self.assertTrue(Settings(str(self.path)).record_to_temp_dir)
        for extra in ("录制完成后自动转为mp4格式 = 否\n",
                      "录制完成后自动转为mp4格式 = 是\n视频保存格式ts|mkv|flv|mp4 = mkv\n"):
            with self.subTest(extra=extra):
                self.path.write_text(base.replace("视频保存格式ts|mkv|flv|mp4 = ts\n", "") + extra
                                     + ("" if "mkv" in extra else "视频保存格式ts|mkv|flv|mp4 = ts\n"),
                                     encoding=text_encoding)
                with self.assertRaisesRegex(ValueError, "TempRec"):
                    Settings(str(self.path))


if __name__ == "__main__":
    unittest.main()
