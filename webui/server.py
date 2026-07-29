# -*- encoding: utf-8 -*-
"""FastAPI 控制面板后端。

读写策略：
- 读共享运行时状态（recording / recording_time_list / running_list）
  必须先在 `runtime.state_lock` 下拿 snapshot，再释放锁处理。
- 写 URL_config.ini 统一走 `runtime.file_update_lock`。
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psutil
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import live_history, runtime
from src.config_loader import text_encoding
from src.logger import get_recent_logs

app = FastAPI()

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

_url_config_file: str | None = None
_disk_sample_path: str = os.getcwd()

# 后台线程固定 1s 采样 CPU（EMA 平滑）与磁盘剩余，请求端只读不阻塞。
_cpu_smoothed: float | None = None
_disk_free_gb: float = 0.0
_stats_lock = threading.Lock()
_stats_thread_started = False
_CPU_EMA_ALPHA = 0.3


def _stats_sampler_loop() -> None:
    global _cpu_smoothed, _disk_free_gb
    while True:
        try:
            sample = psutil.cpu_percent(interval=1.0)
        except Exception:
            time.sleep(1.0)
            continue
        try:
            free_gb = shutil.disk_usage(_disk_sample_path).free / (1024 ** 3)
        except Exception:
            free_gb = 0.0
        with _stats_lock:
            if _cpu_smoothed is None:
                _cpu_smoothed = sample
            else:
                _cpu_smoothed = _CPU_EMA_ALPHA * sample + (1 - _CPU_EMA_ALPHA) * _cpu_smoothed
            _disk_free_gb = free_gb


def init(url_config_file: str, disk_sample_path: str | None = None) -> None:
    """由 main.py 调用，告知 ini 路径及磁盘检测目录。"""
    global _url_config_file, _disk_sample_path, _stats_thread_started
    _url_config_file = url_config_file
    if disk_sample_path:
        _disk_sample_path = disk_sample_path
    if not _stats_thread_started:
        _stats_thread_started = True
        threading.Thread(target=_stats_sampler_loop, daemon=True).start()


# ---------- Models ----------------------------------------------------------

class StreamerAdd(BaseModel):
    url: str
    quality: str = "原画"
    name: str = ""


class StreamerAction(BaseModel):
    url: str


def _normalize_streamer_url(url: str) -> str:
    """生成用于配置项比对的 URL 键，不使用容易误命中的子串判断。"""
    value = url.strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value

    parsed = urlsplit(value)
    if not parsed.netloc:
        return ""

    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _parse_streamer_line(line: str) -> tuple[bool, str, str, str] | None:
    """解析一行主播配置，返回 (是否暂停, 画质, URL, 名称)。"""
    stripped = line.strip()
    if len(stripped) < 18:
        return None

    paused = stripped.startswith("#")
    if paused:
        stripped = stripped.lstrip("#").strip()

    parts = re.split("[,，]", stripped)
    if len(parts) < 2:
        return None

    if re.search(r"https?://", parts[0]):
        quality = "原画"
        url = parts[0]
        name = parts[1] if len(parts) > 1 else ""
    else:
        quality = parts[0]
        url = parts[1] if len(parts) > 1 else ""
        name = parts[2] if len(parts) > 2 else ""

    url = url.strip()
    if not _normalize_streamer_url(url):
        return None
    return paused, quality, url, name


def _toggle_line_comment(line: str, currently_paused: bool) -> str:
    """切换配置行开头的注释符，同时保留原有缩进与换行。"""
    content = line.lstrip()
    leading = line[:len(line) - len(content)]
    if currently_paused:
        return leading + content.lstrip("#")
    return leading + "#" + content


# ---------- Pages -----------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(str(static_dir / "index.html"))


# ---------- API -------------------------------------------------------------

@app.get("/api/status")
async def get_status():
    now = datetime.datetime.now()
    with runtime.state_lock:
        rec_snapshot = list(runtime.recording)
        time_snapshot = {
            name: list(runtime.recording_time_list.get(name, [])) for name in rec_snapshot
        }
        monitoring_count = len(runtime.running_list)

    recordings = []
    for rec_name in rec_snapshot:
        entry = time_snapshot.get(rec_name)
        if entry:
            rt, qa, url = entry[0], entry[1], entry[2]
            duration = str(now - rt).split(".")[0]
            recordings.append({"name": rec_name, "quality": qa, "duration": duration, "url": url})
        else:
            recordings.append({"name": rec_name, "quality": "?", "duration": "0:00:00", "url": ""})

    with _stats_lock:
        cpu_percent = _cpu_smoothed if _cpu_smoothed is not None else 0.0
        disk_free_gb = _disk_free_gb

    return {
        "monitoring": monitoring_count,
        "recording_count": len(recordings),
        "error_count": runtime.error_count,
        "cpu_percent": round(cpu_percent, 1),
        "disk_free_gb": round(disk_free_gb, 1),
        "recordings": recordings,
    }


@app.get("/api/logs")
async def get_logs(limit: int = 200):
    return {"logs": get_recent_logs(limit)}


@app.get("/api/streamers")
async def get_streamers():
    return _parse_url_config()


@app.post("/api/streamers")
async def add_streamer(data: StreamerAdd):
    url = data.url.strip()
    if "://" not in url:
        url = "https://" + url

    url_host = url.split("/")[2] if len(url.split("/")) > 2 else ""
    if url_host not in ("live.douyin.com", "v.douyin.com", "www.douyin.com"):
        return {"ok": False, "error": "仅支持抖音直播链接"}
    if url_host == "live.douyin.com":
        url = url.split("?")[0]

    normalized_url = _normalize_streamer_url(url)
    for s in _parse_url_config():
        if _normalize_streamer_url(s["url"]) == normalized_url:
            return {"ok": False, "error": "该链接已存在"}

    line = f"{data.quality},{url}"
    if data.name:
        line += f",{data.name}"
    line += "\n"

    with runtime.file_update_lock:
        with open(_url_config_file, "a", encoding=text_encoding) as f:
            f.write(line)
    live_history.initialize_missing([url])

    return {"ok": True}


@app.delete("/api/streamers")
async def delete_streamer(data: StreamerAction):
    if not _delete_url_line(data.url.strip()):
        return {"ok": False, "error": "未找到完全匹配的主播链接"}
    return {"ok": True}


@app.patch("/api/streamers/toggle")
async def toggle_streamer(data: StreamerAction):
    url = data.url.strip()
    target_url = _normalize_streamer_url(url)
    if not target_url:
        return {"ok": False, "error": "主播链接无效"}

    paused_after_toggle = False
    matched_url = ""
    with runtime.file_update_lock:
        with open(_url_config_file, "r", encoding=text_encoding, errors="ignore") as f:
            lines = f.readlines()

        new_lines: list[str] = []
        toggled = False
        for line in lines:
            parsed = _parse_streamer_line(line)
            is_exact_match = (
                parsed is not None
                and _normalize_streamer_url(parsed[2]) == target_url
            )
            if not toggled and is_exact_match:
                paused, _, matched_url, _ = parsed
                if paused:
                    new_lines.append(_toggle_line_comment(line, currently_paused=True))
                    paused_after_toggle = False
                else:
                    new_lines.append(_toggle_line_comment(line, currently_paused=False))
                    paused_after_toggle = True
                toggled = True
            else:
                new_lines.append(line)

        with open(_url_config_file, "w", encoding=text_encoding) as f:
            f.writelines(new_lines)

    if not toggled:
        return {"ok": False, "error": "未找到完全匹配的主播链接"}

    if paused_after_toggle:
        _request_stop_url(matched_url)
    else:
        _clear_stop_url(matched_url)

    return {"ok": True}


# ---------- Helpers ---------------------------------------------------------

def _parse_url_config() -> list[dict]:
    streamers: list[dict] = []
    history_snapshot = live_history.get_snapshot()
    now = datetime.datetime.now().astimezone()
    with runtime.state_lock:
        running_snapshot = list(runtime.running_list)
        current_name_snapshot = dict(runtime.current_anchor_names)
        live_url_snapshot = set(runtime.live_urls)
        recording_url_snapshot = {
            entry[2]
            for entry in runtime.recording_time_list.values()
            if len(entry) >= 3 and entry[2]
        }
    try:
        with open(_url_config_file, "r", encoding=text_encoding, errors="ignore") as f:
            for line in f:
                parsed = _parse_streamer_line(line)
                if parsed is None:
                    continue

                paused, quality, url, name = parsed

                is_recording = url in recording_url_snapshot
                is_monitoring = url in running_snapshot and not paused
                is_live = url in live_url_snapshot and not paused
                if paused:
                    state = "已暂停"
                elif is_recording:
                    state = "录制中"
                elif is_live:
                    state = "重连中"
                else:
                    state = "监控中"
                history = history_snapshot.get(url, {})
                ended_at_raw = history.get("last_ended_at")
                elapsed_seconds: int | None = None
                if ended_at_raw:
                    try:
                        ended_at = datetime.datetime.fromisoformat(ended_at_raw)
                        if ended_at.tzinfo is None:
                            ended_at = ended_at.astimezone()
                        elapsed_seconds = max(0, int((now - ended_at).total_seconds()))
                    except (TypeError, ValueError):
                        ended_at_raw = None

                configured_name = name.replace("主播: ", "").strip()

                streamers.append({
                    "url": url,
                    "quality": quality,
                    "name": current_name_snapshot.get(url, configured_name),
                    "paused": paused,
                    "is_recording": is_recording and not paused,
                    "is_live": is_live,
                    "is_monitoring": is_monitoring,
                    "state": state,
                    "last_live_ended_at": ended_at_raw,
                    "last_live_elapsed_seconds": elapsed_seconds,
                    "last_live_duration_seconds": history.get("last_duration_seconds"),
                    "last_live_is_baseline": bool(history.get("is_baseline")),
                })
    except FileNotFoundError:
        pass
    return streamers


def _delete_url_line(url: str) -> bool:
    target_url = _normalize_streamer_url(url)
    if not target_url:
        return False

    matched_url = ""
    with runtime.file_update_lock:
        with open(_url_config_file, "r", encoding=text_encoding, errors="ignore") as f:
            lines = f.readlines()

        kept_lines: list[str] = []
        for line in lines:
            parsed = _parse_streamer_line(line)
            is_exact_match = (
                parsed is not None
                and _normalize_streamer_url(parsed[2]) == target_url
            )
            if is_exact_match:
                matched_url = parsed[2]
            else:
                kept_lines.append(line)

        if not matched_url:
            return False

        with open(_url_config_file, "w", encoding=text_encoding) as f:
            f.writelines(kept_lines)

    _request_stop_url(matched_url)
    for candidate in {matched_url, target_url}:
        live_history.remove(candidate)
    with runtime.state_lock:
        for candidate in {matched_url, target_url}:
            runtime.current_anchor_names.pop(candidate, None)
            runtime.pending_name_updates.pop(candidate, None)
            runtime.live_urls.discard(candidate)
    return True


def _url_stop_candidates(url: str) -> set[str]:
    stripped = url.strip()
    if not stripped:
        return set()
    with_scheme = stripped if "://" in stripped else "https://" + stripped
    return {stripped, with_scheme, with_scheme.split("?")[0]}


def _request_stop_url(url: str) -> None:
    candidates = _url_stop_candidates(url)
    if not candidates:
        return
    with runtime.state_lock:
        for candidate in candidates:
            if candidate not in runtime.url_comments:
                runtime.url_comments.append(candidate)


def _clear_stop_url(url: str) -> None:
    candidates = _url_stop_candidates(url)
    if not candidates:
        return
    with runtime.state_lock:
        runtime.url_comments = [u for u in runtime.url_comments if u not in candidates]


# ---------- Start -----------------------------------------------------------

def start_server(host: str = "0.0.0.0", port: int = 9527) -> None:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
