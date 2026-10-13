import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import wraps

import pytest
from app.trajectory.acquisition import AcquisitionLimiter, AcquisitionRuntime, acquire_trajectory
from app.trajectory.codec import canonical_dumps
from app.trajectory.evidence import load_trajectory_evidence
from app.trajectory.reader import ObservationPage, ObservationReadError
from app.trajectory.schema import TrajectoryScope
from test_trajectory_assembler import configured, prepared
from test_trajectory_evidence import db as evidence_db


@pytest.fixture
def db(tmp_path):
    yield from evidence_db.__wrapped__(tmp_path)


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.base = datetime(2026, 10, 13, tzinfo=UTC)

    def utcnow(self):
        return self.base + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds


class Reader:
    def __init__(self, rounds):
        self.rounds, self.calls = rounds, []
        self.round = -1

    async def read_page(self, scope, **options):
        self.calls.append((scope, options))
        if options["cursor"] is None:
            self.round += 1
        pages = self.rounds[min(self.round, len(self.rounds) - 1)]
        page = pages[0 if options["cursor"] is None else int(options["cursor"])]
        if isinstance(page, Exception):
            raise page
        return page


def page(rows, cursor=None, *, size=None):
    return ObservationPage(tuple(deepcopy(rows)), cursor,
                           size if size is not None else len(canonical_dumps(rows).encode()))


def setup(db, **limits):
    manager, trajectory, contract = db
    contract = configured(contract, limits={"deadline_seconds": Decimal(10), "quiet_window_seconds": Decimal(2), **limits})
    clock = Clock()
    runtime = AcquisitionRuntime(trajectory.scope, contract, session_factory=manager.get_session,
                                 started_at=clock.utcnow(), clock=clock)
    rows, _, _ = prepared()
    return runtime, clock, contract, rows


async def collect(runtime, contract, reader):
    with runtime.activate():
        return await acquire_trajectory(runtime.scope, contract, client=reader)


def frozen(db, result, contract):
    with db[0].get_session() as session:
        return load_trajectory_evidence(session, result.evidence_ref, scope=result.scope, contract=contract)


@run_async
async def test_late_pages_and_quiet_converge_to_real_complete_artifact(db):
    runtime, clock, contract, rows = setup(db, page_size=3)
    reader = Reader([[page(rows[:1])], [page(rows[:3], "1"), page(rows[3:])]])
    first = await collect(runtime, contract, reader)
    assert first.state == "PENDING" and first.evidence_ref is None
    assert first.completeness.status == "ROOT_ONLY"
    clock.advance(2)
    assert (await collect(runtime, contract, reader)).state == "PENDING"
    clock.advance(2)
    ready = await collect(runtime, contract, reader)
    assert ready.state == "READY" and ready.completeness.status == "OBSERVED_COMPLETE"
    assert not ready.retryable and ready.deadline_at == first.deadline_at
    restored = frozen(db, ready, contract)
    assert len(restored.steps) == 6 and "synthetic-secret" not in canonical_dumps(restored)
    before = len(reader.calls)
    assert await collect(runtime, contract, reader) == ready
    assert len(reader.calls) == before


@run_async
async def test_reordered_equivalent_duplicates_do_not_restart_quiet(db):
    runtime, clock, contract, rows = setup(db)
    reader = Reader([[page(rows)], [page(list(reversed(rows)) + [rows[2]])]])
    assert (await collect(runtime, contract, reader)).state == "PENDING"
    clock.advance(2)
    ready = await collect(runtime, contract, reader)
    assert ready.state == "READY" and ready.completeness.status == "OBSERVED_COMPLETE"


@run_async
async def test_changed_semantic_evidence_restarts_quiet(db):
    runtime, clock, contract, rows = setup(db)
    changed = deepcopy(rows)
    changed[2]["metadata"]["attributes.workflow.duration_ms"] = 9
    reader = Reader([[page(rows)], [page(changed)]])
    await collect(runtime, contract, reader)
    clock.advance(2)
    assert (await collect(runtime, contract, reader)).state == "PENDING"
    clock.advance(2)
    assert (await collect(runtime, contract, reader)).state == "READY"


