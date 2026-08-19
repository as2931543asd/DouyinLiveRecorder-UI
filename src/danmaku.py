# -*- encoding: utf-8 -*-
"""抖音直播弹幕伴随录制。

从本地 ``douyinLive`` WebSocket 伴随服务读取 JSON，只把普通聊天消息写成
结构化 JSON Lines。网络、解析和文件错误都被限制在本模块内，不影响视频录制。
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TextIO
from urllib.parse import quote, unquote, urlsplit

import websockets

from .logger import logger

_CHAT_METHOD = "WebcastChatMessage"
_SCHEMA_NAME = "douyin-live-recorder-ui.danmaku"
_SCHEMA_VERSION = 1
_RECONNECT_DELAYS = (1, 2, 5, 10, 15)
_HEALTHY_CONNECTION_SECONDS = 30.0

# 这把锁只保护启动闸门和活动登记，绝不能在锁内执行文件 I/O。处于
# ``starting`` 状态的 recorder 也会先登记，因此 shutdown 可以立即关闭闸门、
# 向它发出停止请求；慢 ``writer.open()`` 返回后再按生命周期状态回滚。
_registry_lock = threading.RLock()
_active_recorders: set["DanmakuRecorder"] = set()
_shutdown_started = False
_shutdown_deadline: float | None = None


def _identifier(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip()
    return normalized or None


def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_mappings(child)


def extract_room_identifier(api_data: Mapping[str, Any] | None, record_url: str) -> str | None:
    """优先从直播 API 数据提取 web_rid，最后回退到 live.douyin.com 路径。"""
    mappings = list(_walk_mappings(api_data)) if api_data else []

    # owner.web_rid 是网页/API 返回中最稳定、最明确的直播间标识。
    for mapping in mappings:
        owner = mapping.get("owner")
        if isinstance(owner, Mapping):
            candidate = _identifier(owner.get("web_rid"))
            if candidate:
                return candidate

    # 部分接口把 web_rid 直接放在 room 对象或更外层。
    for mapping in mappings:
        candidate = _identifier(mapping.get("web_rid"))
        if candidate:
            return candidate

    try:
        parsed = urlsplit(record_url if "://" in record_url else f"https://{record_url}")
        if (parsed.hostname or "").rstrip(".").lower() != "live.douyin.com":
            return None
        path_parts = [unquote(part).strip() for part in parsed.path.split("/") if part.strip()]
        return _identifier(path_parts[-1]) if path_parts else None
    except (TypeError, ValueError):
        return None


def danmaku_jsonl_path(video_path: str | Path) -> Path:
    """返回与视频同基名的 JSONL；分段模板 ``_%03d`` 不进入文件名。"""
    path = Path(video_path)
    stem = path.stem
    if stem.endswith("_%03d"):
        stem = stem[: -len("_%03d")]
    return path.with_name(f"{stem}.danmaku.jsonl")


@dataclass(frozen=True)
class ChatMessage:
    nickname: str | None
    user_id: str | None
    content: str


@dataclass(frozen=True)
class _RawMessageResult:
    valid_business_message: bool = False
    room_not_found: bool = False


def _optional_text_field(mapping: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        if name not in mapping:
            continue
        value = mapping[name]
        if value is not None:
            return _utf8_text(str(value))
    return None


def _utf8_text(value: str) -> str:
    """替换 UTF-8 无法编码的孤立代理码位，保留可由 JSON 转义的控制字符。"""
    return "".join(
        "\ufffd" if "\ud800" <= char <= "\udfff" else char for char in value
    )


def _unix_ms(timestamp: float) -> int:
    """把 Unix 秒向下取整为整数毫秒。"""
    return math.floor(timestamp * 1000)


def _business_message(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping) or payload.get("type") == "system":
        return None

    # 兼容把业务消息放进 data/message 对象的轻量转发器。
    message: Mapping[str, Any] = payload
    if message.get("method") is None:
        for field in ("data", "message"):
            nested = message.get(field)
            if isinstance(nested, Mapping) and nested.get("method") is not None:
                message = nested
                break
    method = message.get("method")
    if not isinstance(method, str) or not method.strip():
        return None
    return message


def _is_room_not_found(payload: Any) -> bool:
    return (
        isinstance(payload, Mapping)
        and payload.get("type") == "system"
        and payload.get("event") == "live_status"
        and payload.get("code") == "ROOM_NOT_FOUND"
    )


def parse_chat_message(payload: Any) -> ChatMessage | None:
    """筛选并规范化伴随服务消息，兼容常见的字符串/数字用户 ID 字段。"""
    message = _business_message(payload)
    if message is None:
        return None
    if message.get("method") != _CHAT_METHOD:
        return None

    raw_user = message.get("user")
    if raw_user is None:
        user: Mapping[str, Any] = {}
    elif isinstance(raw_user, Mapping):
        user = raw_user
    else:
        return None

    nickname = _optional_text_field(user, "nickname", "nick_name", "display_id")
    user_id = _optional_text_field(user, "id", "id_str", "sec_uid", "short_id")
    content = _optional_text_field(message, "content", "default_content")
    if content is None:
        return None
    return ChatMessage(nickname=nickname, user_id=user_id, content=content)


class DanmakuJsonlWriter:
    """线程安全、逐行落盘的结构化弹幕 JSONL 写入器。"""

    def __init__(
        self,
        path: str | Path,
        room_identifier: str,
        started_at: float,
        started_monotonic: float | None = None,
    ) -> None:
        self.path = Path(path)
        self.room_identifier = _utf8_text(str(room_identifier))
        self.started_at_unix_ms = _unix_ms(started_at)
        self.started_monotonic = (
            time.monotonic() if started_monotonic is None else started_monotonic
        )
        self._file: TextIO | None = None
        self._sequence = 0
        self._lock = threading.RLock()

    @staticmethod
    def _encode_line(record: Mapping[str, Any]) -> str:
        return json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"

    def open(self) -> None:
        with self._lock:
            if self._file is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            jsonl_file = self.path.open(
                "x", encoding="utf-8", buffering=1, newline="\n"
            )
            try:
                metadata = {
                    "type": "metadata",
                    "schema": _SCHEMA_NAME,
                    "version": _SCHEMA_VERSION,
                    "platform": "douyin",
                    "room_identifier": self.room_identifier,
                    "recording_started_at_unix_ms": self.started_at_unix_ms,
                    "time_units": {
                        "recording_started_at_unix_ms": "milliseconds since Unix epoch UTC",
                        "received_at_unix_ms": "milliseconds since Unix epoch UTC",
                        "offset_ms": "milliseconds from recording start",
                    },
                }
                jsonl_file.write(self._encode_line(metadata))
                jsonl_file.flush()
            except Exception:
                jsonl_file.close()
                raise
            self._file = jsonl_file

    def write(
        self,
        message: ChatMessage,
        received_at: float | None = None,
        received_monotonic: float | None = None,
    ) -> None:
        if received_at is None:
            timestamp = time.time()
            if received_monotonic is None:
                received_monotonic = time.monotonic()
        else:
            timestamp = received_at
        received_at_unix_ms = _unix_ms(timestamp)
        if received_monotonic is None:
            # 显式 wall-clock 时间保留给确定性测试和离线调用路径。
            offset_ms = max(0, received_at_unix_ms - self.started_at_unix_ms)
        else:
            offset_ms = max(
                0,
                math.floor(
                    (received_monotonic - self.started_monotonic) * 1000
                ),
            )
        with self._lock:
            if self._file is None:
                raise RuntimeError("弹幕 JSONL 尚未打开或已经关闭")
            sequence = self._sequence + 1
            record = {
                "type": "chat",
                "sequence": sequence,
                "method": _CHAT_METHOD,
                "offset_ms": offset_ms,
                "received_at_unix_ms": received_at_unix_ms,
                "user": {
                    "id": (
                        None
                        if message.user_id is None
                        else _utf8_text(message.user_id)
                    ),
                    "nickname": (
                        None
                        if message.nickname is None
                        else _utf8_text(message.nickname)
                    ),
                },
                "content": _utf8_text(message.content),
            }
            self._file.write(self._encode_line(record))
            self._file.flush()
            self._sequence = sequence

    def close(self, deadline: float | None = None) -> bool:
        """刷新并关闭 JSONL；指定 deadline 时只在期限内等待 writer 锁。

        返回 ``False`` 表示未能及时取得锁，文件保持打开，供仍存活的弹幕线程
        在退出路径中再次收尾。JSONL 不需要写文件尾。未指定 deadline 的调用
        保留正常的阻塞关闭语义。
        """
        if deadline is None:
            acquired = self._lock.acquire()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                acquired = self._lock.acquire(blocking=False)
            else:
                acquired = self._lock.acquire(timeout=remaining)
        if not acquired:
            return False

        try:
            if self._file is None:
                return True
            jsonl_file = self._file
            self._file = None
            try:
                jsonl_file.flush()
            finally:
                jsonl_file.close()
            return True
        finally:
            self._lock.release()


class DanmakuRecorder:
    """在独立 daemon 线程中连接本地伴随服务并维护单次录制的 JSONL。"""

    def __init__(
        self,
        room_identifier: str,
        service_url: str,
        jsonl_path: str | Path,
        started_at: float,
        started_monotonic: float | None = None,
    ) -> None:
        self.room_identifier = room_identifier
        self.service_url = service_url.strip().rstrip("/")
        self.jsonl_path = Path(jsonl_path)
        self.writer = DanmakuJsonlWriter(
            self.jsonl_path,
            room_identifier=room_identifier,
            started_at=started_at,
            started_monotonic=started_monotonic,
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_state = "new"
        self._stop_deadline: float | None = None
        self._state_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._leave_writer_open = False

    @property
    def websocket_url(self) -> str:
        base = self.service_url
        if not base.endswith("/ws"):
            base += "/ws"
        return f"{base}/{quote(self.room_identifier, safe='')}"

    def start(self) -> None:
        """先写入 JSONL metadata，再异步连接；任何失败只写日志。"""
        # 先以 ``starting`` 状态登记，再离开全局锁做所有文件 I/O。这样 shutdown
        # 能看到正在慢 open 的实例，同时不会被 mkdir/open/metadata write 阻塞。
        with _registry_lock:
            with self._lifecycle_lock:
                if (
                    _shutdown_started
                    or self._stop_event.is_set()
                    or self._lifecycle_state != "new"
                ):
                    return
                self._lifecycle_state = "starting"
                _active_recorders.add(self)

        try:
            self.writer.open()
        except Exception as exc:
            with _registry_lock:
                with self._lifecycle_lock:
                    self._lifecycle_state = "stopped"
                    _active_recorders.discard(self)
            logger.error(f"弹幕 JSONL 创建失败，视频录制继续: {self.jsonl_path} ({exc})")
            return

        thread = threading.Thread(
            target=self._thread_main,
            name=f"danmaku-{self.room_identifier}",
            daemon=True,
        )
        rollback = False
        close_deadline: float | None = None
        thread_start_error: Exception | None = None

        # open 完成后重新在线性化点检查 shutdown/stop。只有检查通过的实例才会
        # 发布并启动网络线程；shutdown 无法在检查与 thread.start 之间漏掉它。
        with _registry_lock:
            with self._lifecycle_lock:
                if (
                    _shutdown_started
                    or self._stop_event.is_set()
                    or self._lifecycle_state != "starting"
                ):
                    rollback = True
                    close_deadline = self._stop_deadline
                    if _shutdown_deadline is not None:
                        close_deadline = (
                            _shutdown_deadline
                            if close_deadline is None
                            else min(close_deadline, _shutdown_deadline)
                        )
                    self._lifecycle_state = "stopped"
                    _active_recorders.discard(self)
                else:
                    self._thread = thread
                    try:
                        thread.start()
                    except Exception as exc:
                        thread_start_error = exc
                        self._thread = None
                        self._lifecycle_state = "stopped"
                        _active_recorders.discard(self)
                    else:
                        self._lifecycle_state = "running"

        if rollback:
            self._close_writer(deadline=close_deadline)
            return

        if thread_start_error is not None:
            self._close_writer()
            logger.error(f"弹幕线程启动失败，视频录制继续: {thread_start_error}")

    def stop(self, timeout: float = 3.0) -> None:
        """取消网络任务并在同一个绝对期限内等待和关闭 JSONL。"""
        deadline = time.monotonic() + max(0.0, timeout)
        self._request_stop(deadline)
        self._wait_and_close(deadline)

    def _request_stop(self, deadline: float | None = None) -> None:
        """只发取消、不等待，供单路和批量关闭共用。"""
        # Event 先置位，既不需要全局锁，也不会被慢 writer.open()/write() 阻塞。
        self._stop_event.set()
        with self._lifecycle_lock:
            if deadline is not None and (
                self._stop_deadline is None or deadline < self._stop_deadline
            ):
                self._stop_deadline = deadline
            if self._lifecycle_state in {"new", "starting", "running"}:
                self._lifecycle_state = "stopping"
        with self._state_lock:
            loop = self._loop
            task = self._task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                return

    def _wait_and_close(self, deadline: float) -> None:
        """只在绝对期限内等待；活线程自行负责稍后的 JSONL 收尾。"""
        with self._lifecycle_lock:
            thread = self._thread
        thread_alive = thread is threading.current_thread()
        try:
            if thread is not None and not thread_alive:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                thread_alive = thread.is_alive()

            if thread_alive:
                logger.warning(
                    "弹幕线程未在关闭期限内退出，未强制抢占 JSONL 写锁；"
                    f"线程退出时将自行关闭，视频录制或进程退出继续: {self.jsonl_path}"
                )
            else:
                self._close_writer(deadline=deadline)
        finally:
            with _registry_lock:
                _active_recorders.discard(self)

    def record_raw_message(
        self, raw_message: str | bytes, received_at: float | None = None
    ) -> _RawMessageResult:
        """解析一帧 JSON；数组帧也逐项处理，非法内容只记日志。"""
        valid_business_message = False
        room_not_found = False
        try:
            payload = json.loads(raw_message)
        except Exception as exc:
            logger.warning(f"弹幕消息解析或写入失败，已跳过且视频录制继续: {exc}")
            return _RawMessageResult()

        items = payload if isinstance(payload, list) else (payload,)
        for item in items:
            try:
                room_not_found = room_not_found or _is_room_not_found(item)
                if _business_message(item) is not None:
                    valid_business_message = True
                message = parse_chat_message(item)
                if message is not None:
                    self.writer.write(message, received_at=received_at)
            except Exception as exc:
                logger.warning(
                    f"弹幕消息解析或写入失败，已跳过且视频录制继续: {exc}"
                )
        return _RawMessageResult(
            valid_business_message=valid_business_message,
            room_not_found=room_not_found,
        )

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(self._consume())
        with self._state_lock:
            self._loop = loop
            self._task = task
        if self._stop_event.is_set():
            task.cancel()

        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"弹幕线程异常退出，视频录制不受影响: {exc}")
        finally:
            with self._state_lock:
                self._task = None
                self._loop = None
            loop.close()
            leave_open_for_later_stop = (
                self._leave_writer_open and not self._stop_event.is_set()
            )
            if not leave_open_for_later_stop:
                self._close_writer()
            with self._lifecycle_lock:
                self._lifecycle_state = (
                    "network-stopped" if leave_open_for_later_stop else "stopped"
                )

    async def _consume(self) -> None:
        failures = 0
        while not self._stop_event.is_set():
            connected_at: float | None = None
            try:
                # websockets 15 会读取系统代理；本地服务必须显式禁用代理。
                async with websockets.connect(
                    self.websocket_url,
                    proxy=None,
                    open_timeout=5,
                    close_timeout=1,
                    ping_interval=20,
                    ping_timeout=20,
                ) as websocket:
                    connected_at = self._monotonic()
                    logger.info(f"弹幕伴随服务已连接: {self.room_identifier}")
                    async for raw_message in websocket:
                        if self._stop_event.is_set():
                            break
                        result = self.record_raw_message(raw_message)
                        if result.valid_business_message:
                            failures = 0
                        if result.room_not_found:
                            self._leave_writer_open = True
                            logger.warning(
                                f"弹幕伴随服务报告直播间不存在 ({self.room_identifier})，"
                                "本轮停止弹幕网络重连，视频录制继续"
                            )
                            return

                if not self._stop_event.is_set():
                    raise ConnectionError("伴随服务连接已关闭")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                if self._leave_writer_open:
                    return
                if (
                    connected_at is not None
                    and self._monotonic() - connected_at
                    >= _HEALTHY_CONNECTION_SECONDS
                ):
                    failures = 0
                delay = _RECONNECT_DELAYS[min(failures, len(_RECONNECT_DELAYS) - 1)]
                failures += 1
                logger.warning(
                    f"弹幕伴随服务连接失败 ({self.room_identifier}): {exc}; "
                    f"视频录制继续，{delay} 秒后重连"
                )
                await self._retry_sleep(delay)

    @staticmethod
    def _monotonic() -> float:
        return time.monotonic()

    async def _retry_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def _close_writer(self, deadline: float | None = None) -> bool:
        try:
            closed = self.writer.close(deadline=deadline)
        except Exception as exc:
            logger.error(
                f"弹幕 JSONL 关闭失败，视频录制不受影响: {self.jsonl_path} ({exc})"
            )
            return False
        if not closed:
            logger.warning(
                "弹幕 JSONL 写锁未能在关闭期限内取得，已跳过同步关闭以免阻塞；"
                f"弹幕线程或启动线程稍后将再次收尾: {self.jsonl_path}"
            )
        return closed


def stop_all_active_recorders(timeout: float = 3.0) -> None:
    """永久禁止新弹幕启动，并在一个共享超时内关闭全部活动 recorder。"""
    global _shutdown_deadline, _shutdown_started

    requested_deadline = time.monotonic() + max(0.0, timeout)
    with _registry_lock:
        _shutdown_started = True
        if _shutdown_deadline is None or requested_deadline < _shutdown_deadline:
            _shutdown_deadline = requested_deadline
        deadline = _shutdown_deadline
        recorders = tuple(_active_recorders)

    # 必须先统一发取消，避免等待第一个线程时其它 recorder 仍在收消息。
    for recorder in recorders:
        try:
            recorder._request_stop(deadline)
        except Exception as exc:
            logger.error(f"进程关闭时取消弹幕录制失败: {recorder.jsonl_path} ({exc})")

    for recorder in recorders:
        try:
            recorder._wait_and_close(deadline)
        except Exception as exc:
            # shutdown 不能因单个 recorder 的异常阻止其它 JSONL 收尾或进程退出。
            logger.error(f"进程关闭时停止弹幕录制失败: {recorder.jsonl_path} ({exc})")
            with _registry_lock:
                _active_recorders.discard(recorder)
