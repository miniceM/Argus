"""真实PG/Redis数据恢复；临时项目，无真实Langfuse或业务容器。"""
import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import deploy


@unittest.skipUnless(os.getenv('RUN_DOCKER_TESTS') == '1', '设置 RUN_DOCKER_TESTS=1 才运行隔离Docker恢复测试')
class DockerRestoreTests(unittest.TestCase):
    def test_backup_restore_preserves_old_and_new_data(self):
        with tempfile.TemporaryDirectory(prefix='argus-skill-test-') as td:
            root = Path(td).resolve()
            nonce = secrets.token_hex(5)
            project = 'argus-skill-test-' + nonce
            site_id = secrets.token_hex(16)
            private, lfnet = project + '-private', project + '-lf-unused'
            pgvol, rdvol = project + '-pg', project + '-redis'
            created_volumes = [pgvol, rdvol]
            pg = common.inspect_one('image', 'postgres:17')
            rd = common.inspect_one('image', 'redis:7')
            self.assertEqual(pg['Architecture'], 'arm64')
            self.assertEqual(rd['Architecture'], 'arm64')
            site = {'site_id': site_id, 'project': project, 'pg_user': 'argus', 'pg_database': 'argus',
                'pg_volume': pgvol, 'redis_volume': rdvol, 'private_network': private,
                'postgres': {'ref': 'postgres:17', 'id': pg['Id']},
                'redis': {'ref': 'redis:7', 'id': rd['Id']},
                'langfuse_network': lfnet, 'langfuse_containers': [], 'langfuse_fingerprints': {}}
            deployment = root / 'deployments/original'
            deployment.mkdir(parents=True)
            for n in ('config', 'data', 'migrations'):
                p = deployment / 'source' / n
                p.mkdir(parents=True)
                (p / 'test.txt').write_text('synthetic test fixture')
            model = {'services': {}, 'networks': {'private': {'name': private, 'external': True}},
                     'volumes': {'pg': {'name': pgvol, 'external': True}, 'rd': {'name': rdvol, 'external': True}}}
            model['services']['postgres'] = {'image': 'postgres:17', 'pull_policy': 'never',
                'environment': {'POSTGRES_USER': 'argus', 'POSTGRES_DB': 'argus', 'POSTGRES_PASSWORD': 'synthetic-only'},
                'volumes': [{'type': 'volume', 'source': 'pg', 'target': '/var/lib/postgresql/data'}],
                'networks': {'private': {}}, 'healthcheck': {'test': ['CMD', 'pg_isready', '-U', 'argus', '-d', 'argus'],
                 'interval': '1s', 'timeout': '3s', 'retries': 60}}
            model['services']['redis'] = {'image': 'redis:7', 'pull_policy': 'never',
                'environment': {'REDISCLI_AUTH': 'synthetic-only'},
                'command': ['redis-server', '--requirepass', 'synthetic-only', '--appendonly', 'yes'],
                'volumes': [{'type': 'volume', 'source': 'rd', 'target': '/data'}], 'networks': {'private': {}},
                'healthcheck': {'test': ['CMD', 'redis-cli', 'ping'], 'interval': '1s', 'timeout': '3s', 'retries': 60}}
            for app in common.APPS:
                model['services'][app] = {'image': 'redis:7', 'pull_policy': 'never', 'networks': {'private': {}}}
            model['services']['eval-runner']['volumes'] = [
                {'type': 'bind', 'source': str(deployment / 'source' / n), 'target': '/app/' + n, 'read_only': True}
                for n in ('config', 'data', 'migrations')]
            common.write_json(root / 'site.json', site)
            common.write_json(deployment / 'deployment.json', {'git_sha': 'a' * 40, 'source': str(deployment / 'source')})
            common.write_compose(deployment, model)
            common.write_json(root / 'active.json', {'deployment': str(deployment), 'git_sha': 'a' * 40})
            try:
                for v in created_volumes:
                    common.docker('volume', 'create', '--label', 'io.argus-offline.site=' + site_id, v)
                common.docker('network', 'create', '--internal', private)
                common.docker('network', 'create', '--internal', lfnet)
                site['langfuse_network_id'] = common.inspect_one('network', lfnet)['Id']
                common.write_json(root / 'site.json', site)
                common.compose(root, deployment, 'up', '-d', '--pull', 'never', '--no-build', '--no-deps', 'postgres', 'redis')
                common.wait_health(project, 'postgres', 90)
                common.wait_health(project, 'redis', 90)
                deploy.sql(site, "CREATE TABLE schema_migrations(version text PRIMARY KEY,checksum text); "
                           "INSERT INTO schema_migrations VALUES ('001_test.sql','abc'); "
                           "CREATE TABLE fixture(id integer PRIMARY KEY,value text); INSERT INTO fixture VALUES(1,'before-upgrade'); "
                           "CREATE ROLE fixture_reader; GRANT SELECT ON fixture TO fixture_reader;")
                self.assertEqual(deploy.redis_cli(site, 'SET', 'fixture', 'before-upgrade'), 'OK')
                with mock.patch.object(deploy, 'probe_secrets'):
                    backup = deploy.backup_data(root, deployment, site)
                manifest_hash = common.sha_file(backup / 'SHA256SUMS')
                self.assertEqual(common.read_json(backup / 'backup.json')['pg_restore_rehearsal'], 'PASS')
                common.verify_sums(backup)
                deploy.sql(site, "UPDATE fixture SET value='after-upgrade'; ALTER TABLE fixture ADD COLUMN new_column text; "
                           "REVOKE SELECT ON fixture FROM fixture_reader;")
                deploy.redis_cli(site, 'SET', 'fixture', 'after-upgrade')
                network_id_before = common.inspect_one('network', lfnet)['Id']
                pg_id_before = common.service_container(project, 'postgres')['Id']
                # 此测试只验证真实存储恢复；没有Argus应用镜像，不伪称应用或业务E2E通过。
                real_sql = deploy.sql
                swaps = []
                def checked_sql(site, query, **kwargs):
                    if query.startswith('ALTER DATABASE "argus" RENAME TO argus_hold_'):
                        journal = common.read_json(root / 'transaction.json')
                        self.assertEqual(journal['phase'], 'DATABASE_SWAP_PREPARED')
                        self.assertIn(journal['held_database'], query)
                        self.assertTrue(journal['restored_database'].startswith('argus_verify_'))
                        common.inspect_one('volume', journal['restored_redis_volume'])
                        swaps.append(journal)
                    return real_sql(site, query, **kwargs)
                with mock.patch.object(deploy, 'activate_apps') as activate, \
                     mock.patch.object(deploy, 'probe_secrets'), \
                     mock.patch.object(deploy, 'sql', side_effect=checked_sql):
                    deploy.rollback(argparse.Namespace(root=str(root), backup=str(backup), backup_sha256=manifest_hash,
                                                        restore_data=True, maintenance_window=True))
                    activate.assert_called_once()
                self.assertEqual(len(swaps), 1)
                restored_site = common.read_json(root / 'site.json')
                created_volumes.append(restored_site['redis_volume'])
                self.assertEqual(deploy.sql(restored_site, 'SELECT value FROM fixture;'), 'before-upgrade')
                self.assertEqual(deploy.sql(restored_site, "SELECT has_table_privilege('fixture_reader','fixture','SELECT');"), 't')
                self.assertEqual(deploy.redis_cli(restored_site, 'GET', 'fixture'), 'before-upgrade')
                self.assertEqual(common.service_container(project, 'postgres')['Id'], pg_id_before)
                self.assertEqual(common.inspect_one('network', lfnet)['Id'], network_id_before)
                holds = deploy.sql(restored_site, "SELECT datname FROM pg_database WHERE datname LIKE 'argus_hold_%';", db='postgres').splitlines()
                self.assertEqual(len(holds), 1)
                self.assertEqual(deploy.sql(restored_site, 'SELECT value FROM fixture;', db=holds[0]), 'after-upgrade')
                self.assertNotEqual(restored_site['redis_volume'], rdvol)
                common.inspect_one('volume', rdvol)  # 原卷仍存在。
                report = {'postgres_dump_real_restore': 'PASS', 'postgres_schema_and_row_restore': 'PASS',
                          'postgres_table_grants_restored': 'PASS',
                          'redis_aof_data_restore': 'PASS', 'post_upgrade_database_retained': 'PASS',
                          'original_redis_volume_retained': 'PASS', 'postgres_container_unchanged': 'PASS',
                          'rollback_journal_before_database_swap': 'PASS',
                          'external_network_id_unchanged': 'PASS', 'argus_app_start': 'MOCKED_NOT_TESTED',
                          'langfuse_business_e2e': 'NOT_RUN'}
                destination = os.getenv('ARGUS_TEST_REPORT')
                if destination:
                    common.write_json(destination, report)
                print(json.dumps(report))
            finally:
                # 只删除本测试随机项目/随机site_id拥有的合成资源；无全局prune或down -v。
                ids = common.docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project).decode().split()
                for cid in ids:
                    common.docker('rm', '-f', cid)
                volumes = common.docker('volume', 'ls', '-q', '--filter', 'label=io.argus-offline.site=' + site_id).decode().split()
                for v in volumes:
                    common.docker('volume', 'rm', v)
                for n in (private, lfnet):
                    common.docker('network', 'rm', n)


if __name__ == '__main__':
    unittest.main()
