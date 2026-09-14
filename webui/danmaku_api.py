"""检索接口使用同步路由在线程池执行，不阻塞状态面板的事件循环。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from src.danmaku_index import DanmakuIndex, IndexBusyError
from src.video_locator import VIDEO_EXTENSIONS, locate_video, safe_recording_path

router = APIRouter(prefix='/api/danmaku')
_index: DanmakuIndex | None = None
_probe_slots = threading.BoundedSemaphore(2)


def configure(root: Path, database: Path | None = None) -> None:
    global _index
    database = database or Path(__file__).resolve().parent.parent / '.cache/danmaku/index-v1.sqlite3'
    _index = DanmakuIndex(root, database)


def get_index() -> DanmakuIndex:
    if _index is None:
        raise HTTPException(503, '尚未配置录制目录')
    return _index


@router.get('/index')
def index_status():
    try:
        return get_index().status()
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, '无法读取索引，请检查缓存目录和磁盘空间') from exc


@router.post('/index/refresh')
def refresh_index():
    try:
        return get_index().refresh()
    except IndexBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, '无法更新索引，请检查录制目录、缓存目录和磁盘空间') from exc


@router.get('/search')
def search(
    q: str = Query('', max_length=500),
    scope: Literal['content', 'anchor', 'both', 'viewer', 'all'] = 'both',
    match: Literal['any', 'all'] = 'any',
    anchor: str = Query('', max_length=200), viewer: str = Query('', max_length=200),
    since_ms: int | None = Query(None, ge=0, le=253402300799999),
    until_ms: int | None = Query(None, ge=0, le=253402300799999),
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(50, ge=1, le=100),
    order: Literal['asc', 'desc'] = 'desc',
):
    try:
        return get_index().search(q=q, scope=scope, match=match, anchor=anchor, viewer=viewer,
                                  since_ms=since_ms, until_ms=until_ms, page=page,
                                  page_size=page_size, order=order)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, '索引暂不可用，请检查缓存目录或稍后重试') from exc


@router.get('/locate')
def locate(file: str = Query(..., max_length=2000), line: int = Query(..., ge=1)):
    index = get_index()
    try:
        result = index.get_chat(file, line)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, '索引暂不可用，请稍后重试') from exc
    if result is None:
        raise HTTPException(404, '弹幕不存在，请刷新索引后重新搜索')
    if not _probe_slots.acquire(blocking=False):
        raise HTTPException(429, '视频定位繁忙，请稍后重试')
    try:
        return locate_video(index.root, file, result['offset_ms'])
    except (OSError, ValueError) as exc:
        raise HTTPException(404, '录制文件不可访问，请刷新索引并检查文件是否已移动') from exc
    finally:
        _probe_slots.release()


@router.api_route('/media', methods=['GET', 'HEAD'])
def media(file: str = Query(..., max_length=2000), download: bool = False):
    try:
        path = safe_recording_path(get_index().root, file, VIDEO_EXTENSIONS)
    except (OSError, ValueError) as exc:
        raise HTTPException(404, '视频不存在或禁止访问') from exc
    media_types = {'.mp4': 'video/mp4', '.ts': 'video/mp2t', '.mkv': 'video/x-matroska', '.flv': 'video/x-flv'}
    # Starlette FileResponse supports Range/206 without loading the video into memory.
    return FileResponse(path, media_type=media_types[path.suffix.lower()], filename=path.name,
                        content_disposition_type='attachment' if download else 'inline',
                        headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})
