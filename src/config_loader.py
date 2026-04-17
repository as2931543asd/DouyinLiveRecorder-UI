# -*- encoding: utf-8 -*-
"""config/config.ini 的读取与缓存。

`Settings` 是一个**可重载**的配置快照：`reload()` 会重新读取 ini 文件并把
字段覆盖到 self 上，因此线程只要持有同一个 `Settings` 实例，就能实时看
到最新值——与原先基于 module-level 全局变量的语义保持一致。
"""

from __future__ import annotations

import configparser
from typing import Any

text_encoding = "utf-8-sig"
options = {"是": True, "否": False}

VIDEO_SAVE_TYPES = ("FLV", "MKV", "TS", "MP4")
QUALITY_CHOICES = ("原画", "蓝光", "超清", "高清", "标清", "流畅")


class Settings:
    def __init__(self, config_file: str) -> None:
        self.config_file = config_file
        self._parser = configparser.RawConfigParser()
        self.reload()

    # -- 内部 ----------------------------------------------------------------
    def _get(self, section: str, option: str, default: Any) -> Any:
        try:
            self._parser.read(self.config_file, encoding=text_encoding)
            if "录制设置" not in self._parser.sections():
                self._parser.add_section("录制设置")
            if "Cookie" not in self._parser.sections():
                self._parser.add_section("Cookie")
            return self._parser.get(section, option)
        except (configparser.NoSectionError, configparser.NoOptionError):
            self._parser.set(section, option, str(default))
            with open(self.config_file, "w", encoding=text_encoding) as f:
                self._parser.write(f)
            return default

    @staticmethod
    def _bool(raw: Any, default: bool) -> bool:
        return options.get(raw, default)

    # -- 公共 ----------------------------------------------------------------
    def reload(self) -> None:
        g = self._get

        self.video_save_path = g("录制设置", "直播保存路径(不填则默认)", "")
        self.folder_by_author = self._bool(g("录制设置", "保存文件夹是否以作者区分", "是"), False)
        self.folder_by_time = self._bool(g("录制设置", "保存文件夹是否以时间区分", "否"), False)
        self.folder_by_title = self._bool(g("录制设置", "保存文件夹是否以标题区分", "否"), False)
        self.filename_by_title = self._bool(g("录制设置", "保存文件名是否包含标题", "否"), False)
        self.clean_emoji = self._bool(g("录制设置", "是否去除名称中的表情符号", "是"), True)

        raw_save_type = g("录制设置", "视频保存格式ts|mkv|flv|mp4", "ts")
        self.video_save_type = (
            raw_save_type.upper()
            if raw_save_type and raw_save_type.upper() in VIDEO_SAVE_TYPES
            else "TS"
        )

        self.video_record_quality = g("录制设置", "原画|超清|高清|标清|流畅", "原画")
        self.use_proxy = self._bool(g("录制设置", "是否使用代理ip(是/否)", "是"), False)
        proxy_addr_bak = g("录制设置", "代理地址", "")
        self.proxy_addr = None if not self.use_proxy else proxy_addr_bak

        self.max_request = int(g("录制设置", "同一时间访问网络的线程数", 3))
        self.delay_default = int(g("录制设置", "循环时间(秒)", 120))
        self.local_delay_default = int(g("录制设置", "排队读取网址时间(秒)", 0))
        self.startup_stagger_delay = int(g("录制设置", "首次启动排队读取网址时间(秒)", 3))
        self.startup_stagger_jitter = int(g("录制设置", "首次启动随机抖动时间(秒)", 2))
        self.loop_time = self._bool(g("录制设置", "是否显示循环秒数", "否"), False)
        self.show_url = self._bool(g("录制设置", "是否显示直播源地址", "否"), False)
        self.disk_space_limit = float(g("录制设置", "录制空间剩余阈值(gb)", 1.0))
        self.converts_to_mp4 = self._bool(g("录制设置", "录制完成后自动转为mp4格式", "否"), False)
        self.converts_to_h264 = self._bool(g("录制设置", "mp4格式重新编码为h264", "否"), False)
        self.delete_origin_file = self._bool(g("录制设置", "追加格式后删除原文件", "否"), False)
        self.split_video_by_time = self._bool(g("录制设置", "分段录制是否开启", "否"), False)
        self.split_time = str(g("录制设置", "视频分段时间(秒)", 1800))
        self.dy_cookie = g("Cookie", "抖音cookie", "")
