"""Issue 198 S6.6: platform token Scores on Langfuse (item `tokens_total`, run `run_tokens_total`).

用户定稿口径：
- token 总数含 reasoning（`total_tokens`），不做货币换算；
- 分母 = total_cases，失败用例与重试 attempt 的 token 全部计入分子；
- 证据不完美时 fail-closed：无 token 证据不上报 Score（不写 0、不编造）。
"""
from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services" / "eval-runner")]

from app.costs import extract_usage_cost  # noqa: E402
from app.db_models import ExecutionAttemptRecord as Attempt  # noqa: E402
from app.db_models import ExperimentItemExecutionRecord as Item  # noqa: E402
from app.db_models import ExperimentLaunchRecord as Launch  # noqa: E402
from app.db_models import (  # noqa: E402
    LangfuseRunScoreTaskRecord,
    LangfuseSyncTaskRecord,
)
from app.langfuse_projection import stable_score_id  # noqa: E402
from app.langfuse_run_scores import LangfuseRunScoreSyncer  # noqa: E402
from app.langfuse_sync import LangfuseOutboxSyncer  # noqa: E402
from app.result_snapshots import create_result_snapshot  # noqa: E402
from fake_langfuse import FakeLangfuseSDK  # noqa: E402
from link_helpers import make_service  # noqa: E402
from test_pr17_review_regressions import create_launch_helper  # noqa: E402

TOKEN_MAPPING = {
    "source": "provider_reported",
    "input_tokens_path": "usage.prompt_tokens",
    "output_tokens_path": "usage.completion_tokens",
    "total_tokens_path": "usage.total_tokens",
}


