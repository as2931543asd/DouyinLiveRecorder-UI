from __future__ import annotations

import csv
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.danmaku_index import DanmakuIndex, IndexBusyError
from src.video_locator import locate_video, probe_duration, safe_recording_path, segment_list_path
from webui import danmaku_api, server

START = 1_700_000_000_000


class ArchiveFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'recordings'
        self.folder = self.root / '抖音直播' / '主播Alpha'
        self.folder.mkdir(parents=True)
        self.base = '主播Alpha_2026-09-01_23-59-59'
        self.source = self.folder / (self.base + '.danmaku.jsonl')
        self.relative = str(self.source.relative_to(self.root))
        self.metadata = {'type': 'metadata', 'recording_started_at_unix_ms': START}
        self.write_records([self.metadata, self.chat(1000, '大家晚安'), self.chat(2000, '你好 100%_')])
        self.index = DanmakuIndex(self.root, Path(self.temp.name) / 'index.sqlite3')

    @staticmethod
    def chat(offset, content='晚上好', **extra):
        return {'type': 'chat', 'offset_ms': offset, 'user': {'nickname': 'Alice', 'id': '123'},
                'content': content, **extra}

    def write_records(self, records, mode='w'):
        with self.source.open(mode, encoding='utf-8') as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + '\n')

    def video(self, number=0, ext='.mp4'):
        path = self.folder / (self.base + (f'_{number:03d}' if number is not None else '') + ext)
        path.write_bytes(b'fake-video-data')
        return path

    def manifest(self, rows):
        path = self.folder / (self.base + '.segments.csv')
        with path.open('w', encoding='utf-8', newline='') as handle:
            csv.writer(handle).writerows(rows)
        return path


class IndexTests(ArchiveFixture):
    def test_filter_pagination_and_literal_search(self):
        status = self.index.refresh()
        self.assertEqual((status['files'], status['chats']), (1, 2))
        data = self.index.search(q='主播alpha 晚安', match='all', scope='both')
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['results'][0]['matched_fields'], ['content', 'anchor'])
        self.assertEqual(data['results'][0]['timestamp_ms'], START + 1000)
        self.assertEqual(self.index.search(q='100%_')['total'], 1)
        self.assertEqual(self.index.search(q="' OR 1=1 --")['total'], 0)
        self.assertEqual(self.index.search(anchor='alpha', viewer='ALICE')['total'], 2)
        self.assertEqual(self.index.search(viewer='missing')['total'], 0)
        self.assertEqual(self.index.search(since_ms=START+2000, until_ms=START+2000)['total'], 1)
        self.assertEqual(self.index.search(page=2, page_size=1)['results'][0]['offset_ms'], 1000)
        self.assertEqual(self.index.search(order='asc')['results'][0]['offset_ms'], 1000)
        with self.assertRaises(ValueError):
            self.index.search(since_ms=2, until_ms=1)

    def test_incremental_partial_tail_rewrite_and_delete(self):
        self.index.refresh()
        self.assertEqual(self.index.refresh()['changed_files'], 0)
        tail = json.dumps(self.chat(3000), ensure_ascii=False).encode()
        with self.source.open('ab') as handle:
            handle.write(tail[:-3])
        status = self.index.refresh()
        self.assertEqual((status['chats'], status['pending_files']), (2, 1))
        with self.source.open('ab') as handle:
            handle.write(tail[-3:] + b'\n')
        status = self.index.refresh()
        self.assertEqual((status['chats'], status['pending_files']), (3, 0))
        self.assertEqual(self.index.search()['results'][0]['line'], 4)
        self.write_records([self.metadata, self.chat(4000, '替换记录')])
        self.assertEqual(self.index.refresh()['chats'], 1)
        self.assertEqual(self.index.search()['results'][0]['content'], '替换记录')
        self.source.unlink()
        self.assertEqual(self.index.refresh()['chats'], 0)

    def test_corruption_and_timestamp_fallback(self):
        self.write_records([self.metadata, [], self.chat(10, content=None), self.chat(50),
                            self.chat(60, received_at_unix_ms=START+100),
                            self.chat(70, received_at_unix_ms=10**100)])
        with self.source.open('ab') as handle:
            handle.write(b'invalid json\n')
        status = self.index.refresh()
        self.assertEqual(status['bad_records'], 3)
        data = self.index.search()
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['results'][0]['timestamp_ms'], START+100)
        self.assertEqual(data['results'][-1]['timestamp_ms'], None)
        self.assertEqual(self.index.search(since_ms=START)['total'], 2)

    def test_refresh_lock_and_persistence(self):
        self.index.refresh()
        with self.index._refresh_lock:
            with self.assertRaises(IndexBusyError):
                self.index.refresh()
            self.assertEqual(self.index.search()['total'], 2)
        reopened = DanmakuIndex(self.root, self.index.database)
        self.assertEqual(reopened.search()['total'], 2)
        other = Path(self.temp.name) / 'other'
        other.mkdir()
        self.assertEqual(DanmakuIndex(other, self.index.database).search()['total'], 0)

    def test_rejects_outside_symlinks(self):
        outside = Path(self.temp.name) / 'outside.danmaku.jsonl'
        outside.write_text(self.source.read_text(), encoding='utf-8')
        self.source.unlink()
        self.source.symlink_to(outside)
        status = self.index.refresh()
        self.assertEqual(status['chats'], 0)
        self.assertEqual(status['file_errors'], 1)


