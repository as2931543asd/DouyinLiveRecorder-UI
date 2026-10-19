#!/usr/bin/env python3
"""Search recorded Douyin danmaku JSONL files without project dependencies.

Examples:
    python3 danmaku_search.py 你好
    python3 danmaku_search.py 你好 --anchor 半糖织雪
    python3 danmaku_search.py 半糖 --scope anchor
    python3 danmaku_search.py 你好 晚安 --match all --limit 0 --json
    python3 danmaku_search.py 关键词 --root /path/to/recordings
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


DEFAULT_ROOT = Path("/srv/nas/Record")
EXCLUDED_DIRECTORY = Path("/srv/nas/CloudDrive")
DANMAKU_SUFFIX = ".danmaku.jsonl"
PLATFORM_DIRECTORY_NAMES = {"抖音直播"}
# 录制中的文件放在平台目录下的这个子目录，录完再移出。名字固定：Jellyfin 内置忽略 **/TempRec/**。
TEMP_RECORDING_DIRECTORY = "TempRec"
FILENAME_TIME_RE = re.compile(r"_(?=\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:_|$))")

SCOPE_FIELDS = {
    "content": ("content",),
    "anchor": ("anchor",),
    "both": ("content", "anchor"),
    "viewer": ("viewer",),
    "all": ("content", "anchor", "viewer"),
}
FIELD_LABELS = {"content": "弹幕", "anchor": "主播", "viewer": "发言者"}


@dataclass
class SearchStats:
    files_scanned: int = 0
    chat_records_scanned: int = 0
    matches: int = 0
    bad_records: int = 0
    file_errors: int = 0
    limit_reached: bool = False


def non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "流式搜索录制生成的 .danmaku.jsonl；默认同时匹配弹幕正文和主播名。"
        ),
        epilog=(
            "退出码：找到结果为 0，没有结果为 1，参数或读取根目录失败为 2。"
            " --limit 0 表示不限制结果数。"
        ),
    )
    parser.add_argument("keywords", nargs="+", metavar="关键词", help="一个或多个搜索词")
    parser.add_argument(
        "-r",
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"弹幕文件或录制根目录（默认：{DEFAULT_ROOT}）",
    )
    parser.add_argument(
        "--scope",
        choices=tuple(SCOPE_FIELDS),
        default="both",
        help=(
            "关键词搜索范围：content=正文，anchor=主播，both=两者（默认），"
            "viewer=发言者，all=全部"
        ),
    )
    parser.add_argument(
        "--anchor",
        metavar="主播名",
        help="只搜索主播名包含此文本的文件；它是过滤条件，不受 --scope 影响",
    )
    parser.add_argument(
        "--match",
        choices=("any", "all"),
        default="any",
        help="多个关键词匹配任意一个（any，默认）或全部（all）",
    )
    parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="区分英文字母大小写（默认不区分）",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=non_negative_int,
        default=100,
        help="最多输出多少条结果（默认 100，0 表示不限）",
    )
    parser.add_argument(
        "--json",
        "--jsonl",
        dest="json_output",
        action="store_true",
        help="以 JSON Lines 输出，便于交给其他程序处理",
    )
    return parser


def normalized(value: str, case_sensitive: bool) -> str:
    return value if case_sensitive else value.casefold()


def is_inside_excluded_directory(path: Path) -> bool:
    """Reject the excluded directory itself and aliases that resolve into it."""
    try:
        absolute = path.absolute()
        resolved = path.resolve(strict=False)
    except OSError:
        absolute = path.absolute()
        resolved = absolute

    for candidate in (absolute, resolved):
        if candidate == EXCLUDED_DIRECTORY or EXCLUDED_DIRECTORY in candidate.parents:
            return True
    return False


def iter_danmaku_files(root: Path, stats: SearchStats) -> Iterator[Path]:
    if is_inside_excluded_directory(root):
        raise ValueError(f"拒绝搜索已排除目录：{EXCLUDED_DIRECTORY}")
    if not root.exists():
        raise ValueError(f"路径不存在：{root}")
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise ValueError(f"不是普通文件或目录：{root}")

    def record_walk_error(exc: OSError) -> None:
        stats.file_errors += 1
        print(f"警告：无法遍历 {exc.filename or root}：{exc}", file=sys.stderr)

    for current, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False, onerror=record_walk_error
    ):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not is_inside_excluded_directory(current_path / name)
        )
        for name in sorted(file_names):
            if not name.endswith(DANMAKU_SUFFIX):
                continue
            candidate = current_path / name
            if not is_inside_excluded_directory(candidate):
                yield candidate


def anchor_from_filename(path: Path) -> str:
    stem = path.name[: -len(DANMAKU_SUFFIX)] if path.name.endswith(DANMAKU_SUFFIX) else path.stem
    match = FILENAME_TIME_RE.search(stem)
    return stem[: match.start()] if match else stem


def infer_anchor(path: Path, root: Path) -> str:
    """Infer the broadcaster from the standard recording directory layout."""
    parts = tuple(part for part in path.parts if part != TEMP_RECORDING_DIRECTORY)
    for index, part in enumerate(parts[:-1]):
        if part in PLATFORM_DIRECTORY_NAMES and index + 1 < len(parts) - 1:
            return parts[index + 1]

    directory_root = root if root.is_dir() else root.parent
    try:
        relative = path.relative_to(directory_root)
    except ValueError:
        relative = path

    relative_directories = relative.parts[:-1]
    if len(relative_directories) >= 2 and relative_directories[0] in PLATFORM_DIRECTORY_NAMES:
        return relative_directories[1]
    if relative_directories:
        return relative_directories[0]
    if path.parent.name and path.parent != directory_root:
        return path.parent.name
    if root.is_dir() and root.name not in PLATFORM_DIRECTORY_NAMES:
        return root.name
    return anchor_from_filename(path) or "未知主播"


def integer_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def timestamp_for_chat(chat: dict[str, object], metadata: dict[str, object]) -> int | None:
    received_at = integer_or_none(chat.get("received_at_unix_ms"))
    if received_at is not None:
        return received_at
    started_at = integer_or_none(metadata.get("recording_started_at_unix_ms"))
    offset = integer_or_none(chat.get("offset_ms"))
    if started_at is not None and offset is not None:
        return started_at + offset
    return None


def display_time(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    try:
        moment = dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=dt.timezone.utc)
        return moment.astimezone().isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return None


def matching_fields(
    *,
    keywords: Sequence[str],
    match_mode: str,
    selected_fields: Sequence[str],
    values: dict[str, str],
    case_sensitive: bool,
) -> list[str]:
    normalized_values = {
        field: normalized(values.get(field, ""), case_sensitive) for field in selected_fields
    }
    fields_per_keyword = [
        [field for field, value in normalized_values.items() if keyword in value]
        for keyword in keywords
    ]
    matched = (
        all(fields_per_keyword) if match_mode == "all" else any(fields_per_keyword)
    )
    if not matched:
        return []
    return [
        field
        for field in selected_fields
        if any(field in keyword_fields for keyword_fields in fields_per_keyword)
    ]


def relative_source(path: Path, root: Path) -> str:
    directory_root = root if root.is_dir() else root.parent
    try:
        return str(path.relative_to(directory_root))
    except ValueError:
        return str(path)


def chat_result(
    record: dict[str, object], metadata: dict[str, object],
    *, anchor: str, source: str, line: int,
) -> dict[str, object] | None:
    """共享 CLI 与网页索引的记录规范化，保留毫秒与来源行号。"""
    content = record.get("content")
    if record.get("type") != "chat" or not isinstance(content, str):
        return None
    user = record.get("user")
    user = user if isinstance(user, dict) else {}
    timestamp_ms = timestamp_for_chat(record, metadata)
    started_ms = integer_or_none(metadata.get("recording_started_at_unix_ms"))
    offset_ms = integer_or_none(record.get("offset_ms"))
    if offset_ms is None and timestamp_ms is not None and started_ms is not None:
        offset_ms = timestamp_ms - started_ms
    room = metadata.get("room_identifier")
    return {
        "anchor": anchor,
        "room_identifier": room if isinstance(room, str) else None,
        "time": display_time(timestamp_ms),
        "timestamp_ms": timestamp_ms,
        "offset_ms": offset_ms,
        "viewer_id": user.get("id") if isinstance(user.get("id"), str) else None,
        "viewer_nickname": user.get("nickname") if isinstance(user.get("nickname"), str) else None,
        "content": content,
        "file": source,
        "line": line,
    }


def iter_file_matches(
    path: Path,
    *,
    root: Path,
    keywords: Sequence[str],
    scope: str,
    match_mode: str,
    anchor_filter: str | None,
    case_sensitive: bool,
    stats: SearchStats,
) -> Iterator[dict[str, object]]:
    anchor = infer_anchor(path, root)
    if anchor_filter is not None and normalized(anchor_filter, case_sensitive) not in normalized(
        anchor, case_sensitive
    ):
        return

    metadata: dict[str, object] = {}
    stats.files_scanned += 1
    try:
        with path.open("rb") as source:
            for line_number, raw_line in enumerate(source, start=1):
                if not raw_line.strip():
                    continue
                try:
                    text_line = raw_line.decode("utf-8-sig" if line_number == 1 else "utf-8")
                    record = json.loads(text_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # An actively written file can temporarily end in a partial JSON line.
                    stats.bad_records += 1
                    continue
                if not isinstance(record, dict):
                    stats.bad_records += 1
                    continue
                if record.get("type") == "metadata":
                    metadata = record
                    continue
                if record.get("type") != "chat":
                    continue

                result = chat_result(
                    record, metadata, anchor=anchor,
                    source=relative_source(path, root), line=line_number,
                )
                if result is None:
                    stats.bad_records += 1
                    continue
                content = result["content"]
                viewer_text = " ".join(
                    value for value in (result["viewer_nickname"], result["viewer_id"])
                    if value is not None
                )

                stats.chat_records_scanned += 1
                fields = matching_fields(
                    keywords=keywords,
                    match_mode=match_mode,
                    selected_fields=SCOPE_FIELDS[scope],
                    values={"content": content, "anchor": anchor, "viewer": viewer_text},
                    case_sensitive=case_sensitive,
                )
                if not fields:
                    continue

                result["matched_fields"] = fields
                yield result
    except OSError as exc:
        stats.file_errors += 1
        print(f"警告：无法读取 {path}：{exc}", file=sys.stderr)


def clean_terminal_text(value: object) -> str:
    if value is None:
        return "-"
    return str(value).replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def print_match(match: dict[str, object], json_output: bool) -> None:
    if json_output:
        print(json.dumps(match, ensure_ascii=False, separators=(",", ":")))
        return
    labels = ",".join(FIELD_LABELS[field] for field in match["matched_fields"])
    print(
        "\t".join(
            (
                clean_terminal_text(match["time"]),
                f"主播={clean_terminal_text(match['anchor'])}",
                f"发言者={clean_terminal_text(match['viewer_nickname'])}",
                f"弹幕={clean_terminal_text(match['content'])}",
                f"命中={labels}",
                f"来源={clean_terminal_text(match['file'])}:{match['line']}",
            )
        )
    )


def print_summary(stats: SearchStats) -> None:
    match_text = f"至少 {stats.matches}" if stats.limit_reached else str(stats.matches)
    details = (
        f"扫描 {stats.files_scanned} 个文件、{stats.chat_records_scanned} 条弹幕，"
        f"找到 {match_text} 条结果"
    )
    extras: list[str] = []
    if stats.limit_reached:
        extras.append("已达到输出上限")
    if stats.bad_records:
        extras.append(f"跳过 {stats.bad_records} 条损坏或不完整记录")
    if stats.file_errors:
        extras.append(f"遇到 {stats.file_errors} 个文件/目录读取错误")
    if extras:
        details += "；" + "，".join(extras)
    print(details, file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    if any(keyword == "" for keyword in args.keywords):
        print("错误：关键词不能为空", file=sys.stderr)
        return 2
    keywords = [normalized(keyword, args.case_sensitive) for keyword in args.keywords]
    stats = SearchStats()
    try:
        paths = iter_danmaku_files(args.root, stats)
        for path in paths:
            for match in iter_file_matches(
                path,
                root=args.root,
                keywords=keywords,
                scope=args.scope,
                match_mode=args.match,
                anchor_filter=args.anchor,
                case_sensitive=args.case_sensitive,
                stats=stats,
            ):
                print_match(match, args.json_output)
                stats.matches += 1
                if args.limit and stats.matches >= args.limit:
                    stats.limit_reached = True
                    print_summary(stats)
                    return 0
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print_summary(stats)
    return 0 if stats.matches else 1


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
