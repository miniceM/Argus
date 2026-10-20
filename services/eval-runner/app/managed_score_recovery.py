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
        "OBSERVATION_PROJECTION_FAILED",
        "OBSERVATION_PROJECTION_MISSING",
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
    except (TypeError, ValueError, OverflowError):
        max_attempts = 10
    if max_attempts < 1:
        max_attempts = 10
    # 上界 100：deadline（默认 1800s）下更大值形同无界，高频 429 会 hammer 远端。
    max_attempts = min(max_attempts, 100)

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



def _retry_delay(cfg, attempt: int, retry_after, now, cycle_start) -> float:
    """尊重 Retry-After（秒数或 HTTP-date），但不把周期延长到 Deadline 之后。"""
    import math
    from email.utils import parsedate_to_datetime

    delay = backoff_delay(attempt, base=float(cfg["base_delay"]), cap=float(cfg["max_delay"]))
    if retry_after is not None:
        try:
            remote_delay = float(retry_after)
        except (TypeError, ValueError, OverflowError):
            try:
                remote_delay = (_as_utc(parsedate_to_datetime(str(retry_after))) - now).total_seconds()
            except (TypeError, ValueError, OverflowError):
                remote_delay = 0.0
        if math.isfinite(remote_delay):
            delay = max(delay, remote_delay)
    if cycle_start is not None:
        remaining = (_as_utc(cycle_start) + timedelta(seconds=cfg["deadline"]) - now).total_seconds()
        delay = min(delay, max(0.0, remaining))
    return delay


def reconcile_managed_interruption(session, observed, *, require_expired: bool = True) -> bool:
    """用原行锁/CAS 接管中断，不复活同一代际、不重置收集周期预算。

    扫描是候选提示而非写入授权：按 Launch → Item 加锁后重新读取 token、
    generation、dispatch 与过期时间，防扫描后已续租/接管的 Worker 被清租。
    取消可关闭无租约的退避任务；Builtin 不进入此路径。
    """
    from .db_models import ExperimentItemExecutionRecord as Item
    from .db_models import ExperimentLaunchRecord as Launch

    item_id, launch_id = observed.id, observed.launch_id
    generation, token, dispatch = (
        observed.evaluation_generation, observed.evaluation_lease_token, observed.dispatch_generation
    )
    is_pg = session.bind.dialect.name == "postgresql"
    if not is_pg:
        # SQLite 无 FOR UPDATE；同样先获得 Launch 写锁，保持统一锁顺序。
        session.execute(update(Launch).where(Launch.id == launch_id).values(id=Launch.id))
    launch_stmt = select(Launch).where(Launch.id == launch_id)
    if is_pg:
        launch_stmt = launch_stmt.with_for_update()
    launch = session.scalar(launch_stmt.execution_options(populate_existing=True))
    stmt = select(Item).where(Item.id == item_id)
    if is_pg:
        stmt = stmt.with_for_update()
    item = session.scalar(stmt.execution_options(populate_existing=True))
    now = _now()
    if not item or not launch or not is_managed_manifest(launch.manifest):
        return False
    if (item.evaluation_status != EVALUATION_RUNNING or item.evaluation_generation != generation
            or item.evaluation_lease_token != token or item.dispatch_generation != dispatch):
        return False
    cancelled = bool(launch.cancel_requested_at or launch.status in ("CANCELLING", "CANCELLED"))
    expiry = _as_utc(item.evaluation_lease_expires_at)
    if require_expired and (expiry is None or expiry > now):
        return False
    if not require_expired and not cancelled:
        return False
    code = "EVALUATION_CANCELLED" if cancelled else "LEASE_EXPIRED"
    _close_attempt(
        session, item_id=item_id, evaluation_generation=generation,
        status="failed", error_type=code, error_message="评分收集中断；原 Agent 输出保持不变。",
        now=now, launch_id=launch_id if require_expired else None,
    )
    # claim 后、attempt 插入前崩溃时会补最小审计行；autoflush=False，
    # 必须先持久化到当前事务，再按周期计数，否则崩溃可绕过最后一次预算。
    session.flush()
    cfg = get_retry_config()
    used = _cycle_attempts_used(session, item_id, item.evaluation_started_at)
    start = _as_utc(item.evaluation_started_at)
    if not cancelled and used >= int(cfg["max_attempts"]):
        code = MANAGED_EXHAUSTED_CODE
    elif not cancelled and (start is None or now >= start + timedelta(seconds=cfg["deadline"])):
        code = MANAGED_DEADLINE_CODE
    item.evaluation_lease_owner = None
    item.evaluation_lease_token = None
    item.evaluation_lease_expires_at = None
    item.quality_conclusion = "unknown"
    item.updated_at = now
    if code != "LEASE_EXPIRED":
        item.eval_status = "failed"
        item.eval_error = item.evaluation_error = f"{code}: 托管评分已终止，质量保持 UNKNOWN。"
        item.evaluation_status = EVALUATION_FAILED
        item.evaluation_completed_at = now
        item.available_at = None
        session.flush()
        _materialize_recovery_snapshot_if_settled(session, launch)
    else:
        # 每次崩溃也消耗一轮：保留旧 attempt，新代际阻断迟到线程及重复旧消息。
        item.evaluation_generation = generation + 1
        item.available_at = now + timedelta(seconds=_retry_delay(cfg, max(1, used), None, now, start))
        item.eval_status = "pending"
        item.eval_error = MANAGED_PENDING_MESSAGE
        item.evaluation_error = "EVALUATION_LEASE_EXPIRED: 评分收集已退避重试，不会再次调用 Agent。"
    return True



