"""真实 Console 验收脚本的安全门禁；单元测试不产生远程调用。"""
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / 'docs/validation/issue219-managed-console/real_console_acceptance.py'


def harness():
    spec = importlib.util.spec_from_file_location('issue219_acceptance', PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_dry_run_does_not_start_services_or_read_credentials(monkeypatch, capsys):
    module = harness()
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: pytest.fail('unexpected subprocess'))
    module.main([])
    assert 'DRY RUN' in capsys.readouterr().out


def test_source_evidence_matches_frozen_score_and_rejects_duplicate_or_api_source():
    module = harness()
    evidence = {'id': 'score-1', 'source': 'EVAL', 'value': True, 'dataType': 'BOOLEAN'}
    module.validate_score_readback([evidence], evidence)
    for rows in ([], [evidence, evidence], [{**evidence, 'source': 'API'}], [{**evidence, 'value': False}]):
        with pytest.raises(AssertionError):
            module.validate_score_readback(rows, evidence)


def test_isolated_flowise_deployment_is_configurable_without_touching_services(monkeypatch, capsys):
    """复验不得依赖已经销毁的 #219 容器、端口或 Flow ID。"""
    module = harness()
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: pytest.fail('unexpected subprocess'))
    module.main(['--flowise-origin', 'http://127.0.0.1:19091',
                 '--flowise-postgres-container', 'argus-flowise-e2e-postgres-1',
                 '--flow-id', '35f4c55f-4ba5-4138-aaf9-d6f5751222a7',
                 '--name-prefix', 'issue212', '--flowise-credential-ref', 'env://FLOWISE_API_TOKEN'])
    assert 'DRY RUN' in capsys.readouterr().out


def test_flow_identifier_rejects_sql_text_before_any_external_call(monkeypatch):
    module = harness()
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: pytest.fail('unexpected subprocess'))
    with pytest.raises(SystemExit) as exc:
        module.main(['--flow-id', "' OR 1=1 --"])
    assert exc.value.code == 2


def test_issue212_flow_clone_preserves_source_and_credentials_but_disables_retries():
    import copy
    path = PATH.parents[1] / 'issue212-product-acceptance/prepare_flowise.py'
    spec = importlib.util.spec_from_file_location('issue212_prepare', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = {'nodes': [{'data': {'name': 'agentAgentflow', 'inputs': {
        'agentModelConfig': {'FLOWISE_CREDENTIAL_ID': 'existing', 'maxRetries': 2},
        'agentEnableMemory': True, 'agentTools': [
            {'agentSelectedTool': 'calculator'}, {'agentSelectedTool': 'requestsGet'}]}}}]}
    before = copy.deepcopy(original)
    result = module.isolated_graph(original)
    assert original == before
    agent = result['nodes'][0]['data']['inputs']
    assert agent['agentModelConfig']['maxRetries'] == 0
    assert agent['agentModelConfig']['FLOWISE_CREDENTIAL_ID'] == 'existing'
    assert agent['agentEnableMemory'] is False
    assert agent['agentTools'] == [{'agentSelectedTool': 'calculator'}]
