"""PR #224 Review 回复：P1-1~P1-4 与 P2 的回归测试（RED 先行）。

覆盖：旧租约/旧代际收集隔离、本轮冲突阻塞终结、取消与崩溃恢复、
人工重提新预算、配置清洗、客户端关闭、独立限流、只读语义。
"""

from __future__ import annotations

import asyncio
import copy
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/eval-runner"))

from app.db_models import (  # noqa: E402
    EvaluationResultRecord,  # noqa: E402
    ManagedScoreEvidenceRecord,  # noqa: E402
)
from app.db_models import ExperimentItemExecutionRecord as Item  # noqa: E402
from app.evaluation_recovery import claim_evaluation  # noqa: E402
from app.managed_score_recovery import (  # noqa: E402
    WORK_TYPE_MANAGED,
    _fail_managed,
    recover_managed_scores,
)
from test_issue_216_managed_scores import Remote  # noqa: E402
from test_issue_217_managed_async import (  # noqa: E402
    _good_score,
    _item,
    _make_collection_due,
    _managed_manifest,
    _observation_for,
    _run_managed_http,
)


def _results(db_mgr, item_id):
    with db_mgr.get_session() as session:
        return list(
            session.scalars(
                select(EvaluationResultRecord).where(
                    EvaluationResultRecord.item_execution_id == item_id
                )
            )
        )


def _expire_lease(db_mgr, item_id, seconds_ago=5):
    with db_mgr.get_session() as session:
        row = session.get(Item, item_id)
        row.evaluation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
        session.commit()
        return row.evaluation_lease_token


def test_stale_lease_collect_writes_no_valid_result(setup_runtime, monkeypatch):
    """P1-1：旧 Worker 远程返回后租约已易主，collect 不得写入有效结果。"""
    from app.langfuse_managed_scores import ManagedScoreCollector

    manifest = _managed_manifest()
    db_mgr, _, _, _, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    item = _item(db_mgr, item_id)
    gen = item.evaluation_generation
    claim_a = claim_evaluation(
        db_mgr, item_id=item_id, evaluation_generation=gen, worker_id="old"
    )
    assert claim_a is not None
    token_a = claim_a["lease_token"]
    _expire_lease(db_mgr, item_id)
    claim_b = claim_evaluation(
        db_mgr, item_id=item_id, evaluation_generation=gen, worker_id="new"
    )
    assert claim_b is not None and claim_b["lease_token"] != token_a
    good = _good_score(trace_id, obs_id, id="stale1")
    report = ManagedScoreCollector(db_mgr, Remote(obs, [good])).collect(
        launch_id,
        item_id,
        fence={
            "evaluation_generation": gen,
            "lease_token": token_a,
            "dispatch_generation": item.dispatch_generation,
        },
    )
    assert report["decisions"]
    assert all(d["decision"] == "REJECTED" for d in report["decisions"])
    assert report["decisions"][0]["reason"] == "LEASE_LOST"
    assert _results(db_mgr, item_id) == []
    with db_mgr.get_session() as session:
        assert not list(
            session.scalars(
                select(ManagedScoreEvidenceRecord).where(
                    ManagedScoreEvidenceRecord.item_execution_id == item_id,
                    ManagedScoreEvidenceRecord.decision == "ADOPTED",
                )
            )
        )


def test_superseded_generation_collect_writes_nothing(setup_runtime):
    """P1-1：代际已推进后，旧代 fence 的收集不得写入。"""
    from app.langfuse_managed_scores import ManagedScoreCollector

    manifest = _managed_manifest()
    db_mgr, _, _, _, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    item = _item(db_mgr, item_id)
    gen = item.evaluation_generation
    claim_a = claim_evaluation(
        db_mgr, item_id=item_id, evaluation_generation=gen, worker_id="old"
    )
    with db_mgr.get_session() as session:
        row = session.get(Item, item_id)
        row.evaluation_generation = gen + 1
        row.evaluation_lease_owner = None
        row.evaluation_lease_token = None
        row.evaluation_lease_expires_at = None
        session.commit()
    good = _good_score(trace_id, obs_id, id="stale2")
    report = ManagedScoreCollector(db_mgr, Remote(obs, [good])).collect(
        launch_id,
        item_id,
        fence={
            "evaluation_generation": gen,
            "lease_token": claim_a["lease_token"],
            "dispatch_generation": item.dispatch_generation,
        },
    )
    assert all(d["decision"] == "REJECTED" for d in report["decisions"])
    assert report["decisions"][0]["reason"] == "SUPERSEDED_GENERATION"
    assert _results(db_mgr, item_id) == []


