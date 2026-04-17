# -*- encoding: utf-8 -*-

import datetime
import re
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# References to main module globals, set by init()
_main = None


def init(main_module):
    global _main
    _main = main_module


# ---------- Models ----------

class StreamerAdd(BaseModel):
    url: str
    quality: str = "原画"
    name: str = ""


class StreamerAction(BaseModel):
    url: str


# ---------- Pages ----------

@app.get("/")
async def index():
    return FileResponse(str(static_dir / "index.html"))


# ---------- API ----------

@app.get("/api/status")
async def get_status():
    now = datetime.datetime.now()
    with _main.state_lock:
        rec_snapshot = list(_main.recording)
        time_snapshot = {name: list(_main.recording_time_list.get(name, [])) for name in rec_snapshot}
        monitoring_count = len(_main.running_list)

    recordings = []
    for rec_name in rec_snapshot:
        entry = time_snapshot.get(rec_name)
        if entry and len(entry) >= 2:
            rt, qa = entry[0], entry[1]
            duration = str(now - rt).split('.')[0]
            recordings.append({"name": rec_name, "quality": qa, "duration": duration})
        else:
            recordings.append({"name": rec_name, "quality": "?", "duration": "0:00:00"})

    return {
        "monitoring": monitoring_count,
        "recording_count": len(recordings),
        "error_count": _main.error_count,
        "recordings": recordings,
    }


@app.get("/api/streamers")
async def get_streamers():
    return _parse_url_config()


@app.post("/api/streamers")
async def add_streamer(data: StreamerAdd):
    url = data.url.strip()
    if '://' not in url:
        url = 'https://' + url

    url_host = url.split('/')[2] if len(url.split('/')) > 2 else ''
    if url_host not in ('live.douyin.com', 'v.douyin.com', 'www.douyin.com'):
        return {"ok": False, "error": "仅支持抖音直播链接"}

    # Check duplicate
    for s in _parse_url_config():
        if s["url"] == url:
            return {"ok": False, "error": "该链接已存在"}

    line = f"{data.quality},{url}"
    if data.name:
        line += f",{data.name}"
    line += "\n"

    with _main.file_update_lock:
        with open(_main.url_config_file, "a", encoding=_main.text_encoding) as f:
            f.write(line)

    return {"ok": True}


@app.delete("/api/streamers")
async def delete_streamer(data: StreamerAction):
    url = data.url.strip()
    _delete_url_line(url)
    return {"ok": True}


@app.patch("/api/streamers/toggle")
async def toggle_streamer(data: StreamerAction):
    url = data.url.strip()
    with _main.file_update_lock:
        with open(_main.url_config_file, "r", encoding=_main.text_encoding, errors='ignore') as f:
            lines = f.readlines()

        new_lines = []
        toggled = False
        for line in lines:
            stripped = line.strip()
            if not toggled and url in stripped:
                if stripped.startswith('#'):
                    new_lines.append(line.lstrip('#'))
                else:
                    new_lines.append('#' + line)
                toggled = True
            else:
                new_lines.append(line)

        with open(_main.url_config_file, "w", encoding=_main.text_encoding) as f:
            f.writelines(new_lines)

    return {"ok": True}


# ---------- Helpers ----------

def _parse_url_config():
    streamers = []
    with _main.state_lock:
        running_snapshot = list(_main.running_list)
    try:
        with open(_main.url_config_file, "r", encoding=_main.text_encoding, errors='ignore') as f:
            for line in f:
                stripped = line.strip()
                if len(stripped) < 18:
                    continue

                paused = stripped.startswith('#')
                if paused:
                    stripped = stripped.lstrip('#')

                parts = re.split('[,，]', stripped)
                if len(parts) < 2:
                    continue

                def contains_url(s):
                    return re.search(r"https?://", s) is not None

                if contains_url(parts[0]):
                    quality = "原画"
                    url = parts[0]
                    name = parts[1] if len(parts) > 1 else ""
                else:
                    quality = parts[0]
                    url = parts[1] if len(parts) > 1 else ""
                    name = parts[2] if len(parts) > 2 else ""

                is_recording = any(url in rec for rec in running_snapshot)

                streamers.append({
                    "url": url,
                    "quality": quality,
                    "name": name.replace("主播: ", "").strip(),
                    "paused": paused,
                    "is_recording": is_recording and not paused,
                })
    except FileNotFoundError:
        pass
    return streamers


def _delete_url_line(url: str):
    with _main.file_update_lock:
        with open(_main.url_config_file, "r", encoding=_main.text_encoding, errors='ignore') as f:
            lines = f.readlines()
        with open(_main.url_config_file, "w", encoding=_main.text_encoding) as f:
            for line in lines:
                if url not in line:
                    f.write(line)


# ---------- Start ----------

def start_server(host="127.0.0.1", port=8000):
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