@run_async
@pytest.mark.parametrize("shape,expected", [("empty", "UNAVAILABLE"), ("root", "ROOT_ONLY"), ("partial", "PARTIAL")])
async def test_deadline_finalizes_only_available_verified_evidence(db, shape, expected):
    runtime, clock, contract, rows = setup(db)
    source = [] if shape == "empty" else rows[:1] if shape == "root" else rows[:3] + rows[-1:]
    reader = Reader([[page(source)]])
    assert (await collect(runtime, contract, reader)).state == "PENDING"
    clock.advance(10)
    final = await collect(runtime, contract, reader)
    assert len(reader.calls) == 1
    if expected == "UNAVAILABLE":
        assert final.state == expected and final.reason_code == "TRAJECTORY_ROOT_UNAVAILABLE"
        assert final.evidence_ref is None
    else:
        assert final.state == "READY" and final.completeness.status == expected
        assert frozen(db, final, contract).completeness == final.completeness


@run_async
@pytest.mark.parametrize("problem", ["page-limit", "cursor-cycle", "wire-limit", "reader-limit", "step-limit"])
async def test_limits_keep_partial_evidence_and_never_claim_complete(db, problem):
    limits = {"page_size": 3, "max_pages": 1} if problem == "page-limit" else {}
    if problem == "wire-limit":
        limits["max_source_bytes"] = 20000
    if problem == "step-limit":
        limits["max_steps"] = 3
    runtime, clock, contract, rows = setup(db, **limits)
    if problem == "page-limit":
        pages = [page(rows[:3], "1")]
    elif problem == "cursor-cycle":
        pages = [page(rows[:3], "1"), page(rows[3:], "1")]
    elif problem == "reader-limit":
        pages = [page(rows[:3], "1"), ObservationReadError("TRAJECTORY_SOURCE_BYTE_LIMIT", retryable=False)]
    else:
        pages = [page(rows, size=20000 if problem == "wire-limit" else None)]
    reader = Reader([pages])
    pending = await collect(runtime, contract, reader)
    assert pending.state == "PENDING"
    clock.advance(10)
    final = await collect(runtime, contract, reader)
    assert final.state == "READY" and final.completeness.status == "PARTIAL"
    restored = frozen(db, final, contract)
    assert any("LIMIT" in reason or "CURSOR" in reason for reason in restored.completeness.warnings)
    assert restored.completeness.capabilities["ordering"] != "COMPLETE"


@run_async
@pytest.mark.parametrize("code,retryable", [("TRAJECTORY_SOURCE_RATE_LIMITED", True),
    ("TRAJECTORY_SOURCE_TEMPORARY_ERROR", True), ("TRAJECTORY_SOURCE_TIMEOUT", True),
    ("TRAJECTORY_SOURCE_AUTH_FAILED", False), ("TRAJECTORY_SOURCE_CONTRACT_ERROR", False)])
async def test_transport_error_classification_and_original_deadline(db, code, retryable):
    runtime, clock, contract, rows = setup(db)
    reader = Reader([[ObservationReadError(code, retryable=retryable)], [page(rows)]])
    first = await collect(runtime, contract, reader)
    assert first.state == ("PENDING" if retryable else "UNAVAILABLE")
    assert first.reason_code == code and first.retryable == retryable
    clock.advance(2)
    second = await collect(runtime, contract, reader)
    assert second.deadline_at == first.deadline_at
    if retryable:
        assert second.state == "PENDING"
        clock.advance(2)
        assert (await collect(runtime, contract, reader)).state == "READY"
    else:
        assert second == first and len(reader.calls) == 1


@run_async
async def test_expired_retryable_transport_never_becomes_empty_root_evidence(db):
    runtime, clock, contract, _ = setup(db)
    reader = Reader([[ObservationReadError("TRAJECTORY_SOURCE_TIMEOUT", retryable=True)]])
    await collect(runtime, contract, reader)
    clock.advance(10)
    final = await collect(runtime, contract, reader)
    assert final.state == "UNAVAILABLE" and final.reason_code == "TRAJECTORY_SOURCE_TIMEOUT"
    assert final.evidence_ref is None and len(reader.calls) == 1


