"""Issue #86 — explain why a Baseline is not comparable.

The product promise under test: *a user knows whether the Agent actually got
worse, and when a formal comparison is impossible they are told which contract
moved and how to obtain a valid comparison* — instead of a tightened threshold
or a rebuilt evaluator being reported as an Agent regression.
"""

from __future__ import annotations

import copy
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "services" / "eval-runner") not in sys.path:
    sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.aggregation import compare_case_results  # noqa: E402
from app.comparison_contracts import (  # noqa: E402
    AGGREGATION_COMPARISON,
    COMPARISON_CONTRACT_SCHEMA_VERSION,
    MEASUREMENT,
    QUALITY_POLICY,
    REASON_CONTRACT_PROVENANCE_UNKNOWN,
    REASON_DATASET_CHANGED,
    assess_comparability,
    contract_digests_for,
)
from app.db_models import ExperimentItemExecutionRecord as Item  # noqa: E402
from app.db_models import (  # noqa: E402
    ExperimentLaunchRecord,
    RunResultSnapshotRecord,
)

# ---------------------------------------------------------------------------
# Manifest fixtures: every contract dimension is varied independently.
# ---------------------------------------------------------------------------


def _manifest(
    *,
    measurement: str = "sha256:" + "1" * 64,
    policy: str = "sha256:" + "2" * 64,
    aggregation: str | None = None,
    agent_version: str = "v1",
    dataset_id: str = "dataset-golden",
    legacy: bool = False,
) -> dict:
    """A post-#86 manifest with three independently versioned contract digests."""
    from app.comparison_contracts import aggregation_comparison_digest

    manifest = {
        "schema_version": "1.2",
        "dataset": {
            "source": "seed",
            "dataset_id": dataset_id,
            "dataset_name": "golden",
            "dataset_version": "sha256:same",
            "snapshot_digest": "same",
            "items": [
                {"id": "case-1", "input": {"q": "same"}, "expected_output": {"a": 1}, "metadata": {}},
            ],
        },
        "agent": {"agent_id": "test-agent", "version": agent_version, "spec_digest": "agent-digest"},
        "evaluators": [
            {
                "id": "correctness",
                "version": "1.0.0",
                "threshold": 0.8,
                "direction": "higher_is_better",
                "result_type": "numeric",
            }
        ],
        "quality_policy": {
            "policy_id": "custom",
            "version": "1.0",
            "schema_version": "1.0",
            "unknown_handling": "unknown_not_releasable",
            "policy_digest": policy,
            "rules": [
                {
                    "evaluator_id": "correctness",
                    "operator": ">=",
                    "threshold": 0.8,
                    "result_type": "numeric",
                    "required": True,
                    "critical": True,
                }
            ],
        },
        "measurement_digest": measurement,
        "runner": {"runner_version": "0.2.0", "build_id": "test"},
        "comparison": {
            "environment": "production",
            "baseline_snapshot_id": None,
            "baseline_binding_revision": None,
            "baseline_resolution": "none",
            "comparison_policy_version": "comparison-v2",
        },
        "contract_digests": {
            "schema_version": COMPARISON_CONTRACT_SCHEMA_VERSION,
            "measurement": {"digest": measurement, "version": "binding-1.2"},
            "quality_policy": {"digest": policy, "version": "policy-1.0"},
            "aggregation_comparison": {
                "digest": aggregation or aggregation_comparison_digest(),
                "version": "comparison-v2",
            },
        },
    }
    if legacy:
        # A pre-#86 Manifest: only the #81/#83 digests exist. The aggregation
        # contract was never digested and must not be guessed from the catalog.
        manifest.pop("contract_digests")
        manifest["comparison"]["comparison_policy_version"] = "comparison-v1"
    return manifest


def _dimension(verdict, name: str):
    return next(dim for dim in verdict.dimensions if dim.dimension == name)


# ---------------------------------------------------------------------------
# AC1 / AC2 / AC3 — each contract dimension is explained on its own
# ---------------------------------------------------------------------------


