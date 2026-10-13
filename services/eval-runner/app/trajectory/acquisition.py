"""按冻结 Attempt 限界的分页轮次；原始页不持久化，不调用业务 Agent。"""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from weakref import WeakValueDictionary

from .assembler import assemble_trajectory
from .codec import canonical_dumps
from .contract import TrajectoryContract
from .evidence import TrajectoryEvidenceUnavailableError, _contract, persist_trajectory_evidence
from .normalizer import NormalizedObservations, ObservationConvergence, normalize_observations
from .reader import ObservationPage, ObservationReader, ObservationReadError, _cursor_valid
from .schema import TrajectoryAcquisition, TrajectoryScope

_RUNTIME = ContextVar("trajectory_acquisition_runtime", default=None)
_LIMITERS = WeakValueDictionary()
_RETRYABLE = frozenset({"TRAJECTORY_SOURCE_TIMEOUT", "TRAJECTORY_SOURCE_TRANSPORT_ERROR",
                       "TRAJECTORY_SOURCE_RATE_LIMITED", "TRAJECTORY_SOURCE_TEMPORARY_ERROR"})
_PERMANENT = frozenset({"TRAJECTORY_SOURCE_AUTH_FAILED", "TRAJECTORY_SOURCE_CONTRACT_ERROR",
    "TRAJECTORY_SOURCE_ENCODING_UNSUPPORTED", "TRAJECTORY_SOURCE_REDIRECT_REJECTED",
    "TRAJECTORY_SOURCE_JSON_INVALID", "TRAJECTORY_SOURCE_DEPTH_LIMIT", "TRAJECTORY_SOURCE_STRING_LIMIT",
    "TRAJECTORY_SOURCE_NUMBER_LIMIT", "TRAJECTORY_SOURCE_UNQUALIFIED", "TRAJECTORY_SOURCE_CONFIGURATION_INVALID",
    "TRAJECTORY_SOURCE_API_CONTRACT_MISMATCH", "TRAJECTORY_SOURCE_API_DOCUMENT_LIMIT",
    "TRAJECTORY_SOURCE_READER_ARTIFACT_UNAVAILABLE"})


class AcquisitionLimiter:
    """同一事件循环共享四个全局槽，并遵守各冻结合同更低的限额。"""

    def __init__(self):
        self.global_slots = asyncio.Semaphore(4)
        self._groups = {limit: asyncio.Semaphore(limit) for limit in range(1, 5)}

    @asynccontextmanager
    async def slot(self, limit):
        async with self._groups[limit], self.global_slots:
            yield


class _Clock:
    @staticmethod
    def utcnow():
        return datetime.now(UTC)

    @staticmethod
    def monotonic():
        return monotonic()


