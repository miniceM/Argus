"""#218：真实采纳边界后的质量判定、冻结证据与现有 Gate。"""

from __future__ import annotations

import copy

import pytest
from app.comparison_contracts import aggregation_comparison_digest, assess_comparability
from app.db_models import EvaluationResultRecord, ExperimentLaunchRecord, RunResultSnapshotRecord
from app.evaluator_binding import canonical_digest
from app.managed_evaluation import freeze_managed_evaluation
from app.managed_score_recovery import recover_managed_scores
from app.release_gates import evaluate_gate
from app.result_snapshots import create_result_snapshot
from sqlalchemy import select
from test_issue_7_release_gates import policy
from test_issue_216_managed_scores import Remote
from test_issue_217_managed_async import (
    _good_score,
    _item,
    _managed_manifest,
    _observation_for,
    _run_managed_http,
)


def manifest218():
    manifest = _managed_manifest()
    raw = copy.deepcopy(manifest["managed_evaluation"])
    raw = {k: raw[k] for k in ("project_id", "environment", "target", "scores")}
    raw["scores"].append(
        {
            "name": "code",
            "data_type": "boolean",
            "required": True,
            "rule_id": "rule-code",
            "evaluator_id": "evaluator-code",
        }
    )
    rules = copy.deepcopy(manifest["quality_policy"]["rules"])
    rules.append(
        {"evaluator_id": "code", "result_type": "boolean", "operator": "==", "expected_value": True, "required": True}
    )
    contract, quality = freeze_managed_evaluation(raw, environment="production", policy_rules=rules)
    manifest.update(
        managed_evaluation=contract,
        measurement_digest=contract["acceptance_digest"],
        quality_policy=quality.to_payload(),
    )
    manifest["dataset"].update(source="seed", dataset_id="ds218", dataset_version="v1")
    manifest["contract_digests"] = {
        "measurement": {"digest": contract["acceptance_digest"]},
        "quality_policy": {"digest": quality.policy_digest},
        "aggregation_comparison": {"digest": aggregation_comparison_digest(managed=True)},
    }
    return manifest


class NamedRemote(Remote):
    def scores_for(self, trace_id, observation_id, name):
        return [r for r in super().scores_for(trace_id, observation_id, name) if r["name"] == name]


def collect218(setup_runtime, monkeypatch, *, values=(True, True), corrupt=None, frozen_manifest=None):
    manifest = frozen_manifest or manifest218()
    db, _, _, _, _, launch_id, item_id, calls, ok = _run_managed_http(setup_runtime, manifest, {"answer": "ok"})
    assert calls == 1 and ok
    with db.get_session() as session:
        session.get(ExperimentLaunchRecord, launch_id).status = "COMPLETED"
    obs, trace, observation = _observation_for(db, item_id)
    rows = []
    for name, value in zip(("correctness", "code"), values, strict=True):
        if value is None:
            continue
        row = _good_score(
            trace,
            observation,
            id=f"s218-{name}",
            name=name,
            value=value,
            dataType=next(
                s["data_type"].upper() for s in manifest["managed_evaluation"]["scores"] if s["name"] == name
            ),
        )
        if name == "code":
            row["metadata"].update(evaluation_rule_id="rule-code", evaluator_id="evaluator-code")
        rows.append(row)
    if corrupt:
        corrupt(rows)
    remote = NamedRemote(obs, rows)
    monkeypatch.setattr("app.managed_score_recovery.get_managed_score_client", lambda: remote)
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "1")
    item = _item(db, item_id)
    outcome = recover_managed_scores(
        db, item_id=item_id, evaluation_generation=item.evaluation_generation, worker_id="w218"
    )
    with db.get_session() as session:
        snapshot = session.scalar(
            select(RunResultSnapshotRecord)
            .where(RunResultSnapshotRecord.launch_id == launch_id)
            .order_by(RunResultSnapshotRecord.revision.desc())
        )
    assert snapshot is not None, outcome
    return db, snapshot, remote, item_id