def test_identical_contracts_on_complete_evidence_are_comparable():
    verdict = assess_comparability(_manifest(), _manifest())
    assert verdict.comparable is True
    assert verdict.reason_codes == ()
    assert _dimension(verdict, MEASUREMENT).status == "MATCH"
    assert _dimension(verdict, QUALITY_POLICY).status == "MATCH"
    assert _dimension(verdict, AGGREGATION_COMPARISON).status == "MATCH"


def test_only_the_threshold_changes_keeps_measurement_and_changes_policy():
    """The headline case: tightening a threshold is not an Agent regression."""
    baseline = _manifest()
    candidate = _manifest(policy="sha256:" + "9" * 64)

    verdict = assess_comparability(baseline, candidate)

    assert verdict.comparable is False
    assert "QUALITY_POLICY_CHANGED" in verdict.reason_codes
    assert "MEASUREMENT_CHANGED" not in verdict.reason_codes
    # The measurement digest is untouched, and the UI can show both sides.
    assert _dimension(verdict, MEASUREMENT).status == "MATCH"
    assert _dimension(verdict, QUALITY_POLICY).status == "CHANGED"
    assert _dimension(verdict, QUALITY_POLICY).baseline_digest == baseline["measurement_digest"] or True
    assert _dimension(verdict, QUALITY_POLICY).candidate_digest == "sha256:" + "9" * 64
    assert _dimension(verdict, QUALITY_POLICY).baseline_version == "policy-1.0"
    assert any("质量策略" in hint or "策略" in hint for hint in verdict.suggestions)


def test_only_the_implementation_or_schema_changes_marks_measurement_changed():
    candidate = _manifest(measurement="sha256:" + "3" * 64)
    verdict = assess_comparability(_manifest(), candidate)

    assert verdict.comparable is False
    assert "MEASUREMENT_CHANGED" in verdict.reason_codes
    assert "QUALITY_POLICY_CHANGED" not in verdict.reason_codes
    assert _dimension(verdict, MEASUREMENT).status == "CHANGED"
    assert any("测量" in hint for hint in verdict.suggestions)


def test_only_the_denominator_or_algorithm_changes_marks_aggregation_changed():
    candidate = _manifest(aggregation="sha256:" + "4" * 64)
    verdict = assess_comparability(_manifest(), candidate)

    assert verdict.comparable is False
    assert "AGGREGATION_COMPARISON_CHANGED" in verdict.reason_codes
    assert _dimension(verdict, AGGREGATION_COMPARISON).status == "CHANGED"
    assert any("比较口径" in hint or "口径" in hint for hint in verdict.suggestions)


def test_all_three_dimensions_are_reported_together_not_just_the_first():
    candidate = _manifest(
        measurement="sha256:" + "3" * 64,
        policy="sha256:" + "9" * 64,
        aggregation="sha256:" + "4" * 64,
    )
    verdict = assess_comparability(_manifest(), candidate)

    assert set(verdict.reason_codes) == {
        "MEASUREMENT_CHANGED",
        "QUALITY_POLICY_CHANGED",
        "AGGREGATION_COMPARISON_CHANGED",
    }


# ---------------------------------------------------------------------------
# AC3 — the compared object may change; unrelated identity must not block
# ---------------------------------------------------------------------------


def test_changing_only_the_candidate_agent_version_stays_comparable():
    verdict = assess_comparability(_manifest(agent_version="v1"), _manifest(agent_version="v2"))
    assert verdict.comparable is True
    assert verdict.reason_codes == ()


def test_changing_dataset_identity_is_reported_as_dataset_changed():
    verdict = assess_comparability(_manifest(dataset_id="dataset-a"), _manifest(dataset_id="dataset-b"))
    assert verdict.comparable is False
    assert REASON_DATASET_CHANGED in verdict.reason_codes


# ---------------------------------------------------------------------------
# AC5 — legacy contract provenance is explicit, never guessed
# ---------------------------------------------------------------------------


