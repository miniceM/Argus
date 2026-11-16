"""站点密钥跨 release 保留，且不进入普通数据库备份。"""
import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import managed_secrets as managed


class ManagedSecretsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.model = {'services': {'eval-runner': {'environment': {}, 'volumes': []}}}
        for name in ('config', 'data', 'migrations'):
            source = self.root / 'release' / name
            source.mkdir(parents=True)
            (source / 'file').write_text('source')
            self.model['services']['eval-runner']['volumes'].append(
                {'type': 'bind', 'source': str(source), 'target': '/app/' + name, 'read_only': True})

    def test_fresh_site_generates_private_unique_files_and_preserves_bytes(self):
        managed.prepare(self.root, self.model, [])
        files = list((self.root / 'secrets').iterdir())
        self.assertEqual(len(files), 2)
        before = {p.name: p.read_bytes() for p in files}
        managed.prepare(self.root, self.model, [])
        self.assertEqual(before, {p.name: p.read_bytes() for p in files})
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in files))
        second = self.root / 'second'
        other = copy.deepcopy(self.model)
        other['services']['eval-runner']['environment'] = {}
        other['services']['eval-runner']['volumes'] = other['services']['eval-runner']['volumes'][:3]
        managed.prepare(second, other, [])
        self.assertNotEqual(before['keyring.json'], (second / 'secrets/keyring.json').read_bytes())

    def test_existing_ciphertext_without_key_fails_before_generating(self):
        with self.assertRaises(common.Stop):
            managed.prepare(self.root, self.model, [{'envelope': {'key_id': 'lost'}}])
        self.assertFalse((self.root / 'secrets/keyring.json').exists())

    def test_legacy_key_migrates_exactly_and_source_backup_excludes_it(self):
        legacy = self.root / 'release/config/.secrets'
        legacy.mkdir(mode=0o700)
        ring = {'active_key_id': 'old', 'keys': {'old': base64.b64encode(b'a' * 32).decode()}}
        for name, content in [('keyring.json', json.dumps(ring)), ('admin-token', 'old-private-token')]:
            p = legacy / name
            p.write_text(content)
            p.chmod(0o600)
        env = self.model['services']['eval-runner']['environment']
        env['ARGUS_MANAGED_SECRET_KEY_FILE'] = '/app/config/.secrets/keyring.json'
        env['ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE'] = '/app/config/.secrets/admin-token'
        managed.prepare(self.root, self.model, [])
        self.assertEqual((legacy / 'keyring.json').read_bytes(), (self.root / 'secrets/keyring.json').read_bytes())
        import deploy
        deploy.snapshot_mounts(self.model, self.root / 'backup')
        self.assertFalse((self.root / 'backup/source/config/.secrets').exists())
        self.assertTrue((legacy / 'keyring.json').exists())
        self.assertEqual(len(self.model['services']['eval-runner']['volumes']), 5)

    def test_missing_configured_file_or_unsafe_permissions_fail_closed(self):
        self.model['services']['eval-runner']['environment']['ARGUS_MANAGED_SECRET_KEY_FILE'] = '/missing'
        with self.assertRaises(common.Stop):
            managed.prepare(self.root, self.model, [])
        self.model['services']['eval-runner']['environment'] = {}
        managed.prepare(self.root, self.model, [])
        (self.root / 'secrets/keyring.json').chmod(0o644)
        with self.assertRaises(common.Stop):
            managed.prepare(self.root, self.model, [])

    def test_wrong_key_id_rejected_and_never_overwritten(self):
        managed.prepare(self.root, self.model, [])
        before = (self.root / 'secrets/keyring.json').read_bytes()
        with self.assertRaises(common.Stop):
            managed.prepare(self.root, self.model, [{'envelope': {'key_id': 'lost'}}])
        self.assertEqual(before, (self.root / 'secrets/keyring.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