@run_async
async def test_recovery_runtime_uses_original_start_and_never_resets_deadline(db):
    runtime, clock, contract, rows = setup(db)
    reader = Reader([[page(rows)]])
    pending = await collect(runtime, contract, reader)
    clock.advance(8)
    recovered = AcquisitionRuntime(runtime.scope, contract, session_factory=db[0].get_session,
                                   started_at=clock.base, clock=clock)
    assert (await collect(recovered, contract, reader)).deadline_at == pending.deadline_at
    clock.advance(2)
    final = await collect(recovered, contract, reader)
    assert final.state == "READY" and final.completeness.status == "PARTIAL"


@run_async
async def test_runtime_scope_contract_profile_and_missing_context_fail_before_io(db):
    runtime, _, contract, rows = setup(db)
    reader = Reader([[page(rows)]])
    with pytest.raises(ValueError, match="RUNTIME_REQUIRED"):
        await acquire_trajectory(runtime.scope, contract, client=reader)
    with runtime.activate():
        other = TrajectoryScope.model_validate(runtime.scope.model_dump() | {"attempt_id": "attempt-2"})
        with pytest.raises(ValueError, match="RUNTIME_MISMATCH"):
            await acquire_trajectory(other, contract, client=reader)
        with pytest.raises(ValueError, match="RUNTIME_MISMATCH"):
            await acquire_trajectory(runtime.scope, configured(contract, limits={"max_steps": 9}), client=reader)
    assert reader.calls == []


@run_async
async def test_unknown_external_exception_text_is_not_a_reason_or_artifact(db):
    runtime, _, contract, rows = setup(db)
    reader = Reader([[ObservationReadError("SYNTHETIC_SECRET_ABC123", retryable=False)]])
    final = await collect(runtime, contract, reader)
    assert final.state == "UNAVAILABLE" and final.reason_code == "TRAJECTORY_SOURCE_CONTRACT_ERROR"
    assert "SYNTHETIC_SECRET" not in canonical_dumps(final)


@run_async
async def test_cancel_and_timeout_release_shared_acquisition_slots(db):
    runtime, _, contract, rows = setup(db, request_timeout_seconds=Decimal("0.05"), max_inflight_acquisitions=1)
    limiter = AcquisitionLimiter()
    entered = asyncio.Event()
    class Slow:
        async def read_page(self, *args, **options):
            entered.set()
            await asyncio.Event().wait()
    runtime.limiter = limiter
    task = asyncio.create_task(collect(runtime, contract, Slow()))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await collect(runtime, contract, Reader([[page(rows)]]))).state == "PENDING"
    result = await collect(runtime, contract, Slow())
    assert result.state == "PENDING" and result.reason_code == "TRAJECTORY_SOURCE_TIMEOUT"
    assert (await collect(runtime, contract, Reader([[page(rows)]]))).state == "PENDING"


@run_async
async def test_waiting_for_shared_slot_is_in_the_original_deadline(db):
    runtime, _, contract, rows = setup(db, deadline_seconds=Decimal("0.05"), quiet_window_seconds=Decimal("0.01"))
    # 实际 asyncio timeout 使用真实时钟；手动占满进程共享槽。
    runtime.limiter = AcquisitionLimiter()
    for _ in range(4):
        await runtime.limiter.global_slots.acquire()
    reader = Reader([[page(rows)]])
    try:
        result = await asyncio.wait_for(collect(runtime, contract, reader), 1)
        assert result.state == "UNAVAILABLE" and reader.calls == []
    finally:
        for _ in range(4):
            runtime.limiter.global_slots.release()


@run_async
async def test_wire_budget_exhaustion_keeps_prior_verified_evidence(db):
    runtime, clock, contract, rows = setup(db, max_source_bytes=20000)
    reader = Reader([[page(rows, size=15000)], [ObservationReadError("TRAJECTORY_SOURCE_BYTE_LIMIT", retryable=False)]])
    await collect(runtime, contract, reader)
    clock.advance(1)
    pending = await collect(runtime, contract, reader)
    assert pending.completeness is not None
    clock.advance(9)
    final = await collect(runtime, contract, reader)
    assert final.state == "READY" and final.completeness.status == "PARTIAL"
    assert len(frozen(db, final, contract).steps) == 6


