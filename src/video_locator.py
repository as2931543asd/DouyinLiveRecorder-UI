"""把一次录制的弹幕 offset 映射到实际视频，不按配置的切片长度猜测。"""
from __future__ import annotations

import csv
import math
import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from danmaku_search import DANMAKU_SUFFIX, is_inside_excluded_directory

VIDEO_EXTENSIONS = {'.mp4', '.ts', '.mkv', '.flv'}
FORMAT_PREFERENCE = {'.mp4': 0, '.mkv': 1, '.ts': 2, '.flv': 3}
TIMING_NOTE = '弹幕以接收时间、视频以媒体时间计时；启动缓冲、网络延迟或转码可能产生偏差，不保证逐帧同步。'


def safe_recording_path(root: Path, relative: str, suffixes: set[str]) -> Path:
    value = Path(relative)
    if value.is_absolute() or '..' in value.parts or not relative:
        raise ValueError('只允许录制目录内的相对路径')
    root = root.resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root) or is_inside_excluded_directory(path):
        raise ValueError('禁止访问录制目录以外或已排除的路径')
    if path.suffix.lower() not in suffixes or not path.is_file():
        raise ValueError('文件不存在或类型不受支持')
    return path


def segment_list_path(video_path: str | Path) -> Path:
    path = Path(video_path)
    stem = path.stem.removesuffix('_%03d')
    return path.with_name(stem + '.segments.csv')


@lru_cache(maxsize=2048)
def _probe_cached(path: str, size: int, mtime_ns: int) -> int:
    executable = shutil.which('ffprobe')
    if executable is None:
        bundled = Path(__file__).resolve().parent.parent / 'ffmpeg' / 'ffprobe'
        executable = str(bundled) if bundled.is_file() else None
    if executable is None:
        raise ValueError('ffprobe 不可用')
    # Exceptions are not cached: temporary I/O errors/timeouts must remain retryable.
    result = subprocess.run(
        [executable, '-v', 'error', '-protocol_whitelist', 'file',
         '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', path],
        capture_output=True, text=True, timeout=5, check=True,
    )
    seconds = float(result.stdout.strip())
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('无效视频时长')
    return round(seconds * 1000)


def probe_duration(path: Path) -> int | None:
    try:
        stat = path.stat()
        return _probe_cached(str(path), stat.st_size, stat.st_mtime_ns)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def locate_video(root: Path, source: str, offset_ms: int | None) -> dict:
    root = root.resolve()
    jsonl = safe_recording_path(root, source, {'.jsonl'})
    if not jsonl.name.endswith(DANMAKU_SUFFIX):
        raise ValueError('不是弹幕 JSONL 文件')
    base = jsonl.name[:-len(DANMAKU_SUFFIX)]
    pattern = re.compile(re.escape(base) + r'(?:_(\d{3,}))?$')
    groups: dict[int | None, list[Path]] = {}
    for candidate in jsonl.parent.iterdir():
        match = pattern.fullmatch(candidate.stem)
        if candidate.suffix.lower() not in VIDEO_EXTENSIONS or not match:
            continue
        try:
            candidate = safe_recording_path(root, str(candidate.relative_to(root)), VIDEO_EXTENSIONS)
        except (OSError, ValueError):
            continue
        number = int(match[1]) if match[1] is not None else None
        groups.setdefault(number, []).append(candidate)
    for paths in groups.values():
        paths.sort(key=lambda p: (FORMAT_PREFERENCE[p.suffix.lower()], p.name))
    candidates = [str(p.relative_to(root)) for paths in groups.values() for p in paths]

    def unresolved(reason):
        return {'status': 'unresolved', 'reason': reason, 'candidates': candidates, 'timing_note': TIMING_NOTE}

    def selected(paths, start, end, basis):
        return {'status': 'located' if basis == 'segment_list' else 'estimated',
                'basis': basis, 'file': str(paths[0].relative_to(root)),
                'absolute_path': str(paths[0]), 'alternatives': [str(p.relative_to(root)) for p in paths[1:]],
                'start_ms': start, 'end_ms': end, 'offset_ms': offset_ms - start,
                'reason': '按 FFmpeg 实际分段时间定位' if basis == 'segment_list' else '历史录像：按实际视频时长累计估算，切片边界附近请核对相邻文件',
                'timing_note': TIMING_NOTE}

    if not groups:
        return unresolved('没有找到同次录制的视频，可能已移动、删除或尚未生成')
    if offset_ms is None or offset_ms < 0:
        return unresolved('缺少有效的录制内时间，不能可靠定位')
    if None in groups and len(groups) > 1:
        return unresolved('同时存在整段和分段视频，无法确认时间轴')

    manifest = jsonl.with_name(base + '.segments.csv')
    if manifest.exists():
        try:
            manifest = safe_recording_path(root, str(manifest.relative_to(root)), {'.csv'})
            intervals = []
            seen_numbers = set()
            with manifest.open(encoding='utf-8', newline='') as handle:
                for row in csv.reader(handle):
                    if len(row) != 3:
                        return unresolved('分段时间表不完整，请等待录制完成后再定位')
                    name = Path(row[0])
                    match = pattern.fullmatch(name.stem)
                    if not match or match[1] is None or name.suffix.lower() not in VIDEO_EXTENSIONS:
                        return unresolved('分段时间表包含无法识别的文件')
                    start, end = (float(value) * 1000 for value in row[1:])
                    if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end:
                        return unresolved('分段时间表包含无效时间')
                    start, end = round(start), round(end)
                    if intervals and start < intervals[-1][2]:
                        return unresolved('分段时间表存在重叠，无法唯一定位')
                    number = int(match[1])
                    if number in seen_numbers:
                        return unresolved('分段时间表存在重复文件，无法唯一定位')
                    seen_numbers.add(number)
                    intervals.append((number, start, end))
            for number, start, end in intervals:
                if start <= offset_ms < end:  # Exact boundary belongs to the next segment.
                    if number not in groups:
                        return unresolved(f'对应第 {number:03d} 段视频已缺失，不会跳到其他视频')
                    return selected(groups[number], start, end, 'segment_list')
            return unresolved('该时间不在已完成分段内，可能处于录制尾段或空缺区间')
        except (OSError, ValueError, csv.Error):
            return unresolved('无法读取分段时间表')

    # Historical reset-timestamp segments: only accumulate a contiguous, probeable prefix.
    elapsed, expected = 0, 0
    deadline = time.monotonic() + 15
    for number in sorted(groups, key=lambda n: -1 if n is None else n):
        if number is not None and number != expected:
            return unresolved('前面的分段视频缺失，无法推算后续时间；不会按当前分段设置猜测')
        if time.monotonic() > deadline:
            return unresolved('视频时长探测超时，可重试以复用已缓存的时长')
        paths = groups[number]
        duration = probe_duration(paths[0])
        if duration is None:
            return unresolved('无法读取视频时长：请检查 ffprobe，或等待录制/转码完成')
        if elapsed <= offset_ms < elapsed + duration:
            return selected(paths, elapsed, elapsed + duration, 'media_duration')
        elapsed += duration
        expected += 1
    return unresolved('弹幕时间超出目前可读取的视频时长，可能仍在录制或存在时间偏差')
