"""鲲鹏 64 KiB 页内核的 Console 配置和离线导入门禁。"""
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deploy


class ConsolePagesTests(unittest.TestCase):
    def test_upstream_zone_supports_64k_pages(self):
        template = Path(__file__).resolve().parents[3] / 'services/console/nginx.conf.template'
        if not template.exists():
            self.skipTest('独立工具包不含 Console 源码；此项在仓库执行')
        match = re.search(r'zone\s+argus_runner\s+(\d+)([km]);', template.read_text())
        self.assertIsNotNone(match)
        size = int(match[1]) * {'k': 1024, 'm': 1024 * 1024}[match[2]]
        # ngx_http_upstream_zone: size >= 8 * ngx_pagesize。
        for page_size in (4096, 16384, 65536):
            with self.subTest(page_size=page_size):
                self.assertGreaterEqual(size, 8 * page_size)

    def test_load_validates_rendered_config_and_stops_before_receipt_on_error(self):
        manifest = {'images': {'console': {'ref': 'test:console', 'file': 'console.tar'}}}
        calls = []

        def docker(*args, **kwargs):
            calls.append(args)
            if args[0] == 'run':
                raise RuntimeError('nginx configuration rejected')
            return b'sha256:test\n'

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(deploy, 'verify_source', return_value=manifest), \
             mock.patch.object(deploy, 'archive_image_ids', return_value={'sha256:test'}), \
             mock.patch.object(deploy, 'inspect_one', return_value={'Id': 'sha256:test', 'Architecture': 'arm64'}), \
             mock.patch.object(deploy, 'docker', side_effect=docker):
            with self.assertRaisesRegex(RuntimeError, 'configuration rejected'):
                deploy.load_images(td, 'a' * 40)
            self.assertFalse((Path(td) / 'evidence').exists())
        command = next(c for c in calls if c[0] == 'run')
        self.assertIn('RUNNER_HOST=127.0.0.1', command)
        self.assertNotIn('--entrypoint', command)
        self.assertEqual(command[-3:], ('test:console', 'nginx', '-t'))
        self.assertIn('none', command)
