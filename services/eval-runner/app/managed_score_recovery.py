"""#217：Langfuse 托管评分异步等待、可靠恢复与执行/评测解耦。

复用 #84 的评测租约/代际/CAS、#84 的 Checkpoint、#216 的 Score Collector、
现有 Queue work_type 与 Reconciler 扫描，不新建通用工作流引擎，不替
Langfuse 实现 Evaluator Queue/Worker，不修改 HTTP Agent 协议，不重构
Release Gate。

状态约定：
- HTTP 成功后 ``execution_status=succeeded``，``eval_status=pending``，
  ``quality_conclusion=unknown``，``evaluation_status=evaluating``。
  Agent HTTP 并发 permit 在 HTTP 结束后立即释放，评分等待永不持有。
- 必选 Score 齐全并通过采纳契约后 → ``eval_status=succeeded``，
  ``evaluation_status=recovered``，质量由冻结 QualityPolicy 判定。
- 缺分/429/5xx/超时等可重试 → 保持 pending，有界退避，复用原 Agent
  Output，不再请求 Agent。
- 认证/契约/Checkpoint/取消等终态或超限/超时 → ``eval_status=failed``，
  ``evaluation_status=failed``，质量保持 UNKNOWN，不伪造分数。
- 重试通过 ``evaluation_generation`` 递增 + ``available_at`` 作为下次
  可执行时间（复用现有列，不新增迁移），由 Reconciler 到期后入队；
  旧 generation、旧 dispatch、租约丢失、取消均经 CAS 丢弃，不覆盖当前结果。
- 重试预算按人工收集周期统计（``EvaluationAttemptRecord.started_at`` 落在
  本周期起点之后的行数），与单调代际解耦；人工重提开启新周期并重置
  起点与 Deadline，旧周期迟到任务仍被代际围栏隔离。
- 后台收集携带租约围栏（generation + lease token + dispatch）：远端
  查询耗时期间失租/换代/取消，迟到写入一律无效，只保留拒绝审计。
- 终结要求已存成功行 **且** 本轮无歧义采纳：同名冲突、来源冲突、
  本轮读失败均阻塞 PASS，保持 UNKNOWN/重试；历史快照不可变。
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .db import DatabaseManager
from .db_models import EvaluationAttemptRecord
from .evaluation_recovery import (
    EVALUATION_FAILED,
    EVALUATION_RECOVERED,
    EVALUATION_RUNNING,
    RecoveryOutcome,
    _close_attempt,
    _launch_cancelled,
    _materialize_recovery_snapshot_if_settled,
    claim_evaluation,
)

WORK_TYPE_MANAGED = "MANAGED_SCORE"

MANAGED_PENDING_CODE = "LANGFUSE_MANAGED_SCORES_PENDING"
MANAGED_PENDING_MESSAGE = "LANGFUSE_MANAGED_SCORES_PENDING: Agent 已成功，Langfuse 正在评分，暂不可用于正式发布。"
MANAGED_EXHAUSTED_CODE = "MANAGED_SCORES_EXHAUSTED"
MANAGED_DEADLINE_CODE = "MANAGED_SCORES_DEADLINE_EXCEEDED"

# 终态：重试也无法收敛，需人工修正配置/凭据/契约后重新提交或手动重评。
TERMINAL_CODES = frozenset(
    {
        "AUTHENTICATION_FAILED",
        "AUTHORIZATION_FAILED",
        "CLIENT_NOT_CONFIGURED",
        "CLIENT_CONFIG_INVALID",
        "MANAGED_NOT_SELECTED",
        "CONTRACT_INVALID",
        "ITEM_NOT_FOUND",
        "EVALUATION_CANCELLED",
        "CHECKPOINT_MISSING",
        "CHECKPOINT_EXPIRED",
        "CHECKPOINT_CORRUPT",
        "CHECKPOINT_OUTPUT_MISSING",
        "CHECKPOINT_DISABLED",
        "CHECKPOINT_BINDING_MISMATCH",
        "CHECKPOINT_MISMATCH",
    }
)


def get_retry_config() -> dict[str, float]:
    """有界重试默认：最多 10 次、基线 5s 指数退避、上限 300s、Deadline 1800s。

    均可经环境变量覆盖，便于测试与运维调参：
    ``ARGUS_MANAGED_SCORE_MAX_ATTEMPTS`` /
    ``ARGUS_MANAGED_SCORE_BASE_DELAY_SECONDS`` /
    ``ARGUS_MANAGED_SCORE_MAX_DELAY_SECONDS`` /
    ``ARGUS_MANAGED_SCORE_DEADLINE_SECONDS``。

    非法、负数、NaN、Infinity 一律回退安全默认；极端值钳入安全上界，
    避免无界重试或退避溢出。
    """
    import math

    def _num(name: str, default: float, low: float, high: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default
        if not math.isfinite(value) or value <= 0:
            return default
        return min(max(value, low), high)

    attempts_raw = os.getenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "10")
    try:
        max_attempts = int(float(attempts_raw))
    except (TypeError, ValueError):
        max_attempts = 10
    if max_attempts < 1:
        max_attempts = 10
    max_attempts = min(max_attempts, 100000)

    return {
        "max_attempts": max_attempts,
        "base_delay": _num("ARGUS_MANAGED_SCORE_BASE_DELAY_SECONDS", 5.0, 0.1, 3600.0),
        "max_delay": _num("ARGUS_MANAGED_SCORE_MAX_DELAY_SECONDS", 300.0, 0.1, 86400.0),
        "deadline": _num("ARGUS_MANAGED_SCORE_DEADLINE_SECONDS", 1800.0, 1.0, 604800.0),
    }


def get_collect_timeout_seconds() -> float:
    """单次完整收集的总时间预算（默认 45s，必须小于 60s Worker 租约）。

    每页 30s 超时叠加可能超过租约；总预算保证 hung 住的远端只转为
    可重试 TIMEOUT，不长期占位。非法值回退默认。
    """
    import math

    try:
        value = float(os.getenv("ARGUS_MANAGED_SCORE_COLLECT_TIMEOUT_SECONDS", "45"))
    except (TypeError, ValueError):
        return 45.0
    if not math.isfinite(value) or value <= 0:
        return 45.0
    return min(value, 55.0)


def backoff_delay(attempt: int, *, base: float = 5.0, cap: float = 300.0) -> float:
    """第 attempt 次失败后的等待秒数（1 起始，指数退避，上限 cap）。"""
    attempt = max(1, int(attempt))
    if attempt - 1 >= 32:
        return float(cap)
    return min(float(base) * (2.0 ** (attempt - 1)), float(cap))


def get_managed_score_client():
    """默认 Score 读取入口；未配置时返回 None，由 Collector 判为终态。"""
    try:
        from .langfuse_scores_client import ScoresV3Client

        return ScoresV3Client.from_env()
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value):
    if value is not None and getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=UTC)
    return value


def is_managed_manifest(manifest: dict[str, Any] | None) -> bool:
    from .managed_evaluation import evaluation_source

    return evaluation_source(manifest) == "langfuse_managed"


def required_score_names(manifest: dict[str, Any] | None) -> list[str]:
    manifest = manifest or {}
    contract = manifest.get("managed_evaluation") or {}
    names = []
    for score in contract.get("scores") or []:
        if isinstance(score, dict) and score.get("required", True):
            names.append(str(score.get("name")))
    return names


def _adopted_by_name(session, item_id: str) -> dict[str, Any]:
    from .db_models import EvaluationResultRecord

    rows = list(
        session.scalars(
            select(EvaluationResultRecord).where(
                EvaluationResultRecord.item_execution_id == item_id,
                EvaluationResultRecord.executor_type == "langfuse_managed",
                EvaluationResultRecord.status == "succeeded",
            )
        )
    )
    return {row.evaluator_id: row for row in rows}


def is_complete(session, item_id: str, manifest: dict[str, Any] | None) -> bool:
    required = required_score_names(manifest)
    if not required:
        return False
    adopted = _adopted_by_name(session, item_id)
    return all(name in adopted for name in required)


def _dominant_reason(report: dict[str, Any] | None) -> str:
    decisions = (report or {}).get("decisions") or []
    for entry in decisions:
        reason = str(entry.get("reason") or "")
        if reason and reason not in ("VERIFIED",):
            return reason
    return "SCORE_MISSING"


# 本轮未解决即阻塞终结的证据问题：已存成功行不能掩盖它们。
# 采纳口径内的有效结论：合法重复读取同一原始 Score 不视为冲突。
ROUND_ADOPTED_DECISIONS = frozenset({"ADOPTED", "ALREADY_ADOPTED"})


def _round_cleanly_adopted(report: dict[str, Any] | None, required: list[str]) -> bool:
    """本轮是否为每个必选 Score 都拿到无歧义采纳结论。

    已存成功行不等于本轮证据完整：上一轮采纳 A、本轮出现合法同名
    A+B（歧义）或本轮读取失败时，必须阻塞终结，保持 UNKNOWN/重试。
    """
    decisions = (report or {}).get("decisions") or []
    adopted = {
        str(entry.get("score_name"))
        for entry in decisions
        if entry.get("decision") in ROUND_ADOPTED_DECISIONS
    }
    return all(name in adopted for name in required)


def _cycle_attempts_used(session, item_id: str, cycle_start) -> int:
    """当前人工收集周期内已消耗的尝试次数。

    按 ``EvaluationAttemptRecord.started_at >= cycle_start`` 计数，与单调
    代际解耦：代际只做防旧隔离，预算按周期计算；人工重提重置周期起点
    后旧周期行自然排除，无需新字段或迁移。
    """
    rows = session.scalars(
        select(EvaluationAttemptRecord.started_at).where(
            EvaluationAttemptRecord.item_execution_id == item_id
        )
    ).all()
    if cycle_start is None:
        return len(rows)
    start = _as_utc(cycle_start)
    return sum(1 for value in rows if _as_utc(value) is not None and _as_utc(value) >= start)


def _collect_with_timeout(collector, launch_id: str, item_id: str, *, fence, timeout_seconds: float) -> dict:
    """有界执行一轮收集：超时即转为可重试 TIMEOUT，不长期占位。

    用 daemon 线程承载阻塞远端调用；超时后不等 hung 住的线程，
    由租约围栏保证其迟到写入无效。
    """
    import threading

    box: dict[str, Any] = {}
    worker_error: list[BaseException] = []

    def _run() -> None:
        try:
            box["report"] = collector.collect(launch_id, item_id, fence=fence)
        except BaseException as exc:  # noqa: BLE001 - 与外层异常归一保持一致
            worker_error.append(exc)

    thread = threading.Thread(target=_run, name=f"managed-collect-{item_id[:8]}", daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        return {
            "launch_id": launch_id,
            "item_execution_id": item_id,
            "quality_conclusion": "unknown",
            "releasable": False,
            "retry_after": None,
            "decisions": [
                {
                    "score_name": "",
                    "score_id": None,
                    "decision": "REJECTED",
                    "reason": "TIMEOUT",
                    "evidence_id": None,
                }
            ],
        }
    if worker_error:
        exc = worker_error[0]
        code = getattr(exc, "code", None) or "UPSTREAM_UNAVAILABLE"
        return {"decisions": [{"reason": str(code)}], "retry_after": None}
    return box.get("report") or {"decisions": [], "retry_after": None}


def _record_managed_attempt_started(
    db_mgr: DatabaseManager,
    *,
    item_id: str,
    launch_id: str,
    evaluation_generation: int,
    target_bindings: list[str],
    reused_output_digest: str,
    worker_id: str,
) -> None:
    """幂等记录收集 attempt：崩溃后租约失效重入同代时复活该行而非崩溃。

    同一 ``(item, generation)`` 唯一约束下，崩溃 Worker 留下的 ``running``
    行先被 Reconciler 标为 ``failed``；重入时将其复活为 ``running``，
    审计保留最新一次有效尝试，不因重复插入崩溃、不重复调用 Agent。
    """
    now = _now()
    with db_mgr.get_session() as session:
        try:
            with session.begin_nested():
                session.add(
                    EvaluationAttemptRecord(
                        id=f"ea_{uuid.uuid4().hex[:24]}",
                        item_execution_id=item_id,
                        launch_id=launch_id,
                        evaluation_generation=evaluation_generation,
                        status="running",
                        target_bindings=list(target_bindings),
                        reused_output_digest=reused_output_digest or "",
                        worker_id=worker_id,
                        lease_token=None,
                        started_at=now,
                    )
                )
                session.flush()
        except IntegrityError:
            session.execute(
                update(EvaluationAttemptRecord)
                .where(
                    EvaluationAttemptRecord.item_execution_id == item_id,
                    EvaluationAttemptRecord.evaluation_generation == evaluation_generation,
                )
                .values(
                    status="running",
                    target_bindings=list(target_bindings),
                    reused_output_digest=reused_output_digest or "",
                    worker_id=worker_id,
                    error_type=None,
                    error_message=None,
                    started_at=now,
                    completed_at=None,
                )
            )
        session.commit()


def submit_managed_collection(
    db_mgr: DatabaseManager,
    queue,
    *,
    item_id: str,
    worker_id: str = "system",
    new_cycle: bool = False,
) -> dict[str, Any] | None:
    """为一次成功的 Managed 执行提交首轮/手动评分收集。

    调用方（Worker HTTP 成功分支或手动重评 API）持有执行结论后调用。
    幂等：已在 ``evaluating`` 的 Item 直接返回已运行，不重复入队。

    ``new_cycle=True``（人工重提）开启新的收集周期：重置周期起点，
    给出新的重试窗口与 Deadline；代际仍单调递增做防旧隔离。
    自动/补偿提交保持 ``False``，不凭空放大预算。
    """
    from .db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord

    now = _now()
    with db_mgr.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, item_id)
        if item is None:
            return None
        launch = session.get(ExperimentLaunchRecord, item.launch_id)
        manifest = (launch.manifest if launch else {}) or {}
        if not is_managed_manifest(manifest):
            session.rollback()
            return None
        if item.execution_status != "succeeded":
            session.rollback()
            return None
        if launch is not None and (
            launch.cancel_requested_at or launch.status in ("CANCELLING", "CANCELLED")
        ):
            session.rollback()
            return None
        if item.evaluation_status == EVALUATION_RUNNING:
            session.rollback()
            return {"already_running": True, "evaluation_generation": item.evaluation_generation}
        new_gen = int(item.evaluation_generation or 0) + 1
        item.evaluation_generation = new_gen
        item.evaluation_status = EVALUATION_RUNNING
        item.evaluation_error = MANAGED_PENDING_MESSAGE
        item.evaluation_lease_owner = None
        item.evaluation_lease_token = None
        item.evaluation_lease_expires_at = None
        if item.evaluation_started_at is None or new_cycle:
            item.evaluation_started_at = now
        item.evaluation_completed_at = None
        # available_at 复用为 Managed 下次可执行时间；None 表示立即到期。
        item.available_at = None
        # 执行已成功但评分未齐：保持 PENDING / UNKNOWN，不伪造结论。
        item.eval_status = "pending"
        item.quality_conclusion = "unknown"
        item.eval_error = MANAGED_PENDING_MESSAGE
        item.updated_at = now
        session.commit()
    queue.enqueue_items([(item_id, new_gen)], work_type=WORK_TYPE_MANAGED)
    return {"evaluation_generation": new_gen, "enqueued": True}


def _fail_managed(
    db_mgr: DatabaseManager,
    claim: dict[str, Any],
    *,
    error_code: str,
    error_message: str,
    launch_id: str | None = None,
) -> RecoveryOutcome:
    """终态失败：保持执行成功、质量 UNKNOWN，关闭重试，不伪造分数。"""
    from .db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord

    is_pg = db_mgr.engine.dialect.name == "postgresql"
    now = _now()
    item_id = claim["item_id"]
    generation = claim["evaluation_generation"]
    token = claim["lease_token"]
    with db_mgr.get_session() as session:
        launch_record = None
        if launch_id:
            stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == launch_id)
            if is_pg:
                stmt = stmt.with_for_update()
            launch_record = session.scalars(stmt.execution_options(populate_existing=True)).first()
        lock_stmt = select(ExperimentItemExecutionRecord).where(
            ExperimentItemExecutionRecord.id == item_id
        )
        if is_pg:
            lock_stmt = lock_stmt.with_for_update()
        item = session.scalars(lock_stmt).first()
        if not item:
            session.rollback()
            return RecoveryOutcome(claimed=True, finalized=False, reason="ITEM_MISSING")
        # 终态写入同样要求有效租约：过期才到的失败不得覆盖当前结果。
        lease_exp = _as_utc(item.evaluation_lease_expires_at)
        if (
            item.evaluation_generation != generation
            or item.evaluation_lease_token != token
            or item.evaluation_status != EVALUATION_RUNNING
            or lease_exp is None
            or lease_exp <= now
        ):
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation,
                status="discarded", error_type="SUPERSEDED_OR_LEASE_LOST",
                error_message="旧评分收集已过期，未覆盖当前结果。", now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="SUPERSEDED_OR_LEASE_LOST")
        item.eval_status = "failed"
        item.eval_error = f"{error_code}: {error_message}"
        item.quality_conclusion = "unknown"
        item.evaluation_status = EVALUATION_FAILED
        item.evaluation_error = f"{error_code}: {error_message}"
        item.evaluation_lease_owner = None
        item.evaluation_lease_token = None
        item.evaluation_lease_expires_at = None
        item.evaluation_completed_at = now
        item.available_at = None
        item.updated_at = now
        _close_attempt(
            session, item_id=item_id, evaluation_generation=generation,
            status="failed", error_type=error_code, error_message=error_message, now=now,
        )
        session.flush()
        _materialize_recovery_snapshot_if_settled(session, launch_record)
        session.commit()
        return RecoveryOutcome(
            claimed=True, finalized=True, reason=error_code,
            exhausted=True, error=item.evaluation_error, launch_id=launch_id,
        )


def _schedule_retry(
    db_mgr: DatabaseManager,
    claim: dict[str, Any],
    *,
    reason: str,
    launch_id: str | None = None,
) -> RecoveryOutcome:
    """可重试：关闭本轮 attempt，递增代际并设置指数退避，不入队（由 Reconciler 到期入队）。"""
    from .db_models import ExperimentItemExecutionRecord

    is_pg = db_mgr.engine.dialect.name == "postgresql"
    now = _now()
    cfg = get_retry_config()
    item_id = claim["item_id"]
    generation = claim["evaluation_generation"]
    token = claim["lease_token"]
    expected_dispatch = claim.get("dispatch_generation")
    with db_mgr.get_session() as session:
        lock_stmt = select(ExperimentItemExecutionRecord).where(
            ExperimentItemExecutionRecord.id == item_id
        )
        if is_pg:
            lock_stmt = lock_stmt.with_for_update()
        item = session.scalars(lock_stmt).first()
        if not item:
            session.rollback()
            return RecoveryOutcome(claimed=True, finalized=False, reason="ITEM_MISSING")
        lease_exp = _as_utc(item.evaluation_lease_expires_at)
        if (
            item.evaluation_generation != generation
            or item.evaluation_lease_token != token
            or item.evaluation_status != EVALUATION_RUNNING
            or lease_exp is None
            or lease_exp <= now
        ):
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation,
                status="discarded", error_type="SUPERSEDED_OR_LEASE_LOST",
                error_message="旧评分收集已过期，未覆盖当前结果。", now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="SUPERSEDED_OR_LEASE_LOST")
        if expected_dispatch is not None and item.dispatch_generation != expected_dispatch:
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation,
                status="discarded", error_type="DISPATCH_SUPERSEDED",
                error_message="该执行已重跑，旧评分收集不再适用。", now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="DISPATCH_SUPERSEDED")
        delay = backoff_delay(generation, base=float(cfg["base_delay"]), cap=float(cfg["max_delay"]))
        next_gen = generation + 1
        _close_attempt(
            session, item_id=item_id, evaluation_generation=generation,
            status="failed", error_type=reason,
            error_message=f"托管评分未齐（{reason}），{delay:.1f}s 后重试（第 {next_gen} 轮）。",
            now=now,
        )
        item.evaluation_generation = next_gen
        item.evaluation_status = EVALUATION_RUNNING
        item.evaluation_error = f"{reason}: 托管评分未齐，{delay:.1f}s 后自动重试，不会再次调用 Agent。"
        item.evaluation_lease_owner = None
        item.evaluation_lease_token = None
        item.evaluation_lease_expires_at = None
        item.available_at = now + timedelta(seconds=delay)
        item.eval_status = "pending"
        item.eval_error = MANAGED_PENDING_MESSAGE
        item.quality_conclusion = "unknown"
        item.updated_at = now
        session.commit()
        return RecoveryOutcome(
            claimed=True, finalized=False, reason="RETRY_SCHEDULED",
            retry_after=str(int(delay)), next_generation=next_gen,
            launch_id=launch_id,
        )


def _finalize_success(
    db_mgr: DatabaseManager,
    claim: dict[str, Any],
    *,
    launch_id: str | None = None,
) -> RecoveryOutcome:
    """必选 Score 齐全：按冻结 QualityPolicy 判定质量并冻结新快照。"""
    from .db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord
    from .evaluation_result_store import result_from_record
    from .evaluator_binding import manifest_quality_policy
    from .evaluator_results import project_legacy_scores
    from .quality_policy import evaluate_quality_policy

    is_pg = db_mgr.engine.dialect.name == "postgresql"
    now = _now()
    item_id = claim["item_id"]
    generation = claim["evaluation_generation"]
    token = claim["lease_token"]
    expected_dispatch = claim.get("dispatch_generation")
    with db_mgr.get_session() as session:
        launch_record = None
        if launch_id:
            stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == launch_id)
            if is_pg:
                stmt = stmt.with_for_update()
            launch_record = session.scalars(stmt.execution_options(populate_existing=True)).first()
        manifest = (launch_record.manifest if launch_record else {}) or {}
        lock_stmt = select(ExperimentItemExecutionRecord).where(
            ExperimentItemExecutionRecord.id == item_id
        )
        if is_pg:
            lock_stmt = lock_stmt.with_for_update()
        item = session.scalars(lock_stmt).first()
        if not item:
            session.rollback()
            return RecoveryOutcome(claimed=True, finalized=False, reason="ITEM_MISSING")
        lease_exp = _as_utc(item.evaluation_lease_expires_at)
        if (
            item.evaluation_generation != generation
            or item.evaluation_lease_token != token
            or item.evaluation_status != EVALUATION_RUNNING
            or lease_exp is None
            or lease_exp <= now
        ):
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation,
                status="discarded", error_type="SUPERSEDED_OR_LEASE_LOST",
                error_message="旧评分收集已过期，未覆盖当前结果。", now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="SUPERSEDED_OR_LEASE_LOST")
        if expected_dispatch is not None and item.dispatch_generation != expected_dispatch:
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation,
                status="discarded", error_type="DISPATCH_SUPERSEDED",
                error_message="该执行已重跑，旧评分收集不再适用。", now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="DISPATCH_SUPERSEDED")
        if _launch_cancelled(session, launch_id):
            # 远程查询期间取消：迟到结果绝不能诞生新的成功结论，
            # 终态归属取消流程，不在此处写入。
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation,
                status="discarded", error_type="EVALUATION_CANCELLED",
                error_message="评测任务已取消，迟到评分未写入。", now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="CANCELLED_SUPERSEDE")
        if not is_complete(session, item_id, manifest):
            # 竞争窗口：采纳结果被回滚或代际已变，降级为重试而非伪造完成。
            session.rollback()
            return RecoveryOutcome(claimed=True, finalized=False, reason="COMPLETION_RACE")
        adopted_rows = list(_adopted_by_name(session, item_id).values())
        adopted = [result_from_record(row) for row in adopted_rows]
        policy = manifest_quality_policy(manifest)
        decision = evaluate_quality_policy(adopted, policy, decided_by="QUALITY_POLICY")
        item.eval_status = "succeeded"
        item.eval_error = None
        item.quality_conclusion = decision.conclusion
        try:
            item.scores = dict(project_legacy_scores(adopted))
        except Exception:
            item.scores = {}
        try:
            item.quality_evaluation = decision.payload()
        except Exception:
            pass
        item.evaluation_status = EVALUATION_RECOVERED
        item.evaluation_error = None
        item.evaluation_lease_owner = None
        item.evaluation_lease_token = None
        item.evaluation_lease_expires_at = None
        item.evaluation_completed_at = now
        item.available_at = None
        item.updated_at = now
        _close_attempt(
            session, item_id=item_id, evaluation_generation=generation,
            status="succeeded", now=now,
        )
        session.flush()
        _materialize_recovery_snapshot_if_settled(session, launch_record)
        session.commit()
        return RecoveryOutcome(
            claimed=True, finalized=True, reason="MANAGED_SCORES_ADOPTED",
            eval_status="succeeded", quality_conclusion=decision.conclusion,
            launch_id=launch_id,
        )


def recover_managed_scores(
    db_mgr: DatabaseManager,
    *,
    item_id: str,
    evaluation_generation: int,
    worker_id: str,
    lease_seconds: int = 60,
    client_factory=None,
) -> RecoveryOutcome:
    """一次托管评分收集：认领租约 → 只读收集 → 完整则终结，否则有界重试。

    永不调用业务 Agent，永不申请 Agent HTTP 并发 permit。
    """
    from .db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord
    from .execution_checkpoint import CheckpointUnavailableError, load_recoverable_checkpoint

    claim = claim_evaluation(
        db_mgr, item_id=item_id, evaluation_generation=evaluation_generation,
        worker_id=worker_id, lease_seconds=lease_seconds,
    )
    if claim is None:
        return RecoveryOutcome(claimed=False, finalized=False, reason="NOT_CLAIMABLE")
    launch_id = claim.get("launch_id")
    now = _now()

    with db_mgr.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, item_id)
        if item is None:
            session.rollback()
            return RecoveryOutcome(claimed=True, finalized=False, reason="ITEM_MISSING")
        expected_dispatch = item.dispatch_generation
        manifest = (session.get(ExperimentLaunchRecord, launch_id).manifest if launch_id else {}) or {}
        output_digest = ""
        try:
            checkpoint = load_recoverable_checkpoint(
                session, item_execution_id=item_id,
                dispatch_generation=item.dispatch_generation,
                manifest=None, now=now,
            )
            output_digest = checkpoint.output_digest or ""
        except CheckpointUnavailableError as exc:
            session.rollback()
            claim["dispatch_generation"] = expected_dispatch
            return _fail_managed(
                db_mgr, claim, error_code=exc.code, error_message=exc.message,
                launch_id=launch_id,
            )
        if _launch_cancelled(session, launch_id):
            session.rollback()
            claim["dispatch_generation"] = expected_dispatch
            return _fail_managed(
                db_mgr, claim, error_code="EVALUATION_CANCELLED",
                error_message="评测任务已取消，迟到评分未写入。", launch_id=launch_id,
            )
        if not is_managed_manifest(manifest):
            session.rollback()
            claim["dispatch_generation"] = expected_dispatch
            return _fail_managed(
                db_mgr, claim, error_code="MANAGED_NOT_SELECTED",
                error_message="该任务未选择 Langfuse 托管评分。", launch_id=launch_id,
            )
        if item.execution_status != "succeeded":
            session.rollback()
            claim["dispatch_generation"] = expected_dispatch
            return _fail_managed(
                db_mgr, claim, error_code="EXECUTION_NOT_READY",
                error_message="Agent 尚未成功，暂不能收集托管评分。", launch_id=launch_id,
            )
        required = required_score_names(manifest)
        session.rollback()
    claim["dispatch_generation"] = expected_dispatch

    _record_managed_attempt_started(
        db_mgr, item_id=item_id, launch_id=launch_id,
        evaluation_generation=evaluation_generation,
        target_bindings=sorted(required),
        reused_output_digest=output_digest,
        worker_id=worker_id,
    )

    # 只读远端，不持有任何 DB 锁与 Agent 并发额度。
    # 租约围栏随收集传入：远端耗时期间失租/换代/取消，迟到写入无效。
    from .langfuse_managed_scores import ManagedScoreCollector

    fence = {
        "evaluation_generation": evaluation_generation,
        "lease_token": claim["lease_token"],
        "dispatch_generation": expected_dispatch,
    }
    factory = client_factory or get_managed_score_client
    client = factory()
    if client is None:
        # 未配置 Langfuse 凭据：终态，需人工配置后手动重评；不烧退避次数。
        return _fail_managed(
            db_mgr, claim, error_code="CLIENT_NOT_CONFIGURED",
            error_message="未配置 Langfuse 评分客户端，质量保持 UNKNOWN，需配置后手动重评。",
            launch_id=launch_id,
        )
    from .langfuse_scores_client import ScoresV3Client as _ScoresV3Client

    owned_client = isinstance(client, _ScoresV3Client)
    try:
        report = _collect_with_timeout(
            ManagedScoreCollector(db_mgr, client),
            launch_id, item_id, fence=fence,
            timeout_seconds=get_collect_timeout_seconds(),
        )
    finally:
        if owned_client:
            try:
                client.close()
            except Exception:
                pass

    with db_mgr.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, item_id)
        manifest = (session.get(ExperimentLaunchRecord, launch_id).manifest if launch_id else {}) or {}
        complete = False
        try:
            complete = is_complete(session, item_id, manifest)
        except Exception:
            complete = False
        started_at = _as_utc(item.evaluation_started_at) if item else None
        try:
            attempts_used = _cycle_attempts_used(session, item_id, started_at)
        except Exception:
            attempts_used = int(evaluation_generation)
        session.rollback()

    # 已存成功行不等于本轮证据完整：歧义/冲突/读失败必须阻塞终结。
    if complete and _round_cleanly_adopted(report, required):
        return _finalize_success(db_mgr, claim, launch_id=launch_id)

    reason = _dominant_reason(report)
    if reason == "CANCELLED_SUPERSEDE":
        reason = "EVALUATION_CANCELLED"
    cfg = get_retry_config()
    if reason in TERMINAL_CODES:
        return _fail_managed(
            db_mgr, claim, error_code=reason,
            error_message=f"托管评分收集终止（{reason}），质量保持 UNKNOWN，需人工处理后重试。",
            launch_id=launch_id,
        )
    # 有界：本周期超限或超期 → 终态 UNKNOWN，不伪造分数。
    # 预算按周期统计（与单调代际解耦），人工重提开启新周期。
    if attempts_used >= int(cfg["max_attempts"]):
        return _fail_managed(
            db_mgr, claim, error_code=MANAGED_EXHAUSTED_CODE,
            error_message=f"托管评分重试已达上限（{int(cfg['max_attempts'])} 次），质量保持 UNKNOWN。",
            launch_id=launch_id,
        )
        return _fail_managed(
            db_mgr, claim, error_code=MANAGED_EXHAUSTED_CODE,
            error_message=f"托管评分重试已达上限（{int(cfg['max_attempts'])} 次），质量保持 UNKNOWN。",
            launch_id=launch_id,
        )
    if started_at is not None and (now - started_at).total_seconds() >= float(cfg["deadline"]):
        return _fail_managed(
            db_mgr, claim, error_code=MANAGED_DEADLINE_CODE,
            error_message="托管评分等待已超时，质量保持 UNKNOWN，可在 Langfuse 出分后手动重评。",
            launch_id=launch_id,
        )
    return _schedule_retry(db_mgr, claim, reason=reason, launch_id=launch_id)



