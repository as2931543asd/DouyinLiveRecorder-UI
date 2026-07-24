# -*- encoding: utf-8 -*-
"""主播直播历史的轻量持久化存储。

历史以直播间 URL 为稳定键。每场直播确认下播后原子写入 JSON，WebUI 因此
能在进程重启后继续显示“距上次直播”的时间，而不依赖内存或日志解析。
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from pathlib import Path
from typing import Any

from .logger import logger

_history_file: Path | None = None
_history: dict[str, dict[str, Any]] = {}
_history_lock = threading.Lock()


def init(history_file: str | Path) -> None:
    """设置历史文件并载入已有记录；文件不存在时保持空历史。"""
    global _history_file, _history

    path = Path(history_file)
    loaded: dict[str, dict[str, Any]] = {}
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                loaded = {
                    str(url): entry
                    for url, entry in raw.items()
                    if isinstance(url, str) and isinstance(entry, dict)
                }
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(f"读取直播历史失败，将从空历史继续: {exc}")

    with _history_lock:
        _history_file = path
        _history = loaded


def _isoformat(value: datetime.datetime) -> str:
    """保存为带本地时区偏移的秒级 ISO 时间，避免时区含义不明确。"""
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat(timespec="seconds")


def _write_locked() -> None:
    if _history_file is None:
        return

    _history_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = _history_file.with_name(f".{_history_file.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(_history, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, _history_file)
    except OSError as exc:
        logger.error(f"写入直播历史失败: {exc}")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def record_completed(
    url: str,
    anchor_name: str,
    started_at: datetime.datetime,
    ended_at: datetime.datetime | None = None,
) -> None:
    """记录一场已确认结束的直播及其持续时间。"""
    ended_at = ended_at or datetime.datetime.now().astimezone()
    if started_at.tzinfo is None:
        started_at = started_at.astimezone()
    if ended_at.tzinfo is None:
        ended_at = ended_at.astimezone()
    duration_seconds = max(0, int((ended_at - started_at).total_seconds()))

    with _history_lock:
        _history[url] = {
            "anchor_name": anchor_name,
            "last_started_at": _isoformat(started_at),
            "last_ended_at": _isoformat(ended_at),
            "last_duration_seconds": duration_seconds,
            "is_baseline": False,
        }
        _write_locked()


def initialize_missing(
    urls: set[str] | list[str] | tuple[str, ...],
    initialized_at: datetime.datetime | None = None,
) -> int:
    """给从未记录过直播的 URL 写入当前统计起点，已有历史保持不变。"""
    initialized_at = initialized_at or datetime.datetime.now().astimezone()
    timestamp = _isoformat(initialized_at)
    added = 0

    with _history_lock:
        for url in dict.fromkeys(urls):
            if not url or url in _history:
                continue
            _history[url] = {
                "anchor_name": "",
                "last_started_at": None,
                "last_ended_at": timestamp,
                "last_duration_seconds": None,
                "is_baseline": True,
            }
            added += 1
        if added:
            _write_locked()
    return added


def get_snapshot() -> dict[str, dict[str, Any]]:
    """返回可脱离锁使用的浅层快照。"""
    with _history_lock:
        return {url: dict(entry) for url, entry in _history.items()}


def remove(url: str) -> None:
    """删除已移除直播间的历史，避免文件长期积累无效条目。"""
    with _history_lock:
        if _history.pop(url, None) is not None:
            _write_locked()
