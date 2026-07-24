# -*- coding: utf-8 -*-
"""FFmpeg 检查与官方安装提示。

本项目在 Linux 服务器上通常由 systemd 托管运行。服务启动阶段不应该主动执行
apt/yum/brew 这类系统包管理操作，也不从第三方网盘下载二进制。因此这里只做
可预测的检查和官方安装提示。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from src.logger import logger

current_platform = platform.system()
project_dir = Path(__file__).resolve().parent

current_env_path = os.environ.get("PATH", "")
ffmpeg_path = str(project_dir / "ffmpeg")

FFMPEG_OFFICIAL_DOWNLOAD_PAGE = "https://ffmpeg.org/download.html"


def _ffmpeg_command_name() -> str:
    return "ffmpeg.exe" if current_platform == "Windows" else "ffmpeg"


def _project_ffmpeg_binary() -> Path:
    return Path(ffmpeg_path) / _ffmpeg_command_name()


def _path_with_project_ffmpeg() -> str:
    paths = [ffmpeg_path]
    if current_env_path:
        paths.append(current_env_path)
    return os.pathsep.join(paths)


def _refresh_process_path() -> None:
    os.environ["PATH"] = _path_with_project_ffmpeg()


def _probe_ffmpeg(command: str | Path = "ffmpeg") -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [str(command), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return False, ""
    except OSError as e:
        logger.error(f"FFmpeg 无法执行: {e}")
        return False, ""
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg 版本检查超时")
        return False, ""

    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    return result.returncode == 0 and bool(first_line), first_line


def _log_manual_install_hint() -> None:
    logger.error("未检测到可用的 FFmpeg，录制功能无法启动")
    logger.error(f"可将 FFmpeg 放到项目目录: {ffmpeg_path}")
    logger.error(f"官方下载入口: {FFMPEG_OFFICIAL_DOWNLOAD_PAGE}")

    if current_platform == "Linux":
        logger.error("Ubuntu/Debian: apt update && apt install ffmpeg")
        logger.error("CentOS/RHEL: yum install epel-release && yum install ffmpeg")
    elif current_platform == "Darwin":
        logger.error("macOS: brew install ffmpeg")
    elif current_platform == "Windows":
        logger.error("Windows: 从官方下载页选择 Windows builds，解压后将 ffmpeg.exe 放入项目的 ffmpeg 目录")
    else:
        logger.error(f"当前平台不支持自动安装: {current_platform}")


def _log_detected_ffmpeg(version_line: str) -> None:
    location = shutil.which("ffmpeg")
    if location:
        logger.info(f"FFmpeg 已就绪: {location}")
    logger.info(version_line)


def check_ffmpeg_installed() -> bool:
    _refresh_process_path()

    project_binary = _project_ffmpeg_binary()
    if project_binary.exists():
        ok, version_line = _probe_ffmpeg(project_binary)
        if ok:
            _log_detected_ffmpeg(version_line)
            return True
        logger.error(f"项目内 FFmpeg 存在但不可用: {project_binary}")

    ok, version_line = _probe_ffmpeg()
    if ok:
        _log_detected_ffmpeg(version_line)
        return True

    return False


def check_ffmpeg() -> bool:
    if check_ffmpeg_installed():
        return True
    _log_manual_install_hint()
    return False