class VideoTests(ArchiveFixture):
    def test_manifest_boundary_and_converted_video(self):
        first, second = self.video(0), self.video(1)
        self.manifest([(first.with_suffix('.ts').name, 0, 3602.25),
                       (second.with_suffix('.ts').name, 3602.25, 7204)])
        result = locate_video(self.root, self.relative, 3_602_250)
        self.assertEqual(result['status'], 'located')
        self.assertEqual(result['file'], str(second.relative_to(self.root)))
        self.assertEqual(result['offset_ms'], 0)
        self.assertTrue(locate_video(self.root, self.relative, 3_602_249)['file'].endswith('_000.mp4'))
        self.assertEqual(locate_video(self.root, self.relative, 7_204_000)['status'], 'unresolved')

    def test_missing_segment_never_chooses_neighbor(self):
        second = self.video(1)
        self.manifest([(self.base+'_000.ts', 0, 10), (second.name, 10, 20)])
        self.assertEqual(locate_video(self.root, self.relative, 5000)['status'], 'unresolved')
        self.assertEqual(locate_video(self.root, self.relative, 15000)['offset_ms'], 5000)

    def test_legacy_uses_actual_duration_not_nominal_hour(self):
        self.video(0); self.video(1)
        with patch('src.video_locator.probe_duration', return_value=3_602_250):
            result = locate_video(self.root, self.relative, 3_603_000)
        self.assertEqual(result['status'], 'estimated')
        self.assertTrue(result['file'].endswith('_001.mp4'))
        self.assertEqual(result['offset_ms'], 750)
        (self.folder / (self.base+'_000.mp4')).unlink()
        with patch('src.video_locator.probe_duration', return_value=3_602_250):
            self.assertEqual(locate_video(self.root, self.relative, 3_603_000)['status'], 'unresolved')

    def test_unprobeable_or_missing_time(self):
        self.video(None)
        with patch('src.video_locator.probe_duration', return_value=None):
            self.assertEqual(locate_video(self.root, self.relative, 1000)['status'], 'unresolved')
        self.assertEqual(locate_video(self.root, self.relative, None)['status'], 'unresolved')
        self.assertEqual(locate_video(self.root, self.relative, -1)['status'], 'unresolved')

    def test_probe_failures_are_retryable_and_success_is_cached(self):
        video = self.video(0)
        with patch('src.video_locator.shutil.which', return_value='/usr/bin/ffprobe'), patch(
            'src.video_locator.subprocess.run',
            side_effect=[subprocess.TimeoutExpired('ffprobe', 5), SimpleNamespace(stdout='2.5')],
        ) as run:
            self.assertIsNone(probe_duration(video))
            self.assertEqual(probe_duration(video), 2500)
            self.assertEqual(probe_duration(video), 2500)
            self.assertEqual(run.call_count, 2)

    def test_manifest_corruption_or_overlap_is_not_guessed(self):
        first = self.video(0); second = self.video(1)
        for rows in [[(first.name, 'NaN', 10)], [(first.name, 0, 10), (second.name, 9, 20)],
                     [(first.name, 0, 10), (first.name, 10, 20)]]:
            self.manifest(rows)
            self.assertEqual(locate_video(self.root, self.relative, 9500)['status'], 'unresolved')

    def test_path_containment_and_file_types(self):
        with self.assertRaises(ValueError):
            safe_recording_path(self.root, '../index.sqlite3', {'.sqlite3'})
        with self.assertRaises(ValueError):
            safe_recording_path(self.root, str(self.source), {'.jsonl'})
        outside = Path(self.temp.name) / 'private.mp4'; outside.write_bytes(b'secret')
        self.video(0).unlink()
        link = self.folder / (self.base+'_000.mp4'); link.symlink_to(outside)
        self.assertEqual(locate_video(self.root, self.relative, 1000)['candidates'], [])
        with self.assertRaises(ValueError):
            safe_recording_path(self.root, str(link.relative_to(self.root)), {'.mp4'})

    def test_record_command_saves_manifest(self):
        from src.recorder import _record_once
        settings = SimpleNamespace(filename_by_title=False, split_video_by_time=True,
                                   split_time='3600', proxy_addr=None, corrupt_probe_decode=False,
                                   record_danmaku=True, converts_to_mp4=False)
        with patch('src.recorder._check_subprocess', return_value='finished') as check:
            _record_once('https://example.invalid/live', '主播', None, str(self.folder),
                         'TS', 'record', 'https://live.douyin.com/123', settings,
                         danmaku_room_identifier='123')
        command = check.call_args.args[2]
        manifest = command[command.index('-segment_list') + 1]
        self.assertTrue(manifest.endswith('.segments.csv'))
        self.assertNotIn('%03d', manifest)
        self.assertEqual(segment_list_path('a_%03d.ts'), Path('a.segments.csv'))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
    def test_real_ffmpeg_segment_list_and_probe(self):
        template = self.folder / (self.base+'_%03d.ts')
        manifest = segment_list_path(template)
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=64x64:rate=10',
                        '-t', '5', '-c:v', 'mpeg2video', '-g', '20', '-f', 'segment',
                        '-segment_time', '2', '-reset_timestamps', '1', '-segment_list', str(manifest),
                        '-segment_list_type', 'csv', str(template)], check=True, timeout=20,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with manifest.open() as handle:
            rows = list(csv.reader(handle))
        boundary = round(float(rows[1][1]) * 1000)
        result = locate_video(self.root, self.relative, boundary)
        self.assertEqual(result['status'], 'located')
        self.assertTrue(result['file'].endswith('_001.ts'))
        self.assertEqual(result['offset_ms'], 0)
        self.assertGreater(probe_duration(self.root / result['file']), 0)