class AcquisitionRuntime:
    """协调器注入原始开始时间/事务工厂，Reader 不承担写权限。

    下一阶段协调器持久化 PENDING.deadline_at 并以 lease/CAS 发布最终 Ref。
    恢复使用同一 started_at；quiet 证明只能由新进程重新观测，不能继承。
    """

    def __init__(self, scope, contract, *, session_factory, started_at, source_bytes_used=0, clock=None, limiter=None):
        _contract(contract)
        self.scope = TrajectoryScope.model_validate(scope.model_dump())
        self.contract = deepcopy(contract)
        self.clock = clock or _Clock()
        now = self.clock.utcnow()
        if (type(started_at) is not datetime or started_at.tzinfo is None
            or started_at > now or not callable(session_factory)
            or type(source_bytes_used) is not int or not 0 <= source_bytes_used <= contract.limits.max_source_bytes):
            raise ValueError("TRAJECTORY_ACQUISITION_RUNTIME_INVALID")
        self.deadline_at = started_at.astimezone(UTC) + timedelta(seconds=float(contract.limits.deadline_seconds))
        self._mono_deadline = self.clock.monotonic() + max(0, (self.deadline_at - now).total_seconds())
        loop = asyncio.get_running_loop()
        if limiter is None:
            limiter = _LIMITERS.setdefault(loop, AcquisitionLimiter())
        self.limiter, self._session_factory = limiter, session_factory
        self._lock = asyncio.Lock()
        self._latest = self._final = self._signature = self._stable_since = self._last_error = None
        self._wire_bytes = source_bytes_used
        self._candidate_invalidated = False

    @contextmanager
    def activate(self):
        token = _RUNTIME.set(self)
        try:
            yield self
        finally:
            _RUNTIME.reset(token)

    def remaining(self):
        # 单调预算不能被墙钟回拨延长；墙钟前跳也不能重置原截止。
        return max(0, min(self._mono_deadline - self.clock.monotonic(),
                          (self.deadline_at - self.clock.utcnow()).total_seconds()))

    def result(self, state, *, trajectory=None, ref=None, reason=None, retryable=False):
        return TrajectoryAcquisition(scope=self.scope, state=state,
            completeness=trajectory.completeness if trajectory else None, evidence_ref=ref,
            reason_code=reason, retryable=retryable, deadline_at=self.deadline_at)

    async def freeze(self, trajectory):
        def save():
            with self._session_factory() as session:
                return persist_trajectory_evidence(session, trajectory, self.contract)
        try:
            # Root-only 必须到读截止后才固化；保存使用冻结单请求限额，独立于读期限。
            async with asyncio.timeout(float(self.contract.limits.request_timeout_seconds)):
                ref = await asyncio.to_thread(save)
            result = self.result("READY", trajectory=trajectory, ref=ref)
        except TimeoutError:
            result = self.result("UNAVAILABLE", reason="TRAJECTORY_EVIDENCE_STORAGE_TIMEOUT", retryable=True)
        except TrajectoryEvidenceUnavailableError as error:
            result = self.result("UNAVAILABLE", reason=error.reason_code, retryable=False)
        except Exception:
            result = self.result("UNAVAILABLE", reason="TRAJECTORY_EVIDENCE_STORAGE_UNAVAILABLE", retryable=True)
        self._final = result
        self._latest = None
        return deepcopy(result)

    async def finish(self):
        if self._latest is not None:
            return await self.freeze(assemble_trajectory(self.scope, self._latest, self.contract))
        elif self._last_error is not None:
            self._final = self.result("UNAVAILABLE", reason=self._last_error, retryable=True)
        else:
            self._final = self.result("UNAVAILABLE", reason="TRAJECTORY_ROOT_UNAVAILABLE")
        return deepcopy(self._final)


def _read_error(error):
    code = error.reason_code
    if code == "TRAJECTORY_SOURCE_BYTE_LIMIT":
        return code, False
    if code in _RETRYABLE:
        return code, True
    return (code if code in _PERMANENT else "TRAJECTORY_SOURCE_CONTRACT_ERROR"), False


def _partial_batch(runtime, normalized):
    """部分轮次可保留旧的已验证闭包；重复冲突留给图阶段，不挑赢家。"""
    previous = runtime._latest
    if previous is None:
        return normalized
    limits = runtime.contract.limits
    records, seen = [], set()
    candidates = normalized.records + previous.records
    candidates.sort(key=lambda record: record.observation_id != runtime.scope.root_observation_id)
    for record in candidates:
        fingerprint = canonical_dumps(record, max_bytes=8 * 1024 * 1024)
        if fingerprint not in seen:
            seen.add(fingerprint)
            records.append(record.model_dump())
    cap = limits.page_size * limits.max_pages
    warnings = set(normalized.warnings + previous.warnings)
    overflow = len(records) > cap or normalized.source_bytes + previous.source_bytes > limits.max_source_bytes
    if overflow:
        warnings.add("SOURCE_ROW_LIMIT")
    return NormalizedObservations.model_validate(normalized.model_dump() | {
        "records": records[:cap], "source_bytes": min(normalized.source_bytes + previous.source_bytes, limits.max_source_bytes),
        "source_bytes_complete": False, "truncated": normalized.truncated or previous.truncated or overflow,
        "warnings": sorted(warnings)})


