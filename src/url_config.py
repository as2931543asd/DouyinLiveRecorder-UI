# -*- encoding: utf-8 -*-
"""URL_config.ini 的解析与 worker 线程派发。

主循环每轮会调用 `load_and_dispatch`：
1. 读 ini，按注释 / 画质 / 自定义名拆分
2. 新出现的 URL 启动一个 `recorder.start_record` daemon 线程
3. 将 worker 抓到的最新主播名原子回填到配置
"""

from __future__ import annotations

import os
import random
import re
import threading
import time
from pathlib import Path

from . import live_history, platforms, runtime, utils
from .config_loader import QUALITY_CHOICES, Settings, text_encoding
from .logger import logger
from .recorder import start_record

def _webui_display_url() -> str:
    host = os.environ.get("WEBUI_HOST", "0.0.0.0")
    port = os.environ.get("WEBUI_PORT", "9527")
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    return f"http://{display_host}:{port}"


def _contains_url(s: str) -> bool:
    pattern = r"(https?://)?(www\.)?[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+(:\d+)?(/.*)?"
    return re.search(pattern, s) is not None


def _parse_line(line: str, default_quality: str) -> tuple[str, str, str] | None:
    """把一行 ini 拆成 (quality, url, name)；不合法时返回 None。"""
    if re.search("[,，]", line):
        parts = re.split("[,，]", line)
    else:
        parts = [line, ""]

    if len(parts) == 1:
        url, quality, name = parts[0], default_quality, ""
    elif len(parts) == 2:
        if _contains_url(parts[0]):
            quality, url, name = default_quality, parts[0], parts[1]
        else:
            quality, url, name = parts[0], parts[1], ""
    else:
        quality, url, name = parts[0], parts[1], parts[2]

    if quality not in QUALITY_CHOICES:
        quality = "原画"
    return quality, url, name


def _ensure_scheme(url: str) -> str:
    return url if "://" in url else "https://" + url


