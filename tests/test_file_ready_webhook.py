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
        with patch.object(recorder.subprocess, "check_output", side_effect=error), \
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


if __name__ == "__main__":
    unittest.main()
