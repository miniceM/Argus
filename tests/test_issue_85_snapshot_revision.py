"""Issue #85 — fixed result reports and Baseline revisions.

The product promise under test: *the result a user shares or selects as a
Baseline is one fixed revision*. A later re-evaluation produces a new revision
and never rewrites the old one, and Baseline eligibility is a property of the
snapshot's own evidence rather than of whatever the Launch happens to be doing
now.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "services" / "eval-runner") not in sys.path:
    sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.baselines import (  # noqa: E402
    BaselineConflictError,
    get_baseline,
    set_baseline,
    validate_baseline_snapshot,
)
from app.db_models import (  # noqa: E402
    EvaluationResultRecord,
    ExperimentLaunchRecord,
    RunResultSnapshotRecord,
)
from app.db_models import (  # noqa: E402
    ExperimentItemExecutionRecord as Item,
)
from app.evaluation_recovery import EVALUATION_RUNNING  # noqa: E402
from app.result_snapshots import (  # noqa: E402
    EVIDENCE_COMPLETE,
    EVIDENCE_DIAGNOSTIC,
    create_result_snapshot,
    evaluate_snapshot_evidence,
    latest_result_snapshot,
    list_result_snapshots,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manifest(case_ids: list[str]) -> dict:
    return {
        "schema_version": "1.2",
        "dataset": {
            "dataset_name": "golden",
            "dataset_version": "v1",
            "snapshot_digest": "dataset-digest",
            "items": [
                {"id": case, "input": {"q": case}, "expected_output": {"intent": "refund"}, "metadata": {}}
                for case in case_ids
            ],
        },
        "agent": {"agent_id": "test-agent", "version": "v1", "spec_digest": "agent-digest"},
        "evaluators": [{"id": "intent_match", "version": "1.0.0", "threshold": 0.8, "critical": True}],
        "quality_policy": {
            "policy_id": "custom",
            "version": "1.0",
            "schema_version": "1.0",
            "unknown_handling": "unknown_not_releasable",
            "policy_digest": "sha256:" + "a" * 64,
            "rules": [
                {
                    "evaluator_id": "intent_match",
                    "operator": ">=",
                    "threshold": 0.8,
                    "result_type": "numeric",
                    "required": True,
                    "critical": True,
                }
            ],
        },
        "measurement_digest": "sha256:" + "b" * 64,
        "runner": {"runner_version": "0.2.0", "build_id": "test"},
        "comparison": {"environment": "production", "comparison_policy_version": "comparison-v1"},
    }


def _create_launch(db_mgr, case_ids: list[str] | None = None, *, status: str = "COMPLETED") -> str:
    case_ids = case_ids or ["case-1", "case-2"]
    launch_id = str(uuid.uuid4())
    with db_mgr.get_session() as session:
        launch = ExperimentLaunchRecord(
            id=launch_id,
            name="issue-85",
            status=status,
            quality_conclusion="pass",
            dataset_name="golden",
            dataset_version="v1",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            manifest=_manifest(case_ids),
            completed_at=datetime.now(UTC),
        )
        session.add(launch)
        session.flush()
        for case_id in case_ids:
            session.add(
                Item(
                    id=str(uuid.uuid4()),
                    launch_id=launch_id,
                    dataset_item_id=case_id,
                    execution_status="succeeded",
                    eval_status="succeeded",
                    quality_conclusion="pass",
                    scores={"intent_match": 1.0},
                    dispatch_generation=1,
                )
            )
        session.commit()
    return launch_id


def _add_typed_result(db_mgr, launch_id: str, case_id: str, *, binding_id: str = "bind-1", value=1.0) -> None:
    """Attach a typed (Issue #82) result so provenance participates in the digest."""
    with db_mgr.get_session() as session:
        item = session.scalar(
            __import__("sqlalchemy").select(Item).where(
                Item.launch_id == launch_id, Item.dataset_item_id == case_id
            )
        )
        session.add(
            EvaluationResultRecord(
                id=str(uuid.uuid4()),
                item_execution_id=item.id,
                launch_id=launch_id,
                evaluator_id="intent_match",
                evaluator_version="1.0.0",
                result_type="numeric",
                status="succeeded",
                value=value,
                binding_id=binding_id,
                definition_digest="sha256:def",
                executor_type="builtin_python",
                manifest_schema_version="1.2",
                contract_status="FROZEN_VERIFIED",
            )
        )
        session.commit()


def _case(db_mgr, launch_id: str, case_id: str) -> Item:
    with db_mgr.get_session() as session:
        return session.scalar(
            __import__("sqlalchemy").select(Item).where(
                Item.launch_id == launch_id, Item.dataset_item_id == case_id
            )
        )


def _freeze(db_mgr, launch_id: str):
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        snapshot = create_result_snapshot(session, launch)
        session.commit()
        return snapshot


def _api_client(monkeypatch, db_mgr):
    """Bind the FastAPI app to the fixture database (see Issue #84 helper)."""
    from app import main
    from app.limiter import MemoryAgentLimiter
    from app.orchestrator import LaunchOrchestrator
    from app.queue import MemoryQueueAdapter
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "db_manager", db_mgr)
    monkeypatch.setattr(
        main, "orchestrator", LaunchOrchestrator(db_mgr, MemoryQueueAdapter(), MemoryAgentLimiter())
    )
    return TestClient(main.app)


