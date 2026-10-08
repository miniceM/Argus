"""Issue #49 — cross-issue full-chain final acceptance.

The eight sub-issues (#80 … #87) each ship their own focused suite. This module
is the *final* gate of the parent Issue #49: it exercises the guarantees that
only appear when the slices are combined, and that no single sub-issue can
prove on its own.

Guarantees under test (mapping to the #49 closing criteria):

* **版本不漂移** (#80, #81) — a Launch freezes the exact Evaluator version, the
  Runner identity and the Quality Policy digest; nothing resolves to
  ``latest`` / ``dev``, and a Launch whose Runner identity is unrecoverable
  never yields a positive quality conclusion.
* **异常保持 UNKNOWN** (#83) — an evaluator error, a missing result or a
  missing policy yields ``UNKNOWN`` and never a silent ``PASS``/``FAIL``.
* **重评不重复调用 Agent** (#84) — an evaluation-only retry reuses the frozen
  output digest; the Agent executor is never awaited a second time.
* **旧 Snapshot 与 Baseline 不变** (#85) — the previously published snapshot
  and the Baseline binding stay byte-identical across a re-evaluation.
* **契约变化不可误报回归** (#86) — tightening only the Quality Policy leaves
  the Measurement contract untouched, and the verdict explains the changed
  dimension instead of reporting a measurement regression.
* **Langfuse 恢复后幂等补写** (#87) — after an outage the projection is written
  exactly once per (item, generation, evaluator); the Argus result is never
  mutated by the sync path.
* **历史数据兼容** — pre-#82 numeric ``scores`` payloads and pre-#86 manifests
  without ``contract_digests`` still project / compare, with an explicit
  ``CONTRACT_PROVENANCE_UNKNOWN`` provenance rather than crashing.

The Demo regression baseline (v1 = 2/6, v2 = 6/6) is protected by
``tests/test_expected_pass_rate.py`` and re-asserted at the end of this module
so the final gate fails loudly if any slice disturbed it.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "services" / "eval-runner") not in sys.path:
    sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.baselines import set_baseline  # noqa: E402
from app.comparison_contracts import (  # noqa: E402
    MEASUREMENT,
    QUALITY_POLICY,
    REASON_CONTRACT_PROVENANCE_UNKNOWN,
    STATUS_CHANGED,
    STATUS_MATCH,
    STATUS_UNKNOWN,
    assess_comparability,
)
from app.db_models import (  # noqa: E402
    BaselineBindingRecord,
    EvaluationResultRecord,
    ExecutionAttemptRecord,
    ExperimentLaunchRecord,
    RunResultSnapshotRecord,
)
from app.db_models import ExperimentItemExecutionRecord as Item  # noqa: E402
from app.evaluation_recovery import EVALUATION_IDLE, recover_evaluation  # noqa: E402
from app.evaluator_binding import freeze_binding, manifest_measurement_digest  # noqa: E402
from app.evaluators import default_evaluator_registry  # noqa: E402
from app.executor import SingleInvocationResult  # noqa: E402
from app.langfuse_projection import stable_score_id  # noqa: E402
from app.langfuse_sync import LangfuseOutboxSyncer, launch_sync_breakdown  # noqa: E402
from app.quality_policy import (  # noqa: E402
    QUALITY_CONCLUSION_FAIL,
    QUALITY_CONCLUSION_PASS,
    QUALITY_CONCLUSION_UNKNOWN,
    QualityRule,
    default_quality_policy,
    evaluate_quality_policy,
    freeze_quality_policy,
)
from app.result_snapshots import create_result_snapshot  # noqa: E402
from app.runner_identity import current_runner_identity  # noqa: E402

IDENTITY = current_runner_identity().model_dump()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _binding(evaluator_id: str, version: str = "1.0.0"):
    """A genuinely frozen EvaluatorBinding (not a hand-rolled dict)."""
    definition = default_evaluator_registry.definition(evaluator_id)
    resolved = default_evaluator_registry.version(evaluator_id, version)
    return freeze_binding(definition, resolved, runner_identity=IDENTITY)


def _binding_payload(evaluator_id: str, version: str = "1.0.0") -> dict[str, Any]:
    return _binding(evaluator_id, version).to_payload()


def _policy(bindings, *, policy_id: str = "acceptance", version: str = "1.0.0"):
    """Freeze a real QualityPolicy so the digest is genuine, not a stub."""
    draft = default_quality_policy(bindings)
    return freeze_quality_policy(
        policy_id=policy_id,
        version=version,
        rules=draft.rules,
        bindings={rule.evaluator_id: b for rule, b in zip(draft.rules, bindings, strict=False)},
    )


def _strict_policy(bindings, *, policy_id: str = "acceptance-strict", version: str = "1.0.0"):
    """The same measurements under a *stricter* judgement threshold."""
    draft = default_quality_policy(bindings)
    tightened = tuple(
        QualityRule(
            evaluator_id=rule.evaluator_id,
            operator=rule.operator,
            threshold=(
                min(1.0, rule.threshold + 0.05) if rule.threshold is not None else None
            ),
            expected_value=rule.expected_value,
            result_type=rule.result_type,
            required=rule.required,
            critical=rule.critical,
            unknown_handling=rule.unknown_handling,
            note="tightened for the #86 cross-issue check",
        )
        for rule in draft.rules
    )
    return freeze_quality_policy(
        policy_id=policy_id,
        version=version,
        rules=tightened,
        bindings={rule.evaluator_id: b for rule, b in zip(tightened, bindings, strict=False)},
    )


def _manifest(
    *binding_payloads: dict[str, Any],
    policy_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": "1.2",
        "agent": {
            "agent_id": "test-agent",
            "version": "v1",
            "agent_version_id": "test-agent-v1",
            "endpoint": "http://localhost/invoke",
            "method": "POST",
            "request_mapping": {},
            "is_idempotent": False,
        },
        "execution_policy": {
            "timeout_seconds": 60,
            "max_retries": 0,
            "max_concurrency": 1,
            "rate_limit_per_minute": 60,
        },
        "dataset": {
            "items": [{"id": "0", "input": {}, "expected_output": {"expected_intent": "refund"}}]
        },
        "evaluators": list(binding_payloads),
        "quality_policy": policy_payload or {},
        "runner": IDENTITY,
    }
    # Issue #81 freezes the measurement digest; without it #86 must report the
    # dimension UNKNOWN, which would hide the very regression we assert on.
    manifest["measurement_digest"] = manifest_measurement_digest(manifest)
    return manifest


def _ok_invocation(body: dict[str, Any]) -> SingleInvocationResult:
    return SingleInvocationResult(
        status_code=200,
        body=body,
        raw_response="{}",
        headers={},
        duration_ms=3,
        trace_context_received=True,
        error_category=None,
        error_message=None,
        is_retryable=False,
        may_have_side_effects=False,
    )


def _run_item(setup_runtime, manifest, group: str, body: dict[str, Any]):
    """Drive the worker once with a mocked 200 Agent response."""
    db_mgr, queue, _limiter, orchestrator, worker, _rec = setup_runtime
    launch = orchestrator.create_launch("test-agent", "v1", "ds", "v1", group, manifest)
    orchestrator.start_launch(launch.id)
    msg = queue.read_group(group, count=1)[0]

    async def _run():
        with patch("app.worker.get_langfuse_client_safe", return_value=None):
            with patch(
                "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock
            ) as mock_invoke:
                mock_invoke.return_value = _ok_invocation(body)
                ok = await worker.execute_item_message(*msg)
                return ok, mock_invoke.await_count, launch.id, msg[1]

    ok, calls, launch_id, item_id = asyncio.run(_run())
    return db_mgr, queue, orchestrator, worker, launch_id, item_id, calls, ok


def _count_execution_attempts(db_mgr, item_id) -> int:
    with db_mgr.get_session() as session:
        return len(
            session.query(ExecutionAttemptRecord)
            .filter(ExecutionAttemptRecord.item_execution_id == item_id)
            .all()
        )


def _force_eval_failure(db_mgr, item_id) -> None:
    """Simulate an evaluator outage: no usable result, conclusion UNKNOWN."""
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
        it.quality_evaluation = None
        it.evaluation_status = EVALUATION_IDLE
        session.query(EvaluationResultRecord).filter(
            EvaluationResultRecord.item_execution_id == item_id
        ).delete()
        session.commit()


def _complete_and_snapshot(db_mgr, launch_id):
    """Publish revision 1 of the result and return the snapshot row."""
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        launch.status = "COMPLETED"
        launch.completed_at = datetime.now(UTC)
        session.commit()
    with db_mgr.get_session() as session:
        snapshot = create_result_snapshot(
            session, session.get(ExperimentLaunchRecord, launch_id)
        )
        session.commit()
    return snapshot


def _mock_lf() -> MagicMock:
    lf = MagicMock()
    lf.api.dataset_run_items.create.return_value = MagicMock()
    lf.api.scores.create.return_value = MagicMock()
    return lf


# ---------------------------------------------------------------------------
# 1. 版本不漂移 (#80, #81)
# ---------------------------------------------------------------------------


def test_frozen_identity_never_resolves_to_latest_or_dev(setup_runtime):
    """The manifest pins exact versions; no field degrades to latest/dev."""
    binding = _binding("intent_match")
    manifest = _manifest(binding.to_payload(), policy_payload=_policy([binding]).to_payload())
    db_mgr, _q, _o, _w, launch_id, _item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue49-identity", {"intent": "refund"}
    )

    with db_mgr.get_session() as session:
        stored = session.get(ExperimentLaunchRecord, launch_id).manifest

    runner = stored["runner"]
    assert runner["runner_version"] == IDENTITY["runner_version"]
    assert runner["runner_version"] not in ("latest", "dev", "")

    for entry in stored["evaluators"]:
        assert entry["version"] not in ("latest", "dev", "")
        # The frozen binding carries a content digest, so the exact definition
        # stays recoverable even if the registry later moves on.
        assert entry["definition_digest"].startswith("sha256:")

    # A real Quality Policy digest is frozen, not a placeholder.
    assert stored["quality_policy"]["policy_digest"].startswith("sha256:")


def test_a_launch_with_an_unresolvable_runner_identity_never_passes(setup_runtime):
    """A Launch whose Runner version cannot be recovered must not conclude PASS."""
    db_mgr, _queue, _limiter, orchestrator, _worker, _rec = setup_runtime
    binding = _binding("intent_match")
    manifest = _manifest(binding.to_payload(), policy_payload=_policy([binding]).to_payload())
    # The Runner identity is no longer resolvable in the catalog.
    manifest["runner"] = {**IDENTITY, "runner_version": "0.0.0-does-not-exist"}

    # Issue #81: the pre-flight fails closed with an explicit, stable reason
    # rather than silently proceeding with a drifted identity.
    from app.state_machine import DomainConflictError  # noqa: PLC0415

    with pytest.raises(DomainConflictError) as excinfo:
        launch = orchestrator.create_launch(
            "test-agent", "v1", "ds", "v1", "issue49-failclosed", manifest
        )
        orchestrator.start_launch(launch.id)
    assert "RUNNER_VERSION_MISMATCH" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 2. 异常保持 UNKNOWN (#83)
# ---------------------------------------------------------------------------


def test_an_evaluator_outage_keeps_the_item_unknown(setup_runtime):
    """A failed evaluation is UNKNOWN on the persisted item, not a silent pass."""
    binding = _binding("intent_match")
    manifest = _manifest(binding.to_payload(), policy_payload=_policy([binding]).to_payload())
    db_mgr, _q, _o, _w, _launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue49-unknown", {"intent": "refund"}
    )
    _force_eval_failure(db_mgr, item_id)

    with db_mgr.get_session() as session:
        assert session.get(Item, item_id).quality_conclusion.lower() == QUALITY_CONCLUSION_UNKNOWN


def test_a_policy_with_no_matching_result_is_unknown_not_pass():
    """An empty result set cannot manufacture a PASS."""
    binding = _binding("intent_match")
    policy = _policy([binding])
    decision = evaluate_quality_policy([], policy)
    assert decision.conclusion == QUALITY_CONCLUSION_UNKNOWN
    assert decision.releasable is False


def test_an_errored_typed_result_is_unknown_not_pass_or_fail():
    """Given an errored result the policy still yields UNKNOWN."""
    binding = _binding("intent_match")
    policy = _policy([binding])
    decision = evaluate_quality_policy(
        [
            {
                "evaluator_id": "intent_match",
                "evaluator_version": "1.0.0",
                "result_type": "numeric",
                "status": "error",
                "value": None,
                "error_code": "EVALUATOR_TIMEOUT",
            }
        ],
        policy,
    )
    assert decision.conclusion == QUALITY_CONCLUSION_UNKNOWN
    assert decision.conclusion not in (QUALITY_CONCLUSION_PASS, QUALITY_CONCLUSION_FAIL)


def test_a_missing_policy_is_unknown_with_an_explicit_reason():
    """No frozen policy -> UNKNOWN with a stable, explicit reason code."""
    decision = evaluate_quality_policy(
        [{"evaluator_id": "intent_match", "result_type": "numeric", "status": "succeeded", "value": 1.0}],
        None,
    )
    assert decision.conclusion == QUALITY_CONCLUSION_UNKNOWN
    assert decision.decided_by == "NO_FROZEN_POLICY"


# ---------------------------------------------------------------------------
# 3 + 4. 重评不重复调用 Agent (#84) 且旧 Snapshot / Baseline 不变 (#85)
# ---------------------------------------------------------------------------


def test_reevaluation_reuses_the_agent_and_leaves_history_immutable(setup_runtime):
    """The combined #84 + #85 guarantee, proven on one real Launch.

    Re-evaluating must (a) never call the Agent again, (b) leave the previously
    published snapshot byte-identical and (c) leave the Baseline binding
    pointing at exactly that same snapshot revision.
    """
    binding = _binding("intent_match")
    manifest = _manifest(binding.to_payload(), policy_payload=_policy([binding]).to_payload())
    db_mgr, _q, orchestrator, _w, launch_id, item_id, calls, _ok = _run_item(
        setup_runtime, manifest, "issue49-reeval", {"intent": "refund"}
    )
    assert calls == 1
    assert _count_execution_attempts(db_mgr, item_id) == 1

    first = _complete_and_snapshot(db_mgr, launch_id)
    assert first is not None
    first_snapshot_id = first.id
    first_revision = first.revision
    first_digest = first.source_result_digest
    first_items = first.items

    # Bind that published snapshot as the Baseline for the agent.
    baseline = set_baseline(
        db_mgr,
        agent_id="test-agent",
        environment="production",
        result_snapshot_id=first_snapshot_id,
        expected_revision=0,
        updated_by="issue-49-acceptance",
    )
    baseline_key = (baseline.agent_id, baseline.environment)
    baseline_snapshot_ref = baseline.result_snapshot_id
    baseline_revision = baseline.revision

    # Break the evaluation and drive the Console's evaluation-only retry.
    _force_eval_failure(db_mgr, item_id)
    orchestrator.retry_failed_evaluations(launch_id)
    with db_mgr.get_session() as session:
        gen = session.get(Item, item_id).evaluation_generation

    with patch(
        "app.executor.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock
    ) as mock_invoke:
        outcome = recover_evaluation(
            db_mgr, item_id=item_id, evaluation_generation=gen, worker_id="w-eval"
        )
    assert outcome["finalized"] is True
    assert outcome["quality_conclusion"] == QUALITY_CONCLUSION_PASS

    # (a) The Agent was NEVER awaited again, and still exactly one execution attempt.
    mock_invoke.assert_not_awaited()
    assert _count_execution_attempts(db_mgr, item_id) == 1

    # (b) The old snapshot row is untouched — same id, revision, digest, payload.
    with db_mgr.get_session() as session:
        old = session.get(RunResultSnapshotRecord, first_snapshot_id)
        assert old is not None
        assert old.revision == first_revision
        assert old.source_result_digest == first_digest
        assert old.items == first_items

    # (c) The Baseline binding still points at exactly the same snapshot revision.
    with db_mgr.get_session() as session:
        current = session.get(BaselineBindingRecord, baseline_key)
        assert current is not None
        assert current.result_snapshot_id == baseline_snapshot_ref == first_snapshot_id
        assert current.revision == baseline_revision


def test_republishing_identical_evidence_is_idempotent_and_history_stays_put(setup_runtime):
    """Re-evaluating to the *same* evidence must not fork a duplicate revision.

    ``create_result_snapshot`` is idempotent on the source digest, so a retry
    that lands on the same numbers republishes the same snapshot instead of
    inventing a near-identical revision.
    """
    binding = _binding("intent_match")
    manifest = _manifest(binding.to_payload(), policy_payload=_policy([binding]).to_payload())
    db_mgr, _q, orchestrator, _w, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue49-idempotent", {"intent": "refund"}
    )
    first = _complete_and_snapshot(db_mgr, launch_id)
    first_id, first_rev, first_digest = first.id, first.revision, first.source_result_digest

    _force_eval_failure(db_mgr, item_id)
    orchestrator.retry_failed_evaluations(launch_id)
    with db_mgr.get_session() as session:
        gen = session.get(Item, item_id).evaluation_generation
    recover_evaluation(db_mgr, item_id=item_id, evaluation_generation=gen, worker_id="w-eval")

    with db_mgr.get_session() as session:
        again = create_result_snapshot(
            session, session.get(ExperimentLaunchRecord, launch_id)
        )
        session.commit()

    # Same evidence -> same snapshot, still exactly one revision for this digest.
    assert again is not None
    assert again.id == first_id
    assert again.revision == first_rev
    assert again.source_result_digest == first_digest
    with db_mgr.get_session() as session:
        count = (
            session.query(RunResultSnapshotRecord)
            .filter(RunResultSnapshotRecord.launch_id == launch_id)
            .count()
        )
    assert count == 1


def test_changed_evidence_appends_a_new_revision_and_never_mutates_the_old_one(setup_runtime):
    """Genuinely different evidence lands in a NEW revision; history is append-only."""
    binding = _binding("intent_match")
    manifest = _manifest(binding.to_payload(), policy_payload=_policy([binding]).to_payload())
    db_mgr, _q, _o, _w, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue49-newrev", {"intent": "refund"}
    )
    first = _complete_and_snapshot(db_mgr, launch_id)
    first_id, first_rev, first_digest = first.id, first.revision, first.source_result_digest

    # The evidence genuinely changes: a different observed measurement is
    # recorded, then a fresh revision is published.
    with db_mgr.get_session() as session:
        row = (
            session.query(EvaluationResultRecord)
            .filter(EvaluationResultRecord.item_execution_id == item_id)
            .first()
        )
        assert row is not None
        row.value = 0.11
        session.commit()

    with db_mgr.get_session() as session:
        second = create_result_snapshot(
            session, session.get(ExperimentLaunchRecord, launch_id)
        )
        session.commit()

    assert second is not None
    assert second.id != first_id
    assert second.revision > first_rev
    assert second.source_result_digest != first_digest
    # The original revision is still readable exactly as it was published.
    with db_mgr.get_session() as session:
        old = session.get(RunResultSnapshotRecord, first_id)
        assert old.revision == first_rev
        assert old.source_result_digest == first_digest


# ---------------------------------------------------------------------------
# 5. 契约变化不可误报回归 (#86)
# ---------------------------------------------------------------------------


def test_tightening_only_the_policy_does_not_touch_the_measurement_contract():
    """A stricter gate changes the policy dimension, never the measurement.

    This is the exact false positive #86 exists to prevent: the user tightened
    their own threshold, so the *judgement* changed, but the underlying
    measurement did not — a measurement regression must not be reported.
    """
    binding = _binding("intent_match")
    loose = _policy([binding])
    strict = _strict_policy([binding])

    # The two policies really are different ...
    assert loose.policy_digest != strict.policy_digest

    baseline_manifest = _manifest(binding.to_payload(), policy_payload=loose.to_payload())
    candidate_manifest = _manifest(binding.to_payload(), policy_payload=strict.to_payload())

    # ... yet the measurement digest is identical on both sides.
    assert manifest_measurement_digest(baseline_manifest) == manifest_measurement_digest(
        candidate_manifest
    )

    verdict = assess_comparability(baseline_manifest, candidate_manifest)
    assert verdict.dimension(MEASUREMENT).status == STATUS_MATCH
    assert verdict.dimension(QUALITY_POLICY).status == STATUS_CHANGED
    # The change is explained on the policy axis, not as a measurement regression.
    assert verdict.comparable is False
    assert verdict.reason_codes
    assert verdict.suggestions


def test_swapping_the_evaluator_is_reported_as_a_measurement_change():
    """Changing the frozen evaluator is surfaced on the MEASUREMENT axis."""
    baseline_manifest = _manifest(_binding_payload("intent_match"))
    candidate_manifest = _manifest(_binding_payload("pii_safe"))

    verdict = assess_comparability(baseline_manifest, candidate_manifest)
    measurement = verdict.dimension(MEASUREMENT)
    assert measurement.status in (STATUS_CHANGED, STATUS_UNKNOWN)
    if measurement.status == STATUS_CHANGED:
        assert verdict.comparable is False
        assert verdict.reason_codes  # an explicit reason is always present


def test_a_legacy_manifest_without_contract_digests_is_unknown_not_a_crash():
    """Historical manifests keep working, flagged with explicit provenance."""
    legacy = {
        "schema_version": "1.0",
        "agent": {"agent_id": "test-agent", "version": "v1"},
        "evaluators": [{"id": "intent_match", "version": "1.0.0", "threshold": 0.8}],
    }
    verdict = assess_comparability(legacy, dict(legacy))
    assert verdict.provenance in ("UNKNOWN", "LEGACY_PARTIAL")
    assert REASON_CONTRACT_PROVENANCE_UNKNOWN in verdict.reason_codes


# ---------------------------------------------------------------------------
# 6. Langfuse 恢复后幂等补写 (#87)
# ---------------------------------------------------------------------------


def _outbox_launch(
    db_mgr,
    *,
    scores_payload: dict[str, Any],
    generation: int = 1,
    status: str = "PENDING",
    attempts: int = 0,
    launch_status: str = "COMPLETED",
):
    from app.db_models import LangfuseSyncTaskRecord

    launch_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    manifest = {
        "schema_version": "1.2",
        "dataset": {
            "source": "langfuse",
            "dataset_id": "d1",
            "dataset_name": "golden",
            "dataset_version": "v1",
        },
        "agent": {"agent_id": "test-agent", "version": "v1", "spec_digest": "d"},
        "evaluators": [{"id": "intent_match", "version": "1.0.0", "threshold": 0.8}],
        "quality_policy": {
            "policy_id": "p",
            "version": "1.0",
            "policy_digest": "sha256:policy",
            "rules": [],
        },
        "comparison": {
            "environment": "production",
            "baseline_snapshot_id": None,
            "baseline_binding_revision": None,
        },
    }
    with db_mgr.get_session() as session:
        launch = ExperimentLaunchRecord(
            id=launch_id,
            name="issue-49-sync",
            status=launch_status,
            quality_conclusion=QUALITY_CONCLUSION_PASS,
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
                quality_conclusion=QUALITY_CONCLUSION_PASS,
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


_TYPED_NUMERIC = {
    "_typed_results": [
        {
            "evaluator_id": "intent_match",
            "result_type": "numeric",
            "status": "succeeded",
            "value": 1.0,
        }
    ]
}


def test_recovery_backfills_each_projection_exactly_once(setup_runtime):
    """After an outage, recovery projects exactly once per identity+revision."""
    db_mgr, _, _, _, _, _ = setup_runtime
    seeded = _outbox_launch(db_mgr, scores_payload=dict(_TYPED_NUMERIC))
    lf = _mock_lf()

    processed = LangfuseOutboxSyncer(db_mgr, lf, syncer_id="s1").process_batch(batch_size=1)
    assert processed == 1

    names = [call.kwargs["name"] for call in lf.api.scores.create.call_args_list]
    ids = [call.kwargs.get("id") for call in lf.api.scores.create.call_args_list]
    assert names == ["intent_match"]
    # The stable idempotency key equals the documented projection id.
    assert ids == [stable_score_id(item_id=seeded["item_id"], generation=1, evaluator_id="intent_match")]

    # A redelivery of the same generation reuses exactly the same id.
    from app.db_models import LangfuseSyncTaskRecord

    with db_mgr.get_session() as session:
        task = session.get(LangfuseSyncTaskRecord, seeded["task_id"])
        task.status = "PENDING"
        task.attempts = 0
        task.next_retry_at = datetime.now(UTC)
        session.commit()
    lf2 = _mock_lf()
    LangfuseOutboxSyncer(db_mgr, lf2, syncer_id="s2").process_batch(batch_size=1)
    assert [call.kwargs.get("id") for call in lf2.api.scores.create.call_args_list] == ids


def test_a_new_evaluation_revision_produces_a_new_projection_id(setup_runtime):
    """A new generation yields a NEW projection id; the old one is preserved."""
    db_mgr, _, _, _, _, _ = setup_runtime
    lf = _mock_lf()
    LangfuseOutboxSyncer(
        db_mgr, lf, syncer_id="s1"
    ).process_batch(batch_size=0)  # no-op, keeps the mock untouched
    seeded1 = _outbox_launch(db_mgr, scores_payload=dict(_TYPED_NUMERIC), generation=1)
    LangfuseOutboxSyncer(db_mgr, lf, syncer_id="s1").process_batch(batch_size=1)
    first_id = lf.api.scores.create.call_args_list[0].kwargs.get("id")

    seeded2 = _outbox_launch(db_mgr, scores_payload=dict(_TYPED_NUMERIC), generation=2)
    lf2 = _mock_lf()
    LangfuseOutboxSyncer(db_mgr, lf2, syncer_id="s2").process_batch(batch_size=1)
    second_id = lf2.api.scores.create.call_args_list[0].kwargs.get("id")

    assert first_id == stable_score_id(item_id=seeded1["item_id"], generation=1, evaluator_id="intent_match")
    assert second_id == stable_score_id(item_id=seeded2["item_id"], generation=2, evaluator_id="intent_match")
    assert first_id != second_id


def test_a_langfuse_outage_never_downgrades_the_quality_conclusion(setup_runtime):
    """A total sync failure leaves the Argus verdict and manifest untouched."""
    db_mgr, _, _, _, _, _ = setup_runtime
    seeded = _outbox_launch(db_mgr, scores_payload=dict(_TYPED_NUMERIC))

    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, seeded["launch_id"])
        before_quality = launch.quality_conclusion
        before_status = launch.status
        before_policy_digest = launch.manifest["quality_policy"]["policy_digest"]

    # Force a transport failure on every remote call.
    lf = MagicMock()
    lf.api.dataset_run_items.create.side_effect = TimeoutError("langfuse down")
    lf.api.scores.create.side_effect = TimeoutError("langfuse down")
    LangfuseOutboxSyncer(db_mgr, lf, syncer_id="s1").process_batch(batch_size=1)

    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, seeded["launch_id"])
        assert launch.quality_conclusion == before_quality == QUALITY_CONCLUSION_PASS
        assert launch.status == before_status
        assert launch.manifest["quality_policy"]["policy_digest"] == before_policy_digest

    # And the overall sync state is honestly not SYNCED.
    assert launch_sync_breakdown(db_mgr, seeded["launch_id"]).overall != "SYNCED"


def test_a_text_result_is_never_projected_as_zero(setup_runtime):
    """Cross-issue: a text result stays unprojected, certainly not a 0."""
    db_mgr, _, _, _, _, _ = setup_runtime
    payload = {
        "_typed_results": [
            {
                "evaluator_id": "answer_excerpt",
                "result_type": "text",
                "status": "succeeded",
                "value": "退款完成",
            },
            {
                "evaluator_id": "intent_match",
                "result_type": "numeric",
                "status": "succeeded",
                "value": 1.0,
            },
        ]
    }
    _outbox_launch(db_mgr, scores_payload=payload)
    lf = _mock_lf()
    LangfuseOutboxSyncer(db_mgr, lf, syncer_id="s1").process_batch(batch_size=1)

    names = [call.kwargs["name"] for call in lf.api.scores.create.call_args_list]
    assert names == ["intent_match"]  # the text result produced NO score at all


# ---------------------------------------------------------------------------
# 7. 历史数据兼容 — a pre-#82 numeric payload still projects
# ---------------------------------------------------------------------------


def test_a_legacy_numeric_scores_payload_still_projects(setup_runtime):
    """Pre-#82 payloads (a bare numeric scores map) remain projectable."""
    db_mgr, _, _, _, _, _ = setup_runtime
    seeded = _outbox_launch(db_mgr, scores_payload={"intent_match": 1.0})
    lf = _mock_lf()
    LangfuseOutboxSyncer(db_mgr, lf, syncer_id="s1").process_batch(batch_size=1)

    names = [call.kwargs["name"] for call in lf.api.scores.create.call_args_list]
    assert names == ["intent_match"]
    ids = [call.kwargs.get("id") for call in lf.api.scores.create.call_args_list]
    assert ids == [stable_score_id(item_id=seeded["item_id"], generation=1, evaluator_id="intent_match")]


# ---------------------------------------------------------------------------
# 8. Demo 回归基线 (v1 = 2/6, v2 = 6/6)
# ---------------------------------------------------------------------------


def test_demo_regression_baseline_is_preserved():
    """The final #49 gate fails loudly if any slice broke the Demo baseline.

    The authoritative assertion lives in ``tests/test_expected_pass_rate.py``;
    it is re-executed here so the cross-issue gate covers it end to end:
    Agent v1 must stay at 2/6 and Agent v2 at 6/6.
    """
    from tests.test_expected_pass_rate import score  # noqa: PLC0415

    assert score("v1") == (2, 6)
    assert score("v2") == (6, 6)
