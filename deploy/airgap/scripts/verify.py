#!/usr/bin/env python3
"""分层验收：健康/构建身份；可选真实 Demo、冻结证据、Gate 和异常路径。"""
import argparse
import base64
import copy
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, urlencode
import uuid
from common import (TERMINAL_LAUNCH_STATUSES, Stop, checksums, compose, docker, finish_main, http_json, need, operation,
                    protected_state, read_json, service_container, stamp, write_json)
from deploy import active, applied_migrations, load_site, preflight, verify_app_images, verify_toolchain

METRICS = ('intent_match', 'required_tool_match', 'pii_safe', 'escalation_match')

# 认证在 Runner 内从现有环境读取，宿主机参数和验收输出不携带 Key。
LF_HELPER = r'''
import base64,json,os,sys,urllib.request,urllib.error
q=json.load(sys.stdin)
path=q['path']
allowed=('/api/public/ready','/api/public/v2/datasets','/api/public/dataset-items',
         '/api/public/v2/observations','/api/public/v3/scores','/api/public/experiment-items')
assert any(path==p or path.startswith(p+'?') or path.startswith(p+'/') for p in allowed)
base=os.environ.get('LANGFUSE_BASE_URL') or os.environ['LANGFUSE_HOST']
auth=base64.b64encode((os.environ['LANGFUSE_PUBLIC_KEY']+':'+os.environ['LANGFUSE_SECRET_KEY']).encode()).decode()
data=json.dumps(q['body']).encode() if q.get('body') is not None else None
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**kw): return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
req=urllib.request.Request(base.rstrip('/')+path,data=data,method=q['method'],
 headers={'Authorization':'Basic '+auth,'Content-Type':'application/json'})
try:
 with opener.open(req,timeout=15) as r:
  status=r.status; body=r.read()
except urllib.error.HTTPError as e:
 status=e.code; body=b'{}'
except Exception:
 print(json.dumps({'status':0,'body':{}}));sys.exit(0)
try: result=json.loads(body)
except ValueError: result={}
print(json.dumps({'status':status,'body':result}))
'''


def lf_request(site, path, method='GET', body=None, allowed=(200,)):
    cid = service_container(site['project'], 'eval-runner')['Id']
    response = json.loads(docker('exec', '-i', cid, 'python', '-c', LF_HELPER,
                     data=json.dumps({'path': path, 'method': method, 'body': body}).encode(), timeout=30))
    need(response['status'] in allowed, 'Langfuse 内网 API HTTP ' + str(response['status']))
    return response['body']


def read_pages(fetch, path, params, max_pages=30):
    params = dict(params)
    rows, seen = [], set()
    for _ in range(max_pages):
        value = fetch(path + '?' + urlencode(params))
        need(isinstance(value, dict) and isinstance(value.get('data'), list) and isinstance(value.get('meta'), dict),
             'Langfuse 分页格式不符；不能把未知响应视为空集合')
        rows.extend(value['data'])
        cursor = value['meta'].get('cursor')
        if cursor is None:
            return rows
        need(isinstance(cursor, str) and cursor and cursor not in seen, 'Langfuse 游标重复/无效')
        seen.add(cursor)
        params['cursor'] = cursor
    raise Stop('Langfuse 分页超过上限')


def api(site, path, method='GET', body=None, allowed=(200,), key=None):
    headers = {'Idempotency-Key': key} if key else None
    return http_json(site['runner_url'] + path, method, body, headers=headers, timeout=40, allowed=allowed)[1]


