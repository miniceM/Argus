#!/usr/bin/env python3
"""为 #212 克隆专用原生 Flowise Flow；不修改已有 Flow/凭据，不产生模型调用。"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]


def isolated_graph(graph):
    graph = copy.deepcopy(graph)
    agents = [n['data']['inputs'] for n in graph['nodes'] if n['data']['name'] == 'agentAgentflow']
    assert len(agents) == 1
    agent = agents[0]
    agent['agentModelConfig'].update(maxRetries=0, temperature=0)
    agent['agentEnableMemory'] = False
    agent['agentTools'] = [t for t in agent['agentTools'] if t['agentSelectedTool'] == 'calculator']
    assert len(agent['agentTools']) == 1
    agent['agentMessages'] = [{'role': 'system', 'content':
        'Use calculator for every arithmetic question. Return only the final numeric answer without explanations.'}]
    return graph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--origin', default='http://127.0.0.1:19091')
    parser.add_argument('--source-flow-id', type=UUID)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    if not args.execute:
        print('DRY RUN: clone Flow with model retries=0, no model invocation, no new API key.')
        return
    assert args.env_file and args.source_flow_id and args.out
    assert not args.out.exists(), 'receipt already exists; do not create duplicate Flow'
    spec = importlib.util.spec_from_file_location('flowise_e2e', ROOT / 'examples/flowise/e2e.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    env = helper.load_env(args.env_file)
    env['FLOWISE_URL'] = args.origin
    client = helper.Clients(env)
    client.login()
    source = client.flow('/chatflows/' + str(args.source_flow_id))
    graph = isolated_graph(json.loads(source['flowData']))
    result = client.flow('/chatflows', {'name': 'Issue212 isolated retries-zero acceptance',
        'flowData': json.dumps(graph), 'type': 'AGENTFLOW', 'apikeyid': source['apikeyid'],
        'deployed': True, 'isPublic': False})
    args.out.write_text(json.dumps({'flow_id': result['id'], 'source_flow_id': str(args.source_flow_id),
        'model_max_retries': 0, 'agent_memory': False, 'tools': ['calculator'],
        'graph_sha256': hashlib.sha256(json.dumps(graph).encode()).hexdigest(),
        'credentials_created': 0, 'existing_flow_modified': False}, indent=2) + '\n')
    print('Created isolated Flow:', result['id'])


if __name__ == '__main__':
    main()
