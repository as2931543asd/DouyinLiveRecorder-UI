# -*- encoding: utf-8 -*-
"""URL_config.ini 行级编辑 + 配置文件定时备份。

行级写操作统一走 `runtime.file_update_lock`，避免和 WebUI 的写入冲突。
"""

from __future__ import annotations

import datetime
import os
import shutil
import time

from . import runtime, utils
from .config_loader import text_encoding
from .logger import logger


def update_file(
    file_path: str,
    old_str: str,
    new_str: str,
    start_str: str | None = None,
    fallback_content: str = "",
) -> str | None:
    if old_str == new_str and start_str is None:
        return old_str
    with runtime.file_update_lock:
        file_data: list[str] = []
        with open(file_path, "r", encoding=text_encoding) as f:
            try:
                for text_line in f:
                    if old_str in text_line:
                        text_line = text_line.replace(old_str, new_str)
                        if start_str:
                            text_line = f"{start_str}{text_line}"
                    if text_line not in file_data:
                        file_data.append(text_line)
            except RuntimeError as e:
                logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                if fallback_content:
                    with open(file_path, "w", encoding=text_encoding) as f2:
                        f2.write(fallback_content)
                    return old_str
        if file_data:
            with open(file_path, "w", encoding=text_encoding) as f:
                f.write("".join(file_data))
        return new_str


def delete_line(file_path: str, del_line: str, delete_all: bool = False) -> None:
    with runtime.file_update_lock:
        with open(file_path, "r+", encoding=text_encoding) as f:
            lines = f.readlines()
            f.seek(0)
            f.truncate()
            skip_line = False
            for txt_line in lines:
                if del_line in txt_line:
                    if delete_all or not skip_line:
                        skip_line = True
                        continue
                else:
                    skip_line = False
                f.write(txt_line)


def backup_file(file_path: str, backup_dir_path: str, limit_counts: int = 6) -> None:
    try:
        if not os.path.exists(backup_dir_path):
            os.makedirs(backup_dir_path)

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_file_name = os.path.basename(file_path) + "_" + timestamp
        backup_file_path = os.path.join(backup_dir_path, backup_file_name).replace("\\", "/")
        shutil.copy2(file_path, backup_file_path)

        files = os.listdir(backup_dir_path)
        _files = [f for f in files if f.startswith(os.path.basename(file_path))]
        _files.sort(key=lambda x: os.path.getmtime(os.path.join(backup_dir_path, x)))

        while len(_files) > limit_counts:
            oldest_file = _files[0]
            os.remove(os.path.join(backup_dir_path, oldest_file))
            _files = _files[1:]
    except Exception as e:
        logger.error(f"备份配置文件 {file_path} 失败：{e}")


def backup_file_loop(config_file: str, url_config_file: str, backup_dir: str) -> None:
    """每 10 分钟对比 md5，若变更则备份一次。作为 daemon 线程启动。"""
    config_md5 = ""
    url_config_md5 = ""

    while True:
        try:
            if os.path.exists(config_file):
                new_md5 = utils.check_md5(config_file)
                if new_md5 != config_md5:
                    backup_file(config_file, backup_dir)
                    config_md5 = new_md5

            if os.path.exists(url_config_file):
                new_md5 = utils.check_md5(url_config_file)
                if new_md5 != url_config_md5:
                    backup_file(url_config_file, backup_dir)
                    url_config_md5 = new_md5
            time.sleep(600)
        except Exception as e:
            logger.error(f"备份配置文件失败, 错误信息: {e}")