@pytest.mark.parametrize(
    ("values", "quality", "gate"),
    [
        ((True, True), "pass", "PASS"),
        ((False, True), "fail", "FAIL"),
        ((True, False), "fail", "FAIL"),
        ((True, None), "unknown", "UNKNOWN"),
    ],
)
def test_adopted_scores_decide_existing_snapshot_and_gate(setup_runtime, monkeypatch, values, quality, gate):
    db, snapshot, remote, item_id = collect218(setup_runtime, monkeypatch, values=values)
    assert _item(db, item_id).quality_conclusion == quality
    assert snapshot.items[0]["quality_conclusion"] == quality
    assert snapshot.evidence_state == ("DIAGNOSTIC" if quality == "unknown" else "COMPLETE")
    result = evaluate_gate(policy(), snapshot)
    assert result.decision == gate, result
    assert result.releasable == (gate == "PASS")
    assert snapshot.summary["required_score_coverage"] == (0.5 if None in values else 1.0)
    assert snapshot.manifest["evaluators"] == []  # 不伪造外部执行版本。


@pytest.mark.parametrize("problem", ["source", "type", "rule", "conflict", "unavailable"])
def test_invalid_external_evidence_never_releases(setup_runtime, monkeypatch, problem):
    if problem == "unavailable":
        from app.langfuse_scores_client import ScoresReadError

        def unavailable(*args):
            raise ScoresReadError("REMOTE_UNAVAILABLE")

        monkeypatch.setattr(NamedRemote, "observations", unavailable)

    def corrupt(rows):
        if problem == "source":
            rows[1]["source"] = "API"
        elif problem == "type":
            rows[1]["dataType"] = "NUMERIC"
        elif problem == "rule":
            rows[1]["metadata"]["evaluation_rule_id"] = "untrusted"
        elif problem == "conflict":
            duplicate = copy.deepcopy(rows[1])
            duplicate["id"] = "another"
            rows.append(duplicate)
        else:
            rows.clear()

    _, snapshot, _, _ = collect218(setup_runtime, monkeypatch, corrupt=corrupt)
    result = evaluate_gate(policy(), snapshot)
    assert snapshot.evidence_state == "DIAGNOSTIC"
    assert result.decision == "UNKNOWN" and not result.releasable


@pytest.mark.parametrize("field", ["source", "value", "binding", "missing", "duplicate"])
def test_frozen_gate_rechecks_adoption_not_complete_label(setup_runtime, monkeypatch, field):
    _, snapshot, _, _ = collect218(setup_runtime, monkeypatch)
    item = snapshot.items[0]
    result = item["evaluation_results"][0]
    if field == "source":
        result["evidence"]["score"]["source"] = "API"
    elif field == "value":
        result["value"] = False
    elif field == "binding":
        result["provenance"]["binding_id"] = "wrong"
    elif field == "missing":
        item["evaluation_results"].pop()
    else:
        item["evaluation_results"].append(copy.deepcopy(result))
    gate = evaluate_gate(policy(), snapshot)
    assert gate.decision == "UNKNOWN" and not gate.releasable


def test_snapshot_revision_and_old_gate_survive_later_results(setup_runtime, monkeypatch):
    db, old, remote, item_id = collect218(setup_runtime, monkeypatch)
    before = canonical_digest({"manifest": old.manifest, "items": old.items, "summary": old.summary})
    gate = evaluate_gate(policy(), old).model_dump()
    # 模拟远端 Rule / Score 随后变化；Gate 完全不读取远端。
    remote.scores[0]["value"] = False
    remote.scores[0]["metadata"]["evaluator_version_id"] = "later-latest"
    remote.scores_for = lambda *args: pytest.fail("Gate must not query Langfuse")
    with db.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, old.launch_id)
        assert create_result_snapshot(session, launch).id == old.id
        row = session.scalar(
            select(EvaluationResultRecord).where(
                EvaluationResultRecord.item_execution_id == item_id,
                EvaluationResultRecord.evaluator_id == "correctness",
            )
        )
        # 当前行发生变化只能产生新 revision；不回写历史 Snapshot。
        row.value = False
        row.evidence = {**row.evidence, "score": {**row.evidence["score"], "value": False}}
        session.flush()
        newer = create_result_snapshot(session, launch)
        assert newer.id != old.id and newer.revision == old.revision + 1
        assert newer.items[0]["quality_conclusion"] == "fail"
    with db.get_session() as session:
        frozen = session.get(RunResultSnapshotRecord, old.id)
        assert (
            canonical_digest({"manifest": frozen.manifest, "items": frozen.items, "summary": frozen.summary}) == before
        )
        assert evaluate_gate(policy(), frozen).model_dump() == gate


