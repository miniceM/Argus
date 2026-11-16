import argparse
import copy
import gzip
import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deploy as site
import common
import pack
import receive


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def image(self, path, ref, sha='a' * 40, arch='arm64'):
        cfg = json.dumps({'architecture': arch, 'os': 'linux', 'config': {
            'Env': ['ARGUS_BUILD_ID=' + sha], 'Labels': {'org.opencontainers.image.revision': sha}}}).encode()
        digest = hashlib.sha256(cfg).hexdigest()
        manifest = json.dumps([{'Config': digest + '.json', 'RepoTags': [ref], 'Layers': []}]).encode()
        with tarfile.open(path, 'w') as tf:
            for name, data in ((digest + '.json', cfg), ('manifest.json', manifest)):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        return 'sha256:' + digest

    def payload(self):
        stage = self.root / 'stage'
        (stage / 'images').mkdir(parents=True)
        sha = 'a' * 40
        (stage / 'ARGUS_BUILD_ID').write_text(sha + '\n')
        source = self.root / 'source'
        for name in ('config/agents.yaml', 'data/dataset.json', 'migrations/001_initial.sql'):
            p = source / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('demo\n')
        name = 'argus-source-' + sha[:12] + '.tar.gz'
        common.make_tar(source, stage / name)
        images = {}
        for role in ('eval-runner', 'demo-agent', 'console'):
            ref = 'argus/' + role + ':git-' + sha[:12]
            image_id = self.image(stage / 'images' / (role + '.tar'), ref)
            images[role] = {'ref': ref, 'id': image_id, 'file': 'images/' + role + '.tar'}
        common.write_json(stage / 'release.json', {'format': 1, 'git_sha': sha, 'source': name, 'images': images,
            'migrations': {'001_initial.sql': common.sha_file(source / 'migrations/001_initial.sql')}})
        common.checksums(stage)
        return stage, sha

    def test_receive_round_trip_and_source_tamper(self):
        stage, sha = self.payload()
        archive = self.root / 'payload.tar.gz'
        common.make_tar(stage, archive)
        digest = pack.split_archive(archive, self.root / 'transfer', chunk_size=512)
        receive.receive(self.root / 'install', self.root / 'transfer', digest, 1000000)
        release = self.root / 'install/releases' / sha
        self.assertEqual(receive.verify_source(release)['git_sha'], sha)
        (release / 'source/config/agents.yaml').write_text('altered')
        with self.assertRaises(common.Stop):
            receive.verify_source(release)

    def test_receiver_rejects_extra_missing_and_corrupt_parts(self):
        archive = self.root / 'a.gz'
        archive.write_bytes(b'a' * 1200)
        transfer = self.root / 'transfer'
        digest = pack.split_archive(archive, transfer, chunk_size=512)
        extra = transfer / 'argus-arm64.tar.gz.part-zzz'
        extra.write_bytes(b'a')
        with self.assertRaises(common.Stop):
            receive.receive(self.root / 'i', transfer, digest, 99999)
        extra.unlink()
        part = transfer / 'argus-arm64.tar.gz.part-aab'
        original = part.read_bytes()
        part.write_bytes(b'tampered')
        with self.assertRaises(common.Stop):
            receive.receive(self.root / 'i', transfer, digest, 99999)
        part.write_bytes(original)
        part.unlink()
        with self.assertRaises((common.Stop, FileNotFoundError)):
            receive.receive(self.root / 'i', transfer, digest, 99999)

    def test_wrong_outer_hash_stops_before_extract(self):
        transfer = self.root / 'transfer'
        transfer.mkdir()
        (transfer / 'SHA256SUMS.parts').write_text('bogus')
        with self.assertRaises(common.Stop):
            receive.receive(self.root / 'i', transfer, '0' * 64, 1000)
        self.assertFalse((self.root / 'i/releases').exists())

    def test_receiver_checks_gzip_footer_even_with_matching_part_hashes(self):
        stage, sha = self.payload()
        archive = self.root / 'truncated.tar.gz'
        common.make_tar(stage, archive)
        archive.write_bytes(archive.read_bytes()[:-8])
        digest = pack.split_archive(archive, self.root / 'transfer')
        with self.assertRaises((common.Stop, EOFError, OSError)):
            receive.receive(self.root / 'install', self.root / 'transfer', digest, 1000000)
        self.assertFalse((self.root / 'install/releases' / sha).exists())

    def test_tar_rejects_traversal_links_duplicates(self):
        for kind in ('traversal', 'link', 'duplicate'):
            archive = self.root / (kind + '.tar')
            with tarfile.open(archive, 'w') as tf:
                info = tarfile.TarInfo('../escape' if kind == 'traversal' else 'file')
                if kind == 'link':
                    info.type = tarfile.SYMTYPE
                    info.linkname = '/etc/passwd'
                tf.addfile(info)
                if kind == 'duplicate':
                    tf.addfile(info)
            with self.assertRaises(common.Stop):
                common.safe_extract(archive, self.root / ('extract-' + kind))
        self.assertFalse((self.root / 'escape').exists())

    def test_checksums_exact_set_and_path_injection(self):
        d = self.root / 'd'
        d.mkdir()
        (d / 'one').write_text('one')
        common.checksums(d)
        common.verify_sums(d)
        (d / 'extra').write_text('extra')
        with self.assertRaises(common.Stop):
            common.verify_sums(d)
        (d / 'SHA256SUMS').write_text('0' * 64 + '  ../outside\n')
        with self.assertRaises(common.Stop):
            common.read_sums(d / 'SHA256SUMS')

    def test_images_require_cpu_and_embedded_sha(self):
        path = self.root / 'image.tar'
        self.image(path, 'test:one', arch='amd64')
        with self.assertRaises(common.Stop):
            common.image_archive(path, 'test:one')
        stage, _ = self.payload()
        self.image(stage / 'images/eval-runner.tar', 'argus/eval-runner:git-' + 'a' * 12, sha='b' * 40)
        manifest = common.read_json(stage / 'release.json')
        manifest['images']['eval-runner']['id'] = common.image_archive(stage / 'images/eval-runner.tar')['id']
        common.write_json(stage / 'release.json', manifest)
        common.checksums(stage)
        with self.assertRaises(common.Stop):
            common.verify_payload(stage)

    def test_migration_mutation_missing_and_new(self):
        self.assertEqual(common.validate_migrations({'001': 'a'}, {'001': 'a', '002': 'b'}), ['002'])
        for target in ({'001': 'b'}, {}):
            with self.assertRaises(common.Stop):
                common.validate_migrations({'001': 'a'}, target)

    def test_lock_and_env_literal(self):
        with common.operation(self.root / 'install'):
            with self.assertRaises(common.Stop):
                with common.operation(self.root / 'install'):
                    pass
        self.assertFalse((self.root / 'install/.operation.lock').exists())
        self.assertEqual(common.literal_config({'secret': '$not-expanded'}), {'secret': '$$not-expanded'})

    def test_protected_root_and_selected_langfuse_url(self):
        for value in ('/', '/data/langfuse', '/data/langfuse/subdir', '/data/langfuse-compat/data'):
            with self.assertRaises(common.Stop):
                common.deployment_root(value)
        self.assertEqual(common.deployment_root(self.root), self.root.resolve())
        identity = {'langfuse_web_hosts': ['langfuse-web', '172.20.0.2']}
        self.assertEqual(site.validate_lf_url('http://langfuse-web:3000/', identity), 'http://langfuse-web:3000')
        for url in ('http://other-service:3000', 'http://secret@langfuse-web:3000',
                    'http://langfuse-web:3000?redirect=other', 'http://langfuse-web:3000/path'):
            with self.assertRaises(common.Stop):
                site.validate_lf_url(url, identity)

    def test_only_runner_joins_selected_langfuse_network(self):
        identity = {'project': 'test', 'private_network': 'private', 'langfuse_network': 'existing-lf'}
        model = {'networks': {'private': {'name': 'private'}, 'lf': {'name': 'existing-lf'}},
                 'services': {s: {'networks': {'private': {}}} for s in common.SERVICES}}
        model['services']['eval-runner']['networks']['lf'] = {}
        def container(project, service):
            names = {'private': {}}
            if service == 'eval-runner':
                names['existing-lf'] = {}
            return {'NetworkSettings': {'Networks': names}}
        with mock.patch.object(site, 'service_container', side_effect=container):
            site.validate_topology(identity, model)
            model['services']['postgres']['networks']['lf'] = {}
            with self.assertRaises(common.Stop):
                site.validate_topology(identity, model)

    def test_quiesce_recognizes_partial_failed_as_terminal(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE experiment_launches(status text)')
            db.execute("INSERT INTO experiment_launches VALUES ('PARTIAL_FAILED')")
            query = lambda identity, value: str(db.execute(value).fetchone()[0])
            with mock.patch.object(site, 'sql', side_effect=query), mock.patch.object(site, 'compose'):
                site.quiesce(self.root, self.root / 'deployment', {})
                db.execute("INSERT INTO experiment_launches VALUES ('RUNNING')")
                with self.assertRaises(common.Stop):
                    site.quiesce(self.root, self.root / 'deployment', {})

    def test_runner_connections_reject_ambiguous_dns_and_wrong_database(self):
        identity = {'project': 'p', 'private_network': 'private', 'pg_database': 'argus', 'pg_user': 'argus'}
        def container(project, service):
            address = {'postgres': '172.20.0.2', 'redis': '172.20.0.3', 'eval-runner': '172.20.0.4'}[service]
            return {'Id': service, 'NetworkSettings': {'Networks': {'private': {'IPAddress': address}}}}
        resolved = {'DATABASE_URL': {'addresses': ['172.20.0.2'], 'database': 'argus', 'user': 'argus'},
                    'ARGUS_REDIS_URL': {'addresses': ['172.20.0.3'], 'database': '0', 'user': None}}
        with mock.patch.object(site, 'service_container', side_effect=container):
            with mock.patch.object(site, 'docker', return_value=json.dumps(resolved).encode()):
                site.validate_connections(identity)
            for field in ('ambiguous_dns', 'wrong_database'):
                changed = copy.deepcopy(resolved)
                if field == 'ambiguous_dns':
                    changed['DATABASE_URL']['addresses'].append('172.21.0.2')
                else:
                    changed['DATABASE_URL']['database'] = 'langfuse'
                with mock.patch.object(site, 'docker', return_value=json.dumps(changed).encode()), self.assertRaises(common.Stop):
                    site.validate_connections(identity)

    def test_candidate_changes_only_apps_and_preserves_credentials(self):
        root = self.root
        base = root / 'old'
        base.mkdir()
        source = root / 'releases' / ('a' * 40) / 'source'
        model = {'services': {s: {'image': 'old/' + s + ':locked', 'environment': {}, 'volumes': []} for s in common.SERVICES},
                 'networks': {'lf': {'external': True, 'name': 'already-existing'}},
                 'volumes': {'db': {'external': True, 'name': 'fixed-pg'}}}
        model['services']['eval-runner']['volumes'] = [{'target': '/app/' + n, 'source': '/old/' + n} for n in ('config', 'data', 'migrations')]
        model['services']['eval-runner']['environment'] = {'ARGUS_BUILD_ID': 'old', 'RUNNER_VERSION': 'old',
            'DATABASE_URL': 'private-db', 'ARGUS_CREDENTIAL_ADMIN_TOKEN': 'keep-me', 'SECRET_ENCRYPTION_KEY': 'keep-key'}
        common.write_json(base / 'model.json', model)
        common.write_json(root / 'site.json', {'project': 'test'})
        manifest = {'git_sha': 'a' * 40, 'images': {n: {'ref': 'new/' + n + ':sha'} for n in ('eval-runner', 'demo-agent', 'console')}}
        with mock.patch.object(site, 'verify_source', return_value=manifest), mock.patch.object(site, 'compose'):
            with mock.patch.object(site, 'credential_rows', return_value=[]), mock.patch.object(site, 'probe_secrets'):
                candidate = site.candidate_deployment(root, 'a' * 40, base)
        new = common.read_json(candidate / 'model.json')
        for s in ('postgres', 'redis'):
            self.assertEqual(new['services'][s], model['services'][s])
        self.assertEqual(new['networks'], model['networks'])
        self.assertEqual(new['volumes'], model['volumes'])
        env = new['services']['eval-runner']['environment']
        self.assertEqual(env['SECRET_ENCRYPTION_KEY'], 'keep-key')
        self.assertNotIn('ARGUS_BUILD_ID', env)
        self.assertEqual(env['ARGUS_AUTO_IMPORT_YAML'], 'false')

    def test_upgrade_refuses_modified_migrations_before_stopping(self):
        root = self.root
        previous = root / 'old'
        candidate = root / 'new'
        previous.mkdir()
        candidate.mkdir()
        common.write_json(previous / 'deployment.json', {'git_sha': 'b' * 40})
        common.write_json(candidate / 'deployment.json', {'source': str(root / 'source')})
        with mock.patch.object(site, 'active', return_value=previous), \
             mock.patch.object(site, 'preflight', return_value={}), \
             mock.patch.object(site, 'load_images'), \
             mock.patch.object(site, 'candidate_deployment', return_value=candidate), \
             mock.patch.object(site, 'applied_migrations', return_value={'001': 'a'}), \
             mock.patch.object(site, 'migration_map', return_value={'001': 'b'}), \
             mock.patch.object(site, 'quiesce') as quiesce:
            with self.assertRaises(common.Stop):
                site.upgrade(argparse.Namespace(root=str(root), sha='a' * 40, maintenance_window=True))
            quiesce.assert_not_called()

    def test_backup_failure_never_starts_new_runner(self):
        root = self.root
        previous, candidate = root / 'old', root / 'new'
        previous.mkdir()
        candidate.mkdir()
        common.write_json(previous / 'deployment.json', {'git_sha': 'b' * 40})
        common.write_json(candidate / 'deployment.json', {'source': str(root / 'source')})
        with mock.patch.object(site, 'active', return_value=previous), \
             mock.patch.object(site, 'preflight', return_value={'project': 'p'}), \
             mock.patch.object(site, 'load_images'), \
             mock.patch.object(site, 'candidate_deployment', return_value=candidate), \
             mock.patch.object(site, 'applied_migrations', return_value={'001': 'a'}), \
             mock.patch.object(site, 'migration_map', return_value={'001': 'a'}), \
             mock.patch.object(site, 'service_container', return_value={'Id': 'original', 'Image': 'image',
                               'Mounts': [], 'NetworkSettings': {'Networks': {}}}), \
             mock.patch.object(site, 'protected_state', return_value={}), \
             mock.patch.object(site, 'quiesce'), \
             mock.patch.object(site, 'backup_data', side_effect=common.Stop('backup failed')), \
             mock.patch.object(site, 'activate_apps') as activate:
            with self.assertRaises(common.Stop):
                site.upgrade(argparse.Namespace(root=str(root), sha='a' * 40, maintenance_window=True))
            activate.assert_not_called()
            self.assertEqual(common.read_json(root / 'transaction.json')['phase'], 'PREPARED')


if __name__ == '__main__':
    unittest.main()
