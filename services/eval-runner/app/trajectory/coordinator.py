"""采集协调器：独立短事务、原期限及预算恢复；不调用业务 Agent。"""

import asyncio
from copy import deepcopy
from threading import Event

from sqlalchemy import text

from .acquisition import _RETRYABLE, AcquisitionRuntime, _Clock, acquire_trajectory
from .assembler import assemble_trajectory
from .checkpoint import (
    TrajectoryCheckpointError,
    abort_trajectory_round,
    begin_trajectory_round,
    publish_trajectory_round,
)
from .evidence import _contract
from .schema import TrajectoryAcquisition, TrajectoryScope


class TrajectoryCoordinatorError(RuntimeError):
    def __init__(self, code):
        self.reason_code = "TRAJECTORY_COORDINATOR_" + code
        super().__init__(self.reason_code)


def _consume_exception(task):
    if not task.cancelled():
        task.exception()


class TrajectoryCoordinator:
    """调用方负责 Reader 生命周期与现有执行租约；不创建 Attempt/租约。"""

    def __init__(self, session_factory, *, clock=None, sleep=None):
        if not callable(session_factory):
            raise ValueError("TRAJECTORY_COORDINATOR_CONFIGURATION_INVALID")
        self._session_factory = session_factory
        self._clock = clock or _Clock()
        self._sleep = sleep or asyncio.sleep

    async def _transaction(self, contract, action):
        abandoned = Event()

        def execute():
            with self._session_factory() as session:
                if session.get_bind().dialect.name == "postgresql":
                    budget = f"{max(1, int(float(contract.limits.request_timeout_seconds) * 1000))}ms"
                    for name in ("lock_timeout", "statement_timeout"):
                        session.execute(text("SELECT set_config(:name, :budget, true)"), {"name": name, "budget": budget})
                result = action(session)
                # to_thread 的取消不能停止线程；已弃用事务必须整体回滚。
                if abandoned.is_set():
                    raise TrajectoryCoordinatorError("TRANSACTION_ABANDONED")
            return result

        task = asyncio.create_task(asyncio.to_thread(execute))
        try:
            async with asyncio.timeout(float(contract.limits.request_timeout_seconds)):
                return await asyncio.shield(task)
        except asyncio.CancelledError:
            abandoned.set()
            task.add_done_callback(_consume_exception)
            raise
        except TimeoutError:
            abandoned.set()
            task.add_done_callback(_consume_exception)
            raise TrajectoryCoordinatorError("STORAGE_TIMEOUT") from None
        except TrajectoryCheckpointError:
            raise
        except Exception:
            raise TrajectoryCoordinatorError("STORAGE_UNAVAILABLE") from None

    async def run(self, scope, contract, *, client, lease_token, cancelled=None):
        """内部执行入口；公开 acquire_trajectory 的 Reader-only 签名保持不变。"""
        _contract(contract)
        scope = TrajectoryScope.model_validate(scope.model_dump())
        contract = deepcopy(contract)
        if cancelled is None:
            return await self._run(scope, contract, client, lease_token)
        if cancelled.is_set():
            raise asyncio.CancelledError
        work = asyncio.create_task(self._run(scope, contract, client, lease_token, cancelled))
        watcher = asyncio.create_task(cancelled.wait())
        try:
            done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                work.cancel()
            return await work
        finally:
            watcher.cancel()
            if not work.done():
                work.cancel()
            await asyncio.gather(work, watcher, return_exceptions=True)

    def _candidate(self, runtime, claim, contract):
        if runtime._latest is not None:
            return assemble_trajectory(claim.acquisition.scope, runtime._latest, contract)
        return None if runtime._candidate_invalidated else claim.candidate

    def _terminal_fallback(self, result, runtime, claim):
        # 仅读截止后保留已校验且未失效的候选；没有新分页/quiet 证明。
        if (result.state == "UNAVAILABLE" and runtime.remaining() <= 0
            and not runtime._candidate_invalidated and claim.candidate is not None
            and result.reason_code in _RETRYABLE | {"TRAJECTORY_ROOT_UNAVAILABLE"}):
            return TrajectoryAcquisition(scope=result.scope, state="READY", completeness=claim.candidate.completeness,
                evidence_ref=claim.candidate_ref, reason_code=None, retryable=False, deadline_at=result.deadline_at)
        return result

    async def _publish(self, claim, contract, lease_token, result, source_bytes_used, candidate):
        return await self._transaction(contract, lambda session: publish_trajectory_round(
            session, claim, contract, lease_token=lease_token, result=result,
            source_bytes_used=source_bytes_used, candidate=candidate, now=self._clock.utcnow))

    async def _run(self, scope, contract, client, lease_token, cancelled=None):
        runtime, claim = None, None
        try:
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise asyncio.CancelledError
                claim = await self._transaction(contract, lambda session: begin_trajectory_round(
                    session, scope, contract, lease_token=lease_token, now=self._clock.utcnow))
                if claim.acquisition.state != "PENDING":
                    return claim.acquisition
                if runtime is None:
                    runtime = AcquisitionRuntime(scope, contract, session_factory=self._session_factory,
                        started_at=claim.started_at, source_bytes_used=claim.source_bytes_used, clock=self._clock)
                if runtime._wire_bytes != claim.source_bytes_used:
                    raise TrajectoryCoordinatorError("BUDGET_MISMATCH")
                if cancelled is not None and cancelled.is_set():
                    raise asyncio.CancelledError
                with runtime.activate():
                    result = await acquire_trajectory(scope, contract, client=client)
                if cancelled is not None and cancelled.is_set():
                    raise asyncio.CancelledError
                candidate = self._candidate(runtime, claim, contract) if result.state == "PENDING" else None
                result = self._terminal_fallback(result, runtime, claim)

                try:
                    frozen = await self._publish(claim, contract, lease_token, result, runtime._wire_bytes, candidate)
                except TrajectoryCheckpointError as error:
                    if error.reason_code != "TRAJECTORY_CHECKPOINT_DEADLINE_EXPIRED" or result.state != "PENDING":
                        raise
                    # 同一 claim 收尾；截止后 Acquire 不再读取源，保存另受请求预算。
                    with runtime.activate():
                        result = await acquire_trajectory(scope, contract, client=client)
                    candidate = None
                    result = self._terminal_fallback(result, runtime, claim)
                    frozen = await self._publish(claim, contract, lease_token, result, runtime._wire_bytes, candidate)
                claim = frozen
                if frozen.acquisition.state != "PENDING":
                    return frozen.acquisition
                interval = min(1.0, max(0.05, float(contract.limits.quiet_window_seconds)))
                await self._sleep(min(interval, runtime.remaining()))
        except BaseException:
            if claim is not None and claim.round_token is not None:
                try:
                    await self._transaction(contract, lambda session: abort_trajectory_round(
                        session, claim, contract, lease_token=lease_token, now=self._clock.utcnow))
                except (Exception, asyncio.CancelledError):
                    pass  # 失去租约/数据库不可用时留保守预留，不覆盖新执行者。
            raise
