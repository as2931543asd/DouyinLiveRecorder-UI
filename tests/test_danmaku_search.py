from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import danmaku_search


class DanmakuSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        anchor_directory = self.root / "抖音直播" / "主播Alpha"
        anchor_directory.mkdir(parents=True)
        self.source = anchor_directory / "主播Alpha_2026-08-23_01-02-03.danmaku.jsonl"
        records = [
            {
                "type": "metadata",
                "schema": "douyin-live-recorder-ui.danmaku",
                "version": 1,
                "room_identifier": "room-1",
                "recording_started_at_unix_ms": 1_700_000_000_000,
            },
            {
                "type": "chat",
                "offset_ms": 1_000,
                "user": {"id": "viewer-1", "nickname": "Alice"},
                "content": "大家晚安",
            },
            {
                "type": "chat",
                "received_at_unix_ms": 1_700_000_005_000,
                "offset_ms": 5_000,
                "user": {"id": None, "nickname": None},
                "content": "第二条",
            },
        ]
        text = "".join(
            json.dumps(record, ensure_ascii=False) + "\n" for record in records
        )
        # Simulate an actively written, incomplete final JSON object.
        self.source.write_bytes((text + '{"type":"chat"').encode())

    def execute(self, *arguments: str) -> tuple[int, list[str], str]:
        args = danmaku_search.build_parser().parse_args(
            [*arguments, "--root", str(self.root), "--json"]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = danmaku_search.run(args)
        return return_code, stdout.getvalue().splitlines(), stderr.getvalue()

    def test_searches_content_and_reports_anchor_and_source(self) -> None:
        return_code, output_lines, summary = self.execute("晚安", "--scope", "content")

        self.assertEqual(return_code, 0)
        self.assertEqual(len(output_lines), 1)
        result = json.loads(output_lines[0])
        self.assertEqual(result["anchor"], "主播Alpha")
        self.assertEqual(result["viewer_nickname"], "Alice")
        self.assertEqual(result["content"], "大家晚安")
        self.assertEqual(result["matched_fields"], ["content"])
        self.assertEqual(result["timestamp_ms"], 1_700_000_001_000)
        self.assertTrue(result["file"].endswith(self.source.name))
        self.assertIn("跳过 1 条损坏或不完整记录", summary)

    def test_all_keywords_can_match_across_anchor_and_content(self) -> None:
        return_code, output_lines, _ = self.execute(
            "主播alpha", "晚安", "--scope", "both", "--match", "all"
        )

        self.assertEqual(return_code, 0)
        result = json.loads(output_lines[0])
        self.assertEqual(result["matched_fields"], ["content", "anchor"])

    def test_anchor_filter_is_independent_of_scope(self) -> None:
        return_code, output_lines, summary = self.execute(
            "晚安", "--scope", "content", "--anchor", "不存在的主播"
        )

        self.assertEqual(return_code, 1)
        self.assertEqual(output_lines, [])
        self.assertIn("找到 0 条结果", summary)

    def test_viewer_scope_is_case_insensitive_by_default(self) -> None:
        return_code, output_lines, _ = self.execute("alice", "--scope", "viewer")

        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(output_lines[0])["matched_fields"], ["viewer"])

    def test_anchor_skips_temp_recording_directory(self) -> None:
        path = self.root / "抖音直播" / "TempRec" / "主播Beta" / "主播Beta_2026-08-23_01-02-03.danmaku.jsonl"
        self.assertEqual(danmaku_search.infer_anchor(path, self.root), "主播Beta")

    def test_excluded_directory_is_always_rejected(self) -> None:
        stats = danmaku_search.SearchStats()
        with self.assertRaisesRegex(ValueError, "拒绝搜索已排除目录"):
            list(
                danmaku_search.iter_danmaku_files(
                    danmaku_search.EXCLUDED_DIRECTORY / "child", stats
                )
            )


if __name__ == "__main__":
    unittest.main()
