from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import file_ops, live_history, platforms, recorder, runtime, url_config, utils
from src.config_loader import Settings, text_encoding
from webui import server


class SettingsSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'config.ini'

    def save(self, content):
        self.path.write_text(content, encoding=text_encoding)

    def test_missing_file_generates_template_once_without_quality_setting(self):
        settings = Settings(str(self.path))
        self.assertEqual(settings.video_record_quality, '原画')
        self.assertEqual(settings.delay_default, 120)
        self.assertEqual(settings.split_time, '1800')
        self.assertNotIn('原画|超清', self.path.read_text(encoding=text_encoding))
        with patch.object(Path, 'read_text', side_effect=AssertionError('unchanged file was read')):
            self.assertFalse(settings.reload())

    def test_changed_file_is_read_once_and_never_rewritten(self):
        self.save('[录制设置]\n循环时间(秒) = 120\n[Cookie]\n抖音cookie = keep%cookie\n')
        settings = Settings(str(self.path))
        self.save('# 保留注释\n[录制设置]\n循环时间(秒) = 60\n[Cookie]\n抖音cookie = keep%cookie\n')
        original = self.path.read_bytes()
        with patch.object(Path, 'read_text', autospec=True, wraps=None) as read:
            read.return_value = original.decode(text_encoding)
            self.assertTrue(settings.reload())
            self.assertEqual(read.call_count, 1)
        self.assertEqual(settings.delay_default, 60)
        self.assertEqual(settings.dy_cookie, 'keep%cookie')
        self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaises(AttributeError):
            _ = settings.no_such_setting

    def test_rejected_update_keeps_entire_snapshot_and_recovers(self):
        self.save('[录制设置]\n直播保存路径(不填则默认) = /original\n循环时间(秒) = 77\n')
        settings = Settings(str(self.path))
        old_values = settings._values
        self.save('[录制设置]\n直播保存路径(不填则默认) = /new\n循环时间(秒) = 120秒\n')
        with patch('src.config_loader.logger.error') as error:
            self.assertFalse(settings.reload())
            self.assertIn('循环时间(秒)', error.call_args.args[0])
            with patch.object(Path, 'read_text', side_effect=AssertionError('unchanged invalid file reread')):
                self.assertFalse(settings.reload())
            self.assertEqual(error.call_count, 1)
        self.assertIs(settings._values, old_values)
        self.assertEqual(settings.video_save_path, '/original')
        self.assertEqual(settings.delay_default, 77)
        self.save('[录制设置]\n直播保存路径(不填则默认) = /new\n循环时间(秒) = 90\n')
        self.assertTrue(settings.reload())
        self.assertEqual(settings.video_save_path, '/new')
        self.assertEqual(settings.delay_default, 90)

    def test_removed_option_and_cookie_do_not_retain_stale_values(self):
        self.save('[录制设置]\n循环时间(秒) = 77\n原画|超清|高清|标清|流畅 = 流畅\n[Cookie]\n抖音cookie = old-cookie\n')
        settings = Settings(str(self.path))
        self.assertEqual(settings.video_record_quality, '原画')
        self.save('[录制设置]\n# 删除项回到默认\n[Cookie]\n')
        self.assertTrue(settings.reload())
        self.assertEqual(settings.delay_default, 120)
        self.assertEqual(settings.dy_cookie, '')

    def test_validation_rejects_bad_numbers_booleans_formats_and_ini(self):
        self.save('[录制设置]\n循环时间(秒) = 77\n')
        settings = Settings(str(self.path))
        for content in (
            '[录制设置]\n同一时间访问网络的线程数 = 0\n',
            '[录制设置]\n循环时间(秒) = -1\n',
            '[录制设置]\n视频分段时间(秒) = 0\n',
            '[录制设置]\n录制空间剩余阈值(gb) = nan\n',
            '[录制设置]\n录制空间剩余阈值(gb) = inf\n',
            '[录制设置]\n是否录制弹幕 = yes\n',
            '[录制设置]\n视频保存格式ts|mkv|flv|mp4 = bad\n',
            '[录制设置]\n循环时间(秒) = 1\n循环时间(秒) = 2\n',
            'not ini; private-cookie', '',
        ):
            with self.subTest(content=content), patch('src.config_loader.logger.error') as error:
                self.save(content)
                self.assertFalse(settings.reload())
                self.assertEqual(settings.delay_default, 77)
                self.assertNotIn('private-cookie', str(error.call_args_list))

    def test_first_start_invalid_config_fails_without_overwrite(self):
        self.save('[录制设置]\n循环时间(秒) = bad\n')
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '未启动录制.*循环时间'):
            Settings(str(self.path))
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_unreadable_and_changed_during_read_keep_previous(self):
        self.save('[录制设置]\n循环时间(秒) = 77\n')
        settings = Settings(str(self.path))
        self.path.unlink()
        self.assertFalse(settings.reload())
        self.assertFalse(self.path.exists())
        self.save('[录制设置]\n循环时间(秒) = 90\n')
        with patch.object(Path, 'read_text', side_effect=PermissionError('private-path')):
            self.assertFalse(settings.reload())
        signature = settings._signature(self.path)
        with patch.object(settings, '_signature', side_effect=[signature, ('changed',)]):
            self.assertFalse(settings.reload())
        self.assertEqual(settings.delay_default, 77)
        self.assertTrue(settings.reload())
        self.assertEqual(settings.delay_default, 90)

    def test_invalid_encoding_keeps_previous(self):
        self.save('[录制设置]\n循环时间(秒) = 77\n')
        settings = Settings(str(self.path))
        self.path.write_bytes(b'\xffnot-utf8')
        self.assertFalse(settings.reload())
        self.assertEqual(settings.delay_default, 77)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'config.ini'
        self.path.write_text('old', encoding='utf-8')

    def test_replace_preserves_permissions_and_readers_see_complete_versions(self):
        self.path.chmod(0o640)
        with self.path.open(encoding='utf-8') as old_reader:
            utils.atomic_write_text(self.path, '完整的新版本')
            self.assertEqual(old_reader.read(), 'old')
        self.assertEqual(self.path.read_text(), '完整的新版本')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_sync_or_replace_failure_preserves_original_and_cleans_temp(self):
        for operation in ('fsync', 'replace'):
            with self.subTest(operation=operation):
                with patch('src.utils.os.' + operation, side_effect=OSError('disk unavailable')):
                    with self.assertRaises(OSError):
                        utils.atomic_write_text(self.path, 'new')
                self.assertEqual(self.path.read_text(), 'old')
                self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_existing_line_helpers_do_not_truncate_on_failure(self):
        self.path.write_text('one\none\ntwo\n', encoding=text_encoding)
        original = self.path.read_bytes()
        calls = (
            lambda: file_ops.update_file(str(self.path), 'one', 'new'),
            lambda: file_ops.delete_line(str(self.path), 'one'),
            lambda: url_config.deduplicate_file(str(self.path)),
        )
        for call in calls:
            with patch('src.utils.os.replace', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    call()
            self.assertEqual(self.path.read_bytes(), original)


class ConfigApiTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.root / 'URL_config.ini'
        self.path.write_text('', encoding=text_encoding)
        stack.enter_context(patch.object(server, '_url_config_file', str(self.path)))
        stack.enter_context(patch.object(live_history, '_history_file', self.root / 'history.json'))
        stack.enter_context(patch.object(live_history, '_history', {}))
        for name, value in {
            'url_comments': [], 'running_list': [], 'recording': set(),
            'recording_time_list': {}, 'current_anchor_names': {}, 'pending_name_updates': {},
            'live_urls': set(), 'text_no_repeat_url': [], 'not_record_list': [],
            'url_tuples_list': [], 'first_start': False, 'exit_recording': False,
            'monitoring': 0, 'semaphore': threading.Semaphore(2),
        }.items():
            stack.enter_context(patch.object(runtime, name, value))
        self.client = stack.enter_context(TestClient(server.app, raise_server_exceptions=False))
        self.url = 'https://live.douyin.com/123'

    def test_add_ignores_old_quality_and_handles_missing_newline(self):
        self.path.write_text('高清,https://live.douyin.com/456,另一个主播', encoding=text_encoding)
        response = self.client.post('/api/streamers', json={
            'url': self.url, 'quality': '流畅\n原画,https://live.douyin.com/999', 'name': '  测试  ',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        lines = self.path.read_text(encoding=text_encoding).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1], f'原画,{self.url},测试')
        items = self.client.get('/api/streamers').json()
        self.assertEqual([item['quality'] for item in items], ['原画', '原画'])
        self.assertNotIn('quality', server.StreamerAdd.model_json_schema()['properties'])
        self.assertIn(self.url, live_history.get_snapshot())

    def test_rejects_config_injection_bad_urls_and_oversize_names(self):
        for payload in (
            {'url': self.url, 'name': 'one\ntwo'},
            {'url': self.url, 'name': 'one,two'},
            {'url': self.url, 'name': 'one，two'},
            {'url': self.url, 'name': 'one\u2028two'},
            {'url': self.url, 'name': 'one\x00two'},
            {'url': self.url, 'name': 'x' * 201},
            {'url': self.url + '\n'}, {'url': self.url + ',name'},
            {'url': 'https://live.douyin.com/'}, {'url': 'https://['},
            {'url': 'https://example.com/123'},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post('/api/streamers', json=payload).status_code, 422)
        self.assertEqual(self.path.read_text(encoding=text_encoding), '')
        self.assertEqual(live_history.get_snapshot(), {})

    def test_legacy_bare_domain_can_still_be_deleted_but_not_added(self):
        legacy_url = 'https://live.douyin.com/'
        self.path.write_text(f'原画,{legacy_url}\n', encoding=text_encoding)
        self.assertEqual(self.client.post('/api/streamers', json={'url': legacy_url}).status_code, 422)
        response = self.client.request('DELETE', '/api/streamers', json={'url': legacy_url})
        self.assertTrue(response.json()['ok'])
        self.assertEqual(self.path.read_text(encoding=text_encoding), '')

    def test_concurrent_duplicate_adds_commit_once(self):
        def add(_):
            return asyncio.run(server.add_streamer(server.StreamerAdd(url=self.url)))
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(add, range(12)))
        self.assertEqual(sum(result['ok'] for result in results), 1)
        self.assertEqual(self.path.read_text(encoding=text_encoding).splitlines(), [f'原画,{self.url}'])

    def test_failed_add_toggle_delete_preserve_file_and_stop_state(self):
        self.path.write_text(f'原画,{self.url},主播\n', encoding=text_encoding)
        original = self.path.read_bytes()
        live_history.initialize_missing([self.url])
        for method, endpoint, payload in (
            ('POST', '/api/streamers', {'url': 'https://live.douyin.com/456'}),
            ('PATCH', '/api/streamers/toggle', {'url': self.url}),
            ('DELETE', '/api/streamers', {'url': self.url}),
        ):
            with self.subTest(method=method), patch('src.utils.os.replace', side_effect=OSError('private-path')):
                response = self.client.request(method, endpoint, json=payload)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('private-path', response.text)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(runtime.url_comments, [])
            self.assertEqual(set(live_history.get_snapshot()), {self.url})

    def test_toggle_and_delete_publish_stop_only_after_atomic_commit(self):
        self.path.write_text(f'原画,{self.url},主播\n', encoding=text_encoding)
        self.assertTrue(self.client.patch('/api/streamers/toggle', json={'url': self.url}).json()['ok'])
        self.assertTrue(self.path.read_text(encoding=text_encoding).startswith('#'))
        self.assertIn(self.url, runtime.url_comments)
        self.assertTrue(self.client.patch('/api/streamers/toggle', json={'url': self.url}).json()['ok'])
        self.assertNotIn(self.url, runtime.url_comments)
        self.assertTrue(self.client.request('DELETE', '/api/streamers', json={'url': self.url}).json()['ok'])
        self.assertEqual(self.path.read_text(encoding=text_encoding), '')
        self.assertIn(self.url, runtime.url_comments)

    def test_single_url_is_visible_before_normalization(self):
        self.path.write_text(self.url + '\n', encoding=text_encoding)
        result = self.client.get('/api/streamers').json()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['url'], self.url)
        self.assertEqual(result[0]['quality'], '原画')
        self.assertFalse(self.client.post('/api/streamers', json={'url': self.url}).json()['ok'])

    def test_snapshot_normalizes_once_and_preserves_pause_and_url_identity(self):
        self.path.write_text(
            '# 说明\n\n流畅,https://live.douyin.com/123?from=test,主播: 目标\n'
            '#高清,https://live.douyin.com/1234,主播: 已暂停\n'
            'https://live.douyin.com/123\nhttps://www.xiaohongshu.com/livestream/456\n',
            encoding=text_encoding,
        )
        runtime.running_list[:] = [self.url, 'https://live.douyin.com/deleted']
        with patch('src.url_config.utils.atomic_write_text', wraps=utils.atomic_write_text) as write:
            active, seen = url_config._load_url_snapshot(str(self.path))
            self.assertEqual(write.call_count, 1)
            url_config._load_url_snapshot(str(self.path))
            self.assertEqual(write.call_count, 1)
        self.assertEqual(len(active), 2)
        self.assertTrue(all(row[0] == '原画' for row in active))
        self.assertEqual(len(seen), 3)
        self.assertNotIn(self.url, runtime.url_comments)
        self.assertIn('https://live.douyin.com/1234', runtime.url_comments)
        self.assertIn('https://live.douyin.com/deleted', runtime.url_comments)
        self.assertTrue(self.path.read_text(encoding=text_encoding).startswith('# 说明\n\n'))

    def test_dispatch_starts_each_active_url_once_at_original_quality(self):
        self.path.write_text(
            f'流畅,{self.url},主播\n#高清,https://live.douyin.com/456,暂停\n',
            encoding=text_encoding,
        )
        settings = SimpleNamespace(local_delay_default=0)
        with patch('src.url_config.threading.Thread') as worker:
            url_config.load_and_dispatch(str(self.path), settings, str(self.root), '')
            url_config.load_and_dispatch(str(self.path), settings, str(self.root), '')
            worker.assert_called_once()
            worker.return_value.start.assert_called_once()
            self.assertEqual(worker.call_args.kwargs['args'][0], ('原画', self.url, '主播'))
        self.assertEqual(runtime.running_list, [self.url])
        self.assertIn('https://live.douyin.com/456', runtime.url_comments)

    def test_failed_snapshot_does_not_publish_partial_stop_flags(self):
        self.path.write_text(f'#高清,{self.url}\n', encoding=text_encoding)
        runtime.running_list.append(self.url)
        with patch('src.utils.os.replace', side_effect=OSError('no space')):
            with self.assertRaises(OSError):
                url_config._load_url_snapshot(str(self.path))
        self.assertEqual(runtime.url_comments, [])
        self.path.write_bytes(b'\xff')
        with self.assertRaises(UnicodeError):
            url_config._load_url_snapshot(str(self.path))
        self.assertEqual(runtime.url_comments, [])

    def test_missing_hot_config_is_not_recreated_as_empty(self):
        self.path.unlink()
        runtime.running_list.append(self.url)
        url_config.ensure_url_config_file(str(self.path))
        with patch('src.url_config.logger.error'):
            url_config.load_and_dispatch(str(self.path), SimpleNamespace(), str(self.root), '')
        self.assertFalse(self.path.exists())
        self.assertEqual(runtime.url_comments, [])

    def test_remote_name_cannot_inject_extra_config_rows(self):
        self.path.write_text(f'高清,{self.url},旧名\n', encoding=text_encoding)
        url_config._apply_name_updates(str(self.path), {self.url: '昵称\n原画,https://live.douyin.com/999'}, '原画')
        self.assertEqual(len(self.path.read_text(encoding=text_encoding).splitlines()), 1)
        self.assertEqual(url_config.get_configured_urls(str(self.path), '原画'), [self.url])

    def test_worker_ignores_low_quality_even_for_legacy_callers(self):
        settings = SimpleNamespace(clean_emoji=False, delay_default=120)
        for url, platform in ((self.url, platforms.DOUYIN),
                              ('https://www.xiaohongshu.com/livestream/456', platforms.XIAOHONGSHU)):
            with self.subTest(url=url), patch('src.recorder._fetch_stream_info', return_value=(
                {'anchor_name': '主播', 'is_live': False, 'fetch_ok': True}, {},
            )) as fetch, patch('src.recorder._sleep_or_stop', return_value=True):
                recorder.start_record(('流畅', url, '主播'), settings, str(self.root), 1)
                fetch.assert_called_once_with(platform, url, 'OD', settings)


if __name__ == '__main__':
    unittest.main()