def test_adopt_then_conflict_blocks_finalize_and_recovers(setup_runtime, monkeypatch):
    """P1-2：先采纳 A，后出现合法同名 A+B，本轮不得 PASS；消歧后可恢复。"""
    from app.langfuse_managed_scores import ManagedScoreCollector

    manifest = _managed_manifest()
    db_mgr, _, _, _, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    score_a = _good_score(trace_id, obs_id, id="p1a")
    first = ManagedScoreCollector(db_mgr, Remote(obs, [score_a])).collect(
        launch_id, item_id
    )
    assert [d["decision"] for d in first["decisions"]] == ["ADOPTED"]
    assert len(_results(db_mgr, item_id)) == 1
    score_b = _good_score(trace_id, obs_id, id="p1b", metadata={
        "evaluation_rule_id": "rule-215", "evaluator_id": "judge-215",
        "job_execution_id": "job217-b", "target_trace_id": trace_id,
        "target_observation_id": obs_id,
    })
    item = _item(db_mgr, item_id)
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Remote(obs, [copy.deepcopy(score_a), copy.deepcopy(score_b)]),
    )
    outcome = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    assert outcome["finalized"] is False
    assert outcome["reason"] in ("SCORE_AMBIGUOUS", "RETRY_SCHEDULED")
    mid = _item(db_mgr, item_id)
    assert mid.eval_status == "pending" and mid.quality_conclusion == "unknown"
    assert mid.evaluation_status == "evaluating"
    assert len(_results(db_mgr, item_id)) == 1
    # 外部消歧（B 撤回）后，同一 A 幂等重读收敛，不调 Agent。
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Remote(obs, [copy.deepcopy(score_a)]),
    )
    item2 = _make_collection_due(db_mgr, item_id)
    with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as mock:
        mock.side_effect = AssertionError("must not call agent")
        outcome2 = recover_managed_scores(
            db_mgr, item_id=item_id,
            evaluation_generation=item2.evaluation_generation, worker_id="w1",
        )
    assert outcome2["finalized"] is True
    done = _item(db_mgr, item_id)
    assert done.eval_status == "succeeded" and done.quality_conclusion != "unknown"


def test_round_cleanly_adopted_helper():
    from app.managed_score_recovery import _round_cleanly_adopted

    assert _round_cleanly_adopted(
        {"decisions": [{"score_name": "correctness", "decision": "ADOPTED"}]},
        ["correctness"],
    ) is True
    assert _round_cleanly_adopted(
        {"decisions": [{"score_name": "correctness", "decision": "ALREADY_ADOPTED"}]},
        ["correctness"],
    ) is True
    assert _round_cleanly_adopted(
        {"decisions": [{"score_name": "correctness", "decision": "REJECTED",
                        "reason": "SCORE_AMBIGUOUS"}]},
        ["correctness"],
    ) is False
    assert _round_cleanly_adopted({"decisions": []}, ["correctness"]) is False


def test_idempotent_reread_same_score(setup_runtime):
    """P1-2：同一原始 Score 重复返回保持幂等，可正常终结。"""
    from app.langfuse_managed_scores import ManagedScoreCollector

    manifest = _managed_manifest()
    db_mgr, _, _, _, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    good = _good_score(trace_id, obs_id, id="idem1")
    first = ManagedScoreCollector(db_mgr, Remote(obs, [good])).collect(launch_id, item_id)
    second = ManagedScoreCollector(db_mgr, Remote(obs, [copy.deepcopy(good)])).collect(
        launch_id, item_id
    )
    assert first["decisions"][0]["decision"] == "ADOPTED"
    assert second["decisions"][0]["decision"] == "ALREADY_ADOPTED"
    assert len(_results(db_mgr, item_id)) == 1


