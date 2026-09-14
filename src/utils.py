# -*- coding: utf-8 -*-
import os
import shutil
import stat
import tempfile
from pathlib import Path
import functools
import hashlib
import re
import traceback
from typing import Any
from urllib.parse import parse_qs, urlparse
from collections import OrderedDict
from .logger import logger

OptionalStr = str | None


def trace_error_decorator(func: callable) -> callable:
    @functools.wraps(func)
    def wrapper(*args: list, **kwargs: dict) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_line = traceback.extract_tb(e.__traceback__)[-1].lineno
            error_info = f"message: type: {type(e).__name__}, {str(e)} in function {func.__name__} at line: {error_line}"
            logger.error(error_info)
            return []

    return wrapper


def check_md5(file_path: str | Path) -> str:
    with open(file_path, 'rb') as fp:
        file_md5 = hashlib.md5(fp.read()).hexdigest()
    return file_md5


def remove_emojis(text: str, replace_text: str = '') -> str:
    emoji_pattern = re.compile(
        "["
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F700-\U0001F77F"  # alchemical symbols
        "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
        "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
        "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
        "\U0001FA00-\U0001FA6F"  # Chess Symbols
        "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
        "\U00002702-\U000027B0"  # Dingbats
        "]+",
        flags=re.UNICODE
    )
    return emoji_pattern.sub(replace_text, text)


def atomic_write_text(file_path: str | Path, content: str, encoding: str = 'utf-8') -> None:
    """同目录写好并同步临时文件，再替换目标；调用者负责读改写事务的锁。"""
    path = Path(file_path).resolve()
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding=encoding, dir=path.parent,
            prefix=f'.{path.name}.', suffix='.tmp', delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def remove_duplicate_lines(file_path: str | Path) -> None:
    unique_lines = OrderedDict()
    text_encoding = 'utf-8-sig'
    with open(file_path, 'r', encoding=text_encoding) as input_file:
        original = input_file.read()
    for line in original.splitlines():
        unique_lines[line.strip()] = None
    content = ''.join(line + '\n' for line in unique_lines)
    if content != original:
        atomic_write_text(file_path, content, encoding=text_encoding)


def check_disk_capacity(file_path: str | Path, show: bool = False) -> float:
    absolute_path = os.path.abspath(file_path)
    directory = os.path.dirname(absolute_path)
    disk_usage = shutil.disk_usage(directory)
    disk_root = Path(directory).anchor
    free_space_gb = disk_usage.free / (1024 ** 3)
    if show:
        logger.info(
            f"{disk_root} Total: {disk_usage.total / (1024 ** 3):.2f} GB "
            f"Used: {disk_usage.used / (1024 ** 3):.2f} GB "
            f"Free: {free_space_gb:.2f} GB"
        )
    return free_space_gb


def handle_proxy_addr(proxy_addr):
    if proxy_addr:
        if not proxy_addr.startswith('http'):
            proxy_addr = 'http://' + proxy_addr
    else:
        proxy_addr = None
    return proxy_addr


def get_query_params(url: str, param_name: OptionalStr) -> dict | list[str]:
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)

    if param_name is None:
        return query_params
    else:
        values = query_params.get(param_name, [])
        return values
