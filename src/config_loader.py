# -*- encoding: utf-8 -*-
"""仅在 config.ini 变化时重载；校验成功后整体发布，错误时保留上一份配置。"""
from __future__ import annotations

import configparser
import io
import math
from pathlib import Path
from typing import Any

from .logger import logger
from .utils import atomic_write_text

text_encoding = "utf-8-sig"
options = {"是": True, "否": False}
VIDEO_SAVE_TYPES = ("FLV", "MKV", "TS", "MP4")
# 仅为兼容旧版 URL_config.ini 的画质列；实际录制固定请求原画。
QUALITY_CHOICES = ("原画", "蓝光", "超清", "高清", "标清", "流畅")


class Settings:
    def __init__(self, config_file: str) -> None:
        self.config_file = config_file
        self._values: dict[str, Any] | None = None
        self._last_signature: tuple | None = None
        self._rejected_signature: tuple | None = None
        self._last_error: str | None = None
        self.reload()

    def __getattr__(self, name: str) -> Any:
        values = self.__dict__.get("_values")
        if values is not None and name in values:
            return values[name]
        raise AttributeError(name)

    @staticmethod
    def _signature(path: Path) -> tuple:
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    @staticmethod
    def _parse(parser: configparser.RawConfigParser) -> dict[str, Any]:
        for section in ("录制设置", "Cookie"):
            if not parser.has_section(section):
                parser.add_section(section)

        def get(option: str, default: Any, section: str = "录制设置") -> str:
            if not parser.has_option(section, option):
                parser.set(section, option, str(default))
            return parser.get(section, option).strip()

        def boolean(option: str, default: str) -> bool:
            raw = get(option, default)
            if raw not in options:
                raise ValueError(f"[录制设置] {option} 必须填写“是”或“否”")
            return options[raw]

        def integer(option: str, default: int, minimum: int = 1) -> int:
            try:
                value = int(get(option, default))
            except ValueError:
                raise ValueError(f"[录制设置] {option} 必须是整数（不带单位）") from None
            if value < minimum:
                raise ValueError(f"[录制设置] {option} 不能小于 {minimum}")
            return value

        def number(option: str, default: float) -> float:
            try:
                value = float(get(option, default))
            except ValueError:
                raise ValueError(f"[录制设置] {option} 必须是数字（不带单位）") from None
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"[录制设置] {option} 必须是有限的非负数")
            return value

        save_type = get("视频保存格式ts|mkv|flv|mp4", "ts").upper()
        if save_type not in VIDEO_SAVE_TYPES:
            raise ValueError("[录制设置] 视频保存格式必须是 TS、MKV、FLV 或 MP4")
        use_proxy = boolean("是否使用代理ip(是/否)", "是")
        proxy_addr = get("代理地址", "")
        file_ready_webhook = get("转码完成回调地址", "")
        if file_ready_webhook and not file_ready_webhook.startswith(("http://", "https://")):
            raise ValueError("[录制设置] 转码完成回调地址必须以 http:// 或 https:// 开头，留空表示不回调")
        converts_to_mp4 = boolean("录制完成后自动转为mp4格式", "否")
        record_to_temp_dir = boolean("录制中文件放到TempRec子目录", "否")
        # 只有转出的 MP4 会移出 TempRec；不转码的格式会一直留在里面。
        if record_to_temp_dir and not (converts_to_mp4 and save_type in {"TS", "FLV"}):
            raise ValueError("[录制设置] 录制中文件放到TempRec子目录 需要保存格式为 TS 或 FLV，并开启录制完成后自动转为mp4格式")
        return {
            "video_save_path": get("直播保存路径(不填则默认)", ""),
            "folder_by_author": boolean("保存文件夹是否以作者区分", "是"),
            "folder_by_time": boolean("保存文件夹是否以时间区分", "否"),
            "folder_by_title": boolean("保存文件夹是否以标题区分", "否"),
            "filename_by_title": boolean("保存文件名是否包含标题", "否"),
            "clean_emoji": boolean("是否去除名称中的表情符号", "是"),
            "record_danmaku": boolean("是否录制弹幕", "否"),
            "danmaku_service_url": get("弹幕服务地址", "ws://127.0.0.1:1088") or "ws://127.0.0.1:1088",
            "video_save_type": save_type,
            # 不再读取或生成画质配置项，旧配置中的低画质也不再生效。
            "video_record_quality": "原画",
            "use_proxy": use_proxy,
            "proxy_addr": (proxy_addr or None) if use_proxy else None,
            "max_request": integer("同一时间访问网络的线程数", 3),
            "delay_default": integer("循环时间(秒)", 120),
            "local_delay_default": integer("排队读取网址时间(秒)", 0, 0),
            "startup_stagger_delay": integer("首次启动排队读取网址时间(秒)", 3, 0),
            "startup_stagger_jitter": integer("首次启动随机抖动时间(秒)", 2, 0),
            "loop_time": boolean("是否显示循环秒数", "否"),
            "show_url": boolean("是否显示直播源地址", "否"),
            "disk_space_limit": number("录制空间剩余阈值(gb)", 1.0),
            "converts_to_mp4": converts_to_mp4,
            "record_to_temp_dir": record_to_temp_dir,
            "converts_to_h264": boolean("mp4格式重新编码为h264", "否"),
            "delete_origin_file": boolean("追加格式后删除原文件", "否"),
            "file_ready_webhook": file_ready_webhook,
            "split_video_by_time": boolean("分段录制是否开启", "否"),
            "split_time": str(integer("视频分段时间(秒)", 1800)),
            "auto_restart_on_corrupt": boolean("自动检测花屏并重录", "是"),
            "corrupt_error_threshold": integer("花屏检测错误阈值", 8),
            "corrupt_error_window_seconds": integer("花屏检测时间窗口(秒)", 20),
            "corrupt_restart_delay": integer("花屏重录冷却时间(秒)", 3, 0),
            "corrupt_probe_decode": boolean("花屏检测是否旁路解码验证", "否"),
            "fast_reconnect_delay": integer("断流首次快速重连时间(秒)", 15),
            "reconnect_backoff_max": integer("连续断流退避上限(秒)", 120),
            "fetch_backoff_max": integer("连续抓取失败退避上限(秒)", 300),
            "stable_record_reset_seconds": integer("稳定录制后重置退避时间(秒)", 300),
            "dy_cookie": get("抖音cookie", "", "Cookie"),
            "xhs_cookie": get("小红书cookie", "", "Cookie"),
        }

    def reload(self) -> bool:
        """成功发布新快照返回 True；未变化或无效的热更新返回 False。

        首次启动没有可回退的快照，错误时明确拒绝启动，绝不猜测保存目录。
        运行期间不会回写用户配置；仅首次启动遇到空/不存在文件时生成模板。
        """
        path = Path(self.config_file)
        try:
            try:
                signature = self._signature(path)
            except FileNotFoundError:
                if self._values is not None:
                    raise
                signature = None
            if self._values is not None and signature in (
                self._last_signature, self._rejected_signature,
            ):
                return False
            content = path.read_text(encoding=text_encoding) if signature is not None else ""
            if signature is not None and self._signature(path) != signature:
                raise OSError("读取期间配置发生变化")
            if self._values is not None and not content.strip():
                raise ValueError("配置文件为空，可能尚未保存完成")
            parser = configparser.RawConfigParser()
            parser.read_string(content)
            values = self._parse(parser)
            if signature is not None and self._signature(path) != signature:
                raise OSError("校验期间配置发生变化")
            if self._values is None and not content.strip():
                path.parent.mkdir(parents=True, exist_ok=True)
                buffer = io.StringIO()
                parser.write(buffer)
                atomic_write_text(path, buffer.getvalue(), encoding=text_encoding)
                signature = self._signature(path)
        except (OSError, UnicodeError, configparser.Error, ValueError) as exc:
            if isinstance(exc, configparser.Error):
                # ConfigParser 的异常可能包含原始行（包括 Cookie），日志只报告类型/行号。
                reason = f"INI 格式错误：{type(exc).__name__}"
                if getattr(exc, "lineno", None):
                    reason += f"（第 {exc.lineno} 行）"
            elif isinstance(exc, (OSError, UnicodeError)):
                reason = f"配置无法完整读取或保存：{type(exc).__name__}"
            else:
                reason = str(exc)
            if self._values is None:
                raise ValueError(f"配置无效，未启动录制：{reason}") from None
            if isinstance(exc, (configparser.Error, ValueError)) and not isinstance(exc, UnicodeError):
                self._rejected_signature = signature
            if reason != self._last_error:
                logger.error(f"配置更新未生效，继续使用上一份有效配置：{reason}")
                self._last_error = reason
            return False

        # 不逐字段覆盖活动对象：其他线程不会看到校验一半的新配置。
        self._values = values
        self._last_signature = signature
        self._rejected_signature = None
        if self._last_error:
            logger.info("配置已修正，热更新成功")
        self._last_error = None
        return True