def _managed_projection_reason(db_mgr, claim, output_digest: str) -> str | None:
    """OTLP Managed 必须先有持久发布凭据，不能把仅关联标记的 SYNCED 当作可评分。

    旧版本已 SYNCED 的空标记只在每个人工收集周期补投一次；复用同一 Outbox、
    Checkpoint 和发布租约，不另建任务。失败/新周期的预算独立且均有上界。
    """
    from .db_models import ExperimentItemExecutionRecord as Item
    from .db_models import ExperimentLaunchRecord as Launch
    from .db_models import LangfuseSyncTaskRecord as Task
    from .langfuse_run_items import resolve_run_item_strategy

    if resolve_run_item_strategy() != "otel":
        return None
    is_pg = db_mgr.engine.dialect.name == "postgresql"
    with db_mgr.get_session() as session:
        if not is_pg:
            session.execute(update(Launch).where(Launch.id == claim["launch_id"]).values(id=Launch.id))
        launch_stmt = select(Launch).where(Launch.id == claim["launch_id"])
        if is_pg:
            launch_stmt = launch_stmt.with_for_update()
        launch = session.scalar(launch_stmt)
        stmt = select(Item).where(Item.id == claim["item_id"])
        if is_pg:
            stmt = stmt.with_for_update()
        item = session.scalar(stmt)
        if not launch or launch.cancel_requested_at or launch.status in ("CANCELLING", "CANCELLED"):
            return "EVALUATION_CANCELLED"
        if (not item or item.evaluation_generation != claim["evaluation_generation"]
                or item.evaluation_lease_token != claim["lease_token"]
                or item.dispatch_generation != claim["dispatch_generation"]
                or _as_utc(item.evaluation_lease_expires_at) is None
                or _as_utc(item.evaluation_lease_expires_at) <= _now()):
            return "LEASE_LOST"
        stmt = select(Task).where(
            Task.item_id == item.id, Task.dispatch_generation == item.dispatch_generation,
            Task.task_type == "FULL_EVAL_SYNC",
        )
        if is_pg:
            stmt = stmt.with_for_update()
        task = session.scalar(stmt)
        if task is None:
            return "OBSERVATION_PROJECTION_MISSING"
        payload = dict(task.scores_payload or {})
        if payload.get("_managed_observation_digest") == output_digest:
            return None
        epoch = _as_utc(item.evaluation_started_at).isoformat()
        if task.status in ("SYNCED", "FAILED", "SKIPPED") and payload.get("_managed_observation_cycle") != epoch:
            task.status = "PENDING"
            task.attempts = 0
            task.next_retry_at = _now()
            task.owner_id = None
            task.claim_token = None
            task.lease_expires_at = None
            task.last_error = "OBSERVATION_PROJECTION_PENDING: 从 Checkpoint 补发完整可评分 Observation。"
        elif task.status in ("FAILED", "SKIPPED"):
            return "OBSERVATION_PROJECTION_FAILED"
        payload["_managed_observation_cycle"] = epoch
        task.scores_payload = payload
        return "OBSERVATION_PROJECTION_PENDING"


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


