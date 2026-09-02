# -*- encoding: utf-8 -*-
"""受支持直播平台的 URL 识别与规范化。"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

DOUYIN = "douyin"
XIAOHONGSHU = "xiaohongshu"

_DOUYIN_HOSTS = {"live.douyin.com", "v.douyin.com", "www.douyin.com"}
_XHS_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com"}
_XHS_SHORT_HOSTS = {"xhslink.com", "www.xhslink.com"}
# 小红书分享链目前可能跳转为以下两种直播间路径：
#   /livestream/<room_id>
#   /livestream/<dynamic_path>/<room_id>
_XHS_LIVESTREAM_PATH = re.compile(
    r"^/livestream/(?:[A-Za-z0-9_-]+/)?\d+/?$"
)

PLATFORM_NAMES = {
    DOUYIN: "抖音直播",
    XIAOHONGSHU: "小红书直播",
}


def identify_platform(url: str) -> str | None:
    """按精确域名和路径识别平台，避免子串误判。"""
    value = url.strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value

    try:
        parsed = urlsplit(value)
    except ValueError:
        return None

    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").rstrip(".").lower()
    if host in _DOUYIN_HOSTS:
        return DOUYIN
    if host in _XHS_SHORT_HOSTS and parsed.path.strip("/"):
        return XIAOHONGSHU
    if host in _XHS_HOSTS and _XHS_LIVESTREAM_PATH.fullmatch(parsed.path):
        return XIAOHONGSHU
    return None


def normalize_supported_url(url: str) -> str | None:
    """返回适合持久化和运行时作为键使用的受支持 URL。"""
    value = url.strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value

    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    platform = identify_platform(value)
    if platform is None:
        return None

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").rstrip(".").lower()
    if not scheme or not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = f"{host}:{port}" if port else host
    path = parsed.path.rstrip("/")

    # 抖音直播页和小红书直播间页的查询参数均为分享/追踪参数；去除后可
    # 避免同一房间被重复添加。短链的查询参数可能参与跳转，予以保留。
    query = parsed.query
    if host == "live.douyin.com" or host in _XHS_HOSTS:
        query = ""

    return urlunsplit((scheme, netloc, path, query, ""))


def platform_name(platform: str) -> str:
    return PLATFORM_NAMES.get(platform, "未知平台")
