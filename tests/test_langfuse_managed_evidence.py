"""Offline consistency checks over retained real receipts; NOT a new live run."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/validation/issue214-managed-evaluation/evidence"
SPEC = importlib.util.spec_from_file_location("managed_evidence_probe", ROOT / "scripts/verify_langfuse_managed.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def read(name):
    return json.loads((EVIDENCE / name).read_text())


@pytest.mark.parametrize("index,label", [(0, "a"), (1, "b")])
def test_retained_real_agent_checkpoint_and_managed_score_join(index, label):
    receipt = read("receipt.json")["runs"][index]
    checkpoint = read(f"{label}-checkpoint.json")
    rows = read(f"{label}-observations.json")["response"]["data"]
    business = probe.verify_observation(rows, receipt)
    assert probe.parse_io(business["output"]) == checkpoint["agent_output"]
    scores = probe.verify_scores(read(f"{label}-scores-v3.json")["response"]["data"], receipt)
    assert len({s["metadata"]["job_execution_id"] for s in scores}) == 2
    probe.verify_experiment_item(read(f"{label}-experiment-items.json")["response"]["data"], receipt)
    item = read(f"{label}-items.json")["response"][0]
    assert item["attempt_count"] == 1 and item["final_attempt_http_status"] == 200
    # External success did not magically become local Builtin/Gate success.
    assert item["quality_conclusion"] == "fail"


def test_real_duplicate_rule_ambiguity_is_not_silently_adopted():
    receipt = read("receipt.json")["runs"][0]
    scores = read("duplicate-rule-scores-v3.json")["response"]["data"]
    code_id = next(e["id"] for e in receipt["evaluators"] if e["type"] == "code")
    code = [s for s in scores if s["metadata"]["evaluator_id"] == code_id]
    assert len(code) == 2 and len({s["metadata"]["evaluation_rule_id"] for s in code}) == 2
    assert all(s["source"] == "EVAL" for s in scores)
    with pytest.raises(probe.EvidenceError, match="ambiguous"):
        probe.verify_scores(scores, receipt)
    error = read("invalid-code-execution.json")["response"]
    assert error["level"] == "ERROR" and "ISSUE214_INVALID_OBSERVATION_IO" in str(error)
    assert read("rule-miss-scores-v3.json")["response"]["data"] == []


def test_evidence_manifest_and_request_safety():
    manifest = read("index.json")
    for entry in manifest["files"]:
        raw = (EVIDENCE / entry["path"]).read_bytes()
        assert len(raw) == entry["bytes"] and hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert b"pk-lf-" not in raw and b"sk-lf-" not in raw
    audit = read("request-audit.json")
    assert not any(row["method"] != "GET" and "/scores" in row["path"] for row in audit)
    assert all(row["method"] == "GET" for row in read("fresh-api/verification.json")["requests"])
    rules = read("all-owned-rules-paused.json")
    assert len(rules["rule_ids"]) == 3
    for rule in rules["rule_ids"]:
        saved = read(f"paused-final-{rule}.json")
        assert saved["status"] == 200 and saved["response"]["enabled"] is False
