# -*- encoding: utf-8 -*-

"""
Author: Hmily
GitHub: https://github.com/ihmily
Date: 2023-07-17 23:52:05
Update: 2025-10-23 19:48:05
Copyright (c) 2023-2025 by Hmily, All Rights Reserved.
Function: Record live stream video.

本文件仅做"装配"：把各子模块串起来，然后进入主循环。
业务逻辑放在 src/*.py 里。
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

from ffmpeg_install import check_ffmpeg, current_env_path, ffmpeg_path
from src import live_history, runtime, url_config, utils
from src.config_loader import Settings
from src.danmaku import stop_all_active_recorders
from src.file_ops import backup_file_loop
from src.logger import logger
from src.monitor import adjust_max_request_loop
from webui import server as webui_server

version = "v4.0.7"

_script_dir = os.path.split(os.path.realpath(sys.argv[0]))[0]
config_file = f"{_script_dir}/config/config.ini"
url_config_file = f"{_script_dir}/config/URL_config.ini"
live_history_file = f"{_script_dir}/config/live_history.json"
backup_dir = f"{_script_dir}/backup_config"
default_path = f"{_script_dir}/downloads"

os.makedirs(default_path, exist_ok=True)
os.environ["PATH"] = ffmpeg_path + os.pathsep + current_env_path


def _signal_handler(_signum, _frame):
    # 不在信号处理器里获取 runtime.state_lock：信号可能恰好中断持锁的主线程。
    # 先让所有录制 worker 停止/拒绝新工作，再关闭仍登记中的弹幕 JSONL。
    runtime.exit_recording = True
    try:
        stop_all_active_recorders()
    except Exception as exc:
        logger.error(f"进程关闭时停止弹幕录制失败，继续退出: {exc}")
    finally:
        sys.exit(0)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _check_ffmpeg_existence() -> bool:
    return check_ffmpeg()


def _print_banner() -> None:
    logger.info("========== DouyinLiveRecorder-UI ==========")
    logger.info(f"版本号: {version}")
    logger.info("GitHub: https://github.com/as2931543asd/DouyinLiveRecorder-UI")
    logger.info("支持平台: 抖音、小红书（当前直播间基础录制）")


def _enforce_disk_space(settings: Settings, first_run: bool) -> None:
    check_path = settings.video_save_path or default_path
    if utils.check_disk_capacity(check_path, show=first_run) < settings.disk_space_limit:
        runtime.exit_recording = True
        if not runtime.recording:
            logger.warning(
                f"Disk space remaining is below {settings.disk_space_limit} GB. "
                f"Exiting program due to the disk space limit being reached."
            )
            sys.exit(-1)


def _ensure_config_file_exists() -> None:
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    if not os.path.isfile(config_file):
        open(config_file, "w", encoding="utf-8-sig").close()


def _start_background_threads(settings: Settings) -> None:
    threading.Thread(
        target=backup_file_loop, args=(config_file, url_config_file, backup_dir), daemon=True
    ).start()
    threading.Thread(target=adjust_max_request_loop, daemon=True).start()


def _start_webui(settings: Settings) -> None:
    save_path = settings.video_save_path or default_path
    webui_server.init(url_config_file, disk_sample_path=save_path)
    host = os.environ.get("WEBUI_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBUI_PORT", "9527"))
    webui_server.start_server(host=host, port=port)
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    logger.info(f"WebUI 已启动: http://{display_host}:{port}")


def main() -> None:
    _print_banner()
    if not _check_ffmpeg_existence():
        logger.error("缺少ffmpeg无法进行录制，程序退出")
        sys.exit(1)

    _ensure_config_file_exists()
    url_config.deduplicate_file(url_config_file) if os.path.isfile(url_config_file) else None

    settings = Settings(config_file)
    live_history.init(live_history_file)
    configured_urls = url_config.get_configured_urls(
        url_config_file,
        settings.video_record_quality,
    )
    initialized_count = live_history.initialize_missing(configured_urls)
    if initialized_count:
        logger.info(f"已为 {initialized_count} 个无历史主播设置直播时间统计起点")
    runtime.max_request = settings.max_request
    runtime.semaphore = threading.Semaphore(settings.max_request)
    _start_background_threads(settings)
    _start_webui(settings)
    runtime.first_run = False

    first_iteration = True
    while True:
        settings.reload()
        ini_url_content = url_config.ensure_url_config_file(url_config_file)
        _enforce_disk_space(settings, first_iteration)

        url_config.load_and_dispatch(
            url_config_file=url_config_file,
            settings=settings,
            default_path=default_path,
            ini_url_content=ini_url_content,
        )

        first_iteration = False
        time.sleep(3)


if __name__ == "__main__":
    main()
