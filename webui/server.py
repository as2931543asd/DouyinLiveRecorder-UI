# -*- encoding: utf-8 -*-
"""FastAPI 控制面板后端。

读写策略：
- 读共享运行时状态（recording / recording_time_list / running_list）
  必须先在 `runtime.state_lock` 下拿 snapshot，再释放锁处理。
- 写 URL_config.ini 统一走 `runtime.file_update_lock`。
"""

from __future__ import annotations

import datetime
import re
import threading
from pathlib import Path

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


def init(url_config_file: str) -> None:
    """由 main.py 调用，告知 ini 路径。"""
    global _url_config_file
    _url_config_file = url_config_file


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

    return {
        "monitoring": monitoring_count,
        "recording_count": len(recordings),
        "error_count": runtime.error_count,
        "max_request": runtime.max_request,
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

def start_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