def _report_covers_adopted(report: dict[str, Any] | None, required: list[str], adopted: dict[str, Any]) -> bool:
    """本轮报告是否背书了当前库内每个必选名的已采纳行。

    纵深绑定：collect 与 finalize 间隙若库行偏离报告（带外改写、
    陈旧报告复用），即使 ``is_complete`` 仍为真，也必须拒绝终结，
    返回 COMPLETION_RACE 重试而非基于旧快照 PASS。按外部评分身份
    （``external_score_id``）逐必选名覆盖判定。
    """
    decisions = (report or {}).get("decisions") or []
    covered: dict[str, set] = {}
    for entry in decisions:
        if entry.get("decision") in ROUND_ADOPTED_DECISIONS:
            covered.setdefault(str(entry.get("score_name")), set()).add(entry.get("score_id"))
    for name in required:
        row = adopted.get(name)
        if row is None:
            return False
        if getattr(row, "external_score_id", None) not in covered.get(name, set()):
            return False
    return True


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


def _collect_with_timeout(db_mgr, client_factory, launch_id: str, item_id: str, *, fence, timeout_seconds: float) -> dict:
    """有界执行一轮收集：超时即转为可重试 TIMEOUT，不长期占位。

    用 daemon 线程承载阻塞远端调用；client 由工作线程自建自关，
    主线程超时后不再触碰该连接（无跨线程 close 竞态），迟到写入
    仍由租约围栏保证无效。未配置客户端同样经报告归一终态，不烧退避。
    """
    import threading

    box: dict[str, Any] = {}
    worker_error: list[BaseException] = []

    def _run() -> None:
        from .langfuse_managed_scores import ManagedScoreCollector
        from .langfuse_scores_client import ScoresV3Client as _ScoresV3Client

        client = None
        try:
            client = client_factory()
            if client is None:
                box["report"] = {
                    "decisions": [{"reason": "CLIENT_NOT_CONFIGURED"}],
                    "retry_after": None,
                }
                return
            try:
                box["report"] = ManagedScoreCollector(db_mgr, client).collect(
                    launch_id, item_id, fence=fence
                )
            finally:
                if isinstance(client, _ScoresV3Client):
                    try:
                        client.close()
                    except Exception:
                        pass
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
    lease_token: str | None = None,
) -> bool:
    """幂等记录收集 attempt：崩溃后租约失效重入同代时复活该行而非崩溃。

    同一 ``(item, generation)`` 唯一约束防重复插入；生产调用带 token，
    只允许仍有效的持有者更新审计。正常崩溃由 Reconciler 换到新代际，
    此处仅兼容同代幂等记录，不允许旧进程复活并污染新周期预算。
    """
    from .db_models import ExperimentItemExecutionRecord as Item
    from .db_models import ExperimentLaunchRecord as Launch

    now = _now()
    with db_mgr.get_session() as session:
        if lease_token is not None:
            # 旧进程可能停在 claim→attempt 间隙。记录 attempt 本身也必须围栏，
            # 否则恢复旧行的 started_at 会污染新的人工收集周期预算。
            if db_mgr.engine.dialect.name == "postgresql":
                session.scalar(select(Launch).where(Launch.id == launch_id).with_for_update())
            else:
                session.execute(update(Launch).where(Launch.id == launch_id).values(id=Launch.id))
            fenced = session.execute(update(Item).where(
                Item.id == item_id, Item.evaluation_generation == evaluation_generation,
                Item.evaluation_lease_token == lease_token, Item.evaluation_status == EVALUATION_RUNNING,
                Item.evaluation_lease_expires_at > _now(),
            ).values(updated_at=Item.updated_at))
            if fenced.rowcount != 1:
                session.rollback()
                return False
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
                        lease_token=lease_token,
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
                    lease_token=lease_token,
                    error_type=None,
                    error_message=None,
                    started_at=now,
                    completed_at=None,
                )
            )
        session.commit()
        return True


