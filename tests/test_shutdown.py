from __future__ import annotations

import json
import signal
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from src import danmaku as danmaku_module
from src.danmaku import DanmakuRecorder, stop_all_active_recorders


# main.py 在导入时会按 argv[0] 计算运行目录并登记信号 handler。测试固定
# argv 且 mock 登记动作，只直接调用 handler，不向测试进程发送真实信号。
_project_main = Path(__file__).resolve().parents[1] / "main.py"
with patch.object(sys, "argv", [str(_project_main)]), patch.object(
    signal, "signal"
) as register_signal:
    import main as app_main


class ProcessDanmakuShutdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self._shutdown_patch = patch.object(danmaku_module, "_shutdown_started", False)
        self._deadline_patch = patch.object(danmaku_module, "_shutdown_deadline", None)
        self._shutdown_patch.start()
        self._deadline_patch.start()
        with danmaku_module._registry_lock:
            self.assertFalse(danmaku_module._active_recorders)

    def tearDown(self) -> None:
        stop_all_active_recorders(timeout=0)
        self._deadline_patch.stop()
        self._shutdown_patch.stop()

    @staticmethod
    def _make_idle_recorder(jsonl_path: Path, room: str) -> DanmakuRecorder:
        recorder = DanmakuRecorder(
            room_identifier=room,
            service_url="ws://127.0.0.1:1088",
            jsonl_path=jsonl_path,
            started_at=1000.0,
        )
        recorder._thread_main = lambda: recorder._stop_event.wait(2)  # type: ignore[method-assign]
        return recorder

    def test_process_shutdown_closes_two_active_recorders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = [
                Path(directory) / "one.danmaku.jsonl",
                Path(directory) / "two.danmaku.jsonl",
            ]
            recorders = [
                self._make_idle_recorder(paths[0], "one"),
                self._make_idle_recorder(paths[1], "two"),
            ]
            shutdown_events: list[str] = []
            request_deadlines: list[float] = []
            wait_deadlines: list[float] = []

            def observe_request_stop(request_stop):
                def observed(deadline: float) -> None:
                    shutdown_events.append("cancel")
                    request_deadlines.append(deadline)
                    request_stop(deadline)

                return observed

            def observe_wait_and_close(wait_and_close):
                def observed(deadline: float) -> None:
                    shutdown_events.append("wait")
                    wait_deadlines.append(deadline)
                    wait_and_close(deadline)

                return observed

            for recorder in recorders:
                recorder.start()
                recorder._request_stop = Mock(  # type: ignore[method-assign]
                    side_effect=observe_request_stop(recorder._request_stop)
                )
                recorder._wait_and_close = Mock(  # type: ignore[method-assign]
                    side_effect=observe_wait_and_close(recorder._wait_and_close)
                )

            self.assertTrue(all(recorder._thread for recorder in recorders))
            self.assertTrue(all(recorder.writer._file is not None for recorder in recorders))

            before_shutdown = danmaku_module.time.monotonic()
            stop_all_active_recorders(timeout=1)
            after_shutdown = danmaku_module.time.monotonic()

            self.assertEqual(shutdown_events, ["cancel", "cancel", "wait", "wait"])
            self.assertEqual(request_deadlines, wait_deadlines)
            self.assertEqual(len(set(wait_deadlines)), 1)
            self.assertGreaterEqual(wait_deadlines[0], before_shutdown + 0.9)
            self.assertLessEqual(wait_deadlines[0], after_shutdown + 1.0)
            self.assertTrue(
                all(not recorder._thread.is_alive() for recorder in recorders if recorder._thread)
            )
            self.assertTrue(all(recorder.writer._file is None for recorder in recorders))
            self.assertEqual(
                [json.loads(path.read_text(encoding="utf-8"))["type"] for path in paths],
                ["metadata", "metadata"],
            )
            with danmaku_module._registry_lock:
                self.assertFalse(danmaku_module._active_recorders)

    def test_slow_open_does_not_block_shutdown_gate_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "racing.danmaku.jsonl"
            recorder = self._make_idle_recorder(first_path, "racing")
            writer_open = recorder.writer.open
            open_entered = threading.Event()
            allow_open_to_finish = threading.Event()
            stop_requested = threading.Event()
            shutdown_returned = threading.Event()
            network_target = Mock()
            recorder._thread_main = network_target  # type: ignore[method-assign]

            def slow_open() -> None:
                # 模拟 mkdir/open/metadata write 持有 writer 锁并长期阻塞。
                with recorder.writer._lock:
                    open_entered.set()
                    self.assertTrue(allow_open_to_finish.wait(timeout=2))
                    writer_open()

            original_request_stop = recorder._request_stop

            def observed_request_stop(deadline: float) -> None:
                stop_requested.set()
                original_request_stop(deadline)

            recorder.writer.open = Mock(side_effect=slow_open)
            recorder._request_stop = Mock(  # type: ignore[method-assign]
                side_effect=observed_request_stop
            )
            starter = threading.Thread(target=recorder.start)
            starter.start()
            self.assertTrue(open_entered.wait(timeout=2))

            def shutdown() -> None:
                stop_all_active_recorders(timeout=0.1)
                shutdown_returned.set()

            shutdown_thread = threading.Thread(target=shutdown)
            shutdown_thread.start()

            # open 仍被测试闸门卡住时，shutdown 已能建立全局闸门、发取消并按
            # deadline 返回；旧实现会卡在 start 持有的 _registry_lock 上。
            self.assertTrue(stop_requested.wait(timeout=0.5))
            with danmaku_module._registry_lock:
                self.assertTrue(danmaku_module._shutdown_started)
                self.assertIsNotNone(danmaku_module._shutdown_deadline)
            self.assertTrue(shutdown_returned.wait(timeout=1))
            self.assertTrue(starter.is_alive())

            blocked_path = Path(directory) / "after-shutdown.danmaku.jsonl"
            blocked = self._make_idle_recorder(blocked_path, "blocked")
            blocked.start()
            self.assertIsNone(blocked._thread)
            self.assertFalse(blocked_path.exists())

            allow_open_to_finish.set()
            starter.join(timeout=2)
            shutdown_thread.join(timeout=2)

            self.assertFalse(starter.is_alive())
            self.assertFalse(shutdown_thread.is_alive())
            network_target.assert_not_called()
            self.assertIsNone(recorder._thread)
            self.assertIsNone(recorder.writer._file)
            self.assertEqual(
                json.loads(first_path.read_text(encoding="utf-8"))["type"],
                "metadata",
            )
            with danmaku_module._registry_lock:
                self.assertFalse(danmaku_module._active_recorders)

    def test_shutdown_deadline_bounds_live_thread_and_busy_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            live_release = threading.Event()
            live_lock_held = threading.Event()
            locked_release = threading.Event()
            locked_lock_held = threading.Event()
            paths = [
                Path(directory) / "live.danmaku.jsonl",
                Path(directory) / "locked.danmaku.jsonl",
            ]

            live = DanmakuRecorder(
                "live", "ws://127.0.0.1:1088", paths[0], started_at=1000.0
            )

            def slow_jsonl_write_then_self_close() -> None:
                with live.writer._lock:
                    live_lock_held.set()
                    self.assertTrue(live_release.wait(timeout=2))
                live._close_writer()

            live._thread_main = slow_jsonl_write_then_self_close  # type: ignore[method-assign]
            live.start()
            self.assertTrue(live_lock_held.wait(timeout=2))

            locked = DanmakuRecorder(
                "locked", "ws://127.0.0.1:1088", paths[1], started_at=1000.0
            )
            locked._thread_main = lambda: None  # type: ignore[method-assign]
            locked.start()
            self.assertIsNotNone(locked._thread)
            locked._thread.join(timeout=2)
            self.assertFalse(locked._thread.is_alive())

            def hold_closed_thread_writer_lock() -> None:
                with locked.writer._lock:
                    locked_lock_held.set()
                    self.assertTrue(locked_release.wait(timeout=2))

            lock_holder = threading.Thread(target=hold_closed_thread_writer_lock)
            lock_holder.start()
            self.assertTrue(locked_lock_held.wait(timeout=2))

            wait_deadlines: list[float] = []

            def observe_wait(recorder: DanmakuRecorder):
                original_wait = recorder._wait_and_close

                def observed(deadline: float) -> None:
                    wait_deadlines.append(deadline)
                    original_wait(deadline)

                return observed

            live._wait_and_close = Mock(  # type: ignore[method-assign]
                side_effect=observe_wait(live)
            )
            locked._wait_and_close = Mock(  # type: ignore[method-assign]
                side_effect=observe_wait(locked)
            )

            started = danmaku_module.time.monotonic()
            try:
                with patch.object(danmaku_module.logger, "warning") as warning:
                    stop_all_active_recorders(timeout=0.15)
                elapsed = danmaku_module.time.monotonic() - started

                self.assertLess(elapsed, 0.5)
                self.assertEqual(len(wait_deadlines), 2)
                self.assertEqual(len(set(wait_deadlines)), 1)
                warnings = "\n".join(str(item) for item in warning.call_args_list)
                self.assertIn("未强制抢占 JSONL 写锁", warnings)
                self.assertIn("JSONL 写锁未能在关闭期限内取得", warnings)
                self.assertTrue(all(recorder.writer._file is not None for recorder in (live, locked)))
                self.assertEqual(
                    [json.loads(path.read_text(encoding="utf-8"))["type"] for path in paths],
                    ["metadata", "metadata"],
                )
            finally:
                live_release.set()
                locked_release.set()
                if live._thread is not None:
                    live._thread.join(timeout=2)
                lock_holder.join(timeout=2)
                locked.writer.close()

            self.assertFalse(live._thread and live._thread.is_alive())
            self.assertFalse(lock_holder.is_alive())
            self.assertTrue(all(recorder.writer._file is None for recorder in (live, locked)))
            self.assertEqual(
                [json.loads(path.read_text(encoding="utf-8"))["type"] for path in paths],
                ["metadata", "metadata"],
            )


class MainSignalHandlerTests(unittest.TestCase):
    def test_sigint_and_sigterm_register_the_same_handler(self) -> None:
        self.assertEqual(
            register_signal.call_args_list,
            [
                call(signal.SIGINT, app_main._signal_handler),
                call(signal.SIGTERM, app_main._signal_handler),
            ],
        )

    def test_signals_set_exit_flag_then_shut_down_and_exit_zero(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                ordered = Mock()

                def assert_flag_was_set() -> None:
                    self.assertTrue(app_main.runtime.exit_recording)
                    raise RuntimeError("mock shutdown failure")

                with patch.object(app_main.runtime, "exit_recording", False), patch.object(
                    app_main, "stop_all_active_recorders", side_effect=assert_flag_was_set
                ) as shutdown, patch.object(
                    app_main.sys, "exit", side_effect=SystemExit(0)
                ) as exit_process, patch.object(app_main.logger, "error") as log_error:
                    ordered.attach_mock(shutdown, "shutdown")
                    ordered.attach_mock(exit_process, "exit")

                    with self.assertRaisesRegex(SystemExit, "0"):
                        app_main._signal_handler(signum, None)

                self.assertEqual(ordered.mock_calls, [call.shutdown(), call.exit(0)])
                log_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