def _normalize_line(line: str, default_quality: str) -> str | None:
    """把手工简写行规范成 `画质,URL[,名称]`。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    parsed = _parse_line(stripped, default_quality)
    if not parsed:
        return None

    quality, url, name = parsed
    url = _ensure_scheme(url)
    normalized = f"{quality},{url}"
    if name:
        normalized += f",{name}"
    return normalized


def get_configured_urls(url_config_file: str, default_quality: str) -> list[str]:
    """读取全部受支持 URL（包含暂停项），供启动时初始化历史统计。"""
    urls: list[str] = []
    try:
        with runtime.file_update_lock:
            with open(url_config_file, "r", encoding=text_encoding, errors="ignore") as file:
                lines = list(file)
    except FileNotFoundError:
        return urls

    for origin_line in lines:
        line = origin_line.strip().lstrip("#").strip()
        parsed = _parse_line(line, default_quality) if line else None
        if not parsed:
            continue
        _, url, _ = parsed
        normalized_url = platforms.normalize_supported_url(url)
        if normalized_url and normalized_url not in urls:
            urls.append(normalized_url)
    return urls


def _apply_name_updates(
    url_config_file: str,
    updates: dict[str, str],
    default_quality: str,
) -> int:
    """按 URL 精确更新主播名，保留暂停状态并原子替换配置文件。"""
    if not updates:
        return 0

    target_updates = {
        platforms.normalize_supported_url(url) or _ensure_scheme(url).split("?")[0]:
        re.sub(r"[\x00-\x1f\x7f,，\u0085\u2028\u2029]", "_", name).strip()
        for url, name in updates.items()
    }
    changed = 0

    with runtime.file_update_lock:
        path = Path(url_config_file)
        try:
            lines = path.read_text(encoding=text_encoding).splitlines(keepends=True)
        except FileNotFoundError:
            return 0

        rewritten: list[str] = []
        for origin_line in lines:
            newline = "\n" if origin_line.endswith(("\n", "\r")) else ""
            stripped = origin_line.strip()
            paused = stripped.startswith("#")
            content = stripped.lstrip("#").strip() if paused else stripped
            parsed = _parse_line(content, default_quality) if content else None
            if not parsed:
                rewritten.append(origin_line)
                continue

            quality, url, old_name = parsed
            normalized_url = (
                platforms.normalize_supported_url(url)
                or _ensure_scheme(url).split("?")[0]
            )
            latest_name = target_updates.get(normalized_url)
            if latest_name is None:
                rewritten.append(origin_line)
                continue

            display_old_name = old_name.replace("主播:", "", 1).strip()
            if display_old_name == latest_name:
                rewritten.append(origin_line)
                continue

            prefix = "#" if paused else ""
            rewritten.append(f"{prefix}{quality},{normalized_url},主播: {latest_name}{newline}")
            changed += 1

        if changed:
            utils.atomic_write_text(path, "".join(rewritten), encoding=text_encoding)

    return changed


def _drain_name_updates(url_config_file: str, default_quality: str) -> None:
    with runtime.state_lock:
        # 每轮也拿当前抓取值校验一次，使用户或外部工具手工改回旧名后，
        # 不必等主播再次改名即可恢复监控名与录制目录的一致性。
        updates = dict(runtime.current_anchor_names)
        updates.update(runtime.pending_name_updates)
        runtime.pending_name_updates.clear()

    if not updates:
        return

    try:
        changed = _apply_name_updates(url_config_file, updates, default_quality)
        if changed:
            logger.info(f"已同步 {changed} 个主播的最新昵称到监控配置")
    except Exception:
        # 写入失败时放回队列，下一轮继续尝试，不要求重启监控进程。
        with runtime.state_lock:
            for url, name in updates.items():
                runtime.pending_name_updates.setdefault(url, name)
        raise


def parse_streamer_line(line: str) -> tuple[bool, str, str, str] | None:
    """共享的 WebUI/调度解析器；兼容旧画质列，但对外固定返回原画。"""
    stripped = line.strip()
    paused = stripped.startswith("#")
    content = stripped.lstrip("#").strip() if paused else stripped
    parsed = _parse_line(content, "原画") if content else None
    if parsed is None:
        return None
    _, url, name = parsed
    url = platforms.normalize_supported_url(url.strip())
    if url is None:
        return None
    return paused, "原画", url, name.strip()


def _load_url_snapshot(url_config_file: str) -> tuple[list[tuple], set[str]]:
    """读、去重、规范化、回写及停止标志发布共用一把锁，只提交完整版本。"""
    with runtime.file_update_lock:
        path = Path(url_config_file)
        content = path.read_text(encoding=text_encoding)
        active: list[tuple] = []
        seen: set[str] = set()
        paused_urls: list[str] = []
        rewritten: list[str] = []
        for line_number, original in enumerate(content.splitlines(keepends=True), 1):
            parsed = parse_streamer_line(original)
            if parsed is None:
                if len(original.strip()) >= 18 and not original.lstrip().startswith("#"):
                    logger.warning(f"监控配置第 {line_number} 行不是受支持的直播链接，已注释")
                    rewritten.append("#" + original)
                else:
                    rewritten.append(original)
                continue
            paused, quality, url, name = parsed
            if url in seen:
                continue
            seen.add(url)
            if name.count("主播: ") > 1:
                name = "主播: " + name.rsplit("主播: ", 1)[-1]
            name = re.sub(r"[\x00-\x1f\x7f\u0085\u2028\u2029]", "_", name)
            prefix = "#" if paused else ""
            rewritten.append(f"{prefix}{quality},{url}" + (f",{name}" if name else "") + "\n")
            if paused:
                paused_urls.append(url)
            else:
                active.append((quality, url, name))

        updated = "".join(rewritten)
        if updated != content:
            utils.atomic_write_text(path, updated, encoding=text_encoding)
        # WebUI 的暂停/恢复也在 file_update_lock 内更新这些标志，避免旧快照
        # 覆盖刚收到的操作。写入失败时不会到达此处，更不会误停录制任务。
        with runtime.state_lock:
            runtime.url_comments = paused_urls + [
                url for url in runtime.running_list if url not in seen
            ]
        return active, seen


def load_and_dispatch(
    url_config_file: str,
    settings: Settings,
    default_path: str,
    ini_url_content: str,
) -> None:
    try:
        _drain_name_updates(url_config_file, "原画")
        active_urls, seen_urls = _load_url_snapshot(url_config_file)
        initialized_count = live_history.initialize_missing(list(seen_urls))
        if initialized_count:
            logger.info(f"已为 {initialized_count} 个新增主播设置直播时间统计起点")

        # 只发布本轮完整快照；失败不会遗留一半的待启动列表。
        runtime.text_no_repeat_url = active_urls

        if runtime.text_no_repeat_url:
            pending_urls = [
                url_tuple for url_tuple in runtime.text_no_repeat_url
                if url_tuple[1] not in runtime.not_record_list and url_tuple[1] not in runtime.running_list
            ]
            if runtime.first_start and pending_urls:
                logger.info(
                    f"首次启动共需加载 {len(pending_urls)} 个直播间，启用缓启动以降低批量请求风控"
                )

            for url_tuple in runtime.text_no_repeat_url:
                with runtime.state_lock:
                    runtime.monitoring = len(runtime.running_list)
                    skip = (
                        runtime.exit_recording
                        or url_tuple[1] in runtime.not_record_list
                        or url_tuple[1] in runtime.url_comments
                    )
                    already_running = url_tuple[1] in runtime.running_list
                    if not skip and not already_running:
                        runtime.monitoring += 1
                        runtime.running_list.append(url_tuple[1])

                if skip or already_running:
                    continue

                tag = "传入" if runtime.first_start else "新增"
                logger.info(f"{tag}地址: {url_tuple[1]}")
                threading.Thread(
                    target=start_record,
                    args=(url_tuple, settings, default_path, runtime.monitoring),
                    daemon=True,
                ).start()
                if runtime.first_start:
                    base_delay = max(settings.local_delay_default, settings.startup_stagger_delay)
                    jitter_delay = max(0, settings.startup_stagger_jitter)
                    sleep_seconds = base_delay + random.randint(0, jitter_delay)
                    if sleep_seconds > 0:
                        logger.info(f"首次启动缓启动中，{sleep_seconds} 秒后继续加载下一位主播")
                        time.sleep(sleep_seconds)
                elif settings.local_delay_default > 0:
                    time.sleep(settings.local_delay_default)

        runtime.url_tuples_list = []
        runtime.first_start = False
    except Exception as err:
        logger.error(f"错误信息: {err} 发生错误的行数: {err.__traceback__.tb_lineno}")


def ensure_url_config_file(url_config_file: str) -> str:
    """仅首次启动创建缺失列表；热运行中丢失文件时不误当成删除全部任务。"""
    ini_url_content = ""
    try:
        with runtime.file_update_lock:
            path = Path(url_config_file)
            if not path.exists() and runtime.first_start:
                utils.atomic_write_text(path, "", encoding=text_encoding)
            ini_url_content = path.read_text(encoding=text_encoding).strip()
        if not ini_url_content:
            logger.warning(f"URL_config.ini 为空，请通过 WebUI ({_webui_display_url()}) 添加直播间地址")
    except (OSError, UnicodeError):
        logger.error("监控配置暂时不可读取，保留当前任务，请检查文件是否正在保存及编码")
    return ini_url_content


def deduplicate_file(url_config_file: str) -> None:
    with runtime.file_update_lock:
        utils.remove_duplicate_lines(url_config_file)
