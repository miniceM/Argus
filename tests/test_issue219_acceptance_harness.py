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
