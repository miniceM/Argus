#!/usr/bin/env python3
"""#219 真实 Console 验收。默认 dry-run；--execute 才启动独立服务并产生模型费用。

凭据仅从隔离容器读入内存；只创建自有 Rule；不写 Score、不手动 collect。
前置 AgentVersion/ReleasePolicy 通过 API 创建，Launch/Run/Gate 均由浏览器操作。
PASS 为原生 Flowise；FAIL/UNKNOWN 为明确标识的错误 HTTP 输出/暂停自有 Rule 注入，
三种场景的 Judge 和 Code（存在时）均由真实 Langfuse 执行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PRIOR = ROOT / 'docs/validation/issue214-managed-evaluation/evidence'


def validate_score_readback(rows, frozen):
    """投影后的独立 ScoresV3 结果必须唯一且与冻结采纳逐字段一致。"""
    assert len(rows) == 1, 'missing or duplicate Score'
    assert rows[0]['source'] == 'EVAL', 'not native EVAL'
    assert rows[0] == frozen, 'remote Score differs from frozen evidence'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--credential-container', default='argus-flowise-e2e-eval-runner-1')
    parser.add_argument('--evidence-dir', type=Path)
    args = parser.parse_args(argv)
    if not args.execute:
        print('DRY RUN：独立 Runner/Console；真实 Flowise 双评分及受控 FAIL/UNKNOWN；不会写 Score。')
        return
    name = 'issue219-' + datetime.now(UTC).strftime('%Y%m%dt%H%M%Sz')
    out = (args.evidence_dir or HERE / 'evidence' / name).resolve()
    out.mkdir(parents=True, exist_ok=False)
    work = Path('/tmp') / name
    work.mkdir(exist_ok=False)
    raw = json.loads(subprocess.check_output(['docker', 'inspect', args.credential_container]))[0]
    provided = dict(x.split('=', 1) for x in raw['Config']['Env'] if '=' in x)
    env = os.environ.copy()
    for key, value in provided.items():
        if key.startswith(('LANGFUSE_', 'FLOWISE_')) or key in ('ARGUS_SECRET_MODE', 'ARGUS_ALLOWED_CREDENTIAL_ENVS'):
            env[key] = value
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    port, ui_port = free_port(), free_port()
    env.update(DATABASE_URL='sqlite:///' + str(work / 'runtime.db'), ARGUS_DB_MODE='standalone',
               ARGUS_AUTO_IMPORT_YAML='false', ARGUS_BUILD_ID=commit, RUNNER_VERSION='0.2.0',
               ARGUS_WORKER_ENABLED='true', ARGUS_RECONCILER_ENABLED='true', ARGUS_WORKER_CONCURRENCY='1',
               ARGUS_DATASET_SOURCE='langfuse', ARGUS_LANGFUSE_RUN_ITEM_STRATEGY='otel',
               LANGFUSE_TRACING_ENVIRONMENT=name, ARGUS_MANAGED_SCORE_MAX_ATTEMPTS='15',
               ARGUS_MANAGED_SCORE_BASE_DELAY_SECONDS='3', ARGUS_MANAGED_SCORE_MAX_DELAY_SECONDS='10',
               ARGUS_MANAGED_SCORE_DEADLINE_SECONDS='240', PYTHONPATH=str(ROOT / 'services/eval-runner'),
               ARGUS_E2E_API_PORT=str(port))
    env.pop('ARGUS_REDIS_URL', None)
    secrets = [v for k, v in provided.items() if v and any(w in k for w in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD'))]

    def save(filename, data):
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n'
        assert all(value not in text for value in secrets), 'sensitive value in evidence'
        (out / (filename + '.json')).write_text(text)

    # 只读核对现有 Flowise 配置；不读取凭据、原始 flowData 不落盘。
    flow_id = '8b8d2606-379c-42c4-9ac9-08dcbf9490e8'
    flow_raw = subprocess.check_output(['docker', 'exec', 'argus-flowise-p0-postgres', 'psql', '-U', 'postgres',
                                      '-d', 'flowise', '-At', '-c', f'SELECT "flowData" FROM chat_flow WHERE id=\'{flow_id}\''])
    models = [n['data']['inputs']['agentModelConfig'] for n in json.loads(flow_raw)['nodes']
              if 'agentModelConfig' in n.get('data', {}).get('inputs', {})]
    assert models and all(m.get('maxRetries') == 0 for m in models)
    save('preflight', dict(source_commit=commit, environment=name, flow_id=flow_id,
                          flow_sha256=hashlib.sha256(flow_raw).hexdigest(),
                          models=[{k: m.get(k) for k in ('modelName', 'maxRetries', 'temperature')} for m in models],
                          agent_version_max_retries=0, network_mocking=False,
                          source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in [Path(__file__), HERE / 'real_console_flow.mjs',
                                                   ROOT / 'services/console/dist/index.html']}))
    rules, audit, processes, logs = [], [], [], []
    lf = httpx.Client(base_url=env['LANGFUSE_BASE_URL'].rstrip('/'),
                      auth=(env['LANGFUSE_PUBLIC_KEY'], env['LANGFUSE_SECRET_KEY']), timeout=90)
    argus = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=180)

    def request(client, method, path, body=None):
        if client is lf:
            assert method == 'GET' or (method == 'POST' and path == '/api/public/v2/evaluation-rules') or (
                method == 'PATCH' and any(path.endswith(r) for r in rules))
            time.sleep(2.2)
        response = client.request(method, path, json=body) if body is not None else client.request(method, path)
        audit.append(dict(service='langfuse' if client is lf else 'argus', method=method, path=path,
                          status=response.status_code, at=datetime.now(UTC).isoformat()))
        save('request-audit', audit)
        if response.status_code not in (200, 201, 202):
            raise RuntimeError(f'{method} {path}: HTTP {response.status_code}; body omitted')
        return response.json()

    class WrongAnswer(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            body = b'{"text":"0"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    wrong = ThreadingHTTPServer(('127.0.0.1', 0), WrongAnswer)
    threading.Thread(target=wrong.serve_forever, daemon=True).start()
    try:
        commands = [([str(ROOT / '.venv/bin/python'), '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                      '--port', str(port)], ROOT),
                    (['pnpm', 'exec', 'vite', 'preview', '--host', '127.0.0.1', '--port', str(ui_port), '--strictPort'],
                     ROOT / 'services/console')]
        for idx, (command, cwd) in enumerate(commands):
            log = (work / f'service-{idx}.log').open('wb')
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT))
        for url in (f'http://127.0.0.1:{port}/openapi.json', f'http://127.0.0.1:{ui_port}'):
            for _ in range(300):
                assert all(p.poll() is None for p in processes), 'service exited; inspect local logs'
                try:
                    if httpx.get(url, timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError('service readiness timeout')
        setup = json.loads((PRIOR / 'setup.json').read_text())
        project = json.loads((PRIOR / 'fresh-api/read-005.json').read_text())['response']['data'][0]['projectId']
        expectations = []
        for evaluator in setup['evaluators']:
            current = request(lf, 'GET', '/api/public/v2/evaluators/' + evaluator['id'])
            save('evaluator-' + evaluator['type'], current)
            body = dict(name=name + '-' + evaluator['type'], enabled=True, sampling=1, filter=[
                dict(type='stringOptions', column='environment', operator='any of', value=[name]),
                dict(type='stringOptions', column='name', operator='any of', value=['remote-agent-http'])],
                        evaluatorAssignments=[dict(evaluatorId=evaluator['id'], variableMapping=None)])
            rule = request(lf, 'POST', '/api/public/v2/evaluation-rules', body)
            rules.append(rule['id'])
            save('rule-' + evaluator['type'], dict(request=body, response=rule))
            expectations.append(dict(name=evaluator['score_name'], rule_id=rule['id'], evaluator_id=evaluator['id']))
        request(argus, 'POST', '/api/v1/agents', dict(id=name, name=name, description='Issue219 Console live acceptance'))
        version = json.loads((PRIOR / 'agent-version.json').read_text())['request']
        version.update(agent_id=name, environment=name, endpoint=f'http://127.0.0.1:18091/api/v1/prediction/{flow_id}')
        request(argus, 'POST', '/api/v1/agent-versions', version)
        save('agent-version', version)
        version.update(version='v2', endpoint=f'http://127.0.0.1:{wrong.server_port}/invoke')
        request(argus, 'POST', '/api/v1/agent-versions', version)
        request(argus, 'POST', '/api/v1/release-policies', dict(name=name, version='1.0.0', agent_id=name, environment=name,
                 rules=[dict(id='quality', metric='pass_rate', operator='>=', threshold=1.0),
                        dict(id='coverage', metric='evaluation_coverage', operator='>=', threshold=1.0)]))
        frozen, checks = [], []
        sys.path.insert(0, str(ROOT / 'services/eval-runner'))
        from app.langfuse_managed_scores import _score_evidence
        from app.langfuse_scores_client import ScoresV3Client
        with ScoresV3Client(*(env[k] for k in ('LANGFUSE_BASE_URL', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'))) as client:
            for label, decision in [('pass', 'PASS'), ('fail', 'FAIL'), ('missing', 'UNKNOWN')]:
                if label == 'missing':
                    request(lf, 'PATCH', '/api/public/v2/evaluation-rules/' + expectations[1]['rule_id'], {'enabled': False})
                config = dict(label=label, expected=decision, ui_url=f'http://127.0.0.1:{ui_port}', agent_id=name,
                              version='v1' if label == 'pass' else 'v2', environment=name, dataset=setup['prefix'],
                              project_id=project, scores=expectations, policy=name, out=str(out))
                with (work / f'browser-{label}.log').open('wb') as log:
                    proc = subprocess.run(['node', str(HERE / 'real_console_flow.mjs')], input=json.dumps(config).encode(),
                                          cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=800)
                if proc.returncode:
                    raise RuntimeError(f'{label}: browser acceptance failed; local log {work}/browser-{label}.log')
                ui = json.loads((out / (label + '-browser.json')).read_text())
                path = '/api/v1/experiment-launches/' + ui['launch_id']
                snapshot_path = path + '/result-snapshots/' + ui['snapshot_id']
                snap = request(argus, 'GET', snapshot_path)
                gate = request(argus, 'GET', '/api/v1/release-gates/' + ui['gate_id'])
                items = request(argus, 'GET', path + '/items')
                assert gate['decision'] == decision
                assert items[0]['attempt_count'] == 1 and items[0]['execution_status'] == 'succeeded'
                assert items[0]['quality_conclusion'] == ('unknown' if label == 'missing' else label)
                save(label + '-snapshot', snap)
                save(label + '-gate', gate)
                save(label + '-items', items)
                save(label + '-diagnostics', request(argus, 'GET', path + '/items/' + items[0]['id'] + '/managed-scores'))
                for item in snap['items']:
                    for expected in expectations:
                        time.sleep(2.2)
                        rows = client._pages('/api/public/v3/scores', dict(traceId=item['trace_id'], observationId=item['observation_id'],
                                             name=expected['name'], fields='details,subject'))
                        rows = [_score_evidence(r) for r in rows if r.get('name') == expected['name']]
                        adopted = [r for r in item['evaluation_results'] if r['evaluator_id'] == expected['name']]
                        if label == 'missing' and expected == expectations[1]:
                            assert rows == adopted == []
                        else:
                            assert len(adopted) == 1
                            validate_score_readback(rows, adopted[0]['evidence']['score'])
                            assert rows[0]['value'] is (label == 'pass')
                        checks.append(dict(scenario=label, score_name=expected['name'], rows=rows, matches_frozen=True))
                frozen.append((label, snapshot_path, snap, gate, ui))
                print(label + ': Console → native EVAL → frozen ' + decision + ' PASS', flush=True)
            save('independent-score-readback', dict(status='PASS', checks=checks, requests=client.requests, score_writes=0))
        # 修改本次自有 Rule，禁止触碰共享 Evaluator；历史服务端和 Console 链接必须不变。
        for rule in rules:
            request(lf, 'PATCH', '/api/public/v2/evaluation-rules/' + rule, {'enabled': False})
        for label, path, snap, gate, ui in frozen:
            assert request(argus, 'GET', path) == snap
            assert request(argus, 'GET', '/api/v1/release-gates/' + gate['id']) == gate
            config = dict(mode='verify-history', ui_url=f'http://127.0.0.1:{ui_port}', out=str(out), label=label, **ui)
            subprocess.run(['node', str(HERE / 'real_console_flow.mjs')], input=json.dumps(config).encode(),
                           cwd=ROOT, check=True, stdout=subprocess.DEVNULL, timeout=120)
        save('receipt', dict(status='PASS', source_commit=commit, environment=name,
                            browser_network_mocking=False, manual_collect_requests=0, score_write_requests=0,
                            rule_ids=rules, scenarios=[dict(label=scenario, launch_id=u['launch_id'], snapshot_id=u['snapshot_id'],
                            gate_id=g['id'], decision=g['decision']) for scenario, _, _, g, u in frozen],
                            historical_api_and_console_unchanged=True, timestamp=datetime.now(UTC).isoformat()))
    finally:
        paused = []
        for rule in rules:
            try:
                r = request(lf, 'PATCH', '/api/public/v2/evaluation-rules/' + rule, {'enabled': False})
                paused.append(dict(id=rule, enabled=r.get('enabled')))
            except Exception:
                paused.append(dict(id=rule, cleanup='FAILED'))
        save('owned-rules-paused', paused)
        for process in processes:
            process.terminate()
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
        wrong.shutdown()
        wrong.server_close()
        argus.close()
        lf.close()
        assert all(p.get('enabled') is False for p in paused), 'owned Rule cleanup incomplete'


if __name__ == '__main__':
    main()