def poll_launch(site, launch_id, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = api(site, '/api/v1/experiment-launches/' + launch_id)
        if value['status'].upper() in TERMINAL_LAUNCH_STATUSES:
            return value
        time.sleep(2)
    raise Stop('Launch 超时；保留 launch_id 诊断，不自动重新调用 Agent')


def frozen_identity(launch, expected_sha, version, selections):
    manifest = launch['manifest']
    need(manifest['runner']['build_id'] == expected_sha, 'Frozen Runner SHA 不符')
    need(manifest['agent']['version'] == version and manifest['agent']['agent_version_id'] == launch['agent_version_id'],
         'Frozen Agent 版本不符')
    need(manifest['dataset'].get('dataset_version') and len(manifest['dataset']['items']) == 6, 'Dataset 未冻结具体版本和六个用例')
    need(manifest.get('quality_policy') and manifest.get('contract_digests') and manifest.get('measurement_digest'),
         'Manifest 缺少评测/质量/比较契约')
    specs = {(e.get('id', e.get('evaluator_id')), e.get('version')) for e in manifest['evaluators']}
    need({(e['id'], e['version']) for e in selections} <= specs, 'Frozen Evaluator 版本不符')


def verify_remote_item(item, observations, scores, experiments, dataset_id, experiment_id):
    trace, obs = item['trace_id'], item['observation_id']
    need(any(o.get('id') == obs and o.get('traceId') == trace and o.get('name') == 'remote-agent-http'
             and o.get('type') == 'TOOL' for o in observations), '缺少同一 Trace 的真实 Agent HTTP Observation')
    need(any(o.get('id') == obs and o.get('traceId') == trace and o.get('experimentId') == experiment_id
             and o.get('experimentDatasetId') == dataset_id and o.get('experimentItemId') == item['dataset_item_id']
             for o in experiments), 'Experiment / Dataset Item / Observation 关联不一致')
    selected = []
    for metric in METRICS:
        expected_id = 'score:%s:gen%s:%s' % (item['id'], item['dispatch_generation'], metric)
        matches = [s for s in scores if s.get('id') == expected_id]
        need(len(matches) == 1, '缺少或重复的稳定 Score ID: ' + metric)
        s = matches[0]
        need(s.get('source') == 'API', 'Builtin Score 来源不符；不能与 Managed EVAL 混用')
        need(s.get('subject') == {'kind': 'observation', 'id': obs, 'traceId': trace}, 'Score subject 指向其他调用')
        need(s.get('name') == metric and s.get('value') == item['scores'][metric], '远端 Score 与本地真实结果不符')
        selected.append(s['id'])
    return selected


def remote_evidence(site, launch, items, output, timeout=180):
    need(launch.get('langfuse_experiment_id'), 'Launch 缺少远端 Experiment ID')
    exp_id = launch['langfuse_experiment_id']
    deadline = time.monotonic() + timeout
    fetch = lambda path: lf_request(site, path)
    last = None
    while time.monotonic() < deadline:
        try:
            experiments = read_pages(fetch, '/api/public/experiment-items', {'experimentId': exp_id, 'limit': 100})
            receipts = []
            for item in items:
                trace = item['trace_id']
                observations = read_pages(fetch, '/api/public/v2/observations',
                    {'traceId': trace, 'fields': 'core,basic,metadata,trace_context', 'limit': 100})
                scores = read_pages(fetch, '/api/public/v3/scores',
                    {'traceId': trace, 'observationId': item['observation_id'], 'source': 'API',
                     'fields': 'subject,details', 'limit': 100})
                score_ids = verify_remote_item(item, observations, scores, experiments, launch['dataset_id'], exp_id)
                receipts.append({'item_id': item['id'], 'trace_id': trace, 'observation_id': item['observation_id'], 'score_ids': score_ids})
                write_json(output / (item['id'] + '-remote.json'), {'observations': observations, 'scores': scores})
            write_json(output / 'experiment-items.json', experiments)
            return receipts
        except Stop as exc:
            last = exc
            time.sleep(3)  # 只重试读取可见性；不重发创建/执行/评分写入。
    raise Stop('远端 Trace/Score/Experiment 未在时限内完整可见：' + str(last))


def demo(site, deployment, output, expected_sha):
    source = Path(read_json(deployment / 'deployment.json')['source'])
    seed = read_json(source / 'data/dataset.json')
    need(len(seed['items']) == 6, 'Demo 数据契约已变化；先重新核对基线')
    token = stamp().lower()
    dataset_name = 'airgap-' + expected_sha[:12] + '-' + token
    agent_id = 'airgap-smoke-' + token
    write_json(output / 'scope.json', {'dataset_name': dataset_name, 'agent_id': agent_id,
                                      'git_sha': expected_sha, 'purpose': 'isolated synthetic acceptance'})
    dataset = lf_request(site, '/api/public/v2/datasets', 'POST',
        {'name': dataset_name, 'description': 'Argus offline synthetic acceptance ' + expected_sha}, allowed=(200, 201))
    for row in seed['items']:
        lf_request(site, '/api/public/dataset-items', 'POST', {'id': str(uuid.uuid4()), 'datasetName': dataset_name,
            'input': row['input'], 'expectedOutput': row.get('expected_output'),
            'metadata': {'purpose': 'argus-airgap-acceptance', 'git_sha': expected_sha}}, allowed=(200, 201))
    reread = lf_request(site, '/api/public/v2/datasets/' + quote(dataset_name, safe=''))
    need(reread.get('id') == dataset.get('id'), 'Dataset 写后读取身份不符')
    write_json(output / 'dataset.json', reread)
    api(site, '/api/v1/agents', 'POST', {'id': agent_id, 'name': '离线验收 ' + token,
        'description': 'Only synthetic demo services; safe for invocation fault test'}, allowed=(200, 201))
    for version, port in (('v1', 8080), ('v2', 8080), ('unreachable', 9)):
        endpoint = 'http://demo-agent-' + ('v1' if version == 'v1' else 'v2') + ':' + str(port) + '/invoke'
        api(site, '/api/v1/agent-versions', 'POST', {'agent_id': agent_id, 'version': version,
            'endpoint': endpoint, 'request_mapping': {'messages': 'input.messages', 'customer_id': 'input.customer_id'},
            'timeout_seconds': 10, 'max_retries': 0, 'max_concurrency': 2,
            'is_idempotent': True, 'environment': site['environment']}, allowed=(200, 201))
    catalog = api(site, '/api/v1/evaluators')
    selections = [{'id': metric, 'version': '1.0.0'} for metric in METRICS]
    for metric in METRICS:
        rows = [e for e in catalog if e['id'] == metric]
        need(len(rows) == 1 and any(v['version'] == '1.0.0' and v['release_eligible'] for v in rows[0]['versions']),
             'Builtin 1.0.0 不可用；不得静默改用 latest')
    policy_name = 'offline-' + token
    policy = {'name': policy_name, 'version': '1.0.0', 'agent_id': agent_id, 'environment': site['environment'],
              'rules': [{'id': 'pass-rate', 'metric': 'pass_rate', 'operator': '>=', 'threshold': 0.95}]}
    saved_policy = api(site, '/api/v1/release-policies', 'POST', policy, allowed=(201,))
    write_json(output / 'release-policy.json', saved_policy)
    runs = {}
    for version, expected_pass, gate_expected in (('v1', 2, 'FAIL'), ('v2', 6, 'PASS'), ('unreachable', 0, 'UNKNOWN')):
        folder = output / version
        folder.mkdir()
        payload = {'agent_id': agent_id, 'agent_version': version, 'dataset_name': dataset_name,
                   'environment': site['environment'], 'evaluation_source': 'builtin',
                   'evaluator_selections': selections, 'max_concurrency': 2, 'name': dataset_name + '-' + version}
        launch = api(site, '/api/v1/experiment-launches', 'POST', payload, allowed=(200, 201), key=agent_id + '-' + version)
        lid = launch['id']
        write_json(folder / 'created-launch.json', launch)
        frozen_identity(launch, expected_sha, version, selections)
        api(site, '/api/v1/experiment-launches/' + lid + '/run', 'POST', allowed=(202,))
        launch = poll_launch(site, lid)
        items = api(site, '/api/v1/experiment-launches/' + lid + '/items?limit=500')
        write_json(folder / 'launch.json', launch)
        write_json(folder / 'items.json', items)
        need(len(items) == 6, '用例执行数量不符')
        passed = sum(i['quality_conclusion'].lower() == 'pass' for i in items)
        need(passed == expected_pass, version + ' 真实质量通过数不符')
        if version != 'unreachable':
            need(all(i['execution_status'] == 'succeeded' and i['eval_status'] == 'succeeded' for i in items),
                 '执行失败与质量失败不能混为一谈')
            need(all(re.fullmatch('[0-9a-f]{32}', i.get('trace_id') or '') for i in items), '缺少真实 Trace ID')
        else:
            need(all(i['execution_status'] == 'failed' and i['quality_conclusion'].lower() != 'pass' for i in items),
                 'Agent 不可达路径未被正确隔离')
        frozen = api(site, '/api/v1/experiment-launches/' + lid + '/result-snapshots', 'POST')
        snapshot = api(site, '/api/v1/experiment-launches/' + lid + '/result-snapshots/' + frozen['snapshot_id'])
        write_json(folder / 'snapshot.json', snapshot)
        request = {'policy_name': policy_name, 'policy_version': '1.0.0', 'candidate_launch_id': lid,
                   'candidate_snapshot_id': frozen['snapshot_id']}
        gate = api(site, '/api/v1/release-gates/evaluate', 'POST', request, allowed=(201,))
        write_json(folder / 'gate.json', gate)
        need(gate['decision'] == gate_expected and gate['releasable'] == (gate_expected == 'PASS'), 'Gate 未按固定证据作出预期决定')
        need(gate['candidate_snapshot_id'] == snapshot['snapshot_id'] and gate['policy_digest'] == saved_policy['policy_digest'], 'Gate 证据身份不符')
        # 重读相同 Snapshot；不能用动态 summary/latest 替代。
        reread = api(site, '/api/v1/experiment-launches/' + lid + '/result-snapshots/' + snapshot['snapshot_id'])
        need(reread == snapshot, '冻结 Snapshot 发生改变')
        runs[version] = {'launch_id': lid, 'snapshot_id': snapshot['snapshot_id'], 'gate_id': gate['id'],
                         'decision': gate['decision'], 'passed': passed}
        if version != 'unreachable':
            # 链接后台同步可能晚于执行结束，只轮询读取该 Launch。
            deadline = time.monotonic() + 180
            while not launch.get('langfuse_experiment_id') and time.monotonic() < deadline:
                time.sleep(2)
                launch = api(site, '/api/v1/experiment-launches/' + lid)
            runs[version]['remote'] = remote_evidence(site, launch, items, folder)
    # 真实 HTTP 反例：跨 Launch Snapshot、策略不可变和错误环境均必须拒绝。
    api(site, '/api/v1/experiment-launches/' + runs['v2']['launch_id'] + '/result-snapshots/' + runs['v1']['snapshot_id'], allowed=(404,))
    bad_gate = {'policy_name': policy_name, 'policy_version': '1.0.0', 'candidate_launch_id': runs['v2']['launch_id'],
                'candidate_snapshot_id': runs['v1']['snapshot_id']}
    api(site, '/api/v1/release-gates/evaluate', 'POST', bad_gate, allowed=(404,))
    changed = copy.deepcopy(policy)
    changed['rules'][0]['threshold'] = 0.5
    api(site, '/api/v1/release-policies', 'POST', changed, allowed=(409,))
    wrong_scope = copy.deepcopy(policy)
    wrong_scope['name'] += '-other'
    wrong_scope['environment'] = 'airgap-other-scope'
    api(site, '/api/v1/release-policies', 'POST', wrong_scope, allowed=(201,))
    bad_gate.update(policy_name=wrong_scope['name'], candidate_snapshot_id=runs['v2']['snapshot_id'])
    api(site, '/api/v1/release-gates/evaluate', 'POST', bad_gate, allowed=(409,))
    write_json(output / 'negative-paths.json', {'cross_launch_snapshot': 'PASS', 'cross_launch_gate': 'PASS',
        'policy_immutability': 'PASS', 'environment_mismatch': 'PASS', 'unreachable_agent_unknown_gate': 'PASS'})
    return runs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', default='/data/argus-offline')
    ap.add_argument('--demo', action='store_true', help='明确创建独立合成 Dataset/Agent/Launch/Policy，调用现有 Demo 容器')
    a = ap.parse_args()
    verify_toolchain()
    root = Path(a.root).resolve()
    with operation(root):
        need(not (root / 'transaction.json').exists(), '仍有失败事务，不能给出验收通过')
        deployment = active(root)
        expected = read_json(deployment / 'deployment.json')['git_sha']
        output = root / 'evidence' / ('verify-' + stamp())
        report = {'git_sha': expected, 'observed_at': stamp(), 'health': 'PENDING', 'business': 'NOT_RUN',
                  'ui': 'PENDING_MANUAL', 'source_quality': 'PENDING_SEPARATE_EVIDENCE', 'overall': 'PENDING'}
        write_json(output / 'verification.json', report)
        try:
            site = preflight(root, deployment)
            verify_app_images(site, deployment)
            # 验证 Console 到 Runner 代理与构建身份；首页 200 不足以证明静态资源或页面正确。
            console_info = http_json(site['console_url'] + '/api/v1/system/info')[1]
            need(console_info.get('build_id') == expected, 'Console API 代理未连接同一 Runner')
            lf_request(site, '/api/public/ready')
            report['health'] = 'PASS'
            report['migrations'] = applied_migrations(site)
            report['langfuse_unchanged'] = protected_state(site)
            if a.demo:
                report['runs'] = demo(site, deployment, output, expected)
                report['business'] = 'PASS'
                report['overall'] = 'API_PASS_UI_AND_SOURCE_EVIDENCE_PENDING'
            write_json(output / 'verification.json', report)
            checksums(output)
            state = read_json(root / 'active.json')
            state['acceptance'] = report['overall']
            state['verification_evidence'] = str(output)
            write_json(root / 'active.json', state)
        except BaseException:
            report['overall'] = 'FAIL_OR_INCOMPLETE'
            write_json(output / 'verification.json', report)
            checksums(output)
            raise
        print('验收报告: ' + str(output / 'verification.json'))
        print(report['overall'] + '；页面、源代码质量及历史数据抽查仍按验收清单完成。')


if __name__ == '__main__':
    finish_main(main)
