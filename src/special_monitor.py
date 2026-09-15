# -*- encoding: utf-8 -*-
"""特别监控的订阅、开播去重及待发事件；不在录制线程里执行网络推送。"""
from __future__ import annotations

import copy
import datetime
import json
import threading
import uuid
from pathlib import Path

from .utils import atomic_write_text

_lock = threading.Lock()
_path: Path | None = None
_entries: dict[str, dict] = {}
EVENT_TTL_SECONDS = 3600


def init(path: str | Path) -> None:
    global _path, _entries
    path = Path(path)
    with _lock:
        entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(entries, dict) or any(
            not isinstance(url, str) or not isinstance(entry, dict)
            or type(entry.get("live")) is not bool
            or (entry.get("pending") is not None and not isinstance(entry["pending"], dict))
            for url, entry in entries.items()
        ):
            raise ValueError("特别监控配置无效，请检查 special_monitor.json")
        _path, _entries = path, entries


def _commit(entries: dict[str, dict]) -> None:
    global _entries
    if _path is None:
        raise OSError("特别监控尚未初始化")
    atomic_write_text(_path, json.dumps(entries, ensure_ascii=False, indent=2) + "\n")
    _entries = entries


def watched_urls() -> set[str]:
    with _lock:
        return set(_entries)


def _event(url: str, name: str, title: str = "") -> dict:
    return {
        "id": uuid.uuid4().hex,
        "type": "live.started",
        "occurred_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"url": url, "name": str(name)[:200], "title": str(title or "")[:200]},
        "acked_by": [],
    }


def set_enabled(url: str, enabled: bool, *, is_live: bool = False, name: str = "") -> None:
    with _lock:
        if (url in _entries) == enabled:
            return
        updated = copy.deepcopy(_entries)
        if enabled:
            # 已经在直播时开启，也提醒一次，不必等阻塞中的 ffmpeg 结束。
            updated[url] = {"live": is_live, "pending": _event(url, name) if is_live else None}
        else:
            updated.pop(url, None)
        _commit(updated)


def observe(url: str, is_live: bool, name: str, title: str = "") -> None:
    """仅传入成功抓取的直播状态；断流/抓取错误/worker 退出不是下播。"""
    with _lock:
        entry = _entries.get(url)
        if entry is None or entry["live"] == is_live:
            return
        updated = copy.deepcopy(_entries)
        updated[url]["live"] = is_live
        if is_live:
            updated[url]["pending"] = _event(url, name, title)
        _commit(updated)


def cancel_pending(url: str) -> None:
    """暂停时取消尚未发送的提醒，但保留订阅与本场直播去重状态。"""
    with _lock:
        if not _entries.get(url, {}).get("pending"):
            return
        updated = copy.deepcopy(_entries)
        updated[url]["pending"] = None
        _commit(updated)


def pending_events(active_urls: set[str], consumer: str) -> list[dict]:
    now = datetime.datetime.now().astimezone()
    events = []
    with _lock:
        for url, entry in _entries.items():
            event = entry.get("pending")
            if url not in active_urls or not event or consumer in event.get("acked_by", []):
                continue
            try:
                age = (now - datetime.datetime.fromisoformat(event["occurred_at"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= age <= EVENT_TTL_SECONDS:
                events.append(copy.deepcopy({key: value for key, value in event.items() if key != "acked_by"}))
    return sorted(events, key=lambda event: event["occurred_at"])


def acknowledge(event_id: str, consumer: str) -> None:
    """按消费者幂等确认；各适配器独立，迟到的 ACK 不影响新一场直播。"""
    with _lock:
        for url, entry in _entries.items():
            event = entry.get("pending")
            if event and event.get("id") == event_id:
                if consumer in event.get("acked_by", []):
                    return
                updated = copy.deepcopy(_entries)
                updated[url]["pending"].setdefault("acked_by", []).append(consumer)
                _commit(updated)
                return
