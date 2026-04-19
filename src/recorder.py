# -*- encoding: utf-8 -*-
"""单个直播间的录制 worker：解析源地址 → 启动 ffmpeg → 循环值守。

`start_record` 会作为一个常驻 daemon 线程运行在每个 URL 上，内部是两层
`while True`：外层兜底重启，内层按 `delay_default` 轮询开播状态。
"""

from __future__ import annotations

import datetime
import glob
import os
import queue
import random
import re
import signal
import subprocess
import threading
import time
from collections import deque
from typing import TextIO

from . import runtime, spider, stream, utils
from .config_loader import Settings
from .logger import logger

# 文件名里不允许出现的字符
_RSTR = r"[\/\\\:\*\？?\"\<\>\|&#.。,， ~！· ]"

_QUALITY_MAPPING = {
    "原画": "OD",
    "蓝光": "BD",
    "超清": "UHD",
    "高清": "HD",
    "标清": "SD",
    "流畅": "LD",
}

_CORRUPT_PATTERNS = (
    "packet corrupt",
    "corrupt input packet",
    "pes packet size mismatch",
    "continuity check failed",
    "error while decoding",
    "non-existing pps",
    "missing picture in access unit",
    "concealing",
    "invalid nal unit",
    "decode_slice_header error",
)


# ---------- 命名 / 画质 ------------------------------------------------------

def clean_name(text: str, clean_emoji: bool) -> str:
    cleaned = re.sub(_RSTR, "_", text.strip()).strip("_")
    cleaned = cleaned.replace("（", "(").replace("）", ")")
    if clean_emoji:
        cleaned = utils.remove_emojis(cleaned, "_").strip("_")
    return cleaned or "空白昵称"


def get_quality_code(quality_zh: str) -> str | None:
    return _QUALITY_MAPPING.get(quality_zh)


# ---------- 子进程工具 -------------------------------------------------------

def _startup_info() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return info


def converts_mp4(converts_file_path: str, settings: Settings) -> None:
    try:
        if not (os.path.exists(converts_file_path) and os.path.getsize(converts_file_path) > 0):
            return

        if settings.converts_to_h264:
            logger.info("正在转码为 MP4 格式并重新编码为 h264")
            ffmpeg_command = [
                "ffmpeg", "-i", converts_file_path,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-vf", "format=yuv420p",
                "-c:a", "copy",
                "-f", "mp4", converts_file_path.rsplit(".", maxsplit=1)[0] + ".mp4",
            ]
        else:
            logger.info("正在转码为 MP4 格式")
            ffmpeg_command = [
                "ffmpeg", "-i", converts_file_path,
                "-c:v", "copy",
                "-c:a", "copy",
                "-f", "mp4", converts_file_path.rsplit(".", maxsplit=1)[0] + ".mp4",
            ]
        subprocess.check_output(ffmpeg_command, stderr=subprocess.STDOUT, startupinfo=_startup_info())

        if settings.delete_origin_file:
            time.sleep(1)
            if os.path.exists(converts_file_path):
                os.remove(converts_file_path)
    except subprocess.CalledProcessError as e:
        logger.error(f"Error occurred during conversion: {e}")
    except Exception as e:
        logger.error(f"An unknown error occurred: {e}")


def _iter_segment_files(segment_template: str) -> list[str]:
    """把 `_%03d.<ext>` 模板还原成已产出的分段文件列表。"""
    pattern = segment_template.replace("_%03d", "_*")
    return sorted(glob.glob(pattern))


def converts_segments_mp4(segment_template: str, settings: Settings) -> None:
    """分段录制收尾：逐个分段转码为 MP4（单线程串行，避免同时起多个 ffmpeg）。"""
    for seg in _iter_segment_files(segment_template):
        if seg.lower().endswith(".mp4"):
            continue
        converts_mp4(seg, settings)


def _spawn_convert(save_file_path: str, segmented: bool, settings: Settings) -> None:
    target = converts_segments_mp4 if segmented else converts_mp4
    threading.Thread(target=target, args=(save_file_path, settings), daemon=True).start()


