"""Issue #87 — Langfuse is a one-way projection of a frozen Argus result.

The promises under test:

* a typed Argus result is projected with its real type, and a type Langfuse
  cannot represent is reported as NOT_APPLICABLE with a reason -- never
  coerced to 0 and never claimed as synced,
* every projection carries its provenance (snapshot, revision, digests,
  attempt) and a stable idempotency key, so re-delivering the same logical
  result never creates a second score while a re-evaluation creates a new one,
* a Langfuse outage never changes the Argus snapshot, its digest, or the
  quality conclusion, and the sync state is reported per scope so a synced
  item/trace scope cannot be presented as "everything synced".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "services" / "eval-runner") not in sys.path:
    sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db_models import LangfuseSyncTaskRecord  # noqa: E402
from app.langfuse_projection import (  # noqa: E402
    NOT_APPLICABLE_TEXT_REASON,
    project_typed_result,
    projection_status_for,
    stable_score_id,
)
from app.langfuse_sync import (  # noqa: E402
    combine_scope_states,
    scope_state_from_tasks,
)

# ---------------------------------------------------------------------------
# AC4 — the typed mapping is explicit; text is never coerced to a number
# ---------------------------------------------------------------------------


def _result(**over):
    base = {
        "evaluator_id": "intent_match",
        "evaluator_version": "1.0.0",
        "result_type": "numeric",
        "status": "succeeded",
        "value": 0.9,
        "normalized_value": None,
    }
    base.update(over)
    return base


def test_a_numeric_result_is_projected_as_its_value():
    projected = project_typed_result(
        _result(), item_id="item-1", generation=1, snapshot_id="snap-1", revision=2
    )
    assert projected.applicable is True
    assert projected.value == pytest.approx(0.9)
    assert projected.not_applicable_reason is None


def test_a_boolean_result_is_projected_with_its_original_value_kept_as_evidence():
    projected = project_typed_result(
        _result(result_type="boolean", value=False, value_type=bool),
        item_id="item-1",
        generation=1,
        snapshot_id="snap-1",
        revision=1,
    )
    assert projected.applicable is True
    assert projected.value == pytest.approx(0.0)
    # The projection is honest: the raw boolean is recoverable, so nobody reads
    # a 0 as "measured zero" instead of "false".
    assert projected.evidence["original_value"] is False
    assert projected.evidence["result_type"] == "boolean"


def test_an_ordered_category_is_projected_through_its_frozen_mapping_only():
    projected = project_typed_result(
        _result(result_type="categorical", value="high", normalized_value=2.0),
        item_id="item-1",
        generation=1,
        snapshot_id="snap-1",
        revision=1,
    )
    assert projected.applicable is True
    assert projected.value == pytest.approx(2.0)
    assert projected.evidence["original_value"] == "high"


def test_an_unordered_category_is_not_applicable_and_never_becomes_zero():
    projected = project_typed_result(
        _result(result_type="categorical", value="review"),
        item_id="item-1",
        generation=1,
        snapshot_id="snap-1",
        revision=1,
    )
    assert projected.applicable is False
    assert projected.value is None
    assert projected.not_applicable_reason


def test_a_text_result_is_not_applicable_and_is_never_forced_to_a_number():
    projected = project_typed_result(
        _result(result_type="text", value="客户要求退款"),
        item_id="item-1",
        generation=1,
        snapshot_id="snap-1",
        revision=1,
    )
    assert projected.applicable is False
    assert projected.value is None
    assert projected.not_applicable_reason == NOT_APPLICABLE_TEXT_REASON
    # The text is still preserved as a reference so nothing is silently lost.
    assert projected.evidence["original_value"] == "客户要求退款"


def test_a_failed_typed_result_is_not_projected_as_a_zero():
    projected = project_typed_result(
        _result(status="failed", result_type="numeric", value=None),
        item_id="item-1",
        generation=1,
        snapshot_id="snap-1",
        revision=1,
    )
    assert projected.applicable is False
    assert projected.value is None
    assert projected.not_applicable_reason


# ---------------------------------------------------------------------------
# AC2 — stable idempotency keys cover the logical result and the eval revision
# ---------------------------------------------------------------------------


def test_the_same_result_and_revision_always_produce_the_same_score_id():
    first = stable_score_id(item_id="item-1", generation=3, evaluator_id="intent_match")
    second = stable_score_id(item_id="item-1", generation=3, evaluator_id="intent_match")
    assert first == second
    assert "item-1" in first and "intent_match" in first


def test_a_new_evaluation_revision_produces_a_new_score_id():
    before = stable_score_id(item_id="item-1", generation=3, evaluator_id="intent_match")
    after = stable_score_id(item_id="item-1", generation=4, evaluator_id="intent_match")
    assert before != after


def test_different_evaluators_never_share_a_score_id():
    a = stable_score_id(item_id="item-1", generation=1, evaluator_id="intent_match")
    b = stable_score_id(item_id="item-1", generation=1, evaluator_id="answer_present")
    assert a != b


def test_the_projection_carries_its_frozen_provenance():
    projected = project_typed_result(
        _result(provenance={"binding_id": "bind-1", "contract_status": "FROZEN_VERIFIED"}),
        item_id="item-1",
        generation=2,
        snapshot_id="snap-7",
        revision=3,
        policy_digest="sha256:policy",
        definition_digest="sha256:definition",
        attempt=2,
    )
    assert projected.evidence["snapshot_id"] == "snap-7"
    assert projected.evidence["revision"] == 3
    assert projected.evidence["policy_digest"] == "sha256:policy"
    assert projected.evidence["definition_digest"] == "sha256:definition"
    assert projected.evidence["attempt"] == 2
    assert projected.evidence["source"] == "ARGUS_FROZEN_SNAPSHOT"
    # The binding identity travels with the projection.
    assert projected.evidence["binding_id"] == "bind-1"


# ---------------------------------------------------------------------------
# AC5 — NOT_APPLICABLE must not leave the scope waiting forever
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("SYNCED", "SYNCED"),
        ("PENDING", "PENDING"),
        ("PROCESSING", "PENDING"),
        ("FAILED", "FAILED"),
        ("SKIPPED", "NOT_APPLICABLE"),
        ("NOT_APPLICABLE", "NOT_APPLICABLE"),
    ],
)
def test_each_task_status_maps_to_a_stable_user_facing_state(status, expected):
    assert projection_status_for(status) == expected


def test_a_retry_exhausted_scope_is_distinguishable_from_a_retrying_one():
    """A scope that ran out of attempts must not read as 'still working'."""
    assert projection_status_for("FAILED", attempts=5, max_attempts=5) == "RETRY_EXHAUSTED"
    assert projection_status_for("FAILED", attempts=1, max_attempts=5) == "FAILED"


def test_a_scope_with_no_applicable_projection_is_not_applicable_not_pending():
    """Text-only results converge to NOT_APPLICABLE instead of waiting forever."""
    assert projection_status_for("SKIPPED", has_applicable_projection=False) == "NOT_APPLICABLE"
    assert projection_status_for("SYNCED", has_applicable_projection=False) == "NOT_APPLICABLE"


# ---------------------------------------------------------------------------
# AC5 — the two sync scopes are reported independently
# ---------------------------------------------------------------------------


def _scope(tasks, max_attempts=5):
    return scope_state_from_tasks(
        [
            {
                "status": status,
                "attempts": attempts,
                "last_error": last_error,
            }
            for status, attempts, last_error in tasks
        ],
        max_attempts=max_attempts,
    )


def test_a_scope_with_no_tasks_converges_to_not_applicable():
    scope = _scope([])
    assert scope.status == "NOT_APPLICABLE"
    assert scope.reason


def test_all_synced_tasks_report_synced():
    assert _scope([("SYNCED", 1, None), ("SYNCED", 1, None)]).status == "SYNCED"


def test_a_pending_task_reports_pending():
    assert _scope([("SYNCED", 1, None), ("PENDING", 0, None)]).status == "PENDING"


def test_a_failed_scope_carries_the_remote_reason():
    scope = _scope([("SYNCED", 1, None), ("FAILED", 2, "Langfuse timeout")])
    assert scope.status == "FAILED"
    assert "Langfuse timeout" in (scope.reason or "")


def test_a_retry_exhausted_scope_is_not_reported_as_still_failing_temporarily():
    scope = _scope([("FAILED", 5, "Langfuse timeout")], max_attempts=5)
    assert scope.status == "RETRY_EXHAUSTED"


def test_skipped_tasks_converge_instead_of_waiting_forever():
    assert _scope([("SKIPPED", 0, None), ("SKIPPED", 0, None)]).status == "NOT_APPLICABLE"


# ---------------------------------------------------------------------------
# AC5 — a synced item scope can never be presented as "everything synced"
# ---------------------------------------------------------------------------


def test_item_synced_with_a_failed_run_score_is_never_reported_as_fully_synced():
    combined = combine_scope_states(
        item_trace=_scope([("SYNCED", 1, None)]),
        run_score=_scope([("FAILED", 3, "Run score publish failed")]),
    )
    assert combined.overall != "SYNCED"
    assert combined.overall == "FAILED"
    # The UI is told exactly which scope is behind.
    assert combined.item_trace.status == "SYNCED"
    assert combined.run_score.status == "FAILED"


def test_a_not_applicable_run_score_does_not_drag_a_synced_item_scope_down():
    combined = combine_scope_states(
        item_trace=_scope([("SYNCED", 1, None)]),
        run_score=_scope([]),
    )
    assert combined.overall == "SYNCED"


def test_both_scopes_synced_reports_fully_synced():
    combined = combine_scope_states(
        item_trace=_scope([("SYNCED", 1, None)]),
        run_score=_scope([("SYNCED", 1, None)]),
    )
    assert combined.overall == "SYNCED"


def test_the_worst_scope_wins_when_both_are_broken():
    combined = combine_scope_states(
        item_trace=_scope([("FAILED", 5, "down")], max_attempts=5),
        run_score=_scope([("FAILED", 1, "down")]),
    )
    assert combined.overall == "RETRY_EXHAUSTED"


# ---------------------------------------------------------------------------
# Integration: the sync path itself
# ---------------------------------------------------------------------------


def _outbox_launch(db_mgr, *, scores_payload, generation=1, status="PENDING", attempts=0):
    import uuid
    from datetime import UTC, datetime

    from app.db_models import ExperimentItemExecutionRecord as Item
    from app.db_models import ExperimentLaunchRecord, LangfuseSyncTaskRecord

    launch_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    manifest = {
        "schema_version": "1.2",
        "dataset": {"source": "langfuse", "dataset_id": "d1", "dataset_name": "golden", "dataset_version": "v1"},
        "agent": {"agent_id": "test-agent", "version": "v1", "spec_digest": "d"},
        "evaluators": [{"id": "intent_match", "version": "1.0.0", "threshold": 0.8}],
        "quality_policy": {"policy_id": "p", "version": "1.0", "policy_digest": "sha256:policy", "rules": []},
        "comparison": {"environment": "production", "baseline_snapshot_id": None, "baseline_binding_revision": None},
    }
    with db_mgr.get_session() as session:
        launch = ExperimentLaunchRecord(
            id=launch_id,
            name="issue-87",
            status="COMPLETED",
            quality_conclusion="pass",
            dataset_name="golden",
            dataset_version="v1",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            manifest=manifest,
            langfuse_experiment_url="https://langfuse.example/runs/x",
            completed_at=now,
        )
        session.add(launch)
        session.flush()
        session.add(
            Item(
                id=item_id,
                launch_id=launch_id,
                dataset_item_id="case-1",
                execution_status="succeeded",
                eval_status="succeeded",
                quality_conclusion="pass",
                scores={"intent_match": 1.0},
                trace_id="trace-1",
                dispatch_generation=generation,
            )
        )
        session.add(
            LangfuseSyncTaskRecord(
                id=task_id,
                launch_id=launch_id,
                item_id=item_id,
                dataset_item_id="case-1",
                dataset_version="v1",
                dispatch_generation=generation,
                task_type="FULL_EVAL_SYNC",
                trace_id="trace-1",
                observation_id="obs-1",
                dataset_run_name="test-run",
                scores_payload=scores_payload,
                status=status,
                attempts=attempts,
                next_retry_at=now,
            )
        )
        session.commit()
    return {"launch_id": launch_id, "item_id": item_id, "task_id": task_id}


def _syncer(db_mgr, mock_lf):
    from app.langfuse_sync import LangfuseOutboxSyncer

    return LangfuseOutboxSyncer(db_mgr, mock_lf, syncer_id="s1")


def _mock_lf():
    from unittest.mock import MagicMock

    lf = MagicMock()
    lf.api.dataset_run_items.create.return_value = MagicMock()
    lf.api.scores.create.return_value = MagicMock()
    return lf


# --- AC4: a text result is never coerced into a numeric score ---------------


def test_a_text_only_result_writes_no_score_and_still_completes(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    _outbox_launch(
        db_mgr,
        scores_payload={
            "_typed_results": [
                {"evaluator_id": "answer_excerpt", "result_type": "text", "status": "succeeded", "value": "退款"},
                {"evaluator_id": "intent_match", "result_type": "numeric", "status": "succeeded", "value": 1.0},
            ]
        },
    )
    lf = _mock_lf()

    processed = _syncer(db_mgr, lf).process_batch(batch_size=1)

    assert processed == 1
    names = [call.kwargs["name"] for call in lf.api.scores.create.call_args_list]
    # The text result produced no score at all; the numeric one still did.
    assert names == ["intent_match"]
    assert all(call.kwargs["value"] == 1.0 for call in lf.api.scores.create.call_args_list)


def test_a_failed_evaluation_is_not_projected_as_zero(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    _outbox_launch(
        db_mgr,
        scores_payload={
            "_typed_results": [
                {"evaluator_id": "intent_match", "result_type": "numeric", "status": "failed", "value": None},
            ]
        },
    )
    lf = _mock_lf()

    _syncer(db_mgr, lf).process_batch(batch_size=1)

    lf.api.scores.create.assert_not_called()


# --- AC2: recovery re-delivers the same logical score, never a duplicate -----


def test_redelivering_the_same_revision_reuses_the_same_score_id(setup_runtime):
    """The same outbox task, delivered again after a recovery, is the same score."""
    from datetime import UTC, datetime, timedelta

    db_mgr, _, _, _, _, _ = setup_runtime
    seeded = _outbox_launch(
        db_mgr,
        scores_payload={
            "_typed_results": [
                {"evaluator_id": "intent_match", "result_type": "numeric", "status": "succeeded", "value": 1.0},
            ]
        },
    )
    first = _mock_lf()
    _syncer(db_mgr, first).process_batch(batch_size=1)
    first_ids = [call.kwargs["id"] for call in first.api.scores.create.call_args_list]
    assert len(first_ids) == 1

    # Redelivery of the very same task (lease expiry, retry, or a second worker).
    with db_mgr.get_session() as session:
        task = session.get(LangfuseSyncTaskRecord, seeded["task_id"])
        task.status = "PENDING"
        task.attempts = 2
        task.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    second = _mock_lf()
    _syncer(db_mgr, second).process_batch(batch_size=1)
    second_ids = [call.kwargs["id"] for call in second.api.scores.create.call_args_list]

    assert second_ids == first_ids, "a redelivery must not create a second logical score"
    assert len(second_ids) == 1


def test_recovery_after_an_outage_projects_exactly_once(setup_runtime):
    """Langfuse down -> task fails; connection restored -> one stable score."""
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock

    db_mgr, _, _, _, _, _ = setup_runtime
    seeded = _outbox_launch(
        db_mgr,
        scores_payload={
            "_typed_results": [
                {"evaluator_id": "intent_match", "result_type": "numeric", "status": "succeeded", "value": 1.0},
            ]
        },
    )
    down = MagicMock()
    down.api.dataset_run_items.create.side_effect = TimeoutError("langfuse unreachable")
    down.api.scores.create.side_effect = TimeoutError("langfuse unreachable")
    assert _syncer(db_mgr, down).process_batch(batch_size=1) == 0

    # A timed-out worker deliberately abandons its write-back, so the task is
    # left claimed until its lease expires; that is what keeps a stalled worker
    # from double-submitting. Recovery happens on the next reclaim.
    with db_mgr.get_session() as session:
        task = session.get(LangfuseSyncTaskRecord, seeded["task_id"])
        assert task.status != "SYNCED"
        task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        task.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    recovered = _mock_lf()
    assert _syncer(db_mgr, recovered).process_batch(batch_size=1) == 1

    ids = [call.kwargs["id"] for call in recovered.api.scores.create.call_args_list]
    assert len(ids) == 1
    assert ids == [
        stable_score_id(item_id=seeded["item_id"], generation=1, evaluator_id="intent_match")
    ]


def test_a_new_evaluation_revision_projects_to_a_new_score_id(setup_runtime):
    """Re-evaluating keeps the old revision's projection and adds a new one."""
    db_mgr, _, _, _, _, _ = setup_runtime
    payload = {
        "_typed_results": [
            {"evaluator_id": "intent_match", "result_type": "numeric", "status": "succeeded", "value": 1.0},
        ]
    }
    _outbox_launch(db_mgr, scores_payload=payload, generation=1)
    _outbox_launch(db_mgr, scores_payload=payload, generation=2)

    lf = _mock_lf()
    _syncer(db_mgr, lf).process_batch(batch_size=5)

    ids = [call.kwargs["id"] for call in lf.api.scores.create.call_args_list]
    assert len(ids) == 2
    assert len(set(ids)) == 2, "the old revision's score id must not be reused"


