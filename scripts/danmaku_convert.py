#!/usr/bin/env python3
"""Convert structured Douyin danmaku JSON Lines to Bilibili XML."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping, Sequence, TextIO
from xml.sax.saxutils import escape, quoteattr

SCHEMA_NAME = "douyin-live-recorder-ui.danmaku"
SCHEMA_VERSION = 1
TIME_UNITS = {
    "recording_started_at_unix_ms": "milliseconds since Unix epoch UTC",
    "received_at_unix_ms": "milliseconds since Unix epoch UTC",
    "offset_ms": "milliseconds from recording start",
}

# XML 展示/兼容默认值只属于派生转换器，不进入来源 JSONL。
XML_DEFAULT_MODE = 1
XML_DEFAULT_FONT_SIZE = 25
XML_DEFAULT_COLOR = 16777215

_XML_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<i>
<chatserver>chat.bilibili.com</chatserver>
<chatid>0</chatid>
<mission>0</mission>
<maxlimit>0</maxlimit>
<state>0</state>
<real_name>0</real_name>
<source>DouyinLiveRecorder-UI</source>
"""
_XML_FOOTER = "</i>\n"


class DanmakuConversionError(ValueError):
    """The source is not a supported, complete danmaku JSONL file."""


class DanmakuRecoveryWarning(UserWarning):
    """A final incomplete JSON fragment was ignored by explicit request."""


def default_output_path(input_path: str | Path) -> Path:
    """Return ``foo.xml`` for ``foo.danmaku.jsonl`` (and other input names)."""
    path = Path(input_path)
    compound_suffix = ".danmaku.jsonl"
    if path.name.endswith(compound_suffix):
        return path.with_name(path.name[: -len(compound_suffix)] + ".xml")
    return path.with_suffix(".xml")