def clear_record_info(record_name: str, record_url: str) -> None:
    removed = False
    with runtime.state_lock:
        runtime.recording.discard(record_name)
        runtime.recording_time_list.pop(record_name, None)
        if record_url in runtime.url_comments and record_url in runtime.running_list:
            runtime.running_list.remove(record_url)
            runtime.monitoring -= 1
            removed = True
    if removed:
        logger.info(f"[{record_name}] 已从录制列表中移除")


def _stream_reader(pipe: TextIO | None, output_queue: queue.Queue[str]) -> None:
    if pipe is None:
        return
    try:
        for raw_line in iter(pipe.readline, ""):
            if not raw_line:
                break
            output_queue.put(raw_line.rstrip())
    finally:
        pipe.close()


def _should_restart_for_corruption(
    line: str,
    error_window: deque[float],
    settings: Settings,
) -> bool:
    if not settings.auto_restart_on_corrupt:
        return False

    lowered = line.lower()
    if not any(pattern in lowered for pattern in _CORRUPT_PATTERNS):
        return False

    now = time.time()
    error_window.append(now)
    threshold = max(1, settings.corrupt_error_threshold)
    window_seconds = max(1, settings.corrupt_error_window_seconds)
    while error_window and now - error_window[0] > window_seconds:
        error_window.popleft()
    return len(error_window) >= threshold


def _stop_ffmpeg_process(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        if process.stdin:
            process.stdin.write("q")
            process.stdin.close()
    else:
        process.send_signal(signal.SIGINT)

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg 在结束信号后未及时退出，已强制终止")
        process.kill()
        process.wait()


def _check_subprocess(
    record_name: str,
    record_url: str,
    ffmpeg_command: list[str],
    save_type: str,
    settings: Settings,
) -> str:
    """启动 ffmpeg 并阻塞监视。

    Returns:
        stop: 因注释或程序退出而主动关停（调用方应 return）
        restart: 录制中检测到坏流或异常退出，需要快速重试
        finished: 录制正常结束
    """
    save_file_path = ffmpeg_command[-1]
    process = subprocess.Popen(
        ffmpeg_command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        startupinfo=_startup_info(),
    )
    output_queue: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=_stream_reader, args=(process.stdout, output_queue), daemon=True)
    reader.start()
    corrupt_window: deque[float] = deque()
    forced_restart = False

    while process.poll() is None:
        if record_url in runtime.url_comments or runtime.exit_recording:
            logger.warning(f"[{record_name}] 录制时已被注释，本条线程将会退出")
            clear_record_info(record_name, record_url)
            _stop_ffmpeg_process(process)
            return "stop"

        try:
            line = output_queue.get(timeout=1)
        except queue.Empty:
            continue

        if _should_restart_for_corruption(line, corrupt_window, settings):
            logger.warning(
                f"[{record_name}] 在 {settings.corrupt_error_window_seconds} 秒内累计检测到 "
                f"{len(corrupt_window)} 条疑似坏流日志，主动中断后重录"
            )
            forced_restart = True
            _stop_ffmpeg_process(process)
            break

    return_code = process.returncode
    stop_time = time.strftime("%Y-%m-%d %H:%M:%S")
    if forced_restart:
        logger.warning(f"{record_name} {stop_time} 已终止当前录制，准备重新拉流")
        with runtime.state_lock:
            runtime.recording.discard(record_name)
            runtime.recording_time_list.pop(record_name, None)
        return "restart"

    if return_code == 0:
        if settings.converts_to_mp4 and save_type == "TS":
            _spawn_convert(save_file_path, settings.split_video_by_time, settings)
        logger.info(f"{record_name} {stop_time} 直播录制完成")
        result = "finished"
    else:
        logger.error(f"{record_name} {stop_time} 直播录制出错，返回码: {return_code}")
        result = "restart"

    with runtime.state_lock:
        runtime.recording.discard(record_name)
        runtime.recording_time_list.pop(record_name, None)
    return result


