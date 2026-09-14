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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from src import live_history, platforms, runtime, url_config, utils
from src.config_loader import text_encoding
from src.logger import get_recent_logs
from webui import danmaku_api

app = FastAPI()
app.include_router(danmaku_api.router)

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
    recording_root = (
        Path(disk_sample_path) if disk_sample_path
        else Path(url_config_file).resolve().parent.parent / "downloads"
    )
    danmaku_api.configure(recording_root)
    if not _stats_thread_started:
        _stats_thread_started = True
        threading.Thread(target=_stats_sampler_loop, daemon=True).start()


# ---------- Models ----------------------------------------------------------

_UNSAFE_CONFIG_CHARS = re.compile(r"[\x00-\x1f\x7f,，\u0085\u2028\u2029]")


class StreamerAction(BaseModel):
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if _UNSAFE_CONFIG_CHARS.search(value):
            raise ValueError("链接不能包含控制字符或配置分隔符")
        value = value.strip()
        if not value or any(char.isspace() for char in value):
            raise ValueError("请填写完整直播间链接，链接中不能包含空白")
        normalized = platforms.normalize_supported_url(value)
        if normalized is None:
            raise ValueError("仅支持抖音直播链接、小红书直播间或分享短链")
        return value


class StreamerAdd(StreamerAction):
    # 旧版 WebUI/QQ 插件的 quality 作为额外字段忽略，不再允许选择低画质。
    name: str = Field(default="", max_length=200)

    @field_validator("url")
    @classmethod
    def require_room_path(cls, value: str) -> str:
        # 只限制新增，旧版误加入的纯域名仍可通过暂停/删除清理。
        normalized = platforms.normalize_supported_url(value)
        if not urlsplit(normalized).path.strip("/"):
            raise ValueError("请填写完整直播间链接，不能只有域名前缀")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if _UNSAFE_CONFIG_CHARS.search(value):
            raise ValueError("名称不能包含换行、控制字符或中英文逗号")
        return value.strip()


def _normalize_streamer_url(url: str) -> str:
    """生成用于配置项比对的 URL 键，不使用容易误命中的子串判断。"""
    value = url.strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value

    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if not parsed.netloc:
        return ""

    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _parse_streamer_line(line: str) -> tuple[bool, str, str, str] | None:
    """WebUI 与录制调度使用同一解析规则，也能展示尚未规范化的单 URL 行。"""
    return url_config.parse_streamer_line(line)


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


@app.get("/danmaku")
async def danmaku_page():
    return FileResponse(str(static_dir / "danmaku.html"))


# ---------- API -------------------------------------------------------------

@app.exception_handler(OSError)
@app.exception_handler(UnicodeError)
async def config_io_error(_request, _exc):
    return JSONResponse(status_code=503, content={
        "ok": False, "error": "配置文件读写失败，请检查权限、编码和磁盘空间",
    })

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
    url = platforms.normalize_supported_url(data.url)
    if not url:
        return {
            "ok": False,
            "error": "仅支持抖音直播链接、小红书 livestream 直链或 xhslink 分享短链",
        }

    normalized_url = _normalize_streamer_url(url)
    line = f"原画,{url}" + (f",{data.name}" if data.name else "") + "\n"
    with runtime.file_update_lock:
        path = Path(_url_config_file)
        try:
            content = path.read_text(encoding=text_encoding)
        except FileNotFoundError:
            content = ""
        # 查重与提交同属一个事务，多个客户端同时添加也只会成功一次。
        for existing in content.splitlines():
            parsed = _parse_streamer_line(existing)
            if parsed and _normalize_streamer_url(parsed[2]) == normalized_url:
                return {"ok": False, "error": "该链接已存在"}
        if content and not content.endswith("\n"):
            content += "\n"
        utils.atomic_write_text(path, content + line, encoding=text_encoding)
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
        with open(_url_config_file, "r", encoding=text_encoding) as f:
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

        if not toggled:
            return {"ok": False, "error": "未找到完全匹配的主播链接"}
        utils.atomic_write_text(_url_config_file, "".join(new_lines), encoding=text_encoding)
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
        with runtime.file_update_lock, open(_url_config_file, "r", encoding=text_encoding) as f:
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
                platform_id = platforms.identify_platform(url)

                streamers.append({
                    "url": url,
                    "platform": platform_id,
                    "platform_name": platforms.platform_name(platform_id or ""),
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
        with open(_url_config_file, "r", encoding=text_encoding) as f:
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

        utils.atomic_write_text(_url_config_file, "".join(kept_lines), encoding=text_encoding)
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
