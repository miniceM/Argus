"""#212 Review 回归：H1/H2/M1/M2/M3 与 L1–L4 修复断言。

每个测试对应审查报告中的一项发现；sqlite 单文件可跑，PG 语义由 CI 覆盖。
"""
from __future__ import annotations

import contextlib
import threading
from types import SimpleNamespace

from app.db_models import EvaluationAttemptRecord, EvaluationResultRecord
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_issue_216_managed_scores import Remote
from test_issue_217_lifecycle_boundaries import _drain_collection_slots
from test_issue_217_managed_async import (
    _good_score,
    _item,
    _managed_manifest,
    _observation_for,
    _run_managed_http,
)


def _start(setup_runtime):
    db, queue, _, _, _, launch_id, item_id, calls, ok = _run_managed_http(
        setup_runtime, _managed_manifest(), {"answer": "ok"}
    )
    assert ok and calls == 1
    obs, trace_id, obs_id = _observation_for(db, item_id)
    return db, queue, launch_id, item_id, obs, _good_score(trace_id, obs_id)


def test_finalize_rereads_generation_before_publishing(setup_runtime, monkeypatch):
    """H1：终结计算窗口内代际被接管 → 拒绝发布，不覆盖新代际。"""
    from app import managed_quality
    from app.db_models import ExperimentItemExecutionRecord as Item
    from app.managed_score_recovery import recover_managed_scores

    db, _, _, item_id, obs, good = _start(setup_runtime)
    real_decide = managed_quality.managed_quality_decision
    def hijack(manifest, item, launch_id):
        with db.get_session() as session:  # 模拟 Reconciler 接管换代
            row = session.get(Item, item_id)
            row.evaluation_generation += 1
            row.evaluation_lease_token = None
            row.evaluation_lease_owner = None
        return real_decide(manifest, item, launch_id)

    monkeypatch.setattr(managed_quality, "managed_quality_decision", hijack)
    out = recover_managed_scores(
        db, item_id=item_id, evaluation_generation=1, worker_id="racer",
        client_factory=lambda: Remote(obs, [good]),
    )
    assert out["reason"] == "SUPERSEDED_OR_LEASE_LOST"
    final = _item(db, item_id)
    assert final.eval_status == "pending" and final.quality_conclusion == "unknown"
    assert final.evaluation_generation == 2  # 新代际未被覆盖


def test_timed_out_collect_releases_slot_for_next_round(monkeypatch):
    """H2：超时即归还槽位并计数抛弃；迟到线程退出不重复递减。"""
    from app import managed_score_recovery as recovery
    from app.langfuse_managed_scores import ManagedScoreCollector

    _drain_collection_slots()
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_CONCURRENCY", "1")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def collect(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:  # 仅首轮 hung 住，后续轮次立即返回
            entered.set()
            try:
                assert release.wait(10)
            finally:
                finished.set()
        return {"decisions": []}

    monkeypatch.setattr(ManagedScoreCollector, "collect", collect)
    abandoned_before = recovery._collection_abandoned_total
    try:
        first = recovery._collect_with_timeout(
            None, lambda: object(), "launch", "hung", fence={}, timeout_seconds=0.05,
        )
        assert entered.is_set() and first["decisions"][0]["reason"] == "TIMEOUT"
        assert recovery._collection_inflight == 0
        assert recovery._collection_abandoned_total == abandoned_before + 1
        release.set()
        assert finished.wait(10)
        second = recovery._collect_with_timeout(
            None, lambda: object(), "launch", "next", fence={}, timeout_seconds=5,
        )
        assert second == {"decisions": []} and len(calls) == 2
    finally:
        release.set()
        assert finished.wait(10)
    assert recovery._collection_inflight == 0