# --- AC1 / AC6: an outage never changes the Argus result --------------------


def test_a_langfuse_outage_leaves_the_argus_result_untouched(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    seeded = _outbox_launch(
        db_mgr,
        scores_payload={
            "_typed_results": [
                {"evaluator_id": "intent_match", "result_type": "numeric", "status": "succeeded", "value": 0.9},
            ]
        },
    )
    from app.db_models import ExperimentLaunchRecord

    with db_mgr.get_session() as session:
        before = session.get(ExperimentLaunchRecord, seeded["launch_id"])
        before_manifest = dict(before.manifest)
        before_quality = before.quality_conclusion
        before_digest = before.request_payload_digest

    # Langfuse is down: every remote call raises.
    from unittest.mock import MagicMock

    lf = MagicMock()
    lf.api.dataset_run_items.create.side_effect = TimeoutError("langfuse unreachable")
    lf.api.scores.create.side_effect = TimeoutError("langfuse unreachable")

    processed = _syncer(db_mgr, lf).process_batch(batch_size=1)
    assert processed == 0, "an outage must not report a successful projection"

    with db_mgr.get_session() as session:
        after = session.get(ExperimentLaunchRecord, seeded["launch_id"])
        assert after.manifest == before_manifest
        assert after.quality_conclusion == before_quality
        assert after.request_payload_digest == before_digest

    # The failure is visible as a sync problem, not as a quality problem.
    from app.langfuse_sync import launch_sync_breakdown

    breakdown = launch_sync_breakdown(db_mgr, seeded["launch_id"])
    assert breakdown.overall != "SYNCED"


def test_the_sync_failure_never_downgrades_the_quality_conclusion(setup_runtime):
    from unittest.mock import MagicMock

    from app.db_models import ExperimentLaunchRecord

    db_mgr, _, _, _, _, _ = setup_runtime
    seeded = _outbox_launch(db_mgr, scores_payload={"intent_match": 1.0})
    lf = MagicMock()
    lf.api.dataset_run_items.create.side_effect = TimeoutError("langfuse unreachable")
    lf.api.scores.create.side_effect = TimeoutError("langfuse unreachable")

    _syncer(db_mgr, lf).process_batch(batch_size=1)

    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, seeded["launch_id"])
        # The Agent still passed; only the projection failed.
        assert launch.quality_conclusion == "pass"