@run_async
async def test_page_processing_after_deadline_cannot_create_complete_proof(db):
    runtime, clock, contract, rows = setup(db, quiet_window_seconds=Decimal(0))
    class Late:
        async def read_page(self, *args, **options):
            clock.advance(11)
            return page(rows)
    final = await collect(runtime, contract, Late())
    assert final.state == "READY" and final.completeness.status == "PARTIAL"


@run_async
async def test_storage_factory_error_never_exposes_text_or_returns_fake_ref(db):
    runtime, _, contract, rows = setup(db, quiet_window_seconds=Decimal(0))
    def broken():
        raise RuntimeError("synthetic-secret-database")
    runtime._session_factory = broken
    final = await collect(runtime, contract, Reader([[page(rows)]]))
    assert final.state == "UNAVAILABLE" and final.reason_code == "TRAJECTORY_EVIDENCE_STORAGE_UNAVAILABLE"
    assert "synthetic-secret" not in canonical_dumps(final) and final.evidence_ref is None


@run_async
async def test_source_contract_shape_and_arbitrary_reader_exception_are_safe(db):
    for response in (ObservationPage((), "bad cursor", 1), ObservationPage((), None, True),
                     ObservationPage([], None, 1), RuntimeError("synthetic-secret-reader")):
        runtime, _, contract, _ = setup(db)
        result = await collect(runtime, contract, Reader([[response]]))
        assert result.state == "UNAVAILABLE" and result.reason_code == "TRAJECTORY_SOURCE_CONTRACT_ERROR"
        assert "synthetic-secret" not in canonical_dumps(result)


@run_async
async def test_quiet_without_terminal_inventory_cannot_claim_complete(db):
    runtime, clock, contract, rows = setup(db)
    reader = Reader([[page(rows[:-1])]])
    await collect(runtime, contract, reader)
    clock.advance(2)
    assert (await collect(runtime, contract, reader)).state == "PENDING"
    clock.advance(8)
    final = await collect(runtime, contract, reader)
    assert final.state == "READY" and final.completeness.status == "PARTIAL"
    assert "TERMINAL_INVENTORY_MISSING" in final.completeness.warnings


@run_async
async def test_shared_limiter_bounds_actual_parallel_reads_across_runtime_instances(db):
    active = peak = 0
    runtime, clock, contract, rows = setup(db, max_inflight_acquisitions=2)
    limiter = AcquisitionLimiter()
    class Concurrent:
        async def read_page(self, *args, **options):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                return page(rows)
            finally:
                active -= 1
    runtimes = [AcquisitionRuntime(runtime.scope, contract, session_factory=db[0].get_session,
                started_at=clock.base, clock=clock, limiter=limiter) for _ in range(7)]
    results = await asyncio.gather(*(collect(r, contract, Concurrent()) for r in runtimes))
    assert peak == 2 and active == 0 and all(result.state == "PENDING" for result in results)


@run_async
async def test_wall_clock_reversal_does_not_extend_monotonic_budget(db):
    runtime, clock, contract, rows = setup(db)
    await collect(runtime, contract, Reader([[page(rows[:1])]]))
    clock.advance(10)
    clock.base -= timedelta(days=1)
    final = await collect(runtime, contract, Reader([[page(rows)]]))
    assert final.state == "READY" and final.completeness.status == "ROOT_ONLY"


@run_async
async def test_storage_timeout_and_cancellation_never_publish_ready(db):
    import time
    from contextlib import contextmanager
    runtime, _, contract, rows = setup(db, quiet_window_seconds=Decimal(0), request_timeout_seconds=Decimal("0.01"))
    @contextmanager
    def delayed():
        time.sleep(0.03)
        with db[0].get_session() as session:
            yield session
    runtime._session_factory = delayed
    final = await collect(runtime, contract, Reader([[page(rows)]]))
    assert final.state == "UNAVAILABLE" and final.reason_code == "TRAJECTORY_EVIDENCE_STORAGE_TIMEOUT"
    assert final.evidence_ref is None