def _case_tokens(case_id: str, prompt: int, completion: int, total: int) -> dict:
    return extract_usage_cost(
        {"usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}},
        TOKEN_MAPPING,
    )


def _usage_launch(
    db_mgr,
    *,
    usage_cases: list[Any],
    dataset_source: str = "langfuse",
) -> str:
    """A completed launch where case i carries usage_cases[i] attempt evidence (None = no evidence)."""
    launch_id = str(uuid.uuid4())
    manifest = {
        "schema_version": "1.1",
        "dataset": {
            "dataset_name": "golden",
            "dataset_id": "ds-golden",
            "dataset_version": "v1",
            "source": dataset_source,
            "snapshot_digest": "dataset-digest",
            "items": [
                {"id": f"case-{i}", "input": {"q": i}, "expected_output": {"a": i}, "metadata": {}}
                for i in range(len(usage_cases))
            ],
        },
        "agent": {"agent_id": "test-agent", "version": "v1", "spec_digest": "agent-digest"},
        "evaluators": [{"id": "correctness", "version": "1.0.0", "threshold": 0.8, "critical": True}],
        "quality_policy": {"mode": "all_selected_must_pass"},
        "runner": {"runner_version": "0.2.0", "build_id": "test"},
        "comparison": {"environment": "production", "comparison_policy_version": "comparison-v1"},
    }
    with db_mgr.get_session() as session:
        session.add(Launch(
            id=launch_id,
            name="token-launch",
            status="COMPLETED",
            quality_conclusion="pass",
            dataset_name="golden",
            dataset_id="ds-golden",
            dataset_version="v1",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            manifest=manifest,
            completed_at=datetime.now(UTC),
        ))
        session.flush()
        for i, usage_cost in enumerate(usage_cases):
            attempt_id = str(uuid.uuid4())
            item_id = str(uuid.uuid4())
            token_usage = None if usage_cost is None else {
                "input_tokens": usage_cost[0],
                "output_tokens": usage_cost[1],
                "total_tokens": usage_cost[2],
            }
            session.add(Attempt(
                id=attempt_id,
                item_execution_id=item_id,
                attempt_no=1,
                status="COMPLETED",
                dispatch_generation=1,
                usage_cost={"usage": token_usage, "cost": {"amount": None, "currency": None}}
                if token_usage else None,
            ))
            session.add(Item(
                id=item_id,
                launch_id=launch_id,
                dataset_item_id=f"case-{i}",
                execution_status="succeeded",
                eval_status="succeeded",
                quality_conclusion="pass",
                scores={"correctness": 1.0},
                final_attempt_id=attempt_id,
                dispatch_generation=1,
            ))
        session.commit()
    return launch_id


def _sync_one_item_task(db_mgr, launch_id: str, dataset_run_id: str = "dataset-run-1") -> None:
    """Persist the frozen item sync task whose payload carries the linked DatasetRun id."""
    with db_mgr.get_session() as session:
        item = session.query(Item).filter_by(launch_id=launch_id).first()
        session.add(LangfuseSyncTaskRecord(
            id=str(uuid.uuid4()),
            launch_id=launch_id,
            item_id=item.id,
            dataset_item_id=item.dataset_item_id,
            dataset_version="v1",
            dispatch_generation=item.dispatch_generation,
            task_type="FULL_EVAL_SYNC",
            trace_id="trace-1",
            dataset_run_name="token-run",
            scores_payload={"_dataset_run_id": dataset_run_id},
            status="SYNCED",
        ))
        session.commit()


# ---------- S6.6 item-level: worker outbox carries case-level usage ----------

def test_worker_outbox_payload_carries_summed_case_tokens(setup_runtime):
    db_mgr, _, _, _, worker, _ = setup_runtime
    launch_id, messages = create_launch_helper(setup_runtime, count=2)
    _, item_id, generation, _work = messages[0]

    claim = worker.claim_item(item_id, generation)
    assert claim is not None
    token = claim["lease_token"]
    # Retry history: attempt 1 already ran and recorded its usage before we authorize attempt 2.
    evidence_a = _case_tokens("a", 35, 15, 50)
    evidence_b = _case_tokens("b", 10, 5, 15)
    with db_mgr.get_session() as session:
        session.add(Attempt(
            id="hist-att-1",
            item_execution_id=item_id,
            attempt_no=1,
            status="FAILED",
            dispatch_generation=generation,
            lease_token="hist-token",
            usage_cost=evidence_a,
        ))
        session.commit()
    assert worker.authorize_attempt(item_id, token, generation) is not None
    with db_mgr.get_session() as session:
        item = session.get(Item, item_id)
        current_attempt_id = item.active_attempt_id

    assert worker.finalize_execution_and_attempt(
        item_id=item_id,
        generation=generation,
        lease_token=token,
        target_item_status="SUCCEEDED",
        target_eval_status="succeeded",
        target_quality_conclusion="pass",
        current_attempt_id=current_attempt_id,
        attempt_updates={"status": "COMPLETED", "usage_cost": evidence_b},
        scores={"accuracy": 1.0},
        trace_id="trace-token",
        launch_id=launch_id,
        dataset_item_id="0",
        dataset_version="2026-10-15T00:00:00+00:00",
        dataset_run_name="token-run",
        dataset_source="langfuse",
    ) is True

    with db_mgr.get_session() as session:
        task = session.query(LangfuseSyncTaskRecord).filter_by(item_id=item_id).one()
        assert task.scores_payload["_usage_tokens"] == {
            "input_tokens": 45,
            "output_tokens": 20,
            "total_tokens": 65,
        }


def test_worker_outbox_has_no_usage_key_without_token_evidence(setup_runtime):
    db_mgr, _, _, _, worker, _ = setup_runtime
    launch_id, messages = create_launch_helper(setup_runtime, count=1)
    _, item_id, generation, _work = messages[0]

    claim = worker.claim_item(item_id, generation)
    token = claim["lease_token"]
    assert worker.authorize_attempt(item_id, token, generation) is not None
    with db_mgr.get_session() as session:
        item = session.get(Item, item_id)
        current_attempt_id = item.active_attempt_id

    assert worker.finalize_execution_and_attempt(
        item_id=item_id,
        generation=generation,
        lease_token=token,
        target_item_status="SUCCEEDED",
        target_eval_status="succeeded",
        target_quality_conclusion="pass",
        current_attempt_id=current_attempt_id,
        attempt_updates={"status": "COMPLETED", "usage_cost": None},
        scores={"accuracy": 1.0},
        trace_id="trace-token",
        launch_id=launch_id,
        dataset_item_id="0",
        dataset_version="2026-10-15T00:00:00+00:00",
        dataset_run_name="token-run",
        dataset_source="langfuse",
    ) is True

    with db_mgr.get_session() as session:
        task = session.query(LangfuseSyncTaskRecord).filter_by(item_id=item_id).one()
        assert "_usage_tokens" not in task.scores_payload


# ---------- S6.6 item-level: outbox publishes item `tokens_total` score ----------

def _sync_outbox(db_mgr, launch_id: str) -> FakeLangfuseSDK:
    lf = FakeLangfuseSDK(dataset_ids={"ds": "ds-real"})
    service = make_service(db_mgr, lf, "https://cloud.langfuse.example.com")
    LangfuseOutboxSyncer(db_mgr, lf, link_service=service).process_batch()
    return lf


def test_outbox_publishes_item_tokens_total_score(setup_runtime):
    db_mgr, _, _, _, worker, _ = setup_runtime
    launch_id, messages = create_launch_helper(setup_runtime, count=1)
    _, item_id, generation, _work = messages[0]
    claim = worker.claim_item(item_id, generation)
    token = claim["lease_token"]
    assert worker.authorize_attempt(item_id, token, generation) is not None
    with db_mgr.get_session() as session:
        item = session.get(Item, item_id)
        current_attempt_id = item.active_attempt_id
    evidence = _case_tokens("a", 35, 15, 50)
    assert worker.finalize_execution_and_attempt(
        item_id=item_id,
        generation=generation,
        lease_token=token,
        target_item_status="SUCCEEDED",
        target_eval_status="succeeded",
        target_quality_conclusion="pass",
        current_attempt_id=current_attempt_id,
        attempt_updates={"status": "COMPLETED", "usage_cost": evidence},
        scores={"accuracy": 1.0},
        trace_id="trace-token",
        observation_id="obs-token",
        launch_id=launch_id,
        dataset_item_id="0",
        dataset_version="2026-10-15T00:00:00+00:00",
        dataset_run_name="token-run",
        dataset_source="langfuse",
    ) is True
    with db_mgr.get_session() as session:
        launch = session.get(Launch, launch_id)
        launch.dataset_name = "ds"
        launch.dataset_id = "ds-real"

    lf = _sync_outbox(db_mgr, launch_id)
    token_scores = [s for s in lf.scores.created if s.get("name") == "tokens_total"]
    assert len(token_scores) == 1
    score = token_scores[0]
    assert score["id"] == stable_score_id(
        item_id=item_id, generation=generation, evaluator_id="tokens_total"
    )
    assert score["value"] == 50.0
    assert score["trace_id"] == "trace-token"
    assert score["observation_id"] == "obs-token"
    assert score["metadata"]["input_tokens"] == 35
    assert score["metadata"]["output_tokens"] == 15


def test_outbox_without_usage_evidence_never_publishes_token_score(setup_runtime):
    db_mgr, _, _, _, worker, _ = setup_runtime
    launch_id, messages = create_launch_helper(setup_runtime, count=1)
    _, item_id, generation, _work = messages[0]
    claim = worker.claim_item(item_id, generation)
    token = claim["lease_token"]
    assert worker.authorize_attempt(item_id, token, generation) is not None
    with db_mgr.get_session() as session:
        item = session.get(Item, item_id)
        current_attempt_id = item.active_attempt_id
    assert worker.finalize_execution_and_attempt(
        item_id=item_id,
        generation=generation,
        lease_token=token,
        target_item_status="SUCCEEDED",
        target_eval_status="succeeded",
        target_quality_conclusion="pass",
        current_attempt_id=current_attempt_id,
        attempt_updates={"status": "COMPLETED", "usage_cost": None},
        scores={"accuracy": 1.0},
        trace_id="trace-token",
        launch_id=launch_id,
        dataset_item_id="0",
        dataset_version="2026-10-15T00:00:00+00:00",
        dataset_run_name="token-run",
        dataset_source="langfuse",
    ) is True
    with db_mgr.get_session() as session:
        launch = session.get(Launch, launch_id)
        launch.dataset_name = "ds"
        launch.dataset_id = "ds-real"

    lf = _sync_outbox(db_mgr, launch_id)
    assert all(s.get("name") != "tokens_total" for s in lf.scores.created)
    # Regular evaluator scores are unaffected.
    assert any(s.get("name") == "accuracy" for s in lf.scores.created)


# ---------- S6.6 run-level: run score payload + `argus_run_tokens_total` ----------

def test_run_score_payload_contains_run_tokens_total(setup_runtime):
    db_mgr = setup_runtime[0]
    usage = [(35, 15, 50), None, (145, 80, 225)]  # case-2 has no token evidence
    launch_id = _usage_launch(db_mgr, usage_cases=usage)
    with db_mgr.get_session() as session:
        launch = session.get(Launch, launch_id)
        snapshot = create_result_snapshot(session, launch)
        session.commit()
        snapshot_id = snapshot.id
    with db_mgr.get_session() as session:
        task = session.query(LangfuseRunScoreTaskRecord).one()
        assert task.snapshot_id == snapshot_id
        assert task.scores_payload.get("run_tokens_total") == 275
        assert "run_tokens_total" in task.scores_payload


def test_run_score_publishes_argus_run_tokens_total(setup_runtime):
    db_mgr = setup_runtime[0]
    usage = [(35, 15, 50), None, (145, 80, 225)]
    launch_id = _usage_launch(db_mgr, usage_cases=usage, dataset_source="langfuse")
    with db_mgr.get_session() as session:
        launch = session.get(Launch, launch_id)
        snapshot = create_result_snapshot(session, launch)
        session.commit()
    _sync_one_item_task(db_mgr, launch_id)

    lf = FakeLangfuseSDK()
    syncer = LangfuseRunScoreSyncer(db_mgr, lf)
    assert syncer.process_batch() == 1

    run_token_scores = [s for s in lf.scores.created if s.get("name") == "argus_run_tokens_total"]
    assert len(run_token_scores) == 1
    score = run_token_scores[0]
    assert score["value"] == 275.0
    assert score["dataset_run_id"] == "dataset-run-1"
    assert score["id"].startswith("argus-run-")
    assert score["metadata"]["argus_snapshot_id"] == snapshot.id


def test_run_score_skips_run_tokens_total_without_evidence(setup_runtime):
    db_mgr = setup_runtime[0]
    launch_id = _usage_launch(db_mgr, usage_cases=[None, None])
    with db_mgr.get_session() as session:
        launch = session.get(Launch, launch_id)
        create_result_snapshot(session, launch)
        session.commit()
    _sync_one_item_task(db_mgr, launch_id)

    lf = FakeLangfuseSDK()
    assert LangfuseRunScoreSyncer(db_mgr, lf).process_batch() == 1
    assert all(s.get("name") != "argus_run_tokens_total" for s in lf.scores.created)
