# -*- encoding: utf-8 -*-
"""进程内共享的可变状态与并发原语。

所有后台线程（录制、监控、WebUI）共用一份状态。为了避免模块间循环依赖，
把这些"全局变量"聚集在此模块；其它模块统一 `from src import runtime` 再
以 `runtime.xxx` 形式读写，便于定位与锁定策略审计。
"""

from __future__ import annotations

import asyncio
import datetime
import threading
from typing import Any

# --- 录制相关 --------------------------------------------------------------
recording: set[str] = set()
recording_time_list: dict[str, list[Any]] = {}
running_list: list[str] = []
url_comments: list[str] = []
url_tuples_list: list[tuple] = []
text_no_repeat_url: list[tuple] = []
# worker 抓到的新昵称：以 URL 为键覆盖旧值，主循环每 3 秒批量回写配置。
pending_name_updates: dict[str, str] = {}
# 当前抖音返回的主播名，供 WebUI 在配置回写前立即展示。
current_anchor_names: dict[str, str] = {}
# 最近一次成功轮询仍标记为直播、但 ffmpeg 暂未录制的 URL（通常正在冷却重连）。
live_urls: set[str] = set()
not_record_list: list[str] = []

# --- 统计 / 错误反馈 -------------------------------------------------------
monitoring: int = 0
error_count: int = 0
error_window: list[int] = []
error_window_size: int = 10
error_threshold: int = 5

# --- 控制开关 --------------------------------------------------------------
exit_recording: bool = False
first_start: bool = True
first_run: bool = True

# --- 时间戳 ----------------------------------------------------------------
start_display_time: datetime.datetime = datetime.datetime.now()

# --- 锁 --------------------------------------------------------------------
state_lock = threading.Lock()
file_update_lock = threading.Lock()
max_request_lock = threading.Lock()

# --- 并发节流 --------------------------------------------------------------
semaphore: threading.Semaphore | None = None  # 由 config 载入后初始化
max_request: int = 3
pre_max_request: int = 10

# --- 后台事件循环（async HTTP 由此线程执行）-------------------------------
_bg_loop = asyncio.new_event_loop()
threading.Thread(target=_bg_loop.run_forever, daemon=True).start()


def run_coro(coro):
    """在后台事件循环中同步执行一个协程。

    当前所有 worker 线程共用一个常驻 event loop，通过 run_coroutine_threadsafe
    桥接——任意线程都可直接调用 spider/stream 的 async 接口。
    """
    return asyncio.run_coroutine_threadsafe(coro, _bg_loop).result()