def test_stored_complete_but_round_read_failure_not_complete(setup_runtime, monkeypatch):
    """P1-2：库里已有 A，但本轮读取失败，不得误判完整而终结。"""
    from app.langfuse_managed_scores import ManagedScoreCollector
    from app.langfuse_scores_client import ScoresReadError

    manifest = _managed_manifest()
    db_mgr, _, _, _, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    good = _good_score(trace_id, obs_id, id="stored1")
    assert ManagedScoreCollector(db_mgr, Remote(obs, [good])).collect(
        launch_id, item_id
    )["decisions"][0]["decision"] == "ADOPTED"

    class Flaky(Remote):
        def scores_for(self, *a):
            raise ScoresReadError("UPSTREAM_UNAVAILABLE")

    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client", lambda: Flaky(obs, [])
    )
    item = _item(db_mgr, item_id)
    outcome = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    assert outcome["finalized"] is False
    assert outcome["reason"] in ("UPSTREAM_UNAVAILABLE", "RETRY_SCHEDULED")
    assert _item(db_mgr, item_id).evaluation_status == "evaluating"


def test_cancel_during_fetch_never_succeeds(setup_runtime, monkeypatch):
    """P1-3：远程查询期间取消，迟到结果不得推进成功。"""
    from app.db_models import ExperimentLaunchRecord

    manifest = _managed_manifest()
    db_mgr, _, _, _, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    good = _good_score(trace_id, obs_id, id="cancel1")

    class Cancelling(Remote):
        def scores_for(self, *a):
            with db_mgr.get_session() as session:
                launch = session.get(ExperimentLaunchRecord, launch_id)
                launch.cancel_requested_at = datetime.now(UTC)
                session.commit()
            return super().scores_for(*a)

    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Cancelling(obs, [good]),
    )
    item = _item(db_mgr, item_id)
    outcome = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    # 取消归属终态：失败落定、绝不成功，质量保持 UNKNOWN。
    assert outcome["finalized"] is True
    assert outcome["reason"] == "EVALUATION_CANCELLED"
    final = _item(db_mgr, item_id)
    assert final.eval_status == "failed"
    assert final.evaluation_status == "failed"
    assert final.eval_status != "succeeded"
    assert final.quality_conclusion == "unknown"
    assert _results(db_mgr, item_id) == []


def test_expired_lease_fail_discards_without_marking(setup_runtime):
    """P1-3：租约已过期才走终态写入，必须丢弃且不改当前结果。"""
    manifest = _managed_manifest()
    db_mgr, _, _, _, _, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    _observation_for(db_mgr, item_id)
    item = _item(db_mgr, item_id)
    claim = claim_evaluation(
        db_mgr, item_id=item_id, evaluation_generation=item.evaluation_generation,
        worker_id="old",
    )
    _expire_lease(db_mgr, item_id)
    outcome = _fail_managed(
        db_mgr, claim, error_code="UPSTREAM_UNAVAILABLE",
        error_message="late failure", launch_id=item.launch_id,
    )
    assert outcome["reason"] == "SUPERSEDED_OR_LEASE_LOST"
    assert outcome["finalized"] is False
    current = _item(db_mgr, item_id)
    assert current.evaluation_status == "evaluating"
    assert current.eval_status == "pending"