async def _round(runtime, client):
    limits, scope = runtime.contract.limits, runtime.scope
    rows, cursor, seen, warnings = [], None, set(), []
    complete, truncated, read_error = False, False, None
    for _ in range(limits.max_pages):
        remaining = runtime.remaining()
        allowance = limits.max_source_bytes - runtime._wire_bytes
        if remaining <= 0:
            warnings.append("SOURCE_BATCH_DEADLINE")
            break
        if allowance <= 0:
            warnings.append("SOURCE_BATCH_BYTE_LIMIT")
            truncated = True
            break
        try:
            timeout = min(float(limits.request_timeout_seconds), remaining)
            async with asyncio.timeout(timeout):
                page = await client.read_page(scope, cursor=cursor, page_size=limits.page_size,
                                             timeout_seconds=timeout, max_bytes=allowance)
        except asyncio.CancelledError:
            # 已开始读取但未报告字节；取消仍扣未知额度并原样传播。
            runtime._wire_bytes += allowance
            raise
        except TimeoutError:
            # 外层取消先于 Reader 的计数报告时，未知在途字节不能退回额度。
            runtime._wire_bytes += allowance
            if runtime.remaining() <= 0:
                if not rows and runtime._latest is not None:
                    return runtime._latest, False, runtime.clock.monotonic(), None
                warnings.append("SOURCE_BATCH_DEADLINE")
                break
            read_error = "TRAJECTORY_SOURCE_TIMEOUT"
            warnings.append("SOURCE_BATCH_TEMPORARY_ERROR")
            break
        except ObservationReadError as error:
            if type(error.source_bytes) is not int or error.source_bytes < 0:
                raise ObservationReadError("TRAJECTORY_SOURCE_CONTRACT_ERROR", retryable=False) from None
            runtime._wire_bytes += min(error.source_bytes, allowance)
            if error.reason_code == "TRAJECTORY_SOURCE_TIMEOUT" and runtime.remaining() <= 0:
                if not rows and runtime._latest is not None:
                    return runtime._latest, False, runtime.clock.monotonic(), None
                warnings.append("SOURCE_BATCH_DEADLINE")
                break
            if error.reason_code != "TRAJECTORY_SOURCE_BYTE_LIMIT":
                code, retryable = _read_error(error)
                if not retryable:
                    raise
                read_error = code
                warnings.append("SOURCE_BATCH_TEMPORARY_ERROR")
                break
            warnings.append("SOURCE_BATCH_BYTE_LIMIT")
            truncated = True
            break
        except Exception:
            raise ObservationReadError("TRAJECTORY_SOURCE_CONTRACT_ERROR", retryable=False) from None
        if (type(page) is not ObservationPage or type(page.rows) is not tuple or len(page.rows) > limits.page_size
            or any(type(row) is not dict for row in page.rows) or not _cursor_valid(page.cursor)
            or type(page.source_bytes) is not int or not 0 < page.source_bytes <= allowance):
            raise ObservationReadError("TRAJECTORY_SOURCE_CONTRACT_ERROR", retryable=False)
        runtime._wire_bytes += page.source_bytes
        rows.extend(page.rows)
        if runtime._wire_bytes >= limits.max_source_bytes:
            warnings.append("SOURCE_BATCH_BYTE_LIMIT")
            truncated = True
            break
        if page.cursor is None:
            complete = True
            break
        if page.cursor in seen:
            warnings.append("SOURCE_BATCH_CURSOR_CYCLE")
            break
        seen.add(page.cursor)
        cursor = page.cursor
    else:
        warnings.append("SOURCE_BATCH_PAGE_LIMIT")
    observed_at = runtime.clock.monotonic()
    if runtime.remaining() <= 0:
        complete = False
        warnings.append("SOURCE_BATCH_DEADLINE")
    reusing = not rows and "SOURCE_BATCH_BYTE_LIMIT" in warnings and runtime._latest is not None
    normalized = runtime._latest if reusing else normalize_observations(rows, runtime.contract, scope=scope)
    previous = runtime._latest
    if (set(warnings) == {"SOURCE_BATCH_DEADLINE"} and previous is not None
        and previous.source_bytes_complete and not previous.truncated and not normalized.warnings):
        known = {canonical_dumps(record) for record in previous.records}
        if all(canonical_dumps(record) in known for record in normalized.records):
            # 截止轮次没有新事实时保留此前完整快照；不补造分页或 quiet 证明。
            return previous, False, observed_at, read_error
    if not complete:
        warnings.append("SOURCE_BATCH_PAGING_INCOMPLETE")
    data = normalized.model_dump()
    data.update(source_bytes_complete=normalized.source_bytes_complete and complete,
                truncated=normalized.truncated or truncated, warnings=sorted(set(normalized.warnings + warnings)))
    normalized = NormalizedObservations.model_validate(data)
    if not complete and not reusing:
        normalized = _partial_batch(runtime, normalized)
    return normalized, complete, observed_at, read_error


