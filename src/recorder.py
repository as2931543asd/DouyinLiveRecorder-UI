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
from pathlib import Path
from typing import Callable, Literal, TextIO

RecordResult = Literal["stop", "corrupt_restart", "ended", "finished"]
SubprocessStartedCallback = Callable[[float, float], None]

from . import live_history, platforms, runtime, spider, stream, utils
from .config_loader import Settings
from .danmaku import DanmakuRecorder, danmaku_jsonl_path, extract_room_identifier
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


def _configured_anchor_name(raw_name: str, clean_emoji: bool) -> str:
    """把 URL_config 中的可选 `主播:` 前缀去掉后用于名称比较。"""
    name = raw_name.strip()
    if "主播:" in name:
        name = name.split("主播:", maxsplit=1)[1].strip()
    return clean_name(name, clean_emoji) if name else ""


def _retry_delay(
    base_seconds: int,
    consecutive_failures: int,
    max_seconds: int,
    jitter_seconds: int = 5,
) -> int:
    """指数退避并加入向下抖动，既快速首连也避免多个 worker 同时请求。"""
    base = max(1, base_seconds)
    ceiling = max(base, max_seconds)
    exponent = min(max(0, consecutive_failures), 20)
    nominal = min(ceiling, base * (2 ** exponent))
    jitter = min(max(0, jitter_seconds), nominal - 1)
    return random.randint(nominal - jitter, nominal)


def _corrupt_retry_delay(
    initial_seconds: int,
    consecutive_restarts: int,
    max_seconds: int = 120,
) -> int:
    """花屏连续出现时使用 3 → 10 → 30 → 120 秒的独立退避。"""
    initial = max(1, initial_seconds)
    stages = (initial, max(initial, 10), max(initial, 30), max(initial, 120))
    delay = stages[min(max(0, consecutive_restarts), len(stages) - 1)]
    return min(max(initial, max_seconds), delay)


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


def _stop_requested(record_url: str) -> bool:
    with runtime.state_lock:
        return runtime.exit_recording or record_url in runtime.url_comments


def clear_record_info(record_name: str, record_url: str) -> None:
    removed = False
    with runtime.state_lock:
        runtime.recording.discard(record_name)
        runtime.recording_time_list.pop(record_name, None)
        runtime.live_urls.discard(record_url)
        if (runtime.exit_recording or record_url in runtime.url_comments) and record_url in runtime.running_list:
            runtime.running_list.remove(record_url)
            runtime.monitoring = max(0, runtime.monitoring - 1)
            removed = True
    if removed:
        logger.info(f"[{record_name}] 已从监控列表中移除")


def _exit_if_stop_requested(record_name: str, record_url: str) -> bool:
    if not _stop_requested(record_url):
        return False
    logger.info(f"[{record_name}] 已被删除或暂停，本条线程将会退出")
    clear_record_info(record_name, record_url)
    return True


def _sleep_or_stop(record_name: str, record_url: str, seconds: float) -> bool:
    deadline = time.monotonic() + max(0, seconds)
    while True:
        if _exit_if_stop_requested(record_name, record_url):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1.0, remaining))


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
            try:
                process.stdin.write("q")
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
    else:
        process.send_signal(signal.SIGINT)

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg 在结束信号后未及时退出，已强制终止")
        process.kill()
        process.wait()


def _log_recent_output(log_fn, record_name: str, lines: deque[str]) -> None:
    if lines:
        log_fn(f"{record_name} ffmpeg 最近输出:\n" + "\n".join(lines))