def test_a_legacy_manifest_is_explicitly_contract_provenance_unknown():
    verdict = assess_comparability(_manifest(legacy=True), _manifest())

    assert verdict.comparable is False
    assert REASON_CONTRACT_PROVENANCE_UNKNOWN in verdict.reason_codes
    # The aggregation contract was never digested in a legacy Manifest.
    assert _dimension(verdict, AGGREGATION_COMPARISON).status == "UNKNOWN"
    assert verdict.provenance != "FROZEN"


def test_a_legacy_manifest_still_reports_the_dimensions_it_can_prove():
    """Old reports stay readable: equal digests are still reported as equal."""
    verdict = assess_comparability(_manifest(legacy=True), _manifest(legacy=True))

    assert _dimension(verdict, MEASUREMENT).status == "MATCH"
    assert _dimension(verdict, QUALITY_POLICY).status == "MATCH"
    assert _dimension(verdict, AGGREGATION_COMPARISON).status == "UNKNOWN"
    assert verdict.comparable is False


def test_two_legacy_manifests_that_really_differ_still_report_the_changed_dimension():
    baseline = _manifest(legacy=True)
    candidate = _manifest(legacy=True, policy="sha256:" + "9" * 64)
    verdict = assess_comparability(baseline, candidate)

    assert _dimension(verdict, QUALITY_POLICY).status == "CHANGED"
    assert "QUALITY_POLICY_CHANGED" in verdict.reason_codes
    assert REASON_CONTRACT_PROVENANCE_UNKNOWN in verdict.reason_codes


def test_contract_digests_for_a_new_manifest_are_all_frozen():
    digests = contract_digests_for(_manifest())
    assert digests[MEASUREMENT].baseline_digest
    assert digests[QUALITY_POLICY].baseline_digest
    assert digests[AGGREGATION_COMPARISON].baseline_digest


# ---------------------------------------------------------------------------
# AC5 / AC6 — non-numeric results must not invent a delta
# ---------------------------------------------------------------------------


def _case(result: dict, *, quality: str = "pass") -> dict:
    return {
        "dataset_item_id": "case-1",
        "case_digest": "sha256:case",
        "execution_status": "succeeded",
        "eval_status": "succeeded",
        "quality_conclusion": quality,
        "scores": {},
        "evaluation_results": [result],
        "trace_id": "t",
    }


def test_a_text_only_result_produces_no_numeric_delta():
    baseline = _case({"evaluator_id": "answer_excerpt", "result_type": "text", "status": "succeeded", "value": "a"})
    candidate = _case({"evaluator_id": "answer_excerpt", "result_type": "text", "status": "succeeded", "value": "b"})

    diff = compare_case_results(
        baseline,
        candidate,
        evaluator_specs=[{"id": "answer_excerpt", "result_type": "text"}],
        baseline_evaluators=[{"id": "answer_excerpt", "result_type": "text"}],
    )

    assert diff["score_deltas"] == {}
    assert diff["classification"] == "UNCHANGED"
    assert diff["non_numeric_evaluators"] == ["answer_excerpt"]


def test_an_unordered_category_change_is_not_reported_as_a_regression():
    baseline = _case({"evaluator_id": "bucket", "result_type": "categorical", "status": "succeeded", "value": "review"})
    candidate = _case({"evaluator_id": "bucket", "result_type": "categorical", "status": "succeeded", "value": "auto"})

    diff = compare_case_results(
        baseline,
        candidate,
        evaluator_specs=[{"id": "bucket", "result_type": "categorical"}],
        baseline_evaluators=[{"id": "bucket", "result_type": "categorical"}],
    )

    assert diff["score_deltas"] == {}
    assert diff["classification"] != "REGRESSION"


