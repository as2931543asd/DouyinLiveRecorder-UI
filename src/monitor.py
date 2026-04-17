# -*- encoding: utf-8 -*-
"""根据错误率动态收紧并发的后台线程。

终端状态面板已由 WebUI 取代，不再输出到 stdout。
"""

from __future__ import annotations

import time

from . import runtime
from .logger import logger


def adjust_max_request_loop() -> None:
    """根据 error_window 里累计的错误计数动态调整并发上限。

    `preset` 在函数启动那一刻冻结——作为"允许的最大值"，避免持续爬升。
    """
    preset = runtime.max_request

    while True:
        time.sleep(5)
        with runtime.max_request_lock:
            if runtime.error_window:
                error_rate = sum(runtime.error_window) / len(runtime.error_window)
            else:
                error_rate = 0

            if error_rate > runtime.error_threshold:
                runtime.max_request = max(1, runtime.max_request - 1)
            elif error_rate < runtime.error_threshold / 2 and runtime.max_request < preset:
                runtime.max_request += 1

            if runtime.pre_max_request != runtime.max_request:
                runtime.pre_max_request = runtime.max_request
                logger.info(f"并发上限动态调整为 {runtime.max_request}")

        runtime.error_window.append(runtime.error_count)
        if len(runtime.error_window) > runtime.error_window_size:
            runtime.error_window.pop(0)
        runtime.error_count = 0
