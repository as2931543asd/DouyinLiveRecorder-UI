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

from . import live_history, runtime, utils
from .config_loader import QUALITY_CHOICES, Settings, text_encoding
from .file_ops import delete_line, update_file
from .logger import logger
from .recorder import start_record

_DOUYIN_HOSTS = {"live.douyin.com", "v.douyin.com", "www.douyin.com"}


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
    """读取全部有效抖音 URL（包含暂停项），供启动时初始化历史统计。"""
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
        url = _ensure_scheme(url)
        parts = url.split("/")
        host = parts[2] if len(parts) > 2 else ""
        if host in _DOUYIN_HOSTS:
            normalized_url = url.split("?")[0]
            if normalized_url not in urls:
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

    target_updates = {_ensure_scheme(url).split("?")[0]: name for url, name in updates.items()}
    changed = 0

    with runtime.file_update_lock:
        path = Path(url_config_file)
        try:
            lines = path.read_text(encoding=text_encoding, errors="ignore").splitlines(keepends=True)
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
            normalized_url = _ensure_scheme(url).split("?")[0]
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
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_text("".join(rewritten), encoding=text_encoding)
            os.replace(temporary, path)

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


def load_and_dispatch(
    url_config_file: str,
    settings: Settings,
    default_path: str,
    ini_url_content: str,
) -> None:
    line_list: list[str] = []
    url_line_list: list[str] = []
    new_url_comments: list[str] = []
    seen_urls: set[str] = set()

    try:
        with open(url_config_file, "r", encoding=text_encoding, errors="ignore") as f:
            for origin_line in f:
                if origin_line in line_list:
                    delete_line(url_config_file, origin_line)
                line_list.append(origin_line)

                line = origin_line.strip()
                if len(line) < 18:
                    continue

                # 去掉重复的"主播:"
                line_spilt = line.split("主播: ")
                if len(line_spilt) > 2:
                    line = update_file(
                        url_config_file, line, f"{line_spilt[0]}主播: {line_spilt[-1]}",
                        fallback_content=ini_url_content,
                    )

                is_comment_line = line.startswith("#")
                if is_comment_line:
                    line = line.lstrip("#")

                normalized_line = _normalize_line(line, settings.video_record_quality)
                if normalized_line and normalized_line != line:
                    line = update_file(
                        url_config_file,
                        old_str=line,
                        new_str=normalized_line,
                        fallback_content=ini_url_content,
                    )

                parsed = _parse_line(line, settings.video_record_quality)
                if not parsed:
                    continue
                quality, url, name = parsed

                if url not in url_line_list:
                    url_line_list.append(url)
                else:
                    delete_line(url_config_file, origin_line)

                url = _ensure_scheme(url)
                url_host = url.split("/")[2] if len(url.split("/")) > 2 else ""

                if url_host in _DOUYIN_HOSTS:
                    if url_host == "live.douyin.com":
                        url = update_file(
                            url_config_file, old_str=url, new_str=url.split("?")[0],
                            fallback_content=ini_url_content,
                        )

                    seen_urls.add(url)
                    new_url_comments = [i for i in new_url_comments if url not in i]
                    if is_comment_line:
                        new_url_comments.append(url)
                    else:
                        runtime.url_tuples_list.append((quality, url, name))
                else:
                    if not origin_line.startswith("#"):
                        logger.warning(
                            f"{origin_line.strip()} 不是抖音直播链接，此条跳过并已注释"
                        )
                        update_file(
                            url_config_file, old_str=origin_line, new_str=origin_line,
                            start_str="#", fallback_content=ini_url_content,
                        )

        initialized_count = live_history.initialize_missing(list(seen_urls))
        if initialized_count:
            logger.info(f"已为 {initialized_count} 个新增主播设置直播时间统计起点")

        _drain_name_updates(url_config_file, settings.video_record_quality)

        # 已从 ini 中整行删除的直播间：借用 url_comments 作为停止信号，
        # 让 worker 走既有的退出路径（clear_record_info 会摘掉 running_list）。
        with runtime.state_lock:
            running_snapshot = list(runtime.running_list)
        for u in running_snapshot:
            if u not in seen_urls and u not in new_url_comments:
                new_url_comments.append(u)

        with runtime.state_lock:
            runtime.url_comments = new_url_comments

        # dict 保序去重，确保首次缓启动遵循 URL_config 的可预期顺序。
        runtime.text_no_repeat_url = list(dict.fromkeys(runtime.url_tuples_list))

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
                    skip = url_tuple[1] in runtime.not_record_list
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
    """保证 URL_config.ini 存在，返回当前文件内容（用于更新失败时回滚）。"""
    ini_url_content = ""
    try:
        if os.path.isfile(url_config_file):
            with open(url_config_file, "r", encoding=text_encoding) as f:
                ini_url_content = f.read().strip()
        if not ini_url_content.strip():
            if not os.path.isfile(url_config_file):
                with open(url_config_file, "w", encoding=text_encoding) as f:
                    pass
            logger.warning(f"URL_config.ini 为空，请通过 WebUI ({_webui_display_url()}) 添加直播间地址")
    except OSError as err:
        logger.error(f"发生 I/O 错误: {err}")
    return ini_url_content


def deduplicate_file(url_config_file: str) -> None:
    utils.remove_duplicate_lines(url_config_file)
