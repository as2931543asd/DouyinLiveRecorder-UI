# -*- encoding: utf-8 -*-
"""控制台状态刷新 & 根据错误率动态收紧并发。

两个独立 daemon 线程：
- `display_info_loop`: 每 5 秒打印一次录制状态
- `adjust_max_request_loop`: 每 5 秒评估一次错误率，调整 `runtime.max_request`
"""

from __future__ import annotations

import datetime
import os
import sys
import time
from pathlib import Path

from . import runtime
from .config_loader import Settings
from .logger import logger


def display_info_loop(settings: Settings) -> None:
    clear_command = "cls" if os.name == "nt" else "clear"
    time.sleep(5)
    while True:
        try:
            sys.stdout.flush()
            time.sleep(5)
            if Path(sys.executable).name != "pythonw.exe":
                os.system(clear_command)

            print(f"\r共监测{runtime.monitoring}个直播中", end=" | ")
            print(f"同一时间访问网络的线程数: {runtime.max_request}", end=" | ")
            print(f"是否开启代理录制: {'是' if settings.use_proxy else '否'}", end=" | ")
            print(f"录制视频质量为: {settings.video_record_quality}", end=" | ")
            print(f"录制视频格式为: {settings.video_save_type}", end=" | ")
            print(f"目前瞬时错误数为: {runtime.error_count}", end=" | ")
            now = time.strftime("%H:%M:%S", time.localtime())
            print(f"当前时间: {now}")

            if len(runtime.recording) == 0:
                time.sleep(5)
                if runtime.monitoring == 0:
                    print("\r没有正在监测和录制的直播")
                else:
                    print(f"\r没有正在录制的直播 循环监测间隔时间：{settings.delay_default}秒")
            else:
                now_time = datetime.datetime.now()
                print("x" * 60)
                no_repeat_recording = list(set(runtime.recording))
                print(f"正在录制{len(no_repeat_recording)}个直播: ")
                for recording_live in no_repeat_recording:
                    entry = runtime.recording_time_list.get(recording_live)
                    if not entry:
                        continue
                    rt, qa = entry
                    have_record_time = now_time - rt
                    print(f"{recording_live}[{qa}] 正在录制中 {str(have_record_time).split('.')[0]}")
                print("x" * 60)
                runtime.start_display_time = now_time
        except Exception as e:
            logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")


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
                print(f"\r同一时间访问网络的线程数动态改为 {runtime.max_request}")

        runtime.error_window.append(runtime.error_count)
        if len(runtime.error_window) > runtime.error_window_size:
            runtime.error_window.pop(0)
        runtime.error_count = 0