def test_finalize_crash_gap_compensated_by_reconciler(setup_runtime, monkeypatch):
    """P1-3：HTTP 已提交、Managed 未入队即崩溃，Reconciler 补偿提交。"""
    from app.executor import SingleInvocationResult
    from app.reconciler import ExecutionReconciler

    manifest = _managed_manifest()
    db_mgr, queue, _, orchestrator, worker, _rec = setup_runtime
    launch = orchestrator.create_launch(
        "test-agent", "v1", "ds", "v1",
        f"m224-crash-{datetime.now(UTC).microsecond}", manifest,
    )
    orchestrator.start_launch(launch.id)
    msg = queue.read_group("m224crash", count=1)[0]

    async def _run():
        with patch("app.worker.get_langfuse_client_safe", return_value=None):
            with patch(
                "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock
            ) as mock:
                mock.return_value = SingleInvocationResult(
                    status_code=200, body={"answer": "ok"}, raw_response="{}",
                    headers={}, duration_ms=3, trace_context_received=True,
                    error_category=None, error_message=None,
                    is_retryable=False, may_have_side_effects=False,
                )
                with patch(
                    "app.managed_score_recovery.submit_managed_collection",
                    side_effect=RuntimeError("crash before enqueue"),
                ):
                    with pytest.raises(RuntimeError, match="crash before enqueue"):
                        await worker.execute_item_message(*msg)
                return mock.await_count, launch.id, msg[1]

    calls, launch_id, item_id = asyncio.run(_run())
    assert calls == 1
    orphan = _item(db_mgr, item_id)
    assert orphan.execution_status == "succeeded"
    assert orphan.eval_status == "pending"
    assert orphan.evaluation_status == "none"
    rec = ExecutionReconciler(db_mgr, queue, setup_runtime[2])
    assert rec.reconcile_managed_submissions() == 1
    submitted = _item(db_mgr, item_id)
    assert submitted.evaluation_status == "evaluating"
    msgs = queue.read_group("m224crash2", count=10)
    managed = [m for m in msgs if m[1] == item_id and m[3] == WORK_TYPE_MANAGED]
    assert managed
    # 补偿后自动收敛，不再调 Agent。
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    good = _good_score(trace_id, obs_id, id="crashgap1")
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Remote(obs, [good]),
    )
    with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as mock:
        mock.side_effect = AssertionError("must not call agent")
        ok = asyncio.run(
            worker.execute_managed_message(managed[0][0], managed[0][1], managed[0][2])
        )
    assert ok is True
    done = _item(db_mgr, item_id)
    assert done.eval_status == "succeeded" and done.evaluation_status == "recovered"


def test_cancelled_launch_expired_lease_not_requeued(setup_runtime):
    """P1-3：已取消 Launch 的过期 Managed 租约不得重新入队。"""
    from app.db_models import ExperimentLaunchRecord
    from app.reconciler import ExecutionReconciler

    manifest = _managed_manifest()
    db_mgr, queue, _, _, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    _observation_for(db_mgr, item_id)
    item = _item(db_mgr, item_id)
    claim_evaluation(
        db_mgr, item_id=item_id, evaluation_generation=item.evaluation_generation,
        worker_id="old",
    )
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        launch.cancel_requested_at = datetime.now(UTC)
        row = session.get(Item, item_id)
        row.evaluation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        session.commit()
    queue.read_group("drain-cancelled", count=100)
    rec = ExecutionReconciler(db_mgr, queue, setup_runtime[2])
    rec.reconcile_expired_evaluation_leases()
    msgs = queue.read_group("probe-cancelled", count=100)
    assert [m for m in msgs if m[1] == item_id and m[3] == WORK_TYPE_MANAGED] == []
    final = _item(db_mgr, item_id)
    assert final.eval_status != "succeeded"


def test_duplicate_redelivery_single_effective_run(setup_runtime, monkeypatch):
    """P1-3：同一代际重复投递只有一次有效执行，不产生重复有效分。"""
    manifest = _managed_manifest()
    db_mgr, _, _, _, _, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    good = _good_score(trace_id, obs_id, id="dup1")
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Remote(obs, [copy.deepcopy(good)]),
    )
    item = _item(db_mgr, item_id)
    first = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    assert first["finalized"] is True
    second = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w2",
    )
    assert second["claimed"] is False
    assert len(_results(db_mgr, item_id)) == 1


