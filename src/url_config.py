# -*- encoding: utf-8 -*-
"""URL_config.ini 的解析与 worker 线程派发。

主循环每轮会调用 `load_and_dispatch`：
1. 读 ini，按注释 / 画质 / 自定义名拆分
2. 新出现的 URL 启动一个 `recorder.start_record` daemon 线程
3. 回填已解析出的主播名（通过 `need_update_line_list`）
"""

from __future__ import annotations

import os
import re
import threading
import time

from . import runtime, utils
from .config_loader import QUALITY_CHOICES, Settings, text_encoding
from .file_ops import delete_line, update_file
from .logger import logger
from .recorder import start_record

_DOUYIN_HOSTS = {"live.douyin.com", "v.douyin.com", "www.douyin.com"}


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


def load_and_dispatch(
    url_config_file: str,
    settings: Settings,
    default_path: str,
    ini_url_content: str,
) -> None:
    line_list: list[str] = []
    url_line_list: list[str] = []
    new_url_comments: list[str] = []

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

        # 回填主播名
        while runtime.need_update_line_list:
            a = runtime.need_update_line_list.pop()
            replace_words = a.split("|")
            if replace_words[0] != replace_words[1]:
                if replace_words[1].startswith("#"):
                    start_with: str | None = "#"
                    new_word = replace_words[1][1:]
                else:
                    start_with = None
                    new_word = replace_words[1]
                update_file(
                    url_config_file, old_str=replace_words[0], new_str=new_word,
                    start_str=start_with, fallback_content=ini_url_content,
                )

        with runtime.state_lock:
            runtime.url_comments = new_url_comments

        runtime.text_no_repeat_url = list(set(runtime.url_tuples_list))

        if runtime.text_no_repeat_url:
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
            logger.warning("URL_config.ini 为空，请通过 WebUI (http://localhost:8000) 添加直播间地址")
    except OSError as err:
        logger.error(f"发生 I/O 错误: {err}")
    return ini_url_content


def deduplicate_file(url_config_file: str) -> None:
    utils.remove_duplicate_lines(url_config_file)