def _check_subprocess(
    record_name: str,
    record_url: str,
    ffmpeg_command: list[str],
    save_type: str,
    save_file_path: str,
    settings: Settings,
    on_started: SubprocessStartedCallback | None = None,
) -> RecordResult:
    """启动 ffmpeg 并阻塞监视。

    Returns:
        stop: 因注释或程序退出而主动关停（调用方应 return）
        corrupt_restart: 检测到坏流主动中断，应按 corrupt_restart_delay 快速重试
        ended: ffmpeg 非 0 退出（下播/断流/源失效等），按默认节奏重试
        finished: 录制正常结束
    """
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
    started_at = time.time()
    started_monotonic = time.monotonic()
    output_queue: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=_stream_reader, args=(process.stdout, output_queue), daemon=True)
    reader.start()
    if on_started is not None:
        try:
            on_started(started_at, started_monotonic)
        except Exception as exc:
            logger.error(f"FFmpeg 启动回调失败，视频录制继续: {exc}")
    corrupt_window: deque[float] = deque()
    recent_lines: deque[str] = deque(maxlen=30)
    forced_restart = False

    while process.poll() is None:
        if _stop_requested(record_url):
            logger.warning(f"[{record_name}] 录制时已被删除或暂停，本条线程将会退出")
            clear_record_info(record_name, record_url)
            _stop_ffmpeg_process(process)
            return "stop"

        try:
            line = output_queue.get(timeout=1)
        except queue.Empty:
            continue

        recent_lines.append(line)

        if _should_restart_for_corruption(line, corrupt_window, settings):
            logger.warning(
                f"[{record_name}] 在 {settings.corrupt_error_window_seconds} 秒内累计检测到 "
                f"{len(corrupt_window)} 条疑似坏流日志，主动中断后重录"
            )
            forced_restart = True
            _stop_ffmpeg_process(process)
            break

    # 进程已退出/被中断，把 pipe 里剩下的几行也捞出来，便于诊断
    reader.join(timeout=2)
    while True:
        try:
            recent_lines.append(output_queue.get_nowait())
        except queue.Empty:
            break

    return_code = process.returncode
    stop_time = time.strftime("%Y-%m-%d %H:%M:%S")
    result: RecordResult
    if forced_restart:
        logger.warning(f"{record_name} {stop_time} 已终止当前录制，准备重新拉流")
        _log_recent_output(logger.warning, record_name, recent_lines)
        result = "corrupt_restart"
    elif return_code == 0:
        if settings.converts_to_mp4 and save_type == "TS":
            _spawn_convert(save_file_path, settings.split_video_by_time, settings)
        logger.info(f"{record_name} {stop_time} 直播录制完成")
        result = "finished"
    else:
        logger.error(f"{record_name} {stop_time} 直播录制出错，返回码: {return_code}")
        _log_recent_output(logger.error, record_name, recent_lines)
        result = "ended"

    with runtime.state_lock:
        runtime.recording.discard(record_name)
        runtime.recording_time_list.pop(record_name, None)
    return result


def _run_danmaku_bootstrap(recorder: DanmakuRecorder) -> None:
    """在独立线程启动弹幕，隔离慢文件初始化和意外异常。"""
    try:
        recorder.start()
    except Exception as exc:
        logger.error(f"弹幕录制启动失败，视频录制继续: {exc}")


def _spawn_danmaku_bootstrap(recorder: DanmakuRecorder) -> None:
    threading.Thread(
        target=_run_danmaku_bootstrap,
        args=(recorder,),
        name=f"danmaku-bootstrap-{recorder.room_identifier}",
        daemon=True,
    ).start()


# ---------- ffmpeg 组装 ------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile Safari/537.36"
)


