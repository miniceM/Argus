"""真实 git archive 和归档接收流程；镜像构建替换为明确的模拟归档。"""
import contextlib
import hashlib
import io
import json
import shutil
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import pack
import receive


class PackFlowTests(unittest.TestCase):
    def test_mac_pack_fixed_git_snapshot_to_linux_receiver(self):
        with tempfile.TemporaryDirectory(prefix='argus-pack-test-') as td:
            root = Path(td)
            repo, out = root / 'repo', root / 'external drive with spaces' / 'release'
            repo.mkdir()
            for name, content in {
                'config/agents.yaml': 'agents: []\n', 'data/dataset.json': '{"items": []}\n',
                'docs/openapi.json': '{}\n', 'migrations/001_initial_schema.sql': 'SELECT 1;\n',
                'services/eval-runner/Dockerfile': 'ARG RUNNER_VERSION=0.1.0\n',
                'services/demo-agent/Dockerfile': 'FROM scratch\n',
                'services/console/Dockerfile': 'FROM scratch\n',
                'services/console/package.json': '{"version":"0.2.0"}\n'
            }.items():
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            shutil.copytree(Path(__file__).resolve().parents[1], repo / 'deploy/airgap',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'SHA256SUMS'))
            common.run(['git', 'init', '-q', repo])
            common.run(['git', '-C', repo, 'remote', 'add', 'origin', 'https://github.com/miniceM/Argus.git'])
            common.run(['git', '-C', repo, 'add', '.'])
            common.run(['git', '-C', repo, '-c', 'user.name=Skill Test', '-c', 'user.email=skill-test@example.invalid',
                        'commit', '-qm', 'synthetic fixture'])
            sha = common.run(['git', '-C', repo, 'rev-parse', 'HEAD']).decode().strip()
            # 未提交变更不得进入交付包，打包也不得重置用户工作树。
            (repo / 'data/dataset.json').write_text('{"uncommitted": true}\n')
            def simulated_docker(*args, **kwargs):
                if args[:2] != ('buildx', 'build'):
                    return b'simulated docker capability check\n'
                ref = args[args.index('-t') + 1]
                destination = Path(args[args.index('--output') + 1].split('dest=', 1)[1])
                cfg = json.dumps({'architecture': 'arm64', 'os': 'linux', 'config': {
                    'Env': ['ARGUS_BUILD_ID=' + sha], 'Labels': {'org.opencontainers.image.revision': sha}}}).encode()
                digest = hashlib.sha256(cfg).hexdigest()
                manifest = json.dumps([{'Config': digest + '.json', 'RepoTags': [ref], 'Layers': []}]).encode()
                with tarfile.open(destination, 'w') as tf:
                    for name, data in ((digest + '.json', cfg), ('manifest.json', manifest)):
                        header = tarfile.TarInfo(name)
                        header.size = len(data)
                        tf.addfile(header, io.BytesIO(data))
                return b''
            argv = ['pack.py', '--repo', str(repo), '--ref', sha, '--out', str(out)]
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(pack, 'docker', side_effect=simulated_docker), \
                 contextlib.redirect_stdout(io.StringIO()):
                pack.main()
            receipt = common.read_json(out / 'PACK-RECEIPT.json')
            self.assertEqual(receipt['git_sha'], sha)
            self.assertEqual(receipt['part_bytes'], 280000000)
            with contextlib.redirect_stdout(io.StringIO()):
                receive.receive(root / 'linux', out / 'transfer', receipt['parts_manifest_sha256'], 10000000)
            release = root / 'linux/releases' / sha
            self.assertEqual(json.loads((release / 'source/data/dataset.json').read_text()), {'items': []})
            self.assertEqual(json.loads((repo / 'data/dataset.json').read_text()), {'uncommitted': True})
            self.assertTrue((release / 'payload/toolkit/SKILL.md').exists())
            self.assertTrue((release / 'payload/toolkit/tests/test_safety.py').exists())
            self.assertEqual(receipt['target_acceptance'], 'NOT_RUN')


if __name__ == '__main__':
    unittest.main()
