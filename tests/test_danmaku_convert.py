from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.danmaku_convert import (
    DanmakuConversionError,
    DanmakuRecoveryWarning,
    convert_jsonl_to_xml,
    default_output_path,
    main,
)


def _metadata() -> dict:
    return {
        "type": "metadata",
        "schema": "douyin-live-recorder-ui.danmaku",
        "version": 1,
        "platform": "douyin",
        "room_identifier": "房间-123",
        "recording_started_at_unix_ms": 1_000_000,
        "time_units": {
            "recording_started_at_unix_ms": "milliseconds since Unix epoch UTC",
            "received_at_unix_ms": "milliseconds since Unix epoch UTC",
            "offset_ms": "milliseconds from recording start",
        },
    }


def _chat() -> dict:
    return {
        "type": "chat",
        "sequence": 1,
        "method": "WebcastChatMessage",
        "offset_ms": 2345,
        "received_at_unix_ms": 1_002_999,
        "user": {"id": 'u<&"\x02', "nickname": "昵称<&\x01"},
        "content": "正文 > & < ' \"\x00",
    }


def _write_jsonl(path: Path, *records: dict, blank_line: bool = False) -> None:
    lines = [json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records]
    separator = "\n\n" if blank_line else "\n"
    path.write_text(separator.join(lines) + "\n", encoding="utf-8")