def _ffmpeg_prologue(
    real_url: str,
    proxy_addr: str | None,
    user_agent: str | None = None,
    referer: str | None = None,
) -> list[str]:
    cmd = [
        "ffmpeg", "-y",
        "-v", "warning",
        "-rw_timeout", "15000000",
        "-hide_banner",
        "-user_agent", user_agent or _USER_AGENT,
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
    if referer:
        input_index = cmd.index("-i")
        cmd[input_index:input_index] = ["-referer", referer]
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
    danmaku_room_identifier: str | None = None,
    ffmpeg_user_agent: str | None = None,
    ffmpeg_referer: str | None = None,
) -> RecordResult:
    """执行一次录制（一场直播一个文件）。

    Returns:
        stop: 因注释/退出需要整体 return
        corrupt_restart: 检测到坏流主动中断，应快速重试
        ended: ffmpeg 非 0 退出（下播/断流/源失效等），按默认节奏重试
        finished: 当前直播正常结束
    """
    filename_started_at = datetime.datetime.now()
    now = filename_started_at.strftime("%Y-%m-%d_%H-%M-%S")
    title_in_name = ""
    if live_title and settings.filename_by_title:
        title_in_name = live_title + "_"

    ext = _FORMAT_EXT[record_save_type]
    segmented = settings.split_video_by_time
    name_suffix = "_%03d" if segmented else ""
    filename = f"{anchor_name}_{title_in_name}{now}{name_suffix}{ext}"
    save_file_path = f"{full_path}/{filename}"
    logger.info(f"{anchor_name} 准备开始录制视频: {save_file_path}")

    ffmpeg_command = _ffmpeg_prologue(
        real_url,
        settings.proxy_addr,
        user_agent=ffmpeg_user_agent,
        referer=ffmpeg_referer,
    )
    if segmented:
        ffmpeg_command.extend(_segment_args(record_save_type, settings.split_time))
    else:
        ffmpeg_command.extend(_FORMAT_ARGS[record_save_type])
    ffmpeg_command.append(save_file_path)
    if settings.corrupt_probe_decode:
        # 旁路解码：不落盘，仅触发视频解码以便捕获 NAL/解码层错误日志
        ffmpeg_command.extend(["-map", "0:v", "-an", "-f", "null", "-"])

    danmaku_recorder: DanmakuRecorder | None = None
    danmaku_parameters: tuple[str, str, Path] | None = None
    if getattr(settings, "record_danmaku", False) and danmaku_room_identifier:
        danmaku_parameters = (
            danmaku_room_identifier,
            getattr(settings, "danmaku_service_url", "ws://127.0.0.1:1088"),
            danmaku_jsonl_path(save_file_path),
        )

    def start_danmaku(started_at: float, started_monotonic: float) -> None:
        nonlocal danmaku_recorder
        if danmaku_parameters is None:
            return
        room_identifier, service_url, jsonl_path = danmaku_parameters
        recorder = DanmakuRecorder(
            room_identifier=room_identifier,
            service_url=service_url,
            jsonl_path=jsonl_path,
            started_at=started_at,
            started_monotonic=started_monotonic,
        )
        # 先同步发布实例，确保极短 FFmpeg 退出时 finally 能先置 stop；真正可能
        # 被 mkdir/open/flush 阻塞的 start 始终放在独立 daemon bootstrap 中。
        danmaku_recorder = recorder
        _spawn_danmaku_bootstrap(recorder)

    try:
        result = _check_subprocess(
            record_name,
            record_url,
            ffmpeg_command,
            record_save_type,
            save_file_path,
            settings,
            on_started=start_danmaku if danmaku_parameters is not None else None,
        )
    finally:
        if danmaku_recorder is not None:
            try:
                danmaku_recorder.stop()
            except Exception as e:
                logger.error(f"弹幕录制停止失败，视频录制结果不受影响: {e}")

    if result == "stop":
        if record_save_type == "TS":
            _spawn_convert(save_file_path, segmented, settings)
        return "stop"

    # FLV 格式结束时走独立转码（区别于 TS：只有 FLV 这里是无条件转）
    if record_save_type == "FLV" and settings.converts_to_mp4:
        try:
            _spawn_convert(save_file_path, segmented, settings)
        except Exception as e:
            logger.error(f"转码失败: {e}")

    return result


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

def _fetch_stream_info(
    platform_id: str,
    record_url: str,
    record_quality: str | None,
    settings: Settings,
) -> tuple[dict, dict]:
    """抓取平台数据，统一返回 (录制信息, 原始房间数据)。"""
    raw_room_data: dict = {}
    if platform_id == platforms.DOUYIN:
        if "v.douyin.com" not in record_url and "/user/" not in record_url:
            raw_room_data = runtime.run_coro(spider.get_douyin_web_stream_data(
                url=record_url,
                proxy_addr=settings.proxy_addr,
                cookies=settings.dy_cookie,
            ))
        else:
            raw_room_data = runtime.run_coro(spider.get_douyin_app_stream_data(
                url=record_url,
                proxy_addr=settings.proxy_addr,
                cookies=settings.dy_cookie,
            ))
        port_info = runtime.run_coro(
            stream.get_douyin_stream_url(raw_room_data, record_quality, settings.proxy_addr)
        )
    elif platform_id == platforms.XIAOHONGSHU:
        port_info = runtime.run_coro(spider.get_xhs_stream_url(
            url=record_url,
            video_quality=record_quality,
            proxy_addr=settings.proxy_addr,
            cookies=getattr(settings, "xhs_cookie", ""),
        ))
    else:
        return {}, raw_room_data

    return (port_info if isinstance(port_info, dict) else {}), (
        raw_room_data if isinstance(raw_room_data, dict) else {}
    )


