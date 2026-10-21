#!/usr/bin/env python3
"""无 Langfuse 配置的真实 HTTP Demo 回归；隔离服务，不调用模型或外部网络。"""
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from real_console_acceptance import ROOT, free_port


def main():
    out = Path(__file__).resolve().parent / 'evidence/builtin-http-regression.json'
    work = Path(tempfile.mkdtemp(prefix='argus219-builtin-'))
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    env = {k: v for k, v in os.environ.items() if not k.startswith(('LANGFUSE_', 'ARGUS_', 'DATABASE_'))}
    env.update(DATABASE_URL='sqlite:///' + str(work / 'runtime.db'), ARGUS_DB_MODE='standalone',
               ARGUS_AUTO_IMPORT_YAML='false', ARGUS_BUILD_ID=commit, RUNNER_VERSION='0.2.0',
               ARGUS_WORKER_ENABLED='true', ARGUS_RECONCILER_ENABLED='true', ARGUS_DATASET_SOURCE='seed',
               PYTHONPATH=str(ROOT / 'services/eval-runner'))
    ports = [free_port() for _ in range(3)]
    processes, logs = [], []
    try:
        for idx, port in enumerate(ports):
            child = env.copy()
            if idx:
                child['AGENT_VERSION'] = f'v{idx}'
            log = (work / f'{idx}.log').open('wb')
            logs.append(log)
            cwd = ROOT if idx == 0 else ROOT / 'services/demo-agent'
            app = 'app.main:app' if idx == 0 else 'app:app'
            processes.append(subprocess.Popen([str(ROOT / '.venv/bin/python'), '-m', 'uvicorn', app,
                '--host', '127.0.0.1', '--port', str(port)], env=child, cwd=cwd, stdout=log, stderr=subprocess.STDOUT))
        for port in ports:
            for _ in range(200):
                assert all(p.poll() is None for p in processes)
                try:
                    if httpx.get(f'http://127.0.0.1:{port}/openapi.json').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('startup timed out')
        with httpx.Client(base_url=f'http://127.0.0.1:{ports[0]}', timeout=180) as client:
            def post(path, body):
                response = client.post(path, json=body)
                assert response.status_code in (200, 201, 202), (path, response.status_code, response.text)
                return response.json()
            post('/api/v1/agents', dict(id='banking-agent', name='Builtin isolated HTTP regression'))
            results = []
            for idx, expected in [(1, 2), (2, 6)]:
                post('/api/v1/agent-versions', dict(agent_id='banking-agent', version=f'v{idx}',
                    endpoint=f'http://127.0.0.1:{ports[idx]}/invoke', protocol='HTTP_JSON', method='POST',
                    request_mapping=dict(messages='input.messages', customer_id='input.customer_id'),
                    timeout_seconds=10, max_retries=0, rate_limit_per_minute=600, max_concurrency=4,
                    trace_propagation='W3C', is_idempotent=False, artifact_ref='git:' + commit))
                result = post('/api/v1/experiment-launches', dict(agent_id='banking-agent', agent_version=f'v{idx}',
                    dataset_name='banking-agent-regression', name=f'issue219-builtin-v{idx}', evaluator_ids=['intent_match', 'required_tool_match', 'pii_safe', 'escalation_match'], max_concurrency=4))
                launch_id = result['id']
                post(f'/api/v1/experiment-launches/{launch_id}/run', {})
                for _ in range(300):
                    result = client.get(f'/api/v1/experiment-launches/{launch_id}').json()
                    if result['status'] in ('COMPLETED', 'FAILED'):
                        break
                    time.sleep(.2)
                summary = client.get(f'/api/v1/experiment-launches/{launch_id}/summary').json()
                results.append(summary)
                assert summary['summary']['quality_pass_count'] == expected and summary['summary']['total_cases'] == 6, summary
            out.write_text(json.dumps(dict(status='PASS', source_commit=commit, langfuse_configured=False,
                scope='real localhost HTTP Demo Agent + independent standalone Runner + seed Dataset',
                expected=dict(v1='2/6', v2='6/6'), results=results,
                source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()), ensure_ascii=False, indent=2) + '\n')
            print('PASS: Builtin real HTTP v1=2/6, v2=6/6; no Langfuse credentials/configuration')
    finally:
        for p in processes:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for log in logs:
            log.close()


if __name__ == '__main__':
    main()
