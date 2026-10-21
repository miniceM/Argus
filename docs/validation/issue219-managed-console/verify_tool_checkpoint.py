#!/usr/bin/env python3
"""只读审核合成用例的原生 Flowise Checkpoint；白名单投影，不导出原始模型配置。"""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-db', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    browser = json.loads((args.evidence_dir / 'pass-browser.json').read_text())
    snapshot = json.loads((args.evidence_dir / 'pass-snapshot.json').read_text())
    item = snapshot['items'][0]
    with sqlite3.connect(f'file:{args.runtime_db}?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute('SELECT launch_id,item_execution_id,trace_id,observation_id,output_digest,agent_output '
                                  'FROM execution_checkpoints WHERE launch_id=?', (browser['launch_id'],)).fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row['item_execution_id'] == item['item_execution_id']
    assert row['trace_id'] == item['trace_id'] and row['observation_id'] == item['observation_id']
    output = json.loads(row.pop('agent_output'))
    assert output['text'].strip() == '1266'
    tools, models = [], []
    for node in output.get('agentFlowExecutedData', []):
        inputs = node.get('data', {}).get('input', {})
        model = inputs.get('agentModelConfig')
        if model:
            models.append({k: model.get(k) for k in ('modelName', 'maxRetries', 'temperature')})
        for message in inputs.get('messages', []):
            if message.get('role') == 'tool':
                tools.append({k: message.get(k) for k in ('name', 'content', 'tool_call_id')})
    assert any(t['name'] == 'calculator' and t['content'].strip() == '1266' for t in tools)
    assert models and all(m['maxRetries'] == 0 for m in models)
    evidence = dict(status='PASS', scope='independent read-only internal audit; user workflow uses Console only',
                    checkpoint=row, final_text=output['text'], tools=tools, models=models,
                    source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (args.evidence_dir / 'native-tool-checkpoint.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print('PASS: original Flowise calculator result 1266, model retries=0, Checkpoint identity matches frozen Item')


if __name__ == '__main__':
    main()