# ---------- ffmpeg 组装 ------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile Safari/537.36"
)


def _ffmpeg_prologue(real_url: str, proxy_addr: str | None) -> list[str]:
    cmd = [
        "ffmpeg", "-y",
        "-v", "verbose",
        "-rw_timeout", "15000000",
        "-loglevel", "error",
        "-hide_banner",
        "-user_agent", _USER_AGENT,
        "-protocol_whitelist", "rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy",
        "-thread_queue_size", "1024",
        "-analyzeduration", "20000000",
        "-probesize", "10000000",
        "-fflags", "+discardcorrupt",
        "-re", "-i", real_url,
        "-bufsize", "8000k",
        "-sn", "-dn",
        "-reconnect_delay_max", "60",
        "-reconnect_streamed", "-reconnect_at_eof",
        "-max_muxing_queue_size", "1024",
        "-correct_ts_overflow", "1",
        "-avoid_negative_ts", "1",
    ]
    if proxy_addr:
        cmd.insert(1, "-http_proxy")
        cmd.insert(2, proxy_addr)
    return cmd


_FORMAT_ARGS = {
    "FLV": ["-map", "0", "-c:v", "copy", "-c:a", "copy", "-bsf:a", "aac_adtstoasc", "-f", "flv"],
    "MKV": ["-flags", "global_header", "-map", "0", "-c:v", "copy", "-c:a", "copy", "-f", "matroska"],
    "MP4": ["-map", "0", "-c:v", "copy", "-c:a", "copy", "-f", "mp4"],
    "TS":  ["-c:v", "copy", "-c:a", "copy", "-map", "0", "-f", "mpegts"],
}

_FORMAT_EXT = {"FLV": ".flv", "MKV": ".mkv", "MP4": ".mp4", "TS": ".ts"}


def _segment_args(record_save_type: str, split_time: str) -> list[str]:
    """按格式返回 ffmpeg 分段录制参数。与非分段走不同 muxer 路径。"""
    if record_save_type == "FLV":
        return [
            "-map", "0", "-c:v", "copy", "-c:a", "copy", "-bsf:a", "aac_adtstoasc",
            "-f", "segment", "-segment_time", split_time, "-segment_format", "flv",
            "-reset_timestamps", "1",
        ]
    if record_save_type == "MKV":
        return [
            "-flags", "global_header", "-c:v", "copy", "-c:a", "aac", "-map", "0",
            "-f", "segment", "-segment_time", split_time, "-segment_format", "matroska",
            "-reset_timestamps", "1",
        ]
    if record_save_type == "MP4":
        return [
            "-c:v", "copy", "-c:a", "aac", "-map", "0",
            "-f", "segment", "-segment_time", split_time, "-segment_format", "mp4",
            "-reset_timestamps", "1", "-movflags", "+frag_keyframe+empty_moov",
        ]
    return [
        "-c:v", "copy", "-c:a", "copy", "-map", "0",
        "-f", "segment", "-segment_time", split_time, "-segment_format", "mpegts",
        "-reset_timestamps", "1",
    ]


