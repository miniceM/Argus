#!/usr/bin/env python3
"""独立只读回查 v4 Experiment/Item/Observation；不使用已废弃的 Trace/Run APIs。"""
import argparse
import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-db', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--credential-container', default='argus-flowise-e2e-eval-runner-1')
    args = parser.parse_args()
    snap = json.loads((args.evidence_dir / 'pass-snapshot.json').read_text())
    item = snap['items'][0]
    with sqlite3.connect(f'file:{args.runtime_db}?mode=ro', uri=True) as db:
        experiment_id = db.execute('SELECT langfuse_experiment_id FROM experiment_launches WHERE id=?', (snap['launch_id'],)).fetchone()[0]
    raw = json.loads(subprocess.check_output(['docker', 'inspect', args.credential_container]))[0]
    env = dict(x.split('=', 1) for x in raw['Config']['Env'] if '=' in x)
    # 合成验收专属时间窗，分页穷尽后只导出目标身份的白名单字段。
    receipt = json.loads((args.evidence_dir / 'receipt.json').read_text())
    start = snap['manifest']['dataset']['dataset_version']
    audit, proof = [], {}
    with httpx.Client(base_url=env['LANGFUSE_BASE_URL'], auth=(env['LANGFUSE_PUBLIC_KEY'], env['LANGFUSE_SECRET_KEY']), timeout=90) as client:
        for label, path in [('observations', '/api/public/v2/observations'), ('experiments', '/api/public/experiments'),
                            ('items', '/api/public/experiment-items')]:
            params = dict(fromStartTime=start, toStartTime=receipt['timestamp'], limit=100)
            if label == 'observations':
                params['traceId'] = item['trace_id']
            selected = []
            for _ in range(20):
                time.sleep(2.2)
                response = client.get(path, params=params)
                audit.append(dict(method='GET', path=path, query=params.copy(), status=response.status_code))
                assert response.status_code == 200, (label, response.status_code, response.text[:500])
                payload = response.json()
                for row in payload['data']:
                    if label == 'experiments' and row.get('id') == experiment_id:
                        selected.append({k: row[k] for k in row if k in ('id', 'name', 'datasetId', 'itemCount', 'createdAt')})
                    elif label == 'items' and row.get('traceId') == item['trace_id']:
                        selected.append({k: row[k] for k in row if k in ('id', 'experimentId', 'experimentItemId', 'experimentDatasetId', 'experimentName', 'traceId')})
                    elif (label == 'observations' and row.get('id') == item['observation_id']
                          and row.get('name') == 'remote-agent-http' and row.get('type') == 'TOOL'):
                        selected.append({k: row[k] for k in row if k in ('id', 'name', 'type', 'traceId', 'environment')})
                cursor = payload.get('meta', {}).get('nextCursor')
                if not cursor:
                    break
                params['cursor'] = cursor
            else:
                raise AssertionError('pagination limit reached')
            assert len(selected) == 1, (label, selected)
            proof[label] = selected
    assert proof['items'][0]['experimentId'] == experiment_id
    assert proof['items'][0]['experimentItemId'] == item['dataset_item_id']
    assert proof['observations'][0]['name'] == 'remote-agent-http' and proof['observations'][0]['type'] == 'TOOL'
    result = dict(status='PASS', launch_id=snap['launch_id'], experiment_id=experiment_id, evidence=proof, requests=audit,
                  source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (args.evidence_dir / 'remote-experiment-readback.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print('PASS: native remote Experiment / Item / TOOL Observation identity matches frozen Argus evidence')


if __name__ == '__main__':
    main()