@run_async
async def test_cancel_during_owned_store_does_not_publish_an_acquisition_ref(db):
    import time
    from contextlib import contextmanager

    from app.db_models import ExecutionCheckpointRecord
    runtime, _, contract, rows = setup(db, quiet_window_seconds=Decimal(0))
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    @contextmanager
    def delayed():
        loop.call_soon_threadsafe(entered.set)
        time.sleep(0.03)
        with db[0].get_session() as session:
            yield session
    runtime._session_factory = delayed
    task = asyncio.create_task(collect(runtime, contract, Reader([[page(rows)]])))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert runtime._final is None
    with db[0].get_session() as session:
        assert session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_acquisition_json is None


@run_async
async def test_frozen_profile_mismatch_is_rejected_before_io(db):
    from app.trajectory.contract import TrajectoryContract
    from test_trajectory_contract import version
    runtime, clock, contract, rows = setup(db)
    payload = contract.model_dump()
    payload["source_profile_set"].append(payload["source_profile_set"][0] | {"profile_id": "native-alternative"})
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    runtime = AcquisitionRuntime(runtime.scope, contract, session_factory=db[0].get_session,
                                 started_at=clock.base, clock=clock)
    reader = Reader([[page(rows)]])
    with runtime.activate(), pytest.raises(ValueError, match="RUNTIME_MISMATCH"):
        await acquire_trajectory(runtime.scope, contract.bind_source_version(version("native-alternative")), client=reader)
    assert reader.calls == []


@run_async
async def test_idle_runtime_and_default_limiter_do_not_retain_an_event_loop(db):
    import gc
    import weakref
    runtime, _, _, _ = setup(db)
    reference = weakref.ref(runtime.limiter)
    del runtime
    gc.collect()
    assert reference() is None


@run_async
async def test_request_timeout_at_deadline_preserves_previously_verified_root(db):
    runtime, clock, contract, rows = setup(db)
    reader = Reader([[page(rows[:1])]])
    await collect(runtime, contract, reader)
    clock.advance(1)
    class DeadlineTimeout:
        async def read_page(self, *args, **options):
            clock.advance(9)
            raise TimeoutError
    final = await collect(runtime, contract, DeadlineTimeout())
    assert final.state == "READY" and final.completeness.status == "ROOT_ONLY"
    assert len(frozen(db, final, contract).steps) == 1


@pytest.mark.parametrize("last_rows", ["same-root", "empty", "new-step", "changed-root"])
@run_async
async def test_deadline_round_preserves_snapshot_only_without_new_or_conflicting_facts(db, last_rows):
    runtime, clock, contract, rows = setup(db)
    await collect(runtime, contract, Reader([[page(rows[:1])]]))
    current = [] if last_rows == "empty" else deepcopy(rows[:1])
    if last_rows == "new-step":
        current += rows[1:2]
    elif last_rows == "changed-root":
        current[0]["output"] = {"intent": "different"}
    class DeadlinePage:
        async def read_page(self, *args, **options):
            clock.advance(10)
            return page(current)
    final = await collect(runtime, contract, DeadlinePage())
    if last_rows == "changed-root":
        assert final.state == "UNAVAILABLE" and final.evidence_ref is None
    else:
        assert final.state == "READY"
        assert final.completeness.status == ("PARTIAL" if last_rows == "new-step" else "ROOT_ONLY")
        assert len(frozen(db, final, contract).steps) == (2 if last_rows == "new-step" else 1)