def test_manual_resubmit_gets_fresh_budget(setup_runtime, monkeypatch):
    """P1-4：耗尽后人工修复并重提，获得新重试窗口；旧代迟到被隔离。"""
    manifest = _managed_manifest()
    db_mgr, queue, _, orchestrator, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client", lambda: Remote(obs, [])
    )
    item = _item(db_mgr, item_id)
    old_gen = item.evaluation_generation
    assert recover_managed_scores(
        db_mgr, item_id=item_id, evaluation_generation=old_gen, worker_id="w1"
    )["finalized"] is False
    item2 = _make_collection_due(db_mgr, item_id)
    exhausted = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item2.evaluation_generation, worker_id="w1",
    )
    assert exhausted["finalized"] is True and exhausted.get("exhausted") is True
    assert _item(db_mgr, item_id).eval_status == "failed"
    # 外部仍缺分时人工重提：新周期必须重置开始时间并给出新窗口。
    old_started = item2.evaluation_started_at
    resub = orchestrator.retry_failed_evaluations(launch_id)
    assert item_id in resub["submitted"]
    fresh = _item(db_mgr, item_id)
    assert fresh.evaluation_generation > old_gen
    assert fresh.eval_status == "pending"
    assert fresh.evaluation_started_at is not None
    assert old_started is not None
    assert fresh.evaluation_started_at > old_started
    # 新周期内仍缺分：应调度重试，而非立即再次耗尽。
    item3 = _item(db_mgr, item_id)
    again = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item3.evaluation_generation, worker_id="w2",
    )
    assert again["finalized"] is False
    assert again["reason"] == "RETRY_SCHEDULED"
    assert _item(db_mgr, item_id).eval_status == "pending"
    # Langfuse 出分后自动收敛，不调 Agent。
    good = _good_score(trace_id, obs_id, id="fresh1")
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Remote(obs, [copy.deepcopy(good)]),
    )
    item4 = _make_collection_due(db_mgr, item_id)
    with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as mock:
        mock.side_effect = AssertionError("must not call agent")
        out = recover_managed_scores(
            db_mgr, item_id=item_id,
            evaluation_generation=item4.evaluation_generation, worker_id="w2",
        )
    assert out["finalized"] is True
    done = _item(db_mgr, item_id)
    assert done.eval_status == "succeeded"
    # 旧代迟到调用不能认领、不能覆盖。
    late = recover_managed_scores(
        db_mgr, item_id=item_id, evaluation_generation=old_gen, worker_id="w1"
    )
    assert late["claimed"] is False
    assert _item(db_mgr, item_id).eval_status == "succeeded"


def test_retry_config_sanitizes_invalid_values(monkeypatch):
    """P2：非法/负数/NaN/Infinity/极端重试配置回退到安全默认。"""
    from app.managed_score_recovery import get_retry_config

    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "not-a-number")
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_BASE_DELAY_SECONDS", "-5")
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_DELAY_SECONDS", "nan")
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_DEADLINE_SECONDS", "inf")
    cfg = get_retry_config()
    assert cfg == {
        "max_attempts": 10, "base_delay": 5.0,
        "max_delay": 300.0, "deadline": 1800.0,
    }
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "0")
    assert get_retry_config()["max_attempts"] == 10
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "3")
    assert get_retry_config()["max_attempts"] == 3
    # 上界 100：deadline 下更大值形同无界，会 hammer 远端。
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "1000000")
    assert get_retry_config()["max_attempts"] == 100


def test_score_client_closed_and_managed_slots_separate(setup_runtime, monkeypatch):
    """P2：收集结束关闭自建客户端；Managed 不占用 HTTP 执行槽。"""
    from app.langfuse_scores_client import ScoresV3Client

    manifest = _managed_manifest()
    db_mgr, _, _, _, worker, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    assert hasattr(worker, "managed_concurrency")
    assert worker.managed_concurrency is not worker.local_concurrency
    _observation_for(db_mgr, item_id)
    client = MagicMock(spec=ScoresV3Client)
    client.observations.return_value = []
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client", lambda: client
    )
    item = _item(db_mgr, item_id)
    recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    client.close.assert_called_once_with()


def test_collection_only_reads_never_creates_remote_jobs(setup_runtime, monkeypatch):
    """语义：自动轮询只做 Score Collection（GET），不创建远端评测任务。"""
    from app.langfuse_scores_client import ScoresV3Client

    assert not any(
        hasattr(ScoresV3Client, name)
        for name in ("post", "put", "delete", "create_job", "trigger_evaluation")
    )
    manifest = _managed_manifest()
    db_mgr, _, _, _, _, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    remote = Remote(obs, [_good_score(trace_id, obs_id, id="readonly1")])
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client", lambda: remote
    )
    item = _item(db_mgr, item_id)
    assert recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )["finalized"] is True
    assert remote.calls
    assert all(kind in ("observations", "scores") for kind, *_ in remote.calls)


