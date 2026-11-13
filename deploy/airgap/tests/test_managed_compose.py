"""正式 initial_model 的隔离 Docker 回归，无真实 Langfuse/业务数据。"""
import base64
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import deploy
import managed_secrets


@unittest.skipUnless(os.environ.get('RUN_MANAGED_COMPOSE_TEST') == '1', '需显式启用隔离 Compose 测试')
class ManagedComposeTests(unittest.TestCase):
    def test_initial_credentials_survive_runner_recreation_and_backup(self):
        repo = Path(__file__).resolve().parents[3]
        manifest = json.loads(Path(os.environ['ARGUS_TEST_RELEASE']).read_text())
        base_manifest = json.loads(Path(os.environ['ARGUS_TEST_BASE_RELEASE']).read_text()) if os.environ.get('ARGUS_TEST_BASE_RELEASE') else manifest
        nonce = secrets.token_hex(5)
        project = 'argus-managed-test-' + nonce
        private, lfnet = project + '-private', project + '-external'
        pgvol, rdvol = project + '-pg', project + '-redis'
        with tempfile.TemporaryDirectory(prefix=project) as directory:
            root = Path(directory).resolve()
            site = {'site_id': nonce, 'project': project, 'environment': 'isolated-kunpeng-test',
                    'postgres': {'ref': 'postgres:17', 'id': common.inspect_one('image', 'postgres:17')['Id']},
                    'redis': {'ref': 'redis:7', 'id': common.inspect_one('image', 'redis:7')['Id']},
                    'pg_password': secrets.token_hex(16), 'redis_password': secrets.token_hex(16),
                    'pg_volume': pgvol, 'redis_volume': rdvol, 'private_network': private,
                    'langfuse_network': lfnet, 'pg_user': 'argus', 'pg_database': 'argus',
                    'langfuse_base_url': 'http://unavailable-langfuse:3000', 'lf_public_key': 'pk-isolated-test',
                    'lf_secret_key': 'sk-isolated-test', 'langfuse_dashboard_url': 'http://127.0.0.1:9',
                    'console_url': 'http://127.0.0.1:9', 'console_bind': '127.0.0.1'}
            deployment = root / 'deployment'
            model = deploy.initial_model(site, base_manifest, repo)
            runner = model['services']['eval-runner']
            runner['environment'].update({'ARGUS_AUTO_IMPORT_YAML': 'false', 'ARGUS_WORKER_ENABLED': 'false',
                'ARGUS_RECONCILER_ENABLED': 'false', 'OTEL_SDK_DISABLED': 'true'})
            for service in model['services'].values():
                service.pop('ports', None)
            managed_secrets.prepare(root, model, [])
            deploy.probe_secrets(model, [])
            before = {p.name: p.read_bytes() for p in (root / 'secrets').iterdir()}
            common.write_json(root / 'site.json', site)
            common.write_compose(deployment, model)
            common.write_json(deployment / 'deployment.json', {'git_sha': base_manifest['git_sha'], 'source': str(repo)})
            created = []
            try:
                for name in (private, lfnet):
                    common.docker('network', 'create', '--internal', name)
                    created.append(('network', name))
                for name in (pgvol, rdvol):
                    common.docker('volume', 'create', name)
                    created.append(('volume', name))
                common.compose(root, deployment, 'up', '-d', '--pull', 'never', '--no-build', '--no-deps', 'postgres', 'redis')
                common.wait_health(project, 'postgres')
                common.wait_health(project, 'redis')
                common.compose(root, deployment, 'up', '-d', '--pull', 'never', '--no-build', '--no-deps', 'eval-runner', 'argus-console')
                common.wait_health(project, 'eval-runner')
                common.wait_health(project, 'argus-console')
                cid = common.service_container(project, 'eval-runner')['Id']
                create = '''import json,urllib.request
from app.credentials import protected_file
body={'name':'offline synthetic','environment':'isolated-kunpeng-test','secret':'synthetic-agent-token','provider':'managed'}
r=urllib.request.Request('http://127.0.0.1:8080/api/v1/credentials',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+protected_file('ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE').strip()})
with urllib.request.urlopen(r) as response:
 print(json.load(response)['id'])
'''
                credential_id = common.docker('exec', cid, 'python', '-c', create).decode().strip()
                common.compose(root, deployment, 'up', '-d', '--pull', 'never', '--no-build', '--no-deps', 'demo-agent-v2')
                common.wait_health(project, 'demo-agent-v2')
                invocation = """import asyncio,os,sys
from app.db import DatabaseManager
from app.registry import AgentRegistry
from app.executor import RemoteAgentExecutor
manager=DatabaseManager(os.environ['DATABASE_URL'])
registry=AgentRegistry(manager)
if sys.argv[2] == 'create':
 registry.create_agent('isolated-agent','isolated-agent')
 registry.create_version('isolated-agent','v1','http://demo-agent-v2:8080/invoke',credential_id=sys.argv[1],environment='isolated-kunpeng-test')
async def call():
 executor=RemoteAgentExecutor(registry.get_version('isolated-agent','v1'),credential_db_manager=manager)
 result=await executor.invoke_once({'messages':[{'role':'user','content':'查询余额'}]}, {'traceparent':'00-'+'a'*32+'-'+'b'*16+'-01'})
 assert result.status_code == 200 and result.error_category is None
 assert result.body['agent_version'] == 'v2'
 assert result.trace_context_received
 assert executor.credential_usage[0]['credential_id'] == sys.argv[1]
 await executor._client.aclose()
asyncio.run(call())
"""
                common.docker('exec', cid, 'python', '-c', invocation, credential_id, 'create')
                rows = deploy.credential_rows(site)
                self.assertEqual(len(rows), 1)
                deploy.probe_secrets(model, rows)
                key_file = root / 'secrets/keyring.json'
                original_key = key_file.read_bytes()
                broken_ring = json.loads(original_key)
                broken_ring['keys'][broken_ring['active_key_id']] = base64.b64encode(secrets.token_bytes(32)).decode()
                try:
                    key_file.write_text(json.dumps(broken_ring))
                    with self.assertRaises(common.Stop):
                        deploy.probe_secrets(model, rows)
                finally:
                    key_file.write_bytes(original_key)
                deploy.probe_secrets(model, rows)
                infrastructure = {s: common.service_container(project, s)['Id'] for s in ('postgres', 'redis')}
                old_cid = cid
                if os.environ.get('ARGUS_TEST_TRANSFER'):
                    import receive
                    transfer = Path(os.environ['ARGUS_TEST_TRANSFER'])
                    receive.receive(root, transfer, common.sha_file(transfer / 'SHA256SUMS.parts'), 20_000_000_000)
                    candidate = deploy.candidate_deployment(root, manifest['git_sha'], deployment)
                    common.compose(root, deployment, 'stop', '-t', '30', *common.APPS)
                    deploy.backup_data(root, deployment, site)
                    deployment = candidate
                    model = common.read_json(candidate / 'model.json')
                    common.compose(root, deployment, 'up', '-d', '--pull', 'never', '--no-build', '--no-deps', 'demo-agent-v2', 'argus-console')
                    common.wait_health(project, 'demo-agent-v2')
                    common.wait_health(project, 'argus-console')
                common.compose(root, deployment, 'up', '-d', '--force-recreate', '--pull', 'never', '--no-build', '--no-deps', 'eval-runner')
                common.wait_health(project, 'eval-runner')
                cid = common.service_container(project, 'eval-runner')['Id']
                self.assertNotEqual(old_cid, cid)
                deploy.probe_secrets(model, rows)
                common.docker('exec', cid, 'python', '-c', invocation, credential_id, 'reuse')
                console = common.service_container(project, 'argus-console')['Id']
                info = json.loads(common.docker('exec', console, 'wget', '-T', '5', '-qO-', 'http://127.0.0.1:8080/api/v1/system/info'))
                self.assertEqual(info['build_id'], manifest['git_sha'])
                self.assertEqual(info['environment'], site['environment'])
                self.assertEqual(credential_id, rows[0]['credential_id'])
                common.compose(root, deployment, 'stop', '-t', '30', 'eval-runner')
                backup = deploy.backup_data(root, deployment, site)
                self.assertTrue((backup / 'secret-dependencies.json').exists())
                self.assertFalse((backup / 'source/config/.secrets').exists())
                self.assertFalse(any(p.name == 'keyring.json' for p in backup.rglob('*')))
                self.assertEqual(before, {p.name: p.read_bytes() for p in (root / 'secrets').iterdir()})
                self.assertEqual(infrastructure, {s: common.service_container(project, s)['Id'] for s in ('postgres', 'redis')})
                managed_secrets.verify_receipt(common.read_json(backup / 'secret-dependencies.json'))
                record = {'result': 'PASS', 'git_sha': manifest['git_sha'], 'model': 'initial_model',
                          'candidate_deployment_from_previous_sha': base_manifest['git_sha'] if os.environ.get('ARGUS_TEST_TRANSFER') else 'NOT_RUN',
                          'credential_created': True, 'persisted_version_credential_http_call_before_and_after': True, 'all_revisions_decrypted_before_and_after_recreation': True,
                          'console_proxy_build_identity': True, 'pg_restore_rehearsal': 'PASS',
                          'secret_bytes_preserved': True, 'same_key_id_wrong_key_bytes_rejected': True, 'keys_excluded_from_database_backup': True,
                          'infrastructure_container_ids_preserved': True, 'langfuse': 'NOT_RUN'}
                if os.environ.get('ARGUS_MANAGED_RECEIPT'):
                    Path(os.environ['ARGUS_MANAGED_RECEIPT']).write_text(json.dumps(record, indent=2) + '\n')
            finally:
                # 只删除本测试新建、已知名称的容器和卷；不使用 down -v 或 prune。
                for service in common.SERVICES:
                    c = common.service_container(project, service, required=False)
                    if c:
                        common.docker('rm', '-f', c['Id'])
                for kind, name in reversed(created):
                    common.docker(kind, 'rm', name)