def _xml_sanitize(value: str) -> str:
    """Remove characters forbidden by XML 1.0, including surrogate code points."""
    return "".join(
        char
        for char in value
        if char in "\t\n\r"
        or "\x20" <= char <= "\ud7ff"
        or "\ue000" <= char <= "\ufffd"
        or "\U00010000" <= char <= "\U0010ffff"
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard numeric constant {value}")


def _incomplete_json_at_eof(line: str, error: json.JSONDecodeError) -> bool:
    """Conservatively recognize a JSON value cut off at the end of a file."""
    content_end = len(line.rstrip())
    if error.pos >= content_end:
        return True
    if error.msg.startswith("Unterminated string"):
        return True
    tail = line[error.pos:content_end]
    if error.msg == "Expecting value":
        return bool(tail) and any(
            literal.startswith(tail) and literal != tail
            for literal in ("true", "false", "null")
        )
    if error.msg == "Invalid \\uXXXX escape":
        return tail.startswith("u") and len(tail) < 5
    if error.msg == "Expecting ',' delimiter":
        return tail in {"e", "E", "e+", "e-", "E+", "E-"}
    return False


def _incomplete_json_before_truncated_utf8(
    raw_line: bytes, error: UnicodeDecodeError
) -> bool:
    try:
        prefix = raw_line[: error.start].decode("utf-8")
        json.loads(prefix, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as json_error:
        return _incomplete_json_at_eof(prefix, json_error)
    except (UnicodeDecodeError, ValueError):
        return False
    return False


def _records(
    source: BinaryIO, *, recover_truncated_tail: bool = False
) -> Iterator[tuple[int, Any]]:
    for line_number, raw_line in enumerate(source, start=1):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            if (
                recover_truncated_tail
                and not raw_line.endswith((b"\n", b"\r"))
                and exc.reason == "unexpected end of data"
                and exc.end == len(raw_line)
                and _incomplete_json_before_truncated_utf8(raw_line, exc)
            ):
                warnings.warn(
                    f"已忽略文件末尾没有换行且不完整的 UTF-8 JSON（第 {line_number} 行）",
                    DanmakuRecoveryWarning,
                    stacklevel=3,
                )
                return
            raise DanmakuConversionError(
                f"第 {line_number} 行不是有效 UTF-8: {exc}"
            ) from exc
        if not line.strip():
            continue
        try:
            record = json.loads(line, parse_constant=_reject_json_constant)
        except json.JSONDecodeError as exc:
            has_line_ending = raw_line.endswith((b"\n", b"\r"))
            if (
                recover_truncated_tail
                and not has_line_ending
                and _incomplete_json_at_eof(line, exc)
            ):
                warnings.warn(
                    f"已忽略文件末尾没有换行且不完整的 JSON（第 {line_number} 行）",
                    DanmakuRecoveryWarning,
                    stacklevel=3,
                )
                return
            raise DanmakuConversionError(
                f"第 {line_number} 行不是有效 JSON: {exc}"
            ) from exc
        except ValueError as exc:
            raise DanmakuConversionError(
                f"第 {line_number} 行不是有效 JSON: {exc}"
            ) from exc
        yield line_number, record


def _mapping(record: Any, line_number: int) -> Mapping[str, Any]:
    if not isinstance(record, Mapping):
        raise DanmakuConversionError(f"第 {line_number} 行必须是 JSON 对象")
    return record


def _integer(
    record: Mapping[str, Any], field: str, line_number: int, *, nonnegative: bool = False
) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DanmakuConversionError(f"第 {line_number} 行字段 {field} 必须是整数")
    if nonnegative and value < 0:
        raise DanmakuConversionError(f"第 {line_number} 行字段 {field} 不能为负数")
    return value


def _string(record: Mapping[str, Any], field: str, line_number: int) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise DanmakuConversionError(f"第 {line_number} 行字段 {field} 必须是字符串")
    return value


def _nullable_string(
    record: Mapping[str, Any], field: str, line_number: int
) -> str | None:
    if field not in record:
        raise DanmakuConversionError(
            f"第 {line_number} 行字段 {field} 必须是字符串或 null"
        )
    value = record[field]
    if value is not None and not isinstance(value, str):
        raise DanmakuConversionError(
            f"第 {line_number} 行字段 {field} 必须是字符串或 null"
        )
    return value


def _validate_metadata(record: Any, line_number: int) -> None:
    metadata = _mapping(record, line_number)
    if metadata.get("type") != "metadata":
        raise DanmakuConversionError(
            f"第 {line_number} 行必须是 type=metadata 的首条记录"
        )
    if metadata.get("schema") != SCHEMA_NAME:
        raise DanmakuConversionError(
            f"第 {line_number} 行 schema 不受支持: {metadata.get('schema')!r}"
        )
    version = _integer(metadata, "version", line_number, nonnegative=True)
    if version != SCHEMA_VERSION:
        raise DanmakuConversionError(
            f"第 {line_number} 行 version 不受支持: {version!r}"
        )
    if metadata.get("platform") != "douyin":
        raise DanmakuConversionError(
            f"第 {line_number} 行 platform 不受支持: {metadata.get('platform')!r}"
        )
    _string(metadata, "room_identifier", line_number)
    _integer(
        metadata,
        "recording_started_at_unix_ms",
        line_number,
        nonnegative=True,
    )
    time_units = metadata.get("time_units")
    if not isinstance(time_units, Mapping):
        raise DanmakuConversionError(f"第 {line_number} 行字段 time_units 必须是对象")
    if dict(time_units) != TIME_UNITS:
        raise DanmakuConversionError(
            f"第 {line_number} 行字段 time_units 必须与 schema v1 的固定键和值完全匹配"
        )


def _chat_xml_line(record: Any, line_number: int, expected_sequence: int) -> str:
    chat = _mapping(record, line_number)
    if chat.get("type") != "chat":
        raise DanmakuConversionError(
            f"第 {line_number} 行记录类型不受支持: {chat.get('type')!r}"
        )
    if chat.get("method") != "WebcastChatMessage":
        raise DanmakuConversionError(
            f"第 {line_number} 行 method 不受支持: {chat.get('method')!r}"
        )

    sequence = _integer(chat, "sequence", line_number, nonnegative=True)
    if sequence != expected_sequence:
        raise DanmakuConversionError(
            f"第 {line_number} 行 sequence 必须是 {expected_sequence}，实际为 {sequence}"
        )
    offset_ms = _integer(chat, "offset_ms", line_number, nonnegative=True)
    received_at_unix_ms = _integer(
        chat, "received_at_unix_ms", line_number, nonnegative=True
    )
    content = _string(chat, "content", line_number)

    user = chat.get("user")
    if not isinstance(user, Mapping):
        raise DanmakuConversionError(f"第 {line_number} 行字段 user 必须是对象")
    source_user_id = _nullable_string(user, "id", line_number)
    source_nickname = _nullable_string(user, "nickname", line_number)
    user_id = "0" if source_user_id is None else source_user_id
    nickname = "匿名用户" if source_nickname is None else source_nickname

    seconds, milliseconds = divmod(offset_ms, 1000)
    p_value = (
        f"{seconds}.{milliseconds:03d},{XML_DEFAULT_MODE},"
        f"{XML_DEFAULT_FONT_SIZE},{XML_DEFAULT_COLOR},"
        f"{received_at_unix_ms // 1000},0,{user_id},{sequence}"
    )
    text = f"{nickname}: {content}"
    return (
        f"<d p={quoteattr(_xml_sanitize(p_value))}>"
        f"{escape(_xml_sanitize(text))}</d>\n"
    )


def _write_xml(
    source: BinaryIO,
    destination: TextIO,
    *,
    recover_truncated_tail: bool = False,
) -> None:
    records = iter(
        _records(source, recover_truncated_tail=recover_truncated_tail)
    )
    try:
        metadata_line, metadata = next(records)
    except StopIteration as exc:
        raise DanmakuConversionError("输入中没有 metadata 记录") from exc

    _validate_metadata(metadata, metadata_line)
    destination.write(_XML_HEADER)
    expected_sequence = 1
    for line_number, record in records:
        destination.write(
            _chat_xml_line(record, line_number, expected_sequence)
        )
        expected_sequence += 1
    destination.write(_XML_FOOTER)


def _same_file_as_stat(path: Path, source_stat: os.stat_result) -> bool:
    try:
        destination_stat = path.stat()
    except FileNotFoundError:
        return False
    return (
        destination_stat.st_dev == source_stat.st_dev
        and destination_stat.st_ino == source_stat.st_ino
    )


def convert_jsonl_to_xml(
    input_path: str | Path,
    output_path: str | Path | None = None,
    *,
    overwrite: bool = False,
    recover_truncated_tail: bool = False,
) -> Path:
    """Convert one JSONL source to Bilibili XML and return the output path.

    The destination is published only after the entire source has been parsed and
    converted successfully. Without ``overwrite``, an existing destination is
    never replaced.
    """
    source_path = Path(input_path)
    destination_path = (
        Path(output_path) if output_path is not None else default_output_path(source_path)
    )
    temporary_path: Path | None = None
    try:
        with source_path.open("rb") as source:
            source_stat = os.fstat(source.fileno())
            if _same_file_as_stat(destination_path, source_stat):
                raise DanmakuConversionError("输入和输出不能是同一个文件")
            if destination_path.exists() and not overwrite:
                raise FileExistsError(
                    f"输出文件已存在，使用 --overwrite 覆盖: {destination_path}"
                )

            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=destination_path.parent,
                prefix=f".{destination_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as destination:
                temporary_path = Path(destination.name)
                _write_xml(
                    source,
                    destination,
                    recover_truncated_tail=recover_truncated_tail,
                )
                destination.flush()
                os.fsync(destination.fileno())

            # 覆盖发布前再次检查，避免转换期间目标被换成来源的别名。
            if _same_file_as_stat(destination_path, source_stat):
                raise DanmakuConversionError("输入和输出不能是同一个文件")
            if overwrite:
                os.replace(temporary_path, destination_path)
            else:
                try:
                    os.link(temporary_path, destination_path)
                except FileExistsError as exc:
                    raise FileExistsError(
                        f"输出文件已存在，使用 --overwrite 覆盖: {destination_path}"
                    ) from exc
                temporary_path.unlink()
        temporary_path = None
        return destination_path
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将弹幕 .danmaku.jsonl 离线转换为 Bilibili XML"
    )
    parser.add_argument("input", type=Path, metavar="INPUT", help="输入 JSONL 文件")
    parser.add_argument("--output", "-o", type=Path, help="输出 XML 文件")
    parser.add_argument(
        "--overwrite", action="store_true", help="允许原子替换已存在的输出文件"
    )
    parser.add_argument(
        "--recover-truncated-tail",
        action="store_true",
        help="仅忽略文件末尾没有换行且不完整的最后一条 JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    recovery_warnings: list[warnings.WarningMessage]
    try:
        with warnings.catch_warnings(record=True) as recovery_warnings:
            warnings.simplefilter("always", DanmakuRecoveryWarning)
            output = convert_jsonl_to_xml(
                args.input,
                args.output,
                overwrite=args.overwrite,
                recover_truncated_tail=args.recover_truncated_tail,
            )
    except (DanmakuConversionError, OSError) as exc:
        print(f"转换失败: {exc}", file=sys.stderr)
        return 1
    for warning in recovery_warnings:
        if issubclass(warning.category, DanmakuRecoveryWarning):
            print(f"警告: {warning.message}", file=sys.stderr)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