def start_record(
    url_data: tuple,
    settings: Settings,
    default_path: str,
    count_variable: int = -1,
) -> None:
    """一个 URL 一个线程的主循环。"""
    record_quality_zh, record_url, anchor_name_hint = url_data
    record_quality = get_quality_code(record_quality_zh)
    worker_name = f"序号{count_variable} {anchor_name_hint or record_url}"
    configured_anchor_name = _configured_anchor_name(anchor_name_hint, settings.clean_emoji)
    live_session_started_at: datetime.datetime | None = None
    last_successful_record_end: datetime.datetime | None = None
    disconnect_streak = 0
    corrupt_restart_streak = 0
    poll_error_streak = 0
    platform_id = platforms.identify_platform(record_url)
    if platform_id is None:
        logger.error(f"{record_url} 不支持的直播地址")
        clear_record_info(worker_name, record_url)
        return
    platform = platforms.platform_name(platform_id)

    while True:
        try:
            next_wait_seconds: int | None = None

            while True:
                try:
                    if _exit_if_stop_requested(worker_name, record_url):
                        return

                    with runtime.semaphore:
                        if _exit_if_stop_requested(worker_name, record_url):
                            return
                        port_info, json_data = _fetch_stream_info(
                            platform_id, record_url, record_quality, settings
                        )

                    fetched_anchor_name = port_info.get("anchor_name", "")
                    fetch_failed = not port_info or (
                        platform_id == platforms.XIAOHONGSHU
                        and not port_info.get("fetch_ok", False)
                    )
                    if platform_id == platforms.DOUYIN and not fetched_anchor_name:
                        fetch_failed = True

                    if fetch_failed:
                        poll_error_streak += 1
                        error_detail = port_info.get("error")
                        logger.warning(
                            f"序号{count_variable} 网址内容获取失败，进行重试中… 地址: {record_url}"
                            + (f"；原因: {error_detail}" if error_detail else "")
                        )
                        with runtime.max_request_lock:
                            runtime.error_count += 1
                            runtime.error_window.append(1)
                    else:
                        poll_error_streak = 0
                        if not fetched_anchor_name:
                            room_suffix = port_info.get("room_id") or "当前场次"
                            fetched_anchor_name = configured_anchor_name or f"小红书直播_{room_suffix}"
                        anchor_name = clean_name(fetched_anchor_name, settings.clean_emoji)
                        record_name = f"序号{count_variable} {anchor_name}"
                        worker_name = record_name

                        if _exit_if_stop_requested(record_name, record_url):
                            return

                        if anchor_name != configured_anchor_name:
                            with runtime.state_lock:
                                runtime.pending_name_updates[record_url] = anchor_name
                            configured_anchor_name = anchor_name
                        with runtime.state_lock:
                            runtime.current_anchor_names[record_url] = anchor_name

                        if port_info.get("is_live", False):
                            if live_session_started_at is None:
                                live_session_started_at = datetime.datetime.now().astimezone()
                            with runtime.state_lock:
                                runtime.live_urls.add(record_url)

                            flv_url = port_info.get("flv_url")
                            codec = utils.get_query_params(flv_url, "codec") if flv_url else None
                            is_h265 = port_info.get("codec") == "h265" or bool(
                                codec and codec[0] == "h265"
                            )
                            if is_h265:
                                logger.warning("FLV 不支持 h265 编码，改用 HLS 源并强制 TS 容器")
                                real_url = port_info.get("m3u8_url") or port_info.get("record_url")
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
                                danmaku_room_identifier = None
                                if settings.record_danmaku and platform_id == platforms.DOUYIN:
                                    danmaku_room_identifier = extract_room_identifier(
                                        json_data, record_url
                                    )
                                    if not danmaku_room_identifier:
                                        logger.warning(
                                            f"[{record_name}] 无法从直播 API 或 live.douyin.com "
                                            "路径确定弹幕直播间标识，本轮跳过弹幕，视频录制继续"
                                        )
                                elif settings.record_danmaku:
                                    logger.info(
                                        f"[{record_name}] 小红书暂不支持弹幕录制，视频录制继续"
                                    )

                                should_return = _record_once(
                                    real_url=real_url,
                                    anchor_name=anchor_name,
                                    live_title=live_title,
                                    full_path=full_path,
                                    record_save_type=record_save_type,
                                    record_name=record_name,
                                    record_url=record_url,
                                    settings=settings,
                                    danmaku_room_identifier=danmaku_room_identifier,
                                    ffmpeg_user_agent=port_info.get("ffmpeg_user_agent"),
                                    ffmpeg_referer=port_info.get("ffmpeg_referer"),
                                )
                                if should_return == "stop":
                                    return
                                record_ended_at = datetime.datetime.now()
                                recorded_seconds = (record_ended_at - start_record_time).total_seconds()
                                if recorded_seconds >= max(1, settings.stable_record_reset_seconds):
                                    disconnect_streak = 0
                                    corrupt_restart_streak = 0
                                if should_return == "finished" or (
                                    should_return == "ended" and recorded_seconds >= 60
                                ):
                                    # 非零退出但已持续录制至少一分钟时，仍视为一段
                                    # 有效直播；几秒内失败的拉流不会污染历史。
                                    last_successful_record_end = record_ended_at.astimezone()
                                if should_return == "corrupt_restart":
                                    next_wait_seconds = _corrupt_retry_delay(
                                        settings.corrupt_restart_delay,
                                        corrupt_restart_streak,
                                    )
                                    corrupt_restart_streak += 1
                                    logger.warning(
                                        f"[{record_name}] 连续花屏重录第 {corrupt_restart_streak} 次，"
                                        f"{next_wait_seconds} 秒后刷新流地址"
                                    )
                                else:
                                    corrupt_restart_streak = 0
                                    next_wait_seconds = _retry_delay(
                                        settings.fast_reconnect_delay,
                                        disconnect_streak,
                                        settings.reconnect_backoff_max,
                                    )
                                    disconnect_streak += 1
                                    logger.info(
                                        f"[{record_name}] 断流重连第 {disconnect_streak} 次，"
                                        f"{next_wait_seconds} 秒后确认直播状态并刷新流地址"
                                    )
                            else:
                                poll_error_streak += 1
                        else:
                            with runtime.state_lock:
                                runtime.live_urls.discard(record_url)
                            # ffmpeg 结束后再由直播状态接口确认一次下播，避免短流
                            # 重连期间反复刷新“上次直播”的结束时间。
                            if live_session_started_at and last_successful_record_end:
                                live_history.record_completed(
                                    record_url,
                                    anchor_name,
                                    live_session_started_at,
                                    last_successful_record_end,
                                )
                            live_session_started_at = None
                            last_successful_record_end = None
                            disconnect_streak = 0
                            corrupt_restart_streak = 0

                except Exception as e:
                    poll_error_streak += 1
                    logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                    with runtime.max_request_lock:
                        runtime.error_count += 1
                        runtime.error_window.append(1)

                # ---- 等待下一轮 ------------------------------------------------
                wait_seconds = max(0, random.randint(-5, 5) + settings.delay_default)
                if poll_error_streak:
                    wait_seconds = _retry_delay(
                        settings.delay_default,
                        poll_error_streak - 1,
                        settings.fetch_backoff_max,
                    )
                    logger.warning(
                        f"[{worker_name}] 连续获取失败第 {poll_error_streak} 次，"
                        f"{wait_seconds} 秒后重试"
                    )
                if next_wait_seconds is not None:
                    wait_seconds = next_wait_seconds
                    next_wait_seconds = None
                if runtime.error_count > 20:
                    wait_seconds += 60
                    logger.warning("瞬时错误过多，本轮延迟额外 +60 秒")
                if _sleep_or_stop(worker_name, record_url, wait_seconds):
                    return
        except Exception as e:
            logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
            with runtime.max_request_lock:
                runtime.error_count += 1
                runtime.error_window.append(1)
            if _sleep_or_stop(worker_name, record_url, 2):
                return