def test_hanging_remote_bounded_by_collect_timeout(setup_runtime, monkeypatch):
    """P2：远端 hung 住时，单次收集有总预算，超时转可重试。"""
    manifest = _managed_manifest()
    db_mgr, _, _, _, _, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, _, _ = _observation_for(db_mgr, item_id)

    class Hanging(Remote):
        def scores_for(self, *a):
            time.sleep(25)
            return []

    monkeypatch.setenv("ARGUS_MANAGED_SCORE_COLLECT_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client", lambda: Hanging(obs, [])
    )
    item = _item(db_mgr, item_id)
    started = time.monotonic()
    outcome = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    elapsed = time.monotonic() - started
    assert outcome["finalized"] is False
    assert outcome["reason"] in ("TIMEOUT", "RETRY_SCHEDULED")
    assert elapsed < 10
    assert _item(db_mgr, item_id).evaluation_status == "evaluating"


def test_fresh_cycle_backoff_starts_small(setup_runtime, monkeypatch):
    """BLOCKER-1：人工重提的新周期退避从 5s 起，而非沿用单调代际封顶 300s。"""
    manifest = _managed_manifest()
    db_mgr, _, _, orchestrator, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, _, _ = _observation_for(db_mgr, item_id)
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client", lambda: Remote(obs, [])
    )
    item = _item(db_mgr, item_id)
    assert recover_managed_scores(
        db_mgr, item_id=item_id, evaluation_generation=item.evaluation_generation,
        worker_id="w1",
    )["finalized"] is False
    item2 = _make_collection_due(db_mgr, item_id)
    exhausted = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item2.evaluation_generation, worker_id="w1",
    )
    assert exhausted["finalized"] is True and exhausted.get("exhausted") is True
    resub = orchestrator.retry_failed_evaluations(launch_id)
    assert item_id in resub["submitted"]
    item3 = _item(db_mgr, item_id)
    before = datetime.now(UTC)
    again = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item3.evaluation_generation, worker_id="w2",
    )
    assert again["finalized"] is False
    assert again["reason"] == "RETRY_SCHEDULED"
    scheduled = _item(db_mgr, item_id).available_at
    assert scheduled is not None
    delay = (scheduled.replace(tzinfo=UTC) - before).total_seconds()
    assert delay <= 10, f"fresh cycle must back off small, got {delay:.1f}s"


def test_finalize_rejects_diverged_report(setup_runtime, monkeypatch):
    """BLOCKER-3：collect 与 finalize 间隙库行偏离报告，终结必须拒绝而非 PASS。"""
    from app.evaluation_recovery import claim_evaluation
    from app.langfuse_managed_scores import ManagedScoreCollector
    from app.managed_score_recovery import (
        _finalize_success,
        _round_cleanly_adopted,
        required_score_names,
    )

    manifest = _managed_manifest()
    db_mgr, _, _, _, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    good = _good_score(trace_id, obs_id, id="p1a")
    report = ManagedScoreCollector(
        db_mgr, Remote(obs, [copy.deepcopy(good)])
    ).collect(launch_id, item_id)
    required = required_score_names(manifest)
    assert _round_cleanly_adopted(report, required) is True
    # 间隙：库行被带外改写为报告未背书的身份（同名不同源）。
    with db_mgr.get_session() as session:
        row = session.scalars(
            select(EvaluationResultRecord).where(
                EvaluationResultRecord.item_execution_id == item_id
            )
        ).one()
        row.external_score_id = "p1x-injected"
        evidence = dict(row.evidence or {})
        score = dict(evidence.get("score") or {})
        score["id"] = "p1x-injected"
        evidence["score"] = score
        row.evidence = evidence
        session.commit()
    item = _item(db_mgr, item_id)
    claim = claim_evaluation(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w9",
    )
    assert claim is not None
    out = _finalize_success(db_mgr, claim, launch_id=launch_id, report=report, required=required)
    assert out["finalized"] is False
    assert out["reason"] == "COMPLETION_RACE"
    assert _item(db_mgr, item_id).eval_status != "succeeded"
