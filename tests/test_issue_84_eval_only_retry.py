"""Issue #84 — evaluation-only retry that reuses the Agent output.

Covers the acceptance criteria of Argus Issue #84:
* the Agent is called exactly once and one execution attempt is recorded, even
  when the Evaluator first fails; an evaluation-only retry then succeeds while
  reusing the original output digest (AC1);
* an evaluation-only retry still works when Langfuse is unavailable / the
  checkpoint is purely local (AC2);
* when several Bindings are frozen and only one fails, recovery re-runs just
  that one and preserves the other results *and* their provenance (AC3);
* a missing / expired / corrupt checkpoint (or a binding mismatch) blocks the
  retry with a clear, stable reason, keeps the item UNKNOWN and never calls the
  Agent again (AC4);
* a double submission and a competing worker produce at most one effective
  evaluation, and a late / superseded / lost-lease result never overwrites the
  current one (AC5);
* the reconciler re-dispatches an evaluation whose worker lease expired without
  re-calling the Agent.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "services" / "eval-runner") not in sys.path:
    sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db_models import (  # noqa: E402
    EvaluationAttemptRecord,
    EvaluationResultRecord,
    ExecutionAttemptRecord,
    ExecutionCheckpointRecord,
)
from app.db_models import (  # noqa: E402
    ExperimentItemExecutionRecord as Item,
)
from app.evaluation_recovery import (  # noqa: E402
    EVALUATION_IDLE,
    EVALUATION_RECOVERED,
    EVALUATION_RUNNING,
    claim_evaluation,
    finalize_evaluation,
    recover_evaluation,
)
from app.evaluator_binding import freeze_binding  # noqa: E402
from app.evaluators import default_evaluator_registry  # noqa: E402
from app.execution_checkpoint import (  # noqa: E402
    CHECKPOINT_CORRUPT,
    CheckpointUnavailableError,
    canonical_output_digest,
    load_recoverable_checkpoint,
    write_execution_checkpoint,
)
from app.executor import SingleInvocationResult  # noqa: E402
from app.quality_policy import (  # noqa: E402
    QUALITY_CONCLUSION_PASS,
    QUALITY_CONCLUSION_UNKNOWN,
)
from app.runner_identity import current_runner_identity  # noqa: E402

IDENTITY = current_runner_identity().model_dump()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _frozen_binding(evaluator_id: str, version: str = "1.0.0") -> dict[str, Any]:
    definition = default_evaluator_registry.definition(evaluator_id)
    resolved = default_evaluator_registry.version(evaluator_id, version)
    return freeze_binding(definition, resolved, runner_identity=IDENTITY).to_payload()


def _manifest(*binding_payloads: dict[str, Any], dataset_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
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
            "items": dataset_items
            or [{"id": "0", "input": {}, "expected_output": {"expected_intent": "refund"}}]
        },
        "evaluators": list(binding_payloads),
        "runner": IDENTITY,
    }


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


def _item(db_mgr, item_id) -> Item:
    with db_mgr.get_session() as session:
        return session.get(Item, item_id)


def _count_execution_attempts(db_mgr, item_id) -> int:
    with db_mgr.get_session() as session:
        return len(
            session.query(ExecutionAttemptRecord)
            .filter(ExecutionAttemptRecord.item_execution_id == item_id)
            .all()
        )


# ---------------------------------------------------------------------------
# AC1: Agent called once, evaluation fails, retry-eval succeeds on the same output
# ---------------------------------------------------------------------------

def test_retry_evaluation_reuses_agent_output_and_keeps_execution_attempt_count(setup_runtime):
    """A first evaluation failure is repaired without a second Agent call."""
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, _queue, orchestrator, _worker, launch_id, item_id, calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-ac1", {"intent": "refund"}
    )

    # The Agent was called exactly once and produced one execution attempt.
    assert calls == 1
    assert _count_execution_attempts(db_mgr, item_id) == 1

    item = _item(db_mgr, item_id)
    assert item.execution_status.lower() == "succeeded"
    assert item.eval_status.lower() == "succeeded"
    assert item.quality_conclusion.lower() == QUALITY_CONCLUSION_PASS
    # A recoverable checkpoint exists for the current generation.
    with db_mgr.get_session() as session:
        cp = session.query(ExecutionCheckpointRecord).filter(
            ExecutionCheckpointRecord.item_execution_id == item_id
        ).one()
        original_digest = cp.output_digest
    assert original_digest == canonical_output_digest({"intent": "refund"})

    # Now force the evaluation into a failed/UNKNOWN state (simulating an
    # evaluator outage) and clear the results, mimicking a real outage.
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

    # Submit an evaluation-only retry, then run it. The Agent is never called.
    result = orchestrator.retry_failed_evaluations(launch_id)
    assert result["submitted"] == [item_id]

    outcome = recover_evaluation(
        db_mgr, item_id=item_id, evaluation_generation=1, worker_id="w-eval"
    )
    assert outcome["finalized"] is True
    assert outcome["eval_status"] == "succeeded"
    assert outcome["quality_conclusion"] == QUALITY_CONCLUSION_PASS

    # No second Agent call and still exactly one execution attempt; the digest
    # of the reused output is recorded for audit.
    assert _count_execution_attempts(db_mgr, item_id) == 1
    item = _item(db_mgr, item_id)
    assert item.execution_status.lower() == "succeeded"
    assert item.evaluation_status == EVALUATION_RECOVERED
    assert item.evaluation_reused_output_digest == original_digest
    with db_mgr.get_session() as session:
        # Exactly one evaluation attempt row (the recovery), no execution retry.
        attempts = session.query(EvaluationAttemptRecord).filter(
            EvaluationAttemptRecord.item_execution_id == item_id
        ).all()
        assert len(attempts) == 1
        assert attempts[0].status == "succeeded"
        assert attempts[0].reused_output_digest == original_digest


def test_retry_evaluation_does_not_call_the_agent(setup_runtime):
    """The recovery worker never invokes the Agent executor."""
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, _queue, orchestrator, _worker, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-noagent", {"intent": "refund"}
    )
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
        it.evaluation_status = EVALUATION_IDLE
        session.commit()

    orchestrator.retry_failed_evaluations(launch_id)
    with patch(
        "app.executor.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock
    ) as mock_invoke:
        outcome = recover_evaluation(
            db_mgr, item_id=item_id, evaluation_generation=1, worker_id="w-eval"
        )
    assert outcome["finalized"] is True
    mock_invoke.assert_not_awaited()


# ---------------------------------------------------------------------------
# AC2: Langfuse unavailable / not synced — recovery uses the local checkpoint
# ---------------------------------------------------------------------------

def test_retry_evaluation_works_without_langfuse(setup_runtime):
    """Recovery is driven purely by the local checkpoint, never by Langfuse."""
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, _queue, orchestrator, _worker, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-nolf", {"intent": "refund"}
    )
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
        it.evaluation_status = EVALUATION_IDLE
        session.query(EvaluationResultRecord).filter(
            EvaluationResultRecord.item_execution_id == item_id
        ).delete()
        session.commit()

    # Even with Langfuse fully unavailable, the local checkpoint suffices:
    # the recovery path never resolves or contacts Langfuse.
    with patch("app.execution.get_langfuse_client_safe", return_value=None):
        orchestrator.retry_failed_evaluations(launch_id)
        outcome = recover_evaluation(
            db_mgr, item_id=item_id, evaluation_generation=1, worker_id="w-eval"
        )
    assert outcome["finalized"] is True
    assert outcome["quality_conclusion"] == QUALITY_CONCLUSION_PASS


# ---------------------------------------------------------------------------
# AC3: several bindings, one fails — only that one is re-run
# ---------------------------------------------------------------------------

def test_retry_evaluation_reruns_only_the_failed_binding(setup_runtime):
    """A successful sibling result is preserved with its provenance."""
    intent = _frozen_binding("intent_match")
    pii = _frozen_binding("pii_safe")
    manifest = _manifest(intent, pii)
    db_mgr, _queue, orchestrator, _worker, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-partial", {"intent": "refund"}
    )

    with db_mgr.get_session() as session:
        # Keep the intent_match result, drop the pii_safe one and mark failed.
        session.query(EvaluationResultRecord).filter(
            EvaluationResultRecord.item_execution_id == item_id,
            EvaluationResultRecord.evaluator_id == "pii_safe",
        ).delete()
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
        it.evaluation_status = EVALUATION_IDLE
        session.commit()
    with db_mgr.get_session() as session:
        kept = (
            session.query(EvaluationResultRecord)
            .filter(
                EvaluationResultRecord.item_execution_id == item_id,
                EvaluationResultRecord.evaluator_id == "intent_match",
            )
            .one()
        )
        kept_binding_id = kept.binding_id
        kept_definition_digest = kept.definition_digest

    # Only the missing binding should be re-judged; the successful one is reused.
    orchestrator.retry_failed_evaluations(launch_id)
    with db_mgr.get_session() as session:
        item_row = session.get(Item, item_id)
        gen = item_row.evaluation_generation
    outcome = recover_evaluation(
        db_mgr, item_id=item_id, evaluation_generation=gen, worker_id="w-eval"
    )
    assert outcome["finalized"] is True

    with db_mgr.get_session() as session:
        # The preserved result kept its original provenance, byte-for-byte.
        kept = (
            session.query(EvaluationResultRecord)
            .filter(
                EvaluationResultRecord.item_execution_id == item_id,
                EvaluationResultRecord.evaluator_id == "intent_match",
            )
            .one()
        )
        assert kept.binding_id == kept_binding_id
        assert kept.definition_digest == kept_definition_digest
        assert kept.status == "succeeded"
        # A fresh pii_safe result now exists and succeeded.
        pii_row = (
            session.query(EvaluationResultRecord)
            .filter(
                EvaluationResultRecord.item_execution_id == item_id,
                EvaluationResultRecord.evaluator_id == "pii_safe",
            )
            .one()
        )
        assert pii_row.status == "succeeded"

        # The attempt targeted only the failed binding.
        att = (
            session.query(EvaluationAttemptRecord)
            .filter(EvaluationAttemptRecord.item_execution_id == item_id)
            .one()
        )
        targeted = att.target_bindings
        assert len(targeted) == 1
        assert "pii" not in targeted[0]
        assert "intent" not in targeted[0]


# ---------------------------------------------------------------------------
# AC4: missing / expired / corrupt checkpoint blocks the retry
# ---------------------------------------------------------------------------

def test_missing_checkpoint_blocks_retry_and_stays_unknown(setup_runtime):
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, _queue, orchestrator, _worker, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-missing", {"intent": "refund"}
    )
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
        it.evaluation_status = EVALUATION_IDLE
        session.query(ExecutionCheckpointRecord).filter(
            ExecutionCheckpointRecord.item_execution_id == item_id
        ).delete()
        session.commit()

    from app.state_machine import DomainConflictError

    with pytest.raises(DomainConflictError):
        orchestrator.retry_failed_evaluations(launch_id)

    item = _item(db_mgr, item_id)
    assert item.quality_conclusion == QUALITY_CONCLUSION_UNKNOWN
    assert item.eval_status == "failed"
    assert _count_execution_attempts(db_mgr, item_id) == 1


def test_expired_checkpoint_blocks_retry(setup_runtime):
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, _queue, orchestrator, _worker, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-expired", {"intent": "refund"}
    )
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
        it.evaluation_status = EVALUATION_IDLE
        cp = session.query(ExecutionCheckpointRecord).filter(
            ExecutionCheckpointRecord.item_execution_id == item_id
        ).one()
        cp.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    from app.state_machine import DomainConflictError

    with pytest.raises(DomainConflictError):
        orchestrator.retry_failed_evaluations(launch_id)
    item = _item(db_mgr, item_id)
    assert item.quality_conclusion == QUALITY_CONCLUSION_UNKNOWN
    assert _count_execution_attempts(db_mgr, item_id) == 1


def test_corrupt_checkpoint_is_detected_by_digest(setup_runtime):
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, _queue, _orchestrator, _worker, _launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-corrupt", {"intent": "refund"}
    )
    with db_mgr.get_session() as session:
        cp = session.query(ExecutionCheckpointRecord).filter(
            ExecutionCheckpointRecord.item_execution_id == item_id
        ).one()
        # Tamper with the stored output so it no longer matches the digest.
        cp.agent_output = {"intent": "TAMPERED"}
        session.commit()
        with pytest.raises(CheckpointUnavailableError) as exc:
            load_recoverable_checkpoint(
                session, item_execution_id=item_id, dispatch_generation=1, manifest=manifest
            )
        assert exc.value.code == CHECKPOINT_CORRUPT


def test_binding_mismatch_blocks_retry(setup_runtime):
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, _queue, _orchestrator, _worker, _launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-mismatch", {"intent": "refund"}
    )
    # A different frozen binding set must not reuse this checkpoint.
    other_manifest = _manifest(_frozen_binding("pii_safe"))
    with db_mgr.get_session() as session:
        with pytest.raises(CheckpointUnavailableError) as exc:
            load_recoverable_checkpoint(
                session,
                item_execution_id=item_id,
                dispatch_generation=1,
                manifest=other_manifest,
            )
        assert exc.value.code == "CHECKPOINT_BINDING_MISMATCH"


# ---------------------------------------------------------------------------
# AC5: idempotent submission + late / superseded results cannot overwrite
# ---------------------------------------------------------------------------

def test_double_submit_produces_one_effective_evaluation(setup_runtime):
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, _queue, orchestrator, _worker, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-double", {"intent": "refund"}
    )
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
        it.evaluation_status = EVALUATION_IDLE
        session.commit()

    first = orchestrator.retry_failed_evaluations(launch_id)
    assert first["submitted"] == [item_id]
    # A second click while the first is still marked running is a no-op.
    second = orchestrator.retry_failed_evaluations(launch_id)
    assert second["submitted"] == []
    assert second["already_running"] == [item_id]

    item = _item(db_mgr, item_id)
    # The generation advanced exactly once.
    assert item.evaluation_generation == 1
    assert item.evaluation_status == EVALUATION_RUNNING


def test_competing_workers_only_one_claims(setup_runtime):
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, _queue, orchestrator, _worker, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-race", {"intent": "refund"}
    )
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
        it.evaluation_status = EVALUATION_IDLE
        session.commit()
    orchestrator.retry_failed_evaluations(launch_id)

    # Two workers race to claim generation 1: exactly one wins.
    c1 = claim_evaluation(db_mgr, item_id=item_id, evaluation_generation=1, worker_id="w1")
    c2 = claim_evaluation(db_mgr, item_id=item_id, evaluation_generation=1, worker_id="w2")
    assert c1 is not None
    assert c2 is None


def test_late_old_generation_result_cannot_overwrite(setup_runtime):
    """A stale result for an old generation is discarded, current one survives."""
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, _queue, orchestrator, _worker, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-late", {"intent": "refund"}
    )
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
        it.evaluation_status = EVALUATION_IDLE
        session.commit()

    # Generation 1 is claimed, then a *newer* generation 2 takes over.
    orchestrator.retry_failed_evaluations(launch_id)
    gen1_claim = claim_evaluation(db_mgr, item_id=item_id, evaluation_generation=1, worker_id="w1")
    assert gen1_claim is not None
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.evaluation_generation = 2  # simulate a newer retry superseding gen 1
        session.commit()

    # The stale worker now tries to finalize its old-generation result.
    from app.evaluator_binding import bindings_from_manifest, resolve_execution_plan, summarize_typed_results
    from app.evaluator_results import build_result

    plan = [r for r in resolve_execution_plan(manifest) if r.binding.scope == "item"]
    binding_obj = bindings_from_manifest(manifest)[0]
    result = build_result(binding_obj, 1.0)
    summary = summarize_typed_results(plan, [result], manifest=manifest)

    ok = finalize_evaluation(
        db_mgr,
        claim=gen1_claim,
        summary=summary,
        merged_results=[result],
        output_digest="sha256:stale",
        reused_bindings=[],
    )
    # The stale finalize is rejected.
    assert ok is False
    item = _item(db_mgr, item_id)
    # Current generation is 2 and its verdict is untouched by the stale write.
    assert item.evaluation_generation == 2
    assert item.evaluation_reused_output_digest != "sha256:stale"
    with db_mgr.get_session() as session:
        att = session.query(EvaluationAttemptRecord).filter(
            EvaluationAttemptRecord.item_execution_id == item_id
        ).one()
        assert att.status == "discarded"


# ---------------------------------------------------------------------------
# Reconciler: an expired evaluation lease is re-dispatched, not re-invoked
# ---------------------------------------------------------------------------

def test_reconciler_redispatches_expired_evaluation_lease(setup_runtime):
    db_mgr, queue, _limiter, _orchestrator, _worker, reconciler = setup_runtime
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    _db, _q, orchestrator, _w, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-recon", {"intent": "refund"}
    )
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.evaluation_status = EVALUATION_RUNNING
        it.evaluation_generation = 1
        it.evaluation_lease_token = "dead-token"
        it.evaluation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        session.commit()

    recovered = reconciler.reconcile_expired_evaluation_leases()
    assert recovered == 1
    # The stored output is untouched; only the evaluation is re-queued.
    with db_mgr.get_session() as session:
        cp = session.query(ExecutionCheckpointRecord).filter(
            ExecutionCheckpointRecord.item_execution_id == item_id
        ).one()
        assert cp.output_digest == canonical_output_digest({"intent": "refund"})
    msgs = queue.read_group("recon", count=10, block_ms=0)
    assert any(m[1] == item_id and m[3] == "EVALUATION" for m in msgs)


# ---------------------------------------------------------------------------
# API contract: POST /retry-evaluation and the recoverable flag on the item list
# ---------------------------------------------------------------------------
def _api_client(monkeypatch, db_mgr, queue):
    """Bind the FastAPI app to an isolated runtime database.

    ``get_services`` resolves both ``db_manager`` and ``orchestrator`` from
    ``app.main`` at call time, but ``main.orchestrator`` is bound to the
    module-level database, so both objects must be replaced to keep the route
    on the fixture database.
    """
    from app import main
    from app.limiter import MemoryAgentLimiter
    from app.orchestrator import LaunchOrchestrator
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "db_manager", db_mgr)
    monkeypatch.setattr(
        main, "orchestrator", LaunchOrchestrator(db_mgr, queue, MemoryAgentLimiter())
    )
    return TestClient(main.app)


def test_retry_evaluation_route_returns_contract_and_marks_items_recoverable(
    setup_runtime, monkeypatch
):
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, queue, orchestrator, _worker, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-api", {"intent": "refund"}
    )

    # The item finished, so nothing is retryable yet: the action must be
    # refused with 409 instead of silently re-running a healthy evaluation.
    client = _api_client(monkeypatch, db_mgr, queue)

    items = client.get(f"/api/v1/experiment-launches/{launch_id}/items")
    assert items.status_code == 200, items.text
    payload = items.json()[0]
    # A successful evaluation still leaves a usable checkpoint on disk.
    assert payload["evaluation_recoverable"] is True
    assert payload["evaluation_status"] == "none"
    assert payload["evaluation_generation"] == 0

    healthy = client.post(f"/api/v1/experiment-launches/{launch_id}/retry-evaluation")
    assert healthy.status_code == 409, healthy.text

    # Break the evaluation the way an evaluator outage would.
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
        it.quality_evaluation = None
        session.query(EvaluationResultRecord).filter(
            EvaluationResultRecord.item_execution_id == item_id
        ).delete()
        session.commit()

    response = client.post(f"/api/v1/experiment-launches/{launch_id}/retry-evaluation")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submitted"] == [item_id]
    assert body["already_running"] == []
    assert body["blocked"] == []
    assert "不会再次调用 Agent" in body["message"]
    assert body["launch"]["id"] == launch_id

    # While the re-judgement is in flight the item advertises its own
    # evaluation lifecycle, independently of execution_status.
    inflight = client.get(f"/api/v1/experiment-launches/{launch_id}/items").json()[0]
    assert inflight["execution_status"].lower() == "succeeded"
    assert inflight["evaluation_status"] == EVALUATION_RUNNING
    assert inflight["evaluation_generation"] == 1


def test_retry_evaluation_route_reports_blocked_checkpoint(setup_runtime, monkeypatch):
    binding = _frozen_binding("intent_match")
    manifest = _manifest(binding)
    db_mgr, queue, orchestrator, _worker, launch_id, item_id, _calls, _ok = _run_item(
        setup_runtime, manifest, "issue84-api-blocked", {"intent": "refund"}
    )

    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
        session.query(ExecutionCheckpointRecord).filter(
            ExecutionCheckpointRecord.item_execution_id == item_id
        ).delete()
        session.commit()

    client = _api_client(monkeypatch, db_mgr, queue)
    response = client.post(f"/api/v1/experiment-launches/{launch_id}/retry-evaluation")
    # Nothing recoverable -> the request is refused with a conflict, not a
    # silent success.
    assert response.status_code == 409, response.text

    # With a retryable-but-failing case present, the blocked reason travels in
    # the response body so the Console can explain it.
    with db_mgr.get_session() as session:
        it = session.get(Item, item_id)
        it.eval_status = "failed"
        session.commit()
    with db_mgr.get_session() as session:
        write_execution_checkpoint(
            session,
            item_execution_id=item_id,
            launch_id=launch_id,
            dataset_item_id="0",
            dispatch_generation=it.dispatch_generation,
            output={"intent": "refund"},
            input_payload={},
            expected_output={"expected_intent": "refund"},
            manifest=manifest,
            final_attempt_id=None,
        )
        session.commit()
    with db_mgr.get_session() as session:
        cp = session.query(ExecutionCheckpointRecord).filter(
            ExecutionCheckpointRecord.item_execution_id == item_id
        ).one()
        cp.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    blocked = client.post(f"/api/v1/experiment-launches/{launch_id}/retry-evaluation")
    assert blocked.status_code == 409, blocked.text


def test_retry_evaluation_route_returns_404_for_unknown_launch(setup_runtime, monkeypatch):
    db_mgr, queue, limiter, *_ = setup_runtime
    client = _api_client(monkeypatch, db_mgr, queue)
    response = client.post("/api/v1/experiment-launches/does-not-exist/retry-evaluation")
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Real HTTP acceptance: an actual Agent server, an actual evaluator outage and
# an actual HTTP POST prove "the Agent is called exactly once" end to end.
# ---------------------------------------------------------------------------

def test_real_http_agent_is_called_exactly_once_across_an_evaluation_outage(
    setup_runtime, monkeypatch
):
    """End-to-end proof for the user-acceptance demo of Issue #84.

    Nothing about the Agent call is mocked: a real HTTP server stands in for
    the Agent and counts requests. The evaluator outage is produced by removing
    the persisted results, the retry goes through the real HTTP route, and the
    real worker re-judges the case from the stored output.
    """
    import json as json_mod
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls: list[dict[str, object]] = []

    class _AgentHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length") or 0)
            body = json_mod.loads(self.rfile.read(length) or b"{}")
            calls.append({"body": body, "traceparent": self.headers.get("traceparent")})
            payload = json_mod.dumps(
                {"intent": "refund", "answer": "已为您办理退款", "escalated": False}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("x-demo-traceparent-received", "true")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # silence the default stderr logging
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/invoke"

    try:
        db_mgr, queue, _limiter, orchestrator, worker, _rec = setup_runtime
        manifest = _manifest(_frozen_binding("intent_match"))
        manifest["agent"]["endpoint"] = endpoint

        launch = orchestrator.create_launch("test-agent", "v1", "ds", "v1", "issue84-http", manifest)
        orchestrator.start_launch(launch.id)
        msg = queue.read_group("issue84-http", count=1)[0]

        async def _run():
            with patch("app.worker.get_langfuse_client_safe", return_value=None):
                return await worker.execute_item_message(*msg)

        ok = asyncio.run(_run())
        assert ok is True
        # Exactly one real HTTP call happened.
        assert len(calls) == 1, calls
        assert calls[0]["traceparent"], "W3C traceparent must still propagate"

        item_id = msg[1]
        assert _count_execution_attempts(db_mgr, item_id) == 1
        with db_mgr.get_session() as session:
            it = session.get(Item, item_id)
            assert it.execution_status.lower() == "succeeded"
            assert it.eval_status.lower() == "succeeded"

        # --- the evaluator outage: results are gone and the case is UNKNOWN ---
        with db_mgr.get_session() as session:
            it = session.get(Item, item_id)
            it.eval_status = "failed"
            it.quality_conclusion = QUALITY_CONCLUSION_UNKNOWN
            it.quality_evaluation = None
            session.query(EvaluationResultRecord).filter(
                EvaluationResultRecord.item_execution_id == item_id
            ).delete()
            session.commit()

        client = _api_client(monkeypatch, db_mgr, queue)
        response = client.post(f"/api/v1/experiment-launches/{launch.id}/retry-evaluation")
        assert response.status_code == 200, response.text
        assert response.json()["submitted"] == [item_id]

        eval_msg = queue.read_group("issue84-http", count=1)[0]
        assert eval_msg[3] == "EVALUATION"

        async def _run_eval():
            with patch("app.worker.get_langfuse_client_safe", return_value=None):
                return await worker.execute_evaluation_message(eval_msg[0], eval_msg[1], eval_msg[2])

        outcome = asyncio.run(_run_eval())
        assert outcome is True

        # The Agent was NOT called again: still exactly one real HTTP request.
        assert len(calls) == 1, calls
        assert _count_execution_attempts(db_mgr, item_id) == 1

        item = _item(db_mgr, item_id)
        assert item.eval_status.lower() == "succeeded"
        assert item.quality_conclusion.lower() == QUALITY_CONCLUSION_PASS
        assert item.evaluation_status == EVALUATION_RECOVERED
        assert item.evaluation_reused_output_digest == canonical_output_digest(
            {"intent": "refund", "answer": "已为您办理退款", "escalated": False}
        )

        # A second submission is a no-op: the case is already recovered.
        again = client.post(f"/api/v1/experiment-launches/{launch.id}/retry-evaluation")
        assert again.status_code == 409, again.text
    finally:
        server.shutdown()
        server.server_close()