class _RaceSession:
    """复现并发落败：首次 select 未见行，insert 撞主键，重查见胜者。"""

    def __init__(self, winner):
        self.winner = winner
        self.selects = 0
        self.expired = False

    def add(self, row):
        pass

    def flush(self):
        raise IntegrityError("INSERT INTO evaluation_results", {}, Exception("duplicate key"))

    @contextlib.contextmanager
    def begin_nested(self):
        yield self

    def scalar(self, stmt):
        self.selects += 1
        return None if self.selects == 1 else self.winner

    def expire_all(self):
        self.expired = True


def _race_ctx_and_result(score_id="s-race"):
    ctx = {"item_execution_id": "item-race", "launch_id": "launch-race", "dispatch_generation": 1}
    score = {"id": score_id, "value": True}
    result = SimpleNamespace(
        evaluator_id="correctness", result_type="boolean", status="succeeded",
        value=True, normalized_value=True, evidence={"score": dict(score)},
        provenance=SimpleNamespace(
            binding_id="b", definition_digest="d",
            manifest_schema_version="1.0", contract_status="MANAGED_SCORE_VERIFIED",
        ),
    )
    return ctx, {"project_id": "project-race"}, result, score


def test_insert_conflict_resolves_already_adopted():
    """M2：并发同证据落败 → ALREADY_ADOPTED，而非 SCORE_ALREADY_CLAIMED。"""
    from app.langfuse_managed_scores import ManagedScoreCollector

    ctx, contract, result, score = _race_ctx_and_result()
    winner = SimpleNamespace(
        executor_type="langfuse_managed", evidence={**ctx, "score": dict(score)},
    )
    decision, reason = ManagedScoreCollector.__new__(ManagedScoreCollector)._insert_result(
        _RaceSession(winner), ctx, contract, result,
    )
    assert (decision, reason) == ("ALREADY_ADOPTED", "VERIFIED")


def test_insert_conflict_resolves_result_conflict():
    """M2：并发同名异值落败 → RESULT_CONFLICT，不覆盖。"""
    from app.langfuse_managed_scores import ManagedScoreCollector

    ctx, contract, result, _ = _race_ctx_and_result()
    winner = SimpleNamespace(
        executor_type="langfuse_managed",
        evidence={**ctx, "score": {"id": "s-other", "value": False}},
    )
    decision, reason = ManagedScoreCollector.__new__(ManagedScoreCollector)._insert_result(
        _RaceSession(winner), ctx, contract, result,
    )
    assert (decision, reason) == ("REJECTED", "RESULT_CONFLICT")


def test_insert_conflict_without_winner_keeps_claimed():
    """M2：同 (item,name) 查无但外部键冲突 → 仍为 SCORE_ALREADY_CLAIMED。"""
    from app.langfuse_managed_scores import ManagedScoreCollector

    ctx, contract, result, _ = _race_ctx_and_result()
    decision, reason = ManagedScoreCollector.__new__(ManagedScoreCollector)._insert_result(
        _RaceSession(None), ctx, contract, result,
    )
    assert (decision, reason) == ("REJECTED", "SCORE_ALREADY_CLAIMED")