def test_managed_baseline_contract_difference_is_not_quality_regression(setup_runtime, monkeypatch):
    _, candidate, _, _ = collect218(setup_runtime, monkeypatch)
    baseline = copy.deepcopy(candidate)
    baseline.id = "baseline218"
    candidate.manifest = copy.deepcopy(candidate.manifest)
    candidate.manifest["comparison"]["baseline_snapshot_id"] = baseline.id
    baseline.manifest = copy.deepcopy(baseline.manifest)
    baseline.manifest["contract_digests"]["quality_policy"]["digest"] = "different-policy"
    assert not assess_comparability(baseline.manifest, candidate.manifest).comparable
    result = evaluate_gate(
        policy({"id": "delta", "metric": "pass_rate_delta", "operator": ">=", "threshold": 0.0}), candidate, baseline
    )
    assert result.decision == "UNKNOWN" and "QUALITY_POLICY_CHANGED" in result.reason_codes


def test_partial_run_keeps_all_cases_in_denominator(setup_runtime, monkeypatch):
    from app.aggregation import aggregate_run

    _, snapshot, _, _ = collect218(setup_runtime, monkeypatch)
    missing = copy.deepcopy(snapshot.items[0])
    missing.update(
        dataset_item_id="missing",
        eval_status="pending",
        quality_conclusion="unknown",
        evaluation_results=[],
        managed_evidence_reasons=["MANAGED_SCORE_UNVERIFIED:correctness", "MANAGED_SCORE_UNVERIFIED:code"],
    )
    summary = aggregate_run([snapshot.items[0], missing], [], manifest=snapshot.manifest)
    assert summary["pass_rate"] == 0.5
    assert summary["pass_rate_denominator"] == 2
    assert summary["evaluation_coverage"] == summary["required_score_coverage"] == 0.5
    assert summary["decided_pass_rate"] == 1.0 and summary["decision_coverage"] == 0.5


def test_managed_raw_scores_are_never_projected_back(setup_runtime, monkeypatch):
    from app.langfuse_sync import _project_scores

    _, snapshot, _, _ = collect218(setup_runtime, monkeypatch)
    payload = {"item_id": "item218", "dispatch_generation": 1}
    typed = snapshot.items[0]["evaluation_results"]
    projected = _project_scores(
        payload, scores={"_typed_results": typed}, snapshot_id=snapshot.id, revision=1, policy_digest="policy"
    )
    assert not projected
    projected = _project_scores(
        payload,
        scores={"_evaluation_source": "langfuse_managed", "code": 1.0},
        snapshot_id=snapshot.id,
        revision=1,
        policy_digest="policy",
    )
    assert not projected


@pytest.mark.parametrize("versions", [("v1", "v1", "PASS"), ("v1", "v2", "UNKNOWN"), (None, None, "UNKNOWN")])
def test_managed_baseline_never_claims_latest_rule_is_frozen(setup_runtime, monkeypatch, versions):
    from app.aggregation import compare_case_results
    from app.managed_quality import evaluation_specs

    _, snapshot, _, _ = collect218(setup_runtime, monkeypatch)
    before, after = copy.deepcopy(snapshot.items[0]), copy.deepcopy(snapshot.items[0])
    for item, version in zip((before, after), versions[:2], strict=True):
        if version:
            for result in item["evaluation_results"]:
                result["evidence"]["score"]["metadata"]["evaluator_version_id"] = version
    diff = compare_case_results(
        before,
        after,
        evaluator_specs=evaluation_specs(snapshot.manifest),
        baseline_evaluators=evaluation_specs(snapshot.manifest),
    )
    assert (diff["classification"] == "UNCHANGED") == (versions[2] == "PASS")
    if versions[2] == "UNKNOWN":
        assert diff["classification"] == "NOT_COMPARABLE"