async def acquire_trajectory(scope, contract: TrajectoryContract, *, client: ObservationReader) -> TrajectoryAcquisition:
    """读取一轮；暂态 PENDING，收敛或截止后只返回真实已保存的证据。"""
    runtime = _RUNTIME.get()
    if runtime is None:
        raise ValueError("TRAJECTORY_ACQUISITION_RUNTIME_REQUIRED")
    _contract(contract)
    if (scope != runtime.scope or contract.digest != runtime.contract.digest
        or contract.source_profile != runtime.contract.source_profile):
        raise ValueError("TRAJECTORY_ACQUISITION_RUNTIME_MISMATCH")
    async with runtime._lock:
        if runtime._final is not None:
            return deepcopy(runtime._final)
        remaining = runtime.remaining()
        if remaining <= 0:
            return await runtime.finish()
        runtime._candidate_invalidated = False
        normalized = None
        try:
            async with asyncio.timeout(remaining) as gate_timeout:
                async with runtime.limiter.slot(contract.limits.max_inflight_acquisitions):
                    # 等待槽计入原期限；随后每请求限时，允许轮次保留已读页。
                    gate_timeout.reschedule(None)
                    normalized, paging, observed_at, read_error = await _round(runtime, client)
            runtime._last_error = read_error
            trajectory = assemble_trajectory(scope, normalized, runtime.contract)
        except TimeoutError:
            runtime._mono_deadline = runtime.clock.monotonic()
            if runtime._latest is None:
                runtime._last_error = "TRAJECTORY_SOURCE_TIMEOUT"
            return await runtime.finish()
        except ObservationReadError as error:
            code, retryable = _read_error(error)
            runtime._signature = runtime._stable_since = None
            runtime._last_error = code
            if not retryable or runtime.remaining() <= 0:
                result = runtime.result("UNAVAILABLE", reason=code, retryable=retryable)
                runtime._final = result
                runtime._latest = None
                runtime._candidate_invalidated = not retryable
                return result
            return runtime.result("PENDING", reason=code, retryable=True)
        except ValueError:
            # Root 尚不可验证时继续等；不能补造 Root 或保留原始异常文本。
            runtime._latest = runtime._signature = runtime._stable_since = None
            runtime._candidate_invalidated = normalized is None or any(
                record.observation_id == scope.root_observation_id for record in normalized.records)
            if runtime.remaining() <= 0:
                return await runtime.finish()
            return runtime.result("PENDING", reason=runtime._last_error or "TRAJECTORY_ROOT_UNAVAILABLE", retryable=True)
        runtime._latest = normalized
        if runtime.remaining() <= 0:
            return await runtime.finish()
        now = observed_at
        signature = trajectory.provenance.trajectory_digest
        if not paging or signature != runtime._signature:
            runtime._signature, runtime._stable_since = signature, now
        quiet = Decimal(str(max(0, now - runtime._stable_since)))
        proof = ObservationConvergence(normalized_digest=normalized.semantic_digest, paging_complete=paging,
                                       quiet_window_seconds=min(quiet, Decimal(60)))
        proved = NormalizedObservations.model_validate(normalized.model_dump() | {"convergence": proof})
        trajectory = assemble_trajectory(scope, proved, runtime.contract)
        if trajectory.completeness.status == "OBSERVED_COMPLETE":
            return await runtime.freeze(trajectory)
        if runtime.remaining() <= 0:
            return await runtime.finish()
        return runtime.result("PENDING", trajectory=trajectory, reason=read_error or "TRAJECTORY_ACQUISITION_PENDING", retryable=True)