def test_concurrent_duplicate_collect_keeps_single_result(setup_runtime):
    """M1/M2：8 路并发重复采纳不抛异常、不产生重复行。"""
    from concurrent.futures import ThreadPoolExecutor

    from app.langfuse_managed_scores import ManagedScoreCollector

    db, _, launch_id, item_id, obs, good = _start(setup_runtime)

    def one(_):
        return ManagedScoreCollector(db, Remote(obs, [good])).collect(launch_id, item_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        reports = list(pool.map(one, range(8)))
    assert all(r["decisions"][0]["decision"] in ("ADOPTED", "ALREADY_ADOPTED") for r in reports)
    with db.get_session() as session:
        rows = list(
            session.scalars(
                select(EvaluationResultRecord).where(
                    EvaluationResultRecord.item_execution_id == item_id
                )
            )
        )
    assert len(rows) == 1


def test_concurrent_submit_bumps_generation_exactly_once(setup_runtime):
    """M3：并发双提交仅一路胜出，代际 +1 且只入队一条 MANAGED 消息。"""
    from concurrent.futures import ThreadPoolExecutor

    from app.db_models import ExperimentItemExecutionRecord as Item
    from app.managed_score_recovery import WORK_TYPE_MANAGED, submit_managed_collection

    db, queue, launch_id, item_id, _, _ = _start(setup_runtime)
    with db.get_session() as session:
        row = session.get(Item, item_id)
        row.evaluation_status = "none"
        row.evaluation_generation = 5
        row.evaluation_started_at = None
    queue._queue.clear()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outs = list(pool.map(lambda _: submit_managed_collection(db, queue, item_id=item_id), range(2)))
    by_enqueued = sorted(outs, key=lambda o: 0 if (o or {}).get("enqueued") else 1)
    assert by_enqueued[0]["enqueued"] is True and by_enqueued[0]["evaluation_generation"] == 6
    assert by_enqueued[1] == {"already_running": True, "evaluation_generation": 6}
    managed = [m for m in queue._queue if m[3] == WORK_TYPE_MANAGED]
    assert len(managed) == 1 and managed[0][2] == 6
    with db.get_session() as session:
        assert session.get(Item, item_id).evaluation_generation == 6


def test_stale_attempt_revival_is_fenced_by_token(setup_runtime):
    """L1：IntegrityError 回退改写必须携带租约围栏，已结算审计不得复活。"""
    from datetime import UTC, datetime, timedelta

    from app.db_models import ExperimentItemExecutionRecord as Item
    from app.managed_score_recovery import _record_managed_attempt_started

    db, _, launch_id, item_id, _, _ = _start(setup_runtime)
    with db.get_session() as session:
        item = session.get(Item, item_id)
        item.evaluation_status = "evaluating"
        item.evaluation_lease_owner = "winner"
        item.evaluation_lease_token = "token-T"
        item.evaluation_lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
    assert (
        _record_managed_attempt_started(
            db, item_id=item_id, launch_id=launch_id,
            evaluation_generation=1, target_bindings=["correctness"],
            reused_output_digest="x", worker_id="w1", lease_token="token-T",
        )
        is True
    )
    with db.get_session() as session:  # 胜者已结算
        row = session.scalars(
            select(EvaluationAttemptRecord).where(
                EvaluationAttemptRecord.item_execution_id == item_id
            )
        ).one()
        row.status = "succeeded"
    # 迟到旧进程同 token 重记 → 回退 UPDATE 被 status/lease 围栏拒绝。
    assert (
        _record_managed_attempt_started(
            db, item_id=item_id, launch_id=launch_id,
            evaluation_generation=1, target_bindings=["correctness"],
            reused_output_digest="x", worker_id="stale", lease_token="token-T",
        )
        is False
    )
    with db.get_session() as session:
        rows = list(
            session.scalars(
                select(EvaluationAttemptRecord).where(
                    EvaluationAttemptRecord.item_execution_id == item_id
                )
            )
        )
    assert len(rows) == 1 and rows[0].status == "succeeded"


def test_malformed_managed_manifest_never_raises_in_aggregation():
    """L4：畸形托管 manifest 走 UNKNOWN 通道，不抛 KeyError/TypeError。"""
    from app.aggregation import aggregate_run

    cases = [
        {"evaluation_source": "langfuse_managed"},
        {"evaluation_source": "langfuse_managed", "managed_evaluation": None},
        {"evaluation_source": "langfuse_managed", "managed_evaluation": {"scores": [None, {"name": 1}]}},
        {"evaluation_source": "langfuse_managed", "managed_evaluation": {"scores": []}, "quality_policy": "broken"},
    ]
    for manifest in cases:
        out = aggregate_run([], [], manifest=manifest)
        assert out["total_cases"] == 0 and out["pass_rate"] is None
