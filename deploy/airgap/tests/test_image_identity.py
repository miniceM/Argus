"""同一制品在经典 Docker 与 containerd 存储后端的身份核验。"""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import deploy


def archive(path, *, corrupt=False, wrong_config=False):
    cfg = json.dumps({'architecture': 'arm64', 'os': 'linux', 'config': {}}).encode()
    cfg_id = 'sha256:' + hashlib.sha256(cfg).hexdigest()
    manifest = json.dumps({'schemaVersion': 2, 'config': {'digest': 'sha256:' + '0' * 64 if wrong_config else cfg_id}, 'layers': []}).encode()
    digest = 'sha256:' + hashlib.sha256(manifest).hexdigest()
    files = {'manifest.json': json.dumps([{'Config': 'config.json', 'RepoTags': ['test:one'], 'Layers': []}]).encode(),
             'config.json': cfg, 'index.json': json.dumps({'schemaVersion': 2, 'manifests': [{'digest': digest}]}).encode(),
             'blobs/sha256/' + digest[7:]: manifest + (b'x' if corrupt else b'')}
    with tarfile.open(path, 'w') as tf:
        for name, body in files.items():
            m = tarfile.TarInfo(name)
            m.size = len(body)
            tf.addfile(m, io.BytesIO(body))
    return cfg_id, digest


class ImageIdentityTests(unittest.TestCase):
    def test_accepts_both_backends_only_when_manifest_links_exact_config(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'image.tar'
            cfg, digest = archive(path)
            self.assertEqual(common.archive_image_ids(path, 'test:one'), {cfg, digest})

    def test_rejects_corrupted_oci_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'image.tar'
            archive(path, corrupt=True)
            with self.assertRaises(common.Stop):
                common.archive_image_ids(path, 'test:one')

    def test_rejects_manifest_pointing_to_other_config(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'image.tar'
            archive(path, wrong_config=True)
            with self.assertRaises(common.Stop):
                common.archive_image_ids(path, 'test:one')

    def check_load(self, actual_id, should_pass):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sha = 'b' * 40
            p = root / 'releases' / sha / 'payload' / 'image.tar'
            p.parent.mkdir(parents=True)
            cfg, digest = archive(p)
            actual = {'manifest': digest, 'config': cfg, 'other': 'sha256:' + '9' * 64}[actual_id]
            release = {'images': {'console': {'ref': 'test:one', 'id': cfg, 'file': 'image.tar'}}}
            with mock.patch.object(deploy, 'verify_source', return_value=release), \
                 mock.patch.object(deploy, 'docker', return_value=(actual + '\n').encode()), \
                 mock.patch.object(deploy, 'inspect_one', return_value={'Id': actual, 'Architecture': 'arm64'}):
                if should_pass:
                    deploy.load_images(root, sha, smoke=False)
                else:
                    with self.assertRaises(common.Stop):
                        deploy.load_images(root, sha, smoke=False)

    def test_load_accepts_containerd_manifest_id(self):
        self.check_load('manifest', True)

    def test_load_accepts_classic_config_id(self):
        self.check_load('config', True)

    def test_load_rejects_unrelated_same_tag(self):
        self.check_load('other', False)