class ApiTests(ArchiveFixture):
    def setUp(self):
        super().setUp()
        self.patcher = patch.object(danmaku_api, '_index', self.index)
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def test_routes_validation_and_search(self):
        self.assertEqual(self.client.get('/danmaku').status_code, 200)
        self.assertIn('弹幕检索', self.client.get('/').text)
        self.assertEqual(self.client.post('/api/danmaku/index/refresh').status_code, 200)
        self.assertEqual(self.client.get('/api/danmaku/search', params={'q': '晚安'}).json()['total'], 1)
        for params in [{'page_size': 0}, {'page_size': 101}, {'scope': 'invalid'}, {'since_ms': -1}]:
            self.assertEqual(self.client.get('/api/danmaku/search', params=params).status_code, 422)
        self.assertEqual(self.client.get('/api/danmaku/search', params={'since_ms': 2, 'until_ms': 1}).status_code, 400)
        self.assertEqual(self.client.get('/api/danmaku/locate', params={'file': 'no', 'line': 1}).status_code, 404)
        with self.index._refresh_lock:
            self.assertEqual(self.client.post('/api/danmaku/index/refresh').status_code, 409)

    def test_unavailable_index_and_probe_capacity_report_errors(self):
        with patch.object(self.index, 'status', side_effect=OSError('disk unavailable')):
            self.assertEqual(self.client.get('/api/danmaku/index').status_code, 503)
        self.index.refresh()
        with danmaku_api._probe_slots, danmaku_api._probe_slots:
            response = self.client.get('/api/danmaku/locate', params={'file': self.relative, 'line': 2})
            self.assertEqual(response.status_code, 429)

    def test_media_range_and_traversal(self):
        video = self.video(0)
        params = {'file': str(video.relative_to(self.root))}
        response = self.client.get('/api/danmaku/media', params=params, headers={'Range': 'bytes=0-3'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b'fake')
        self.assertEqual(response.headers['content-range'], 'bytes 0-3/15')
        self.assertEqual(self.client.head('/api/danmaku/media', params=params).status_code, 200)
        self.assertEqual(self.client.get('/api/danmaku/media', params={**params, 'download': True}).headers['content-disposition'].split(';')[0], 'attachment')
        for file in ['../private.mp4', str(video), self.relative, 'config/config.ini']:
            self.assertEqual(self.client.get('/api/danmaku/media', params={'file': file}).status_code, 404)

    def test_locate_by_indexed_chat(self):
        video = self.video(0)
        self.manifest([(video.name, 0, 10)])
        self.index.refresh()
        response = self.client.get('/api/danmaku/locate', params={'file': self.relative, 'line': 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['offset_ms'], 1000)


if __name__ == '__main__':
    unittest.main()