class DanmakuConvertTests(unittest.TestCase):
    def test_default_output_name(self) -> None:
        self.assertEqual(
            default_output_path(Path("/tmp/foo.danmaku.jsonl")),
            Path("/tmp/foo.xml"),
        )
        self.assertEqual(default_output_path("/tmp/foo.jsonl"), Path("/tmp/foo.xml"))

    def test_conversion_produces_parseable_compatible_xml_and_filters_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "foo.danmaku.jsonl"
            _write_jsonl(source, _metadata(), _chat(), blank_line=True)

            output = convert_jsonl_to_xml(source)

            self.assertEqual(output, Path(directory) / "foo.xml")
            xml_text = output.read_text(encoding="utf-8")
            self.assertIn("&amp;", xml_text)
            self.assertNotIn("\x00", xml_text)
            self.assertNotIn("\x01", xml_text)
            self.assertNotIn("\x02", xml_text)
            root = ET.parse(output).getroot()
            comments = root.findall("d")
            self.assertEqual(len(comments), 1)
            self.assertEqual(comments[0].text, "昵称<&: 正文 > & < ' \"")
            self.assertEqual(
                comments[0].attrib["p"].split(","),
                ["2.345", "1", "25", "16777215", "1002", "0", 'u<&"', "1"],
            )

    def test_null_user_fields_use_xml_compatibility_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "nullable.danmaku.jsonl"
            chat = _chat()
            chat["user"] = {"id": None, "nickname": None}
            chat["content"] = ""
            _write_jsonl(source, _metadata(), chat)

            output = convert_jsonl_to_xml(source)

            comment = ET.parse(output).getroot().find("d")
            self.assertIsNotNone(comment)
            self.assertEqual(comment.text, "匿名用户: ")
            self.assertEqual(comment.attrib["p"].split(",")[6], "0")

    def test_blank_lines_and_unknown_extension_fields_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "future.danmaku.jsonl"
            metadata = _metadata()
            metadata["future_metadata"] = {"source": "manual-recovery"}
            chat = _chat()
            chat["message_id"] = "future-id"
            chat["user"]["future_badge"] = "extension"
            source.write_text(
                "\n\t\n"
                + json.dumps(metadata, ensure_ascii=False)
                + "\n\n"
                + json.dumps(chat, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )

            output = convert_jsonl_to_xml(source)

            comments = ET.parse(output).getroot().findall("d")
            self.assertEqual(len(comments), 1)
            self.assertEqual(comments[0].text, "昵称<&: 正文 > & < ' \"")

    def test_existing_output_is_protected_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "foo.danmaku.jsonl"
            output = Path(directory) / "foo.xml"
            _write_jsonl(source, _metadata(), _chat())
            output.write_text("keep me", encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "--overwrite"):
                convert_jsonl_to_xml(source)

            self.assertEqual(output.read_text(encoding="utf-8"), "keep me")

    def test_overwrite_replaces_existing_output_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "foo.danmaku.jsonl"
            output = Path(directory) / "custom.xml"
            _write_jsonl(source, _metadata(), _chat())
            output.write_text("old", encoding="utf-8")

            result = convert_jsonl_to_xml(source, output, overwrite=True)

            self.assertEqual(result, output)
            self.assertEqual(ET.parse(output).getroot().tag, "i")
            self.assertNotEqual(output.read_text(encoding="utf-8"), "old")

    def test_truncated_json_reports_line_and_does_not_replace_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "broken.danmaku.jsonl"
            output = Path(directory) / "broken.xml"
            metadata_line = json.dumps(_metadata(), ensure_ascii=False)
            source.write_text(metadata_line + '\n{"type":"chat"', encoding="utf-8")
            output.write_text("existing target", encoding="utf-8")

            with self.assertRaisesRegex(DanmakuConversionError, "第 2 行"):
                convert_jsonl_to_xml(source, output, overwrite=True)

            self.assertEqual(output.read_text(encoding="utf-8"), "existing target")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_recover_truncated_tail_converts_complete_prefix_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "recover.danmaku.jsonl"
            source.write_text(
                json.dumps(_metadata(), ensure_ascii=False)
                + "\n"
                + json.dumps(_chat(), ensure_ascii=False)
                + '\n{"type":"chat"',
                encoding="utf-8",
            )

            with self.assertWarnsRegex(DanmakuRecoveryWarning, "第 3 行"):
                output = convert_jsonl_to_xml(
                    source, recover_truncated_tail=True
                )

            self.assertEqual(len(ET.parse(output).getroot().findall("d")), 1)

    def test_recover_truncated_utf8_tail_warns_and_keeps_complete_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "recover-utf8.danmaku.jsonl"
            complete = (
                json.dumps(_metadata(), ensure_ascii=False)
                + "\n"
                + json.dumps(_chat(), ensure_ascii=False)
                + "\n"
            ).encode("utf-8")
            source.write_bytes(complete + b'{"content":"\xe4\xb8')

            with self.assertWarnsRegex(DanmakuRecoveryWarning, "第 3 行"):
                output = convert_jsonl_to_xml(
                    source, recover_truncated_tail=True
                )

            self.assertEqual(len(ET.parse(output).getroot().findall("d")), 1)

    def test_recovery_rejects_middle_newline_terminated_and_invalid_tails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            metadata_line = json.dumps(_metadata(), ensure_ascii=False)
            chat_line = json.dumps(_chat(), ensure_ascii=False)
            cases = {
                "middle": metadata_line + '\n{"type":"chat"\n' + chat_line + "\n",
                "newline-tail": metadata_line + '\n{"type":"chat"\n',
                "invalid-tail": metadata_line + "\nnot-json",
            }
            for name, contents in cases.items():
                with self.subTest(name=name):
                    source = base / f"{name}.danmaku.jsonl"
                    output = base / f"{name}.xml"
                    source.write_text(contents, encoding="utf-8")
                    output.write_text("keep target", encoding="utf-8")

                    with self.assertRaisesRegex(DanmakuConversionError, "第 2 行"):
                        convert_jsonl_to_xml(
                            source,
                            output,
                            overwrite=True,
                            recover_truncated_tail=True,
                        )

                    self.assertEqual(
                        output.read_text(encoding="utf-8"), "keep target"
                    )

    def test_direct_same_path_is_rejected_even_with_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "only.danmaku.jsonl"
            _write_jsonl(source, _metadata(), _chat())
            original = source.read_bytes()

            with self.assertRaisesRegex(DanmakuConversionError, "同一个文件"):
                convert_jsonl_to_xml(source, source, overwrite=True)

            self.assertEqual(source.read_bytes(), original)

    def test_symlink_and_hardlink_output_aliases_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "only.danmaku.jsonl"
            symlink_output = base / "symlink.xml"
            hardlink_output = base / "hardlink.xml"
            _write_jsonl(source, _metadata(), _chat())
            original = source.read_bytes()
            symlink_output.symlink_to(source)
            os.link(source, hardlink_output)

            for output in (symlink_output, hardlink_output):
                with self.subTest(output=output.name):
                    with self.assertRaisesRegex(
                        DanmakuConversionError, "同一个文件"
                    ):
                        convert_jsonl_to_xml(source, output, overwrite=True)
                    self.assertEqual(source.read_bytes(), original)

    def test_schema_version_and_time_units_are_exact(self) -> None:
        invalid_metadata = []
        for version in (True, 2, "1"):
            metadata = _metadata()
            metadata["version"] = version
            invalid_metadata.append((f"version={version!r}", metadata))

        for name, mutate in (
            ("missing-unit", lambda units: units.pop("offset_ms")),
            ("wrong-unit", lambda units: units.__setitem__("offset_ms", "seconds")),
            ("extra-unit", lambda units: units.__setitem__("extra", "milliseconds")),
        ):
            metadata = _metadata()
            mutate(metadata["time_units"])
            invalid_metadata.append((name, metadata))

        with tempfile.TemporaryDirectory() as directory:
            for index, (name, metadata) in enumerate(invalid_metadata):
                with self.subTest(name=name):
                    source = Path(directory) / f"metadata-{index}.danmaku.jsonl"
                    _write_jsonl(source, metadata, _chat())
                    with self.assertRaises(DanmakuConversionError):
                        convert_jsonl_to_xml(source)

    def test_sequence_must_start_at_one_and_strictly_increment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for index, sequences in enumerate(((2,), (1, 3), (1, 1))):
                with self.subTest(sequences=sequences):
                    chats = []
                    for sequence in sequences:
                        chat = _chat()
                        chat["sequence"] = sequence
                        chats.append(chat)
                    source = Path(directory) / f"sequence-{index}.danmaku.jsonl"
                    _write_jsonl(source, _metadata(), *chats)

                    with self.assertRaisesRegex(
                        DanmakuConversionError, "sequence"
                    ):
                        convert_jsonl_to_xml(source)

    def test_numeric_fields_reject_bool(self) -> None:
        cases = []
        metadata = _metadata()
        metadata["recording_started_at_unix_ms"] = False
        cases.append(("recording-start", metadata, _chat()))
        for field in ("sequence", "offset_ms", "received_at_unix_ms"):
            chat = _chat()
            chat[field] = True
            cases.append((field, _metadata(), chat))

        with tempfile.TemporaryDirectory() as directory:
            for index, (name, metadata, chat) in enumerate(cases):
                with self.subTest(name=name):
                    source = Path(directory) / f"bool-{index}.danmaku.jsonl"
                    _write_jsonl(source, metadata, chat)
                    with self.assertRaises(DanmakuConversionError):
                        convert_jsonl_to_xml(source)

    def test_chat_user_and_content_types_are_strict(self) -> None:
        invalid_chats = []
        for field, value in (("id", 1), ("nickname", False)):
            chat = _chat()
            chat["user"][field] = value
            invalid_chats.append((f"user-{field}", chat))
        for field in ("id", "nickname"):
            chat = _chat()
            del chat["user"][field]
            invalid_chats.append((f"missing-user-{field}", chat))
        for value in (None, 1, True):
            chat = _chat()
            chat["content"] = value
            invalid_chats.append((f"content-{value!r}", chat))
        missing_content = _chat()
        del missing_content["content"]
        invalid_chats.append(("missing-content", missing_content))

        with tempfile.TemporaryDirectory() as directory:
            for index, (name, chat) in enumerate(invalid_chats):
                with self.subTest(name=name):
                    source = Path(directory) / f"chat-{index}.danmaku.jsonl"
                    _write_jsonl(source, _metadata(), chat)
                    with self.assertRaises(DanmakuConversionError):
                        convert_jsonl_to_xml(source)

    def test_cli_overwrite_and_invalid_input_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "foo.danmaku.jsonl"
            output = Path(directory) / "foo.xml"
            _write_jsonl(source, _metadata(), _chat())
            output.write_text("old", encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main([str(source), "--overwrite"])
            self.assertEqual(exit_code, 0)
            self.assertIn(str(output), stdout.getvalue())
            self.assertEqual(ET.parse(output).getroot().tag, "i")

            source.write_text("not-json\n", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main([str(source), "--output", str(output), "--overwrite"])
            self.assertEqual(exit_code, 1)
            self.assertIn("第 1 行", stderr.getvalue())
            self.assertEqual(ET.parse(output).getroot().tag, "i")

    def test_cli_recover_truncated_tail_warns_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "recover-cli.danmaku.jsonl"
            output = Path(directory) / "recover-cli.xml"
            source.write_text(
                json.dumps(_metadata(), ensure_ascii=False)
                + "\n"
                + json.dumps(_chat(), ensure_ascii=False)
                + '\n{"type":"chat"',
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [str(source), "--recover-truncated-tail"]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn(str(output), stdout.getvalue())
            self.assertIn("警告", stderr.getvalue())
            self.assertIn("第 3 行", stderr.getvalue())
            self.assertEqual(len(ET.parse(output).getroot().findall("d")), 1)


if __name__ == "__main__":
    unittest.main()