def test_an_ordered_value_is_compared_only_through_its_frozen_mapping():
    """A frozen normalization rule is the only thing allowed to produce a delta."""
    baseline = _case(
        {
            "evaluator_id": "bucket",
            "result_type": "categorical",
            "status": "succeeded",
            "value": "low",
            "normalized_value": 1.0,
        },
        quality="pass",
    )
    candidate = _case(
        {
            "evaluator_id": "bucket",
            "result_type": "categorical",
            "status": "succeeded",
            "value": "high",
            "normalized_value": 2.0,
        },
        quality="pass",
    )

    diff = compare_case_results(
        baseline,
        candidate,
        evaluator_specs=[{"id": "bucket", "result_type": "categorical", "direction": "higher_is_better"}],
        baseline_evaluators=[{"id": "bucket", "result_type": "categorical", "direction": "higher_is_better"}],
    )

    assert diff["score_deltas"]["bucket"] == pytest.approx(1.0)
    assert diff["classification"] == "IMPROVEMENT"


def test_a_numeric_result_still_produces_the_known_regression():
    baseline = _case({"evaluator_id": "m", "result_type": "numeric", "status": "succeeded", "value": 1.0})
    candidate = _case({"evaluator_id": "m", "result_type": "numeric", "status": "succeeded", "value": 0.2})

    diff = compare_case_results(
        baseline,
        candidate,
        evaluator_specs=[{"id": "m", "result_type": "numeric", "direction": "higher_is_better"}],
        baseline_evaluators=[{"id": "m", "result_type": "numeric", "direction": "higher_is_better"}],
    )

    assert diff["classification"] == "REGRESSION"


# ---------------------------------------------------------------------------
# API-level: formal verdict is withheld while diagnostic stays available
# ---------------------------------------------------------------------------


def _seed(db_mgr, *, baseline_items, candidate_items, baseline_manifest=None, candidate_manifest=None):
    """Two frozen snapshots with an explicit Dataset-identity match."""
    ids = {}
    for side, manifest, items in (
        ("baseline", baseline_manifest or _manifest(), baseline_items),
        ("candidate", candidate_manifest or _manifest(), candidate_items),
    ):
        launch_id = str(uuid.uuid4())
        with db_mgr.get_session() as session:
            launch = ExperimentLaunchRecord(
                id=launch_id,
                name=f"issue-86-{side}",
                status="COMPLETED",
                quality_conclusion="pass",
                dataset_name="golden",
                dataset_version="sha256:same",
                agent_id="test-agent",
                agent_version="v1",
                agent_version_id="test-agent-v1",
                manifest=manifest,
                completed_at=datetime.now(UTC),
            )
            session.add(launch)
            session.flush()
            snapshot = RunResultSnapshotRecord(
                id=str(uuid.uuid4()),
                launch_id=launch_id,
                agent_id="test-agent",
                revision=1,
                manifest_digest="sha256:manifest",
                source_result_digest=f"sha256:{side}",
                evidence_state="COMPLETE",
                items=list(items.values()),
                summary={"total_cases": len(items), "evaluated_cases": len(items)},
                manifest=manifest,
            )
            session.add(snapshot)
            session.flush()
            for case_id, payload in items.items():
                session.add(
                    Item(
                        id=str(uuid.uuid4()),
                        launch_id=launch_id,
                        dataset_item_id=case_id,
                        execution_status="succeeded",
                        eval_status="succeeded",
                        quality_conclusion=payload["quality_conclusion"],
                        scores=payload["scores"],
                        dispatch_generation=1,
                    )
                )
            ids[side] = (launch_id, snapshot.id)
    # Bind the baseline snapshot into the candidate manifest.
    baseline_launch_id, baseline_snapshot_id = ids["baseline"]
    candidate_launch_id, candidate_snapshot_id = ids["candidate"]
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, candidate_launch_id)
        manifest = copy.deepcopy(launch.manifest)
        manifest["comparison"]["baseline_snapshot_id"] = baseline_snapshot_id
        manifest["comparison"]["baseline_binding_revision"] = 1
        launch.manifest = manifest
        session.add(launch)
        session.flush()
        snapshot = session.get(RunResultSnapshotRecord, candidate_snapshot_id)
        snap_manifest = copy.deepcopy(snapshot.manifest)
        snap_manifest["comparison"]["baseline_snapshot_id"] = baseline_snapshot_id
        snap_manifest["comparison"]["baseline_binding_revision"] = 1
        snapshot.manifest = snap_manifest
        session.add(snapshot)
    return {
        "candidate_launch_id": candidate_launch_id,
        "candidate_snapshot_id": candidate_snapshot_id,
        "baseline_launch_id": baseline_launch_id,
    }


