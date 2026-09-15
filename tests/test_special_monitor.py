from __future__ import annotations

import datetime
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import live_history, recorder, runtime, special_monitor
from src.config_loader import Settings
from webui import server

URL = "https://live.douyin.com/123"
OTHER = "https://live.douyin.com/1234"


class SpecialMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.urls = self.root / "URL_config.ini"
        self.urls.write_text(f"原画,{URL},主播: 测试\n原画,{OTHER},另一个\n", encoding="utf-8-sig")
        live_history.init(self.root / "history.json")
        server.init(str(self.urls))
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)
        with runtime.state_lock:
            runtime.url_comments.clear()
            runtime.live_urls.clear()
            runtime.recording_time_list.clear()
            runtime.current_anchor_names.clear()
            runtime.pending_name_updates.clear()
        self.addCleanup(self.clear_runtime)

    def clear_runtime(self):
        with runtime.state_lock:
            runtime.url_comments.clear()
            runtime.live_urls.clear()
            runtime.recording_time_list.clear()
            runtime.current_anchor_names.clear()
            runtime.pending_name_updates.clear()

    def enable(self, enabled=True):
        response = self.client.patch("/api/streamers/special-monitor", json={
            "url": URL + "/?from=test", "enabled": enabled,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def events(self, consumer="fake-mail"):
        result = self.client.get("/api/notifications/events", params={"consumer": consumer})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["version"], 1)
        return result.json()["events"]

    def ack(self, event, consumer="fake-mail"):
        return self.client.post("/api/notifications/ack", json={
            "event_id": event["id"], "consumer": consumer,
        })

    def test_live_transition_restart_and_independent_generic_consumers(self):
        special_monitor.observe(URL, True, "未订阅")
        self.assertEqual(self.events(), [])
        self.enable()
        special_monitor.observe(URL, False, "测试")
        self.assertEqual(self.events(), [])
        special_monitor.observe(URL, True, "测试", "标题")
        event = self.events()[0]
        self.assertEqual(event["type"], "live.started")
        self.assertEqual(event["data"], {"url": URL, "name": "测试", "title": "标题"})
        self.assertNotIn("acked_by", event)
        special_monitor.observe(URL, True, "重连改名")
        self.assertEqual(self.events(), [event])
        self.ack(event)
        special_monitor.init(self.root / "special_monitor.json")
        special_monitor.observe(URL, True, "重启后仍在直播")
        self.assertEqual(self.events(), [])
        self.assertEqual(self.events("fake-webhook"), [event])
        special_monitor.observe(URL, False, "测试")
        special_monitor.observe(URL, True, "再次开播")
        newer = self.events()[0]
        self.assertNotEqual(event["id"], newer["id"])
        self.ack(event)  # late ACK of the old session
        self.assertEqual(self.events(), [newer])

    def test_enable_during_recording_immediate_and_idempotent(self):
        with runtime.state_lock:
            runtime.recording_time_list["测试"] = [datetime.datetime.now(), "原画", URL]
        self.enable()
        first = self.events()
        self.assertEqual(len(first), 1)
        self.enable()
        self.assertEqual(self.events(), first)
        items = self.client.get("/api/streamers").json()
        self.assertTrue(items[0]["special_monitor"])
        self.assertFalse(items[1]["special_monitor"])

    def test_pause_resume_disable_and_delete_cancel_pending(self):
        self.enable()
        special_monitor.observe(URL, True, "测试")
        self.assertEqual(len(self.events()), 1)
        self.client.patch("/api/streamers/toggle", json={"url": URL})
        self.assertEqual(self.events(), [])
        self.client.patch("/api/streamers/toggle", json={"url": URL})
        self.assertEqual(self.events(), [])
        self.assertIn(URL, special_monitor.watched_urls())
        self.enable(False)
        self.assertNotIn(URL, special_monitor.watched_urls())
        self.enable()
        special_monitor.observe(URL, True, "测试")
        self.client.request("DELETE", "/api/streamers", json={"url": URL})
        self.assertEqual(self.events(), [])
        self.assertNotIn(URL, special_monitor.watched_urls())

    def test_manual_pause_removal_and_expiry_are_filtered(self):
        self.enable()
        special_monitor.observe(URL, True, "测试")
        self.urls.write_text(f"#原画,{URL},测试\n", encoding="utf-8-sig")
        self.assertEqual(self.events(), [])
        self.urls.write_text("", encoding="utf-8-sig")
        self.assertEqual(self.events(), [])
        self.urls.write_text(f"原画,{URL},测试\n", encoding="utf-8-sig")
        old = datetime.datetime.now().astimezone() - datetime.timedelta(hours=2)
        with patch.dict(special_monitor._entries[URL]["pending"], occurred_at=old.isoformat()):
            self.assertEqual(self.events(), [])

    def test_validation_and_atomic_failure(self):
        for payload in ({"url": URL}, {"url": URL, "enabled": "yes"}):
            self.assertEqual(self.client.patch("/api/streamers/special-monitor", json=payload).status_code, 422)
        self.assertEqual(self.client.patch("/api/streamers/special-monitor", json={
            "url": "https://live.douyin.com/missing", "enabled": True,
        }).status_code, 404)
        self.assertEqual(self.client.get("/api/notifications/events").status_code, 422)
        self.assertEqual(self.client.get("/api/notifications/events?consumer=../bad").status_code, 422)
        with patch.object(special_monitor, "atomic_write_text", side_effect=OSError("disk full")):
            response = self.client.patch("/api/streamers/special-monitor", json={"url": URL, "enabled": True})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(URL, special_monitor.watched_urls())
        self.enable()
        with patch.object(special_monitor, "atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                special_monitor.observe(URL, True, "测试")
        self.assertEqual(self.events(), [])
        special_monitor.observe(URL, True, "测试")
        self.assertEqual(len(self.events()), 1)

    def test_concurrent_consumers_ack_independently_and_failure_preserves_event(self):
        from concurrent.futures import ThreadPoolExecutor
        self.enable()
        special_monitor.observe(URL, True, "测试")
        event = self.events()[0]
        with patch.object(special_monitor, "atomic_write_text", side_effect=OSError("disk full")):
            self.assertEqual(self.ack(event).status_code, 503)
        self.assertEqual(self.events(), [event])
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda consumer: special_monitor.acknowledge(event["id"], consumer),
                              ["fake-mail", "fake-hook", "fake-mail", "fake-hook"]))
        special_monitor.init(self.root / "special_monitor.json")
        self.assertEqual(self.events("fake-mail"), [])
        self.assertEqual(self.events("fake-hook"), [])
        self.assertEqual(self.events("third-adapter"), [event])

    def test_worker_ignores_fetch_errors_and_reconnect_does_not_repeat(self):
        self.enable()
        settings = Settings(str(self.root / "config.ini"))
        observed_ids = []
        def wait_or_stop(*_):
            observed_ids.append([event["id"] for event in self.events()])
            return len(observed_ids) == 6
        def info(live):
            return {"anchor_name": "测试", "is_live": live}, {}
        with patch.object(runtime, "semaphore", threading.Semaphore(1)), \
             patch.object(runtime, "exit_recording", False), \
             patch.object(runtime, "error_count", 0), \
             patch.object(runtime, "error_window", []), \
             patch.object(recorder, "_fetch_stream_info", side_effect=[
                 info(False), info(True), ({}, {}), info(True), info(False), info(True),
             ]), patch.object(recorder, "_sleep_or_stop", side_effect=wait_or_stop):
            recorder.start_record(("原画", URL, "测试"), settings, str(self.root), 1)
        self.assertEqual(observed_ids[0], [])
        self.assertEqual(observed_ids[1], observed_ids[2])
        self.assertEqual(observed_ids[1], observed_ids[3])
        self.assertNotEqual(observed_ids[1], observed_ids[5])


if __name__ == "__main__":
    unittest.main()