# ---------------------------------------------------------------------------
# Unit: the evidence verdict itself
# ---------------------------------------------------------------------------


def test_evidence_is_complete_only_when_every_case_is_decided():
    state, reasons = evaluate_snapshot_evidence(
        [
            {"execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "pass"},
            {"execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "fail"},
        ]
    )
    assert state == EVIDENCE_COMPLETE
    assert reasons == []

    # Evaluation itself failed -> its own reason, not an UNKNOWN reason.
    state, reasons = evaluate_snapshot_evidence(
        [{"execution_status": "succeeded", "eval_status": "failed", "quality_conclusion": "unknown"}]
    )
    assert state == EVIDENCE_DIAGNOSTIC
    assert any("评测失败" in reason for reason in reasons)

    # Evaluation succeeded but the policy could not conclude -> UNKNOWN evidence.
    state, reasons = evaluate_snapshot_evidence(
        [{"execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "unknown"}]
    )
    assert state == EVIDENCE_DIAGNOSTIC
    assert any("UNKNOWN" in reason for reason in reasons)


def test_an_empty_snapshot_is_never_complete():
    state, reasons = evaluate_snapshot_evidence([])
    assert state == EVIDENCE_DIAGNOSTIC
    assert reasons


# ---------------------------------------------------------------------------
# AC3: a running evaluation must not freeze a "complete" report
# ---------------------------------------------------------------------------


def test_no_snapshot_while_an_evaluation_is_being_rejudged(setup_runtime):
    """Issue #84 keeps the Launch terminal during a re-judgement.

    Without the Issue #85 gate this would freeze a report claiming complete
    evidence while the evaluation is still running.
    """
    db_mgr, *_ = setup_runtime
    launch_id = _create_launch(db_mgr)
    with db_mgr.get_session() as session:
        item = session.scalar(
            __import__("sqlalchemy").select(Item).where(
                Item.launch_id == launch_id, Item.dataset_item_id == "case-1"
            )
        )
        # The Launch is terminal, but the evaluation lifecycle is still running.
        item.evaluation_status = EVALUATION_RUNNING
        session.commit()

    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        assert launch.status == "COMPLETED"
        assert create_result_snapshot(session, launch) is None
    with db_mgr.get_session() as session:
        assert list_result_snapshots(session, launch_id) == []


def test_terminal_failure_still_freezes_a_diagnostic_snapshot_with_reasons(setup_runtime):
    db_mgr, *_ = setup_runtime
    launch_id = _create_launch(db_mgr, status="PARTIAL_FAILED")
    with db_mgr.get_session() as session:
        item = session.scalar(
            __import__("sqlalchemy").select(Item).where(
                Item.launch_id == launch_id, Item.dataset_item_id == "case-2"
            )
        )
        item.execution_status = "failed"
        item.eval_status = "failed"
        item.quality_conclusion = "unknown"
        session.commit()

    snapshot = _freeze(db_mgr, launch_id)
    assert snapshot is not None
    assert snapshot.evidence_state == EVIDENCE_DIAGNOSTIC
    assert snapshot.evidence_reasons
    assert any("执行失败" in reason for reason in snapshot.evidence_reasons)


# ---------------------------------------------------------------------------
# AC1 / AC2: revisions are immutable, idempotent and provenance-sensitive
# ---------------------------------------------------------------------------


def test_reevaluation_creates_a_new_revision_and_never_rewrites_the_old_one(setup_runtime):
    db_mgr, *_ = setup_runtime
    launch_id = _create_launch(db_mgr)
    for case_id in ("case-1", "case-2"):
        _add_typed_result(db_mgr, launch_id, case_id)

    first = _freeze(db_mgr, launch_id)
    assert first.revision == 1
    assert first.evidence_state == EVIDENCE_COMPLETE

    # Re-freezing identical results is idempotent: no duplicate revision.
    assert _freeze(db_mgr, launch_id).id == first.id
    with db_mgr.get_session() as session:
        assert len(list_result_snapshots(session, launch_id)) == 1

    original_items = list(first.items)
    original_digest = first.source_result_digest

    # A re-evaluation changes the typed measurement for one case.
    with db_mgr.get_session() as session:
        item = session.scalar(
            __import__("sqlalchemy").select(Item).where(
                Item.launch_id == launch_id, Item.dataset_item_id == "case-2"
            )
        )
        item.scores = {"intent_match": 0.42}
        item.quality_conclusion = "fail"
        session.commit()
        session.query(EvaluationResultRecord).filter(
            EvaluationResultRecord.item_execution_id == item.id,
            EvaluationResultRecord.evaluator_id == "intent_match",
        ).update({"value": 0.42})
        session.commit()

    second = _freeze(db_mgr, launch_id)
    assert second.revision == 2
    assert second.id != first.id
    assert second.source_result_digest != original_digest

    # The old revision is byte-for-byte unchanged.
    with db_mgr.get_session() as session:
        old = session.get(RunResultSnapshotRecord, first.id)
        assert old.revision == 1
        assert old.source_result_digest == original_digest
        assert old.items == original_items
        assert old.summary["pass_rate"] == 1.0
    with db_mgr.get_session() as session:
        assert latest_result_snapshot(session, launch_id).id == second.id


def test_a_changed_binding_provenance_alone_creates_a_new_revision(setup_runtime):
    """Issue #85: the source digest covers provenance, not just numeric scores."""
    db_mgr, *_ = setup_runtime
    launch_id = _create_launch(db_mgr, ["case-1"])
    _add_typed_result(db_mgr, launch_id, "case-1", binding_id="bind-1")
    first = _freeze(db_mgr, launch_id)

    # Same measurement value, different frozen binding identity. The result
    # row is replaced (one row per item+evaluator), so only the provenance
    # differs between the two revisions.
    with db_mgr.get_session() as session:
        item = session.scalar(
            __import__("sqlalchemy").select(Item).where(
                Item.launch_id == launch_id, Item.dataset_item_id == "case-1"
            )
        )
        session.query(EvaluationResultRecord).filter(
            EvaluationResultRecord.item_execution_id == item.id
        ).delete()
        session.commit()
    _add_typed_result(db_mgr, launch_id, "case-1", binding_id="bind-2")

    second = _freeze(db_mgr, launch_id)
    assert second.revision == 2
    assert second.source_result_digest != first.source_result_digest


# ---------------------------------------------------------------------------
# AC1: a shared link is pinned to its revision
# ---------------------------------------------------------------------------


def test_shared_snapshot_link_returns_the_same_revision_after_a_new_one_exists(setup_runtime, monkeypatch):
    db_mgr, *_ = setup_runtime
    launch_id = _create_launch(db_mgr)
    _add_typed_result(db_mgr, launch_id, "case-1")
    _add_typed_result(db_mgr, launch_id, "case-2")
    first = _freeze(db_mgr, launch_id)

    client = _api_client(monkeypatch, db_mgr)

    # The shareable link: /result-snapshots/{snapshot_id} never means "latest".
    pinned = client.get(f"/api/v1/experiment-launches/{launch_id}/result-snapshots/{first.id}")
    assert pinned.status_code == 200, pinned.text
    pinned_body = pinned.json()
    assert pinned_body["revision"] == 1
    assert pinned_body["source_result_digest"] == first.source_result_digest
    assert pinned_body["evidence_state"] == EVIDENCE_COMPLETE
    assert pinned_body["releasable"] is True
    assert pinned_body["summary"]["pass_rate"] == 1.0
    # Typed results round-trip with their provenance.
    typed = pinned_body["items"][0]["evaluation_results"]
    assert typed and typed[0]["provenance"]["binding_id"] == "bind-1"

    # Now a re-evaluation freezes revision 2.
    with db_mgr.get_session() as session:
        item = session.scalar(
            __import__("sqlalchemy").select(Item).where(
                Item.launch_id == launch_id, Item.dataset_item_id == "case-2"
            )
        )
        item.quality_conclusion = "fail"
        item.scores = {"intent_match": 0.1}
        session.commit()
    second = _freeze(db_mgr, launch_id)
    assert second.revision == 2

    # The old link is unchanged: same revision, same digest, same items.
    again = client.get(f"/api/v1/experiment-launches/{launch_id}/result-snapshots/{first.id}")
    assert again.status_code == 200
    assert again.json() == pinned_body

    # The history lists both, newest first, and marks the latest.
    history = client.get(f"/api/v1/experiment-launches/{launch_id}/result-snapshots")
    assert history.status_code == 200, history.text
    body = history.json()
    assert body["latest_snapshot_id"] == second.id
    assert [row["revision"] for row in body["revisions"]] == [2, 1]
    assert body["revisions"][0]["is_latest"] is True
    assert body["revisions"][1]["is_latest"] is False


def test_snapshot_endpoint_rejects_an_id_from_another_launch(setup_runtime, monkeypatch):
    db_mgr, *_ = setup_runtime
    launch_a = _create_launch(db_mgr)
    launch_b = _create_launch(db_mgr)
    snapshot_a = _freeze(db_mgr, launch_a)
    client = _api_client(monkeypatch, db_mgr)

    # Must not reveal that the id exists under another Launch.
    response = client.get(f"/api/v1/experiment-launches/{launch_b}/result-snapshots/{snapshot_a.id}")
    assert response.status_code == 404, response.text


def test_run_summary_reports_the_pinned_revision_evidence(setup_runtime, monkeypatch):
    db_mgr, *_ = setup_runtime
    launch_id = _create_launch(db_mgr)
    first = _freeze(db_mgr, launch_id)
    client = _api_client(monkeypatch, db_mgr)

    response = client.get(
        f"/api/v1/experiment-launches/{launch_id}/summary",
        params={"snapshot_id": first.id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 1
    assert body["source_result_digest"] == first.source_result_digest
    assert body["evidence_state"] == EVIDENCE_COMPLETE
    assert body["evidence_reasons"] == []


# ---------------------------------------------------------------------------
# AC4 / AC6: Baseline eligibility comes from the snapshot, not the Launch
# ---------------------------------------------------------------------------


def test_baseline_stays_valid_when_the_original_launch_starts_recovering(setup_runtime):
    db_mgr, *_ = setup_runtime
    launch_id = _create_launch(db_mgr)
    first = _freeze(db_mgr, launch_id)
    binding = set_baseline(
        db_mgr,
        agent_id="test-agent",
        environment="production",
        result_snapshot_id=first.id,
        expected_revision=0,
    )
    assert binding.revision == 1

    # The original Launch re-enters an active state: a retry/resume bumps the
    # execution generation and the Launch stops being COMPLETED. Issue #85
    # forbids that from retroactively changing a captured Baseline.
    with db_mgr.get_session() as session:
        launch_row = session.get(ExperimentLaunchRecord, launch_id)
        launch_row.status = "RUNNING"
        launch_row.completed_at = None
        item = session.scalar(
            __import__("sqlalchemy").select(Item).where(
                Item.launch_id == launch_id, Item.dataset_item_id == "case-1"
            )
        )
        item.evaluation_status = EVALUATION_RUNNING
        item.dispatch_generation = 2
        session.commit()

    stored, snapshot = get_baseline(db_mgr, "test-agent", "production")
    assert stored.result_snapshot_id == first.id
    assert snapshot.revision == 1
    # The Launch's activity does not retroactively revoke eligibility.
    with db_mgr.get_session() as session:
        validate_baseline_snapshot(
            snapshot, session.get(ExperimentLaunchRecord, launch_id), "test-agent"
        )


def test_diagnostic_snapshot_is_refused_as_a_formal_baseline(setup_runtime):
    db_mgr, *_ = setup_runtime
    launch_id = _create_launch(db_mgr, status="PARTIAL_FAILED")
    with db_mgr.get_session() as session:
        item = session.scalar(
            __import__("sqlalchemy").select(Item).where(
                Item.launch_id == launch_id, Item.dataset_item_id == "case-2"
            )
        )
        item.quality_conclusion = "unknown"
        session.commit()
    snapshot = _freeze(db_mgr, launch_id)
    assert snapshot.evidence_state == EVIDENCE_DIAGNOSTIC

    with pytest.raises(ValueError) as excinfo:
        set_baseline(
            db_mgr,
            agent_id="test-agent",
            environment="production",
            result_snapshot_id=snapshot.id,
            expected_revision=0,
        )
    assert "DIAGNOSTIC" in str(excinfo.value)


def test_unknown_quality_snapshot_is_refused_even_when_marked_complete(setup_runtime):
    """Defense in depth: the summary counts also veto UNKNOWN evidence."""
    db_mgr, *_ = setup_runtime
    launch_id = _create_launch(db_mgr)
    snapshot = _freeze(db_mgr, launch_id)
    # Simulate a snapshot whose summary reports UNKNOWN evidence.
    with db_mgr.get_session() as session:
        row = session.get(RunResultSnapshotRecord, snapshot.id)
        summary = dict(row.summary)
        summary["quality_unknown_count"] = 1
        row.summary = summary
        session.commit()

    with pytest.raises(ValueError) as excinfo:
        set_baseline(
            db_mgr,
            agent_id="test-agent",
            environment="production",
            result_snapshot_id=snapshot.id,
            expected_revision=0,
        )
    assert "UNKNOWN" in str(excinfo.value)


def test_baseline_from_a_different_agent_is_refused(setup_runtime):
    from app.db_models import AgentRecord

    db_mgr, *_ = setup_runtime
    launch_id = _create_launch(db_mgr)
    snapshot = _freeze(db_mgr, launch_id)
    with db_mgr.get_session() as session:
        session.add(AgentRecord(id="other-agent", name="Other Agent"))
        session.commit()

    with pytest.raises(ValueError) as excinfo:
        set_baseline(
            db_mgr,
            agent_id="other-agent",
            environment="production",
            result_snapshot_id=snapshot.id,
            expected_revision=0,
        )
    assert "different Agent" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC6: concurrent Baseline writes conflict instead of silently overwriting
# ---------------------------------------------------------------------------


def test_concurrent_baseline_writes_conflict_on_expected_revision(setup_runtime):
    db_mgr, *_ = setup_runtime
    launch_a = _create_launch(db_mgr)
    launch_b = _create_launch(db_mgr)
    snap_a = _freeze(db_mgr, launch_a)
    snap_b = _freeze(db_mgr, launch_b)

    set_baseline(
        db_mgr,
        agent_id="test-agent",
        environment="production",
        result_snapshot_id=snap_a.id,
        expected_revision=0,
    )

    # A second writer still believes the binding is at revision 0.
    with pytest.raises(BaselineConflictError):
        set_baseline(
            db_mgr,
            agent_id="test-agent",
            environment="production",
            result_snapshot_id=snap_b.id,
            expected_revision=0,
        )

    stored, _snapshot = get_baseline(db_mgr, "test-agent", "production")
    # The loser did not silently overwrite the winner.
    assert stored.result_snapshot_id == snap_a.id
    assert stored.revision == 1


# ---------------------------------------------------------------------------
# AC5: a Candidate keeps the Baseline revision it was created with
# ---------------------------------------------------------------------------


def test_switching_baseline_does_not_move_an_existing_candidate(setup_runtime, monkeypatch):
    from app.manifest import LaunchService
    from app.registry import AgentRegistry

    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    db_mgr, *_ = setup_runtime
    launch_a = _create_launch(db_mgr)
    launch_b = _create_launch(db_mgr)
    snap_a = _freeze(db_mgr, launch_a)
    snap_b = _freeze(db_mgr, launch_b)

    set_baseline(
        db_mgr,
        agent_id="test-agent",
        environment="production",
        result_snapshot_id=snap_a.id,
        expected_revision=0,
    )

    service = LaunchService(db_mgr, AgentRegistry(db_mgr), runner_version="0.2.0")
    # A candidate created now freezes revision 1 of the binding.
    candidate = service.create_launch(
        agent_id="test-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        environment="production",
        evaluator_ids=["intent_match"],
    )
    comparison = candidate.manifest.get("comparison", {})
    assert comparison["baseline_snapshot_id"] == snap_a.id
    assert comparison["baseline_binding_revision"] == 1

    # Explicitly switch the Baseline to the other revision.
    set_baseline(
        db_mgr,
        agent_id="test-agent",
        environment="production",
        result_snapshot_id=snap_b.id,
        expected_revision=1,
    )
    stored, _snapshot = get_baseline(db_mgr, "test-agent", "production")
    assert stored.result_snapshot_id == snap_b.id
    assert stored.revision == 2

    # The existing Candidate still compares against the revision it froze.
    with db_mgr.get_session() as session:
        frozen = session.get(ExperimentLaunchRecord, candidate.id).manifest["comparison"]
    assert frozen["baseline_snapshot_id"] == snap_a.id
    assert frozen["baseline_binding_revision"] == 1

    # A new Candidate picks up the new binding revision.
    newer = service.create_launch(
        agent_id="test-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        environment="production",
        evaluator_ids=["intent_match"],
    )
    assert newer.manifest["comparison"]["baseline_snapshot_id"] == snap_b.id
    assert newer.manifest["comparison"]["baseline_binding_revision"] == 2


# ---------------------------------------------------------------------------
# AC6 / API: the Baseline response names both revisions explicitly
# ---------------------------------------------------------------------------


def test_baseline_api_reports_result_revision_and_binding_revision(setup_runtime, monkeypatch):
    db_mgr, *_ = setup_runtime
    launch_id = _create_launch(db_mgr)
    snapshot = _freeze(db_mgr, launch_id)
    client = _api_client(monkeypatch, db_mgr)

    response = client.post(
        "/api/v1/agents/test-agent/baselines",
        json={
            "environment": "production",
            "result_snapshot_id": snapshot.id,
            "expected_revision": 0,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # No ambiguous "latest": both the pointer and the frozen revision are named.
    assert body["result_snapshot_id"] == snapshot.id
    assert body["result_revision"] == 1
    assert body["revision"] == 1
    assert body["result_evidence_state"] == EVIDENCE_COMPLETE

    # A stale expected_revision is a clear conflict.
    conflict = client.post(
        "/api/v1/agents/test-agent/baselines",
        json={
            "environment": "production",
            "result_snapshot_id": snapshot.id,
            "expected_revision": 0,
        },
    )
    assert conflict.status_code == 409, conflict.text