def _record_once(
    real_url: str,
    anchor_name: str,
    live_title: str | None,
    full_path: str,
    record_save_type: str,
    record_name: str,
    record_url: str,
    settings: Settings,
) -> str:
    """执行一次录制（一场直播一个文件）。

    Returns:
        stop: 因注释/退出需要整体 return
        restart: 当前录制已中断，需要快速重试
        finished: 当前直播正常结束
    """
    now = datetime.datetime.today().strftime("%Y-%m-%d_%H-%M-%S")
    title_in_name = ""
    if live_title and settings.filename_by_title:
        title_in_name = live_title + "_"

    ext = _FORMAT_EXT[record_save_type]
    segmented = settings.split_video_by_time
    name_suffix = "_%03d" if segmented else ""
    filename = f"{anchor_name}_{title_in_name}{now}{name_suffix}{ext}"
    save_file_path = f"{full_path}/{filename}"
    logger.info(f"{anchor_name} 准备开始录制视频: {save_file_path}")

    ffmpeg_command = _ffmpeg_prologue(real_url, settings.proxy_addr)
    if segmented:
        ffmpeg_command.extend(_segment_args(record_save_type, settings.split_time))
    else:
        ffmpeg_command.extend(_FORMAT_ARGS[record_save_type])
    ffmpeg_command.append(save_file_path)

    try:
        result = _check_subprocess(
            record_name, record_url, ffmpeg_command, record_save_type, settings
        )
        if result == "stop":
            if record_save_type == "TS":
                _spawn_convert(save_file_path, segmented, settings)
            return "stop"
        if result == "restart":
            return "restart"
        if result == "finished":
            return "finished"
    except subprocess.CalledProcessError as e:
        logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
        with runtime.max_request_lock:
            runtime.error_count += 1
            runtime.error_window.append(1)

    # FLV 格式结束时走独立转码（区别于 TS：只有 FLV 这里是无条件转）
    if record_save_type == "FLV" and settings.converts_to_mp4:
        try:
            _spawn_convert(save_file_path, segmented, settings)
        except Exception as e:
            logger.error(f"转码失败: {e}")

    return "restart"


# ---------- 目录命名 ---------------------------------------------------------

def _build_full_path(
    default_path: str,
    platform: str,
    anchor_name: str,
    live_title: str | None,
    settings: Settings,
) -> str:
    full_path = f"{default_path}/{platform}"

    try:
        if settings.video_save_path:
            base = settings.video_save_path
            if not base.endswith(("/", "\\")):
                full_path = f"{base}/{platform}"
            else:
                full_path = f"{base}{platform}"
        full_path = full_path.replace("\\", "/")

        if settings.folder_by_author:
            full_path = f"{full_path}/{anchor_name}"
        if settings.folder_by_time:
            today = datetime.datetime.today().strftime("%Y-%m-%d")
            full_path = f"{full_path}/{today}"
        if settings.folder_by_title and live_title:
            if settings.folder_by_time:
                full_path = f"{full_path}/{live_title}_{anchor_name}"
            else:
                today = datetime.datetime.today().strftime("%Y-%m-%d")
                full_path = f"{full_path}/{today}_{live_title}"

        os.makedirs(full_path, exist_ok=True)
    except Exception as e:
        logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")

    return full_path


# ---------- 外层 worker -----------------------------------------------------