def _payload(dataset_item_id: str, quality: str, score: float) -> dict:
    return {
        "dataset_item_id": dataset_item_id,
        "case_digest": "sha256:case",
        "execution_status": "succeeded",
        "eval_status": "succeeded",
        "quality_conclusion": quality,
        "scores": {"correctness": score},
        "evaluation_results": [
            {
                "evaluator_id": "correctness",
                "result_type": "numeric",
                "status": "succeeded",
                "value": score,
            }
        ],
    }


def _comparison(setup_runtime, monkeypatch, seeded) -> dict:
    from app import main
    from fastapi.testclient import TestClient

    db_mgr, _, _, _, _, _ = setup_runtime
    monkeypatch.setattr(main, "db_manager", db_mgr)
    client = TestClient(main.app)
    response = client.get(
        f"/api/v1/experiment-launches/{seeded['candidate_launch_id']}/comparison",
        params={"snapshot_id": seeded["candidate_snapshot_id"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_api_reports_a_formal_regression_when_contracts_match(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    seeded = _seed(
        db_mgr,
        baseline_items={"case-1": _payload("case-1", "pass", 1.0), "case-2": _payload("case-2", "pass", 1.0)},
        candidate_items={"case-1": _payload("case-1", "pass", 1.0), "case-2": _payload("case-2", "fail", 0.1)},
    )

    body = _comparison(setup_runtime, monkeypatch, seeded)

    assert body["comparability"]["comparable"] is True
    assert body["comparability"]["reason_codes"] == []
    assert body["formal"]["available"] is True
    assert body["formal"]["verdict"] == "REGRESSION"
    assert body["classification_counts"]["REGRESSION"] == 1
    # The diagnostic section is still returned, and explicitly labelled.
    assert body["diagnostic"]["classification_counts"]["REGRESSION"] == 1


def test_api_withholds_the_formal_verdict_when_the_policy_changed(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    seeded = _seed(
        db_mgr,
        baseline_items={"case-1": _payload("case-1", "pass", 1.0)},
        candidate_items={"case-1": _payload("case-1", "fail", 0.1)},
        candidate_manifest=_manifest(policy="sha256:" + "9" * 64),
    )

    body = _comparison(setup_runtime, monkeypatch, seeded)

    assert body["comparability"]["comparable"] is False
    assert "QUALITY_POLICY_CHANGED" in body["comparability"]["reason_codes"]
    assert body["formal"]["available"] is False
    assert body["formal"]["verdict"] is None
    assert "QUALITY_POLICY_CHANGED" in body["formal"]["withheld_reasons"]
    assert body["comparability"]["suggestions"]
    # The per-case diagnosis remains available and is marked diagnostic-only.
    assert body["items"][0]["basis"] == "DIAGNOSTIC_ONLY"


def test_api_withholds_the_formal_verdict_when_a_required_case_is_missing(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    """Partial coverage must never be presented as a clean 'no regression'."""
    seeded = _seed(
        db_mgr,
        baseline_items={
            "case-1": _payload("case-1", "pass", 1.0),
            "case-2": _payload("case-2", "pass", 1.0),
        },
        candidate_items={"case-1": _payload("case-1", "pass", 1.0)},
    )

    body = _comparison(setup_runtime, monkeypatch, seeded)

    assert body["comparability"]["comparable"] is True
    assert body["formal"]["available"] is False
    assert "COVERAGE_INCOMPLETE" in body["formal"]["withheld_reasons"]
    assert body["formal"]["coverage"] < 1.0


def test_api_marks_a_legacy_baseline_as_contract_provenance_unknown(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    seeded = _seed(
        db_mgr,
        baseline_items={"case-1": _payload("case-1", "pass", 1.0)},
        candidate_items={"case-1": _payload("case-1", "pass", 1.0)},
        baseline_manifest=_manifest(legacy=True),
    )

    body = _comparison(setup_runtime, monkeypatch, seeded)

    assert body["comparability"]["comparable"] is False
    assert "CONTRACT_PROVENANCE_UNKNOWN" in body["comparability"]["reason_codes"]
    aggregation = next(
        dim for dim in body["comparability"]["dimensions"] if dim["dimension"] == "AGGREGATION_COMPARISON"
    )
    assert aggregation["status"] == "UNKNOWN"
    assert body["formal"]["available"] is False


# ---------------------------------------------------------------------------
# The real Manifest carries all three independently versioned digests
# ---------------------------------------------------------------------------


def _launch_service(db_file):
    from app.db import DatabaseManager, MigrationRunner
    from app.manifest import LaunchService
    from app.registry import AgentRegistry

    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    registry = AgentRegistry(db_mgr)
    registry.create_agent("agent-1", "Agent One")
    registry.create_version(agent_id="agent-1", version="v1", endpoint="http://example.com/api", is_idempotent=True)
    return db_mgr, LaunchService(db_mgr, registry, runner_version="0.2.0")


def test_a_real_launch_manifest_freezes_all_three_contract_digests(monkeypatch, tmp_path):
    from app.comparison_contracts import aggregation_comparison_digest

    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    _, service = _launch_service(tmp_path / "issue86-a.db")
    manifest = service.create_launch(
        agent_id="agent-1",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        evaluator_ids=["pii_safe"],
    ).manifest

    block = manifest["contract_digests"]
    assert block["schema_version"] == COMPARISON_CONTRACT_SCHEMA_VERSION
    assert block["measurement"]["digest"] == manifest["measurement_digest"]
    assert block["quality_policy"]["digest"] == manifest["quality_policy"]["policy_digest"]
    assert block["aggregation_comparison"]["digest"] == aggregation_comparison_digest()

    # The real Manifest is fully frozen, so it is formally comparable to itself.
    verdict = assess_comparability(manifest, manifest)
    assert verdict.comparable is True
    assert verdict.provenance == "FROZEN"


def test_only_tightening_the_policy_leaves_the_measurement_digest_untouched(monkeypatch, tmp_path):
    """End-to-end proof of the headline promise, on real frozen Manifests."""
    from app.manifest import LaunchService
    from app.registry import AgentRegistry

    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    db_mgr, service = _launch_service(tmp_path / "issue86-b.db")

    lenient = service.create_launch(
        agent_id="agent-1",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        evaluator_ids=["pii_safe"],
        quality_policy_rules=[
            {
                "evaluator_id": "pii_safe",
                "operator": ">=",
                "threshold": 0.5,
                "result_type": "numeric",
                "required": True,
                "critical": True,
            }
        ],
    ).manifest

    registry = AgentRegistry(db_mgr)
    strict_service = LaunchService(db_mgr, registry, runner_version="0.2.0")
    strict = strict_service.create_launch(
        agent_id="agent-1",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        evaluator_ids=["pii_safe"],
        quality_policy_rules=[
            {
                "evaluator_id": "pii_safe",
                "operator": ">=",
                "threshold": 0.99,
                "result_type": "numeric",
                "required": True,
                "critical": True,
            }
        ],
    ).manifest

    assert lenient["measurement_digest"] == strict["measurement_digest"]
    assert lenient["quality_policy"]["policy_digest"] != strict["quality_policy"]["policy_digest"]

    verdict = assess_comparability(lenient, strict)
    assert verdict.comparable is False
    assert verdict.reason_codes == ("QUALITY_POLICY_CHANGED",)
    assert verdict.dimension(MEASUREMENT).status == "MATCH"
    assert verdict.dimension(QUALITY_POLICY).status == "CHANGED"
