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

import psutil
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import runtime
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

    for s in _parse_url_config():
        if s["url"] == url:
            return {"ok": False, "error": "该链接已存在"}

    line = f"{data.quality},{url}"
    if data.name:
        line += f",{data.name}"
    line += "\n"

    with runtime.file_update_lock:
        with open(_url_config_file, "a", encoding=text_encoding) as f:
            f.write(line)

    return {"ok": True}


@app.delete("/api/streamers")
async def delete_streamer(data: StreamerAction):
    _delete_url_line(data.url.strip())
    return {"ok": True}


@app.patch("/api/streamers/toggle")
async def toggle_streamer(data: StreamerAction):
    url = data.url.strip()
    with runtime.file_update_lock:
        with open(_url_config_file, "r", encoding=text_encoding, errors="ignore") as f:
            lines = f.readlines()

        new_lines: list[str] = []
        toggled = False
        for line in lines:
            stripped = line.strip()
            if not toggled and url in stripped:
                if stripped.startswith("#"):
                    new_lines.append(line.lstrip("#"))
                else:
                    new_lines.append("#" + line)
                toggled = True
            else:
                new_lines.append(line)

        with open(_url_config_file, "w", encoding=text_encoding) as f:
            f.writelines(new_lines)

    return {"ok": True}


# ---------- Helpers ---------------------------------------------------------

def _parse_url_config() -> list[dict]:
    streamers: list[dict] = []
    with runtime.state_lock:
        running_snapshot = list(runtime.running_list)
        recording_url_snapshot = {
            entry[2]
            for entry in runtime.recording_time_list.values()
            if len(entry) >= 3 and entry[2]
        }
    try:
        with open(_url_config_file, "r", encoding=text_encoding, errors="ignore") as f:
            for line in f:
                stripped = line.strip()
                if len(stripped) < 18:
                    continue

                paused = stripped.startswith("#")
                if paused:
                    stripped = stripped.lstrip("#")

                parts = re.split("[,，]", stripped)
                if len(parts) < 2:
                    continue

                if re.search(r"https?://", parts[0]):
                    quality = "原画"
                    url = parts[0]
                    name = parts[1] if len(parts) > 1 else ""
                else:
                    quality = parts[0]
                    url = parts[1] if len(parts) > 1 else ""
                    name = parts[2] if len(parts) > 2 else ""

                is_recording = url in recording_url_snapshot
                is_monitoring = url in running_snapshot and not paused
                state = "已暂停" if paused else ("录制中" if is_recording else "监控中")

                streamers.append({
                    "url": url,
                    "quality": quality,
                    "name": name.replace("主播: ", "").strip(),
                    "paused": paused,
                    "is_recording": is_recording and not paused,
                    "is_monitoring": is_monitoring,
                    "state": state,
                })
    except FileNotFoundError:
        pass
    return streamers


def _delete_url_line(url: str) -> None:
    with runtime.file_update_lock:
        with open(_url_config_file, "r", encoding=text_encoding, errors="ignore") as f:
            lines = f.readlines()
        with open(_url_config_file, "w", encoding=text_encoding) as f:
            for line in lines:
                if url not in line:
                    f.write(line)


# ---------- Start -----------------------------------------------------------

def start_server(host: str = "0.0.0.0", port: int = 9527) -> None:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