def start_record(
    url_data: tuple,
    settings: Settings,
    default_path: str,
    count_variable: int = -1,
) -> None:
    """一个 URL 一个线程的主循环。"""
    record_quality_zh, record_url, anchor_name_hint = url_data
    record_quality = get_quality_code(record_quality_zh)

    while True:
        try:
            run_once = False
            next_wait_seconds: int | None = None

            while True:
                loop_log_name = f"序号{count_variable}"
                try:
                    if "douyin.com/" not in record_url:
                        logger.error(f"{record_url} 不支持的直播地址，仅支持抖音直播")
                        return

                    platform = "抖音直播"
                    with runtime.semaphore:
                        if "v.douyin.com" not in record_url and "/user/" not in record_url:
                            json_data = runtime.run_coro(spider.get_douyin_web_stream_data(
                                url=record_url,
                                proxy_addr=settings.proxy_addr,
                                cookies=settings.dy_cookie,
                            ))
                        else:
                            json_data = runtime.run_coro(spider.get_douyin_app_stream_data(
                                url=record_url,
                                proxy_addr=settings.proxy_addr,
                                cookies=settings.dy_cookie,
                            ))
                        port_info = runtime.run_coro(
                            stream.get_douyin_stream_url(json_data, record_quality, settings.proxy_addr)
                        )

                    # ---- 名称归一 -------------------------------------------
                    anchor_name = anchor_name_hint
                    if anchor_name and "主播:" in anchor_name:
                        anchor_split = anchor_name.split("主播:")
                        if len(anchor_split) > 1 and anchor_split[1].strip():
                            anchor_name = anchor_split[1].strip()
                        else:
                            anchor_name = port_info.get("anchor_name", "")
                    else:
                        anchor_name = port_info.get("anchor_name", "")

                    if not port_info.get("anchor_name", ""):
                        logger.warning(
                            f"序号{count_variable} 网址内容获取失败，进行重试中… 地址: {url_data}"
                        )
                        with runtime.max_request_lock:
                            runtime.error_count += 1
                            runtime.error_window.append(1)
                    else:
                        anchor_name = clean_name(anchor_name, settings.clean_emoji)
                        record_name = f"序号{count_variable} {anchor_name}"
                        loop_log_name = record_name

                        if record_url in runtime.url_comments:
                            logger.info(f"[{anchor_name}] 已被注释，本条线程将会退出")
                            clear_record_info(record_name, record_url)
                            return

                        # 首次成功获取到主播名后，回写 URL_config
                        if not url_data[-1] and not run_once:
                            runtime.need_update_line_list.append(
                                f"{record_url}|{record_url},主播: {anchor_name.strip()}"
                            )
                            run_once = True

                        if not port_info["is_live"]:
                            logger.info(f"{record_name} 循环值守中，当前未开播")
                        else:
                            logger.info(f"{record_name} 正在直播中")

                            flv_url = port_info.get("flv_url")
                            codec = utils.get_query_params(flv_url, "codec") if flv_url else None
                            is_h265 = bool(codec and codec[0] == "h265")
                            if is_h265:
                                logger.warning("FLV 不支持 h265 编码，改用 HLS 源并强制 TS 容器")
                                real_url = port_info.get("record_url")
                            else:
                                real_url = flv_url or port_info.get("record_url")

                            if real_url:
                                live_title_raw = port_info.get("title")
                                live_title = (
                                    clean_name(live_title_raw, settings.clean_emoji) if live_title_raw else None
                                )

                                full_path = _build_full_path(
                                    default_path, platform, anchor_name, live_title, settings
                                )

                                start_record_time = datetime.datetime.now()
                                with runtime.state_lock:
                                    runtime.recording.add(record_name)
                                    runtime.recording_time_list[record_name] = [
                                        start_record_time, record_quality_zh, record_url
                                    ]

                                if settings.show_url:
                                    logger.info(f"{platform} | {anchor_name} | 直播源地址: {real_url}")

                                record_save_type = "TS" if is_h265 else settings.video_save_type

                                should_return = _record_once(
                                    real_url=real_url,
                                    anchor_name=anchor_name,
                                    live_title=live_title,
                                    full_path=full_path,
                                    record_save_type=record_save_type,
                                    record_name=record_name,
                                    record_url=record_url,
                                    settings=settings,
                                )
                                if should_return == "stop":
                                    return
                                if should_return == "finished":
                                    next_wait_seconds = 30
                                elif should_return == "restart":
                                    next_wait_seconds = max(1, settings.corrupt_restart_delay)

                except Exception as e:
                    logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                    with runtime.max_request_lock:
                        runtime.error_count += 1
                        runtime.error_window.append(1)

                # ---- 等待下一轮 ------------------------------------------------
                wait_seconds = max(0, random.randint(-5, 5) + settings.delay_default)
                if runtime.error_count > 20:
                    wait_seconds += 60
                    logger.warning("瞬时错误过多，本轮延迟额外 +60 秒")
                if next_wait_seconds is not None:
                    wait_seconds = next_wait_seconds
                    next_wait_seconds = None
                logger.info(f"{loop_log_name} {wait_seconds} 秒后开始下一轮值守检查")
                time.sleep(wait_seconds)
        except Exception as e:
            logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
            with runtime.max_request_lock:
                runtime.error_count += 1
                runtime.error_window.append(1)
            time.sleep(2)