def test_full_rows_but_unsuccessful_collection_cannot_be_promoted_by_snapshot(setup_runtime, monkeypatch):
    from app.db_models import ExperimentItemExecutionRecord

    db, snapshot, _, item_id = collect218(setup_runtime, monkeypatch)
    with db.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, item_id)
        item.eval_status = "failed"
        item.quality_conclusion = "unknown"
        newer = create_result_snapshot(session, session.get(ExperimentLaunchRecord, snapshot.launch_id))
        assert newer.items[0]["quality_conclusion"] == "unknown"
        assert newer.evidence_state == "DIAGNOSTIC"
        assert evaluate_gate(policy(), newer).decision == "UNKNOWN"


def test_missing_case_record_stays_in_managed_denominator(setup_runtime, monkeypatch):
    from app.aggregation import aggregate_run

    _, snapshot, _, _ = collect218(setup_runtime, monkeypatch)
    manifest = copy.deepcopy(snapshot.manifest)
    manifest["dataset"]["items"].append({"id": "missing"})
    summary = aggregate_run(snapshot.items, [], manifest=manifest)
    assert summary["total_cases"] == 2 and summary["quality_unknown_count"] == 1
    assert summary["pass_rate"] == summary["evaluation_coverage"] == summary["required_score_coverage"] == 0.5


@pytest.mark.parametrize(
    ("result_type", "value", "expected", "quality"),
    [
        ("numeric", 0.9, 0.8, "PASS"),
        ("numeric", 0.2, 0.8, "FAIL"),
        ("numeric", True, 0.8, "UNKNOWN"),
        ("categorical", "good", "good", "PASS"),
        ("categorical", "bad", "good", "FAIL"),
        ("categorical", "invalid", "good", "UNKNOWN"),
    ],
)
def test_managed_typed_numeric_and_category_policy(setup_runtime, monkeypatch, result_type, value, expected, quality):
    manifest = manifest218()
    raw = {
        k: copy.deepcopy(manifest["managed_evaluation"][k]) for k in ("project_id", "environment", "target", "scores")
    }
    raw["scores"][1]["data_type"] = result_type
    if result_type == "categorical":
        raw["scores"][1]["category_values"] = ["good", "bad"]
    rules = copy.deepcopy(manifest["quality_policy"]["rules"])
    rules[1] = dict(
        evaluator_id="code",
        result_type=result_type,
        required=True,
        operator=">=" if result_type == "numeric" else "==",
        **({"threshold": expected} if result_type == "numeric" else {"expected_value": expected}),
    )
    contract, policy218 = freeze_managed_evaluation(raw, environment="production", policy_rules=rules)
    manifest.update(
        managed_evaluation=contract,
        measurement_digest=contract["acceptance_digest"],
        quality_policy=policy218.to_payload(),
    )
    manifest["contract_digests"]["measurement"]["digest"] = contract["acceptance_digest"]
    manifest["contract_digests"]["quality_policy"]["digest"] = policy218.policy_digest
    _, snapshot, _, _ = collect218(setup_runtime, monkeypatch, values=(True, value), frozen_manifest=manifest)
    assert evaluate_gate(policy(), snapshot).decision == quality
    if result_type == "categorical":
        assert snapshot.summary["score_means"].get("code") is None


def test_stored_gate_is_immutable_and_idempotent(setup_runtime, monkeypatch):
    from app import api_release_gates
    from app.api_release_gates import EvaluateGateRequest, create_release_gate, create_release_policy, get_release_gate

    db, snapshot, remote, _ = collect218(setup_runtime, monkeypatch)
    monkeypatch.setattr(api_release_gates, "_db_manager", lambda: db)
    create_release_policy(policy())
    request = EvaluateGateRequest(
        policy_name=policy().name,
        policy_version=policy().version,
        candidate_launch_id=snapshot.launch_id,
        candidate_snapshot_id=snapshot.id,
    )
    first = create_release_gate(request)
    assert first.decision == "PASS"
    remote.scores.clear()
    assert get_release_gate(first.id).model_dump() == first.model_dump()
    assert create_release_gate(request).model_dump() == first.model_dump()