@run_async
async def test_real_deadline_keeps_already_read_pages_when_next_page_times_out(db):
    runtime, clock, contract, rows = setup(db, deadline_seconds=Decimal("0.05"), quiet_window_seconds=Decimal("0.01"), page_size=3)
    phases = []
    class SlowNext:
        async def read_page(self, *args, **options):
            if options["cursor"] is None:
                phases.append("first-page")
                return page(rows[:3], "1")
            try:
                await asyncio.Event().wait()
            finally:
                # 请求仍由真实 asyncio.timeout 取消；逻辑截止在此发生，
                # 不让 CI 的合同校验/归一化 CPU 速度决定“首查已经到达”的前提。
                clock.advance(0.05)
                phases.append("second-page-cancelled")
    final = await collect(runtime, contract, SlowNext())
    assert final.state == "READY" and final.completeness.status == "PARTIAL"
    assert len(frozen(db, final, contract).steps) == 3
    assert phases == ["first-page", "second-page-cancelled"]


@run_async
async def test_normalization_elapsed_is_not_quiet_observation_time(db, monkeypatch):
    import app.trajectory.acquisition as module
    runtime, clock, contract, rows = setup(db)
    original, calls = module.normalize_observations, 0
    def slow(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 2:
            clock.advance(2)
        return result
    monkeypatch.setattr(module, "normalize_observations", slow)
    reader = Reader([[page(rows)]])
    await collect(runtime, contract, reader)
    clock.advance(0.1)
    assert (await collect(runtime, contract, reader)).state == "PENDING"


@pytest.mark.parametrize("code", ["TRAJECTORY_SOURCE_RATE_LIMITED", "TRAJECTORY_SOURCE_TEMPORARY_ERROR",
                                  "TRAJECTORY_SOURCE_TIMEOUT"])
@run_async
async def test_temporary_failure_after_a_page_preserves_independent_steps_at_deadline(db, code):
    runtime, clock, contract, rows = setup(db, page_size=3)
    reader = Reader([[page(rows[:3], "1"), ObservationReadError(code, retryable=True)]])
    pending = await collect(runtime, contract, reader)
    assert pending.state == "PENDING" and pending.reason_code == code
    assert pending.completeness is not None
    clock.advance(10)
    final = await collect(runtime, contract, reader)
    assert final.state == "READY" and final.completeness.status == "PARTIAL"
    assert {step.step_id for step in frozen(db, final, contract).steps} == {row["id"] for row in rows[:3]}


@run_async
async def test_partial_retry_round_without_root_retains_the_prior_verified_closure(db):
    runtime, clock, contract, rows = setup(db, page_size=6)
    reader = Reader([[page(rows)], [page(rows[1:3], "1"),
        ObservationReadError("TRAJECTORY_SOURCE_RATE_LIMITED", retryable=True)]])
    await collect(runtime, contract, reader)
    clock.advance(1)
    pending = await collect(runtime, contract, reader)
    assert pending.completeness is not None
    clock.advance(9)
    final = await collect(runtime, contract, reader)
    assert final.state == "READY" and final.completeness.status == "PARTIAL"
    assert len(frozen(db, final, contract).steps) == 6


@run_async
async def test_healthy_round_after_partial_retry_must_observe_quiet_again(db):
    runtime, clock, contract, rows = setup(db)
    reader = Reader([[page(rows[:3], "1"), ObservationReadError("TRAJECTORY_SOURCE_RATE_LIMITED", retryable=True)],
                     [page(rows)]])
    await collect(runtime, contract, reader)
    clock.advance(2)
    assert (await collect(runtime, contract, reader)).state == "PENDING"
    clock.advance(2)
    assert (await collect(runtime, contract, reader)).completeness.status == "OBSERVED_COMPLETE"


@run_async
async def test_partial_retry_merge_does_not_choose_a_winner_for_conflicting_root(db):
    runtime, clock, contract, rows = setup(db)
    changed = deepcopy(rows[0])
    changed["output"] = {"intent": "different"}
    reader = Reader([[page(rows)], [page([changed], "1"),
        ObservationReadError("TRAJECTORY_SOURCE_RATE_LIMITED", retryable=True)]])
    await collect(runtime, contract, reader)
    clock.advance(1)
    assert (await collect(runtime, contract, reader)).state == "PENDING"
    clock.advance(9)
    final = await collect(runtime, contract, reader)
    assert final.state == "UNAVAILABLE" and final.evidence_ref is None
