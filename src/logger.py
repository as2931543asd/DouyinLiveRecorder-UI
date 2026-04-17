# -*- coding: utf-8 -*-
"""统一日志配置。

单一 logger 实例（loguru），两路输出：
- stderr：INFO 及以上，带颜色
- logs/app.log：DEBUG 及以上，按大小滚动，保留若干份

所有模块：`from src.logger import logger`。
"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | "
    "<level>{level: <7}</level> | "
    "<level>{message}</level>"
)
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | "
    "{name}:{function}:{line} - {message}"
)

logger.remove()

logger.add(
    sink=sys.stderr,
    format=_CONSOLE_FORMAT,
    level="INFO",
    colorize=True,
    enqueue=True,
)

logger.add(
    sink=str(_LOG_DIR / "app.log"),
    format=_FILE_FORMAT,
    level="DEBUG",
    rotation="10 MB",
    retention=5,
    encoding="utf-8",
    enqueue=True,
)

__all__ = ["logger"]