def _open_collection_cycle(item, *, now, pending_message: str | None, reset_cycle_start: bool) -> int:
    """开启一次评分收集的公共字段写入（首轮提交 / 人工重提 / 补偿提交共用）。

    代际单调递增做防旧隔离；``reset_cycle_start`` 为真（人工重提）时
    重置周期起点，给出新重试窗口与 Deadline。``pending_message`` 为
    None 时为 Builtin 重评，不碰执行侧 pending/unknown 结论。
    """
    new_gen = int(item.evaluation_generation or 0) + 1
    item.evaluation_generation = new_gen
    item.evaluation_status = EVALUATION_RUNNING
    item.evaluation_error = pending_message
    item.evaluation_lease_owner = None
    item.evaluation_lease_token = None
    item.evaluation_lease_expires_at = None
    if reset_cycle_start or item.evaluation_started_at is None:
        item.evaluation_started_at = now
    item.evaluation_completed_at = None
    if pending_message is not None:
        # Managed：执行已成功但评分未齐，保持 PENDING / UNKNOWN，不伪造结论。
        # available_at 复用为 Managed 下次可执行时间；None 表示立即到期。
        item.eval_status = "pending"
        item.quality_conclusion = "unknown"
        item.eval_error = pending_message
        item.available_at = None
    return new_gen


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
        new_gen = _open_collection_cycle(
            item, now=now, pending_message=MANAGED_PENDING_MESSAGE,
            reset_cycle_start=new_cycle,
        )
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
        now = _now()
        lease_exp = _as_utc(item.evaluation_lease_expires_at)
        if (
            item.evaluation_generation != generation
            or item.evaluation_lease_token != token
            or item.evaluation_status != EVALUATION_RUNNING
            or lease_exp is None
            or lease_exp <= now
            or item.dispatch_generation != claim.get("dispatch_generation", item.dispatch_generation)
        ):
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation, lease_token=token,
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
            session, item_id=item_id, evaluation_generation=generation, lease_token=token,
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
    retry_after: str | None = None,
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
        now = _now()
        lease_exp = _as_utc(item.evaluation_lease_expires_at)
        if (
            item.evaluation_generation != generation
            or item.evaluation_lease_token != token
            or item.evaluation_status != EVALUATION_RUNNING
            or lease_exp is None
            or lease_exp <= now
        ):
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation, lease_token=token,
                status="discarded", error_type="SUPERSEDED_OR_LEASE_LOST",
                error_message="旧评分收集已过期，未覆盖当前结果。", now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="SUPERSEDED_OR_LEASE_LOST")
        if expected_dispatch is not None and item.dispatch_generation != expected_dispatch:
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation, lease_token=token,
                status="discarded", error_type="DISPATCH_SUPERSEDED",
                error_message="该执行已重跑，旧评分收集不再适用。", now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="DISPATCH_SUPERSEDED")
        # BLOCKER-1：退避按本周期次数（与预算同一口径），不用单调代际：
        # 人工重提后代际已很大，若沿用代际新周期首退即封顶。本轮 attempt
        # 行此时仍为 running，故计入次数，首退 delay(1)=base。
        cycle_attempt = max(1, int(_cycle_attempts_used(session, item_id, item.evaluation_started_at)))
        delay = _retry_delay(cfg, cycle_attempt, retry_after, now, item.evaluation_started_at)
        next_gen = generation + 1
        _close_attempt(
            session, item_id=item_id, evaluation_generation=generation, lease_token=token,
            status="failed", error_type=reason,
            error_message=f"托管评分未齐（{reason}），{delay:.1f}s 后重试（本周期第 {cycle_attempt} 次）。",
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
    report: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> RecoveryOutcome:
    """必选 Score 齐全：按冻结 QualityPolicy 判定质量并冻结新快照。

    ``report``/``required`` 为本轮收集证据：行锁内复验“报告背书了
    当前库内已采纳行”，防 collect→finalize 间隙偏离（BLOCKER-3）。
    未传入时退化为仅 ``is_complete`` 判定（兼容旧调用）。
    """
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
        now = _now()
        lease_exp = _as_utc(item.evaluation_lease_expires_at)
        if (
            item.evaluation_generation != generation
            or item.evaluation_lease_token != token
            or item.evaluation_status != EVALUATION_RUNNING
            or lease_exp is None
            or lease_exp <= now
        ):
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation, lease_token=token,
                status="discarded", error_type="SUPERSEDED_OR_LEASE_LOST",
                error_message="旧评分收集已过期，未覆盖当前结果。", now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="SUPERSEDED_OR_LEASE_LOST")
        if expected_dispatch is not None and item.dispatch_generation != expected_dispatch:
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation, lease_token=token,
                status="discarded", error_type="DISPATCH_SUPERSEDED",
                error_message="该执行已重跑，旧评分收集不再适用。", now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="DISPATCH_SUPERSEDED")
        if _launch_cancelled(session, launch_id):
            # 远程查询期间取消：迟到结果绝不能诞生新的成功结论，
            # 终态归属取消流程，不在此处写入。
            _close_attempt(
                session, item_id=item_id, evaluation_generation=generation, lease_token=token,
                status="discarded", error_type="EVALUATION_CANCELLED",
                error_message="评测任务已取消，迟到评分未写入。", now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="CANCELLED_SUPERSEDE")
        cycle_start = _as_utc(item.evaluation_started_at)
        if cycle_start is None or now >= cycle_start + timedelta(seconds=get_retry_config()["deadline"]):
            session.rollback()
            return _fail_managed(
                db_mgr, claim, error_code=MANAGED_DEADLINE_CODE,
                error_message="托管评分等待已超时，质量保持 UNKNOWN。", launch_id=launch_id,
            )
        if not is_complete(session, item_id, manifest):
            # 竞争窗口：采纳结果被回滚或代际已变，降级为重试而非伪造完成。
            session.rollback()
            return RecoveryOutcome(claimed=True, finalized=False, reason="COMPLETION_RACE")
        if report is not None and required is not None and not _report_covers_adopted(
            report, required, _adopted_by_name(session, item_id)
        ):
            # 间隙偏离：库内已采纳行不是本轮报告背书的身份，陈旧报告
            # 不得终结，重试由下一轮重采收敛。
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
            session, item_id=item_id, evaluation_generation=generation, lease_token=token,
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
        allow_expired=False, respect_available_at=True,
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
        expected_dispatch = claim["dispatch_generation"]
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
        cycle_start = _as_utc(item.evaluation_started_at)
        attempts_used = _cycle_attempts_used(session, item_id, cycle_start)
        session.rollback()
    claim["dispatch_generation"] = expected_dispatch
    cfg = get_retry_config()
    deadline = cycle_start + timedelta(seconds=cfg["deadline"]) if cycle_start else now
    if _now() >= deadline:
        return _fail_managed(
            db_mgr, claim, error_code=MANAGED_DEADLINE_CODE,
            error_message="托管评分等待已超时，未读取远端；质量保持 UNKNOWN。", launch_id=launch_id,
        )
    if attempts_used >= int(cfg["max_attempts"]):
        return _fail_managed(
            db_mgr, claim, error_code=MANAGED_EXHAUSTED_CODE,
            error_message="托管评分本周期预算已用尽，未读取远端；质量保持 UNKNOWN。", launch_id=launch_id,
        )

    attempt_started = _record_managed_attempt_started(
        db_mgr, item_id=item_id, launch_id=launch_id,
        evaluation_generation=evaluation_generation,
        target_bindings=sorted(required),
        reused_output_digest=output_digest,
        worker_id=worker_id,
        lease_token=claim["lease_token"],
    )
    if not attempt_started:
        return RecoveryOutcome(claimed=True, finalized=False, reason="SUPERSEDED_OR_LEASE_LOST")

    # 只读远端，不持有任何 DB 锁与 Agent 并发额度。
    # 租约围栏随收集传入：远端耗时期间失租/换代/取消，迟到写入无效。
    collect_timeout = min(
        get_collect_timeout_seconds(),
        max(0.0, (deadline - _now()).total_seconds()),
        max(0.0, (claim["lease_expires_at"] - _now()).total_seconds()),
    )
    fence = {
        "cycle_deadline": deadline,
        "collection_deadline": _now() + timedelta(seconds=collect_timeout),
        "evaluation_generation": evaluation_generation,
        "lease_token": claim["lease_token"],
        "dispatch_generation": expected_dispatch,
    }
    factory = client_factory or get_managed_score_client
    projection_reason = _managed_projection_reason(db_mgr, claim, output_digest)
    if projection_reason is not None:
        report = {"decisions": [{"reason": projection_reason}], "retry_after": None}
    else:
        report = _collect_with_timeout(
            db_mgr, factory, launch_id, item_id, fence=fence,
            timeout_seconds=collect_timeout,
        )

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

    # Deadline 是整个收集周期的绝对上界，不能被晚到的成功绕过。
    if _now() >= deadline:
        return _fail_managed(
            db_mgr, claim, error_code=MANAGED_DEADLINE_CODE,
            error_message="托管评分等待已超时，迟到评分未采纳；质量保持 UNKNOWN。", launch_id=launch_id,
        )
    # 已存成功行不等于本轮证据完整：歧义/冲突/读失败必须阻塞终结。
    if complete and _round_cleanly_adopted(report, required):
        return _finalize_success(db_mgr, claim, launch_id=launch_id, report=report, required=required)

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
    if started_at is not None and (now - started_at).total_seconds() >= float(cfg["deadline"]):
        return _fail_managed(
            db_mgr, claim, error_code=MANAGED_DEADLINE_CODE,
            error_message="托管评分等待已超时，质量保持 UNKNOWN，可在 Langfuse 出分后手动重评。",
            launch_id=launch_id,
        )
    return _schedule_retry(
        db_mgr, claim, reason=reason, launch_id=launch_id, retry_after=(report or {}).get("retry_after"),
    )



