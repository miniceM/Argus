import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from threading import Event

import pytest
from app.db_models import ExecutionCheckpointRecord, ExperimentItemExecutionRecord, TrajectoryEvidenceArtifactRecord
from app.trajectory.coordinator import TrajectoryCoordinator, TrajectoryCoordinatorError
from app.trajectory.reader import ObservationReadError
from sqlalchemy import func, select
from test_trajectory_acquisition import Clock, Reader, page, run_async
from test_trajectory_assembler import configured, prepared
from test_trajectory_checkpoint import begin, candidate, publish, view
from test_trajectory_checkpoint import context as checkpoint_context
from test_trajectory_checkpoint import db as checkpoint_db
from test_trajectory_checkpoint import postgres_context as checkpoint_postgres_context
from test_trajectory_checkpoint import postgres_engine as checkpoint_postgres_engine


@pytest.fixture
def db(tmp_path):
    yield from checkpoint_db.__wrapped__(tmp_path)


@pytest.fixture
def context(db):
    manager, scope, contract, now = checkpoint_context.__wrapped__(db)
    return manager, scope, configured(contract, limits={"deadline_seconds": 10, "quiet_window_seconds": 2}), now


def coordinator(context, *, elapsed=0):
    clock = Clock()
    clock.base = context[3]
    clock.advance(elapsed)

    async def sleep(seconds):
        clock.advance(seconds)
        await asyncio.sleep(0)

    return TrajectoryCoordinator(context[0].get_session, clock=clock, sleep=sleep), clock


async def run(context, reader, *, elapsed=0):
    controller, clock = coordinator(context, elapsed=elapsed)
    result = await controller.run(context[1], context[2], client=reader, lease_token="lease-1")
    return result, clock


@run_async
async def test_coordinator_commits_reservation_before_reader_and_publishes_real_artifact(context):
    rows, _, _ = prepared()

    class Source(Reader):
        async def read_page(self, scope, **options):
            assert view(context).source_bytes_used == context[2].limits.max_source_bytes
            return await super().read_page(scope, **options)

    source = Source([[page(rows)]])
    result, _ = await run(context, source)
    assert result.state == "READY" and result.completeness.status == "OBSERVED_COMPLETE"
    assert view(context).acquisition == result
    assert len(source.calls) >= 2 and view(context).candidate is not None


@run_async
async def test_fresh_coordinator_rebuilds_quiet_and_injects_stored_wire_budget(context):
    saved = publish(context, begin(context), used=120, evidence=candidate(context))
    rows, _, _ = prepared()
    reader = Reader([[page(rows, size=200)]])
    result, clock = await run(context, reader, elapsed=1)
    assert result.state == "READY" and result.completeness.status == "OBSERVED_COMPLETE"
    assert clock.elapsed >= 3 and len(reader.calls) >= 2
    assert reader.calls[0][1]["max_bytes"] == context[2].limits.max_source_bytes - 120
    assert view(context).source_bytes_used == 120 + 200 * len(reader.calls)
    assert result.deadline_at == saved.acquisition.deadline_at


@run_async
async def test_terminal_checkpoint_is_read_without_online_io(context):
    saved = publish(context, begin(context), evidence=candidate(context))
    final = begin(context, elapsed=11)
    reader = Reader([[page([])]])
    result, _ = await run(context, reader, elapsed=12)
    assert result == final.acquisition and result.evidence_ref == saved.candidate_ref
    assert reader.calls == []


@run_async
async def test_crash_consumed_budget_preserves_candidate_until_original_deadline(context):
    saved = publish(context, begin(context), evidence=candidate(context))
    active = begin(context, elapsed=2)
    with context[0].get_session() as session:
        from app.trajectory.checkpoint import abort_trajectory_round
        abort_trajectory_round(session, active, context[2], lease_token="lease-1", now=context[3] + timedelta(seconds=2))
    reader = Reader([[page([])]])
    result, clock = await run(context, reader, elapsed=3)
    assert result.state == "READY" and result.evidence_ref == saved.candidate_ref
    assert result.completeness.status == "ROOT_ONLY" and clock.elapsed == 10
    assert reader.calls == [] and view(context).source_bytes_used == context[2].limits.max_source_bytes


@run_async
async def test_no_root_after_restore_does_not_discard_verified_candidate(context):
    saved = publish(context, begin(context), evidence=candidate(context))
    reader = Reader([[page([])]])
    result, _ = await run(context, reader, elapsed=1)
    assert result.state == "READY" and result.evidence_ref == saved.candidate_ref
    assert result.completeness.status == "ROOT_ONLY"


@run_async
async def test_external_cancel_aborts_round_without_refunding_unknown_bytes(context):
    entered = asyncio.Event()

    class Blocking:
        async def read_page(self, *args, **options):
            entered.set()
            await asyncio.Event().wait()

    controller, _ = coordinator(context)
    task = asyncio.create_task(controller.run(context[1], context[2], client=Blocking(), lease_token="lease-1"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = view(context)
    assert stored.acquisition.state == "PENDING" and stored.acquisition.evidence_ref is None
    assert stored.round_token is None and stored.source_bytes_used == context[2].limits.max_source_bytes
    with context[0].get_session() as session:
        cp = session.get(ExecutionCheckpointRecord, "checkpoint")
        assert cp.trajectory_runtime_json["round_token"] is None


@run_async
async def test_deadline_crossed_before_publish_finishes_same_claim_without_new_io(context, monkeypatch):
    import app.trajectory.coordinator as module
    controller, clock = coordinator(context)
    original = module.publish_trajectory_round
    triggered = False

    def cross_deadline(*args, **kwargs):
        nonlocal triggered
        if kwargs["result"].state == "PENDING" and not triggered:
            triggered = True
            clock.advance(10)
            kwargs["now"] = clock.utcnow()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "publish_trajectory_round", cross_deadline)
    rows, _, _ = prepared()
    reader = Reader([[page(rows[:1])]])
    result = await controller.run(context[1], context[2], client=reader, lease_token="lease-1")
    assert triggered and result.state == "READY" and result.completeness.status == "ROOT_ONLY"
    assert len(reader.calls) == 1 and view(context).acquisition == result


@run_async
async def test_already_cancelled_event_prevents_reservation_and_source_io(context, monkeypatch):
    controller, _ = coordinator(context)

    async def fast_database(_contract, action):
        with context[0].get_session() as session:
            return action(session)

    monkeypatch.setattr(controller, "_transaction", fast_database)
    stopped = asyncio.Event()
    stopped.set()
    rows, _, _ = prepared()
    reader = Reader([[page(rows)]])
    with pytest.raises(asyncio.CancelledError):
        await controller.run(context[1], context[2], client=reader, lease_token="lease-1", cancelled=stopped)
    assert reader.calls == []
    with context[0].get_session() as session:
        assert session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_runtime_json is None


@run_async
async def test_normalizer_failure_invalidates_candidate_without_raw_error_or_unbound_local(context, monkeypatch):
    import app.trajectory.acquisition as module
    publish(context, begin(context), evidence=candidate(context))

    def reject(*args, **kwargs):
        raise ValueError("SYNTHETIC_SECRET_BAD_NORMALIZER")

    monkeypatch.setattr(module, "normalize_observations", reject)
    rows, _, _ = prepared()
    result, _ = await run(context, Reader([[page(rows)]]), elapsed=1)
    assert result.state == "UNAVAILABLE" and result.evidence_ref is None
    assert "SYNTHETIC_SECRET" not in str(result.model_dump())


@run_async
async def test_callers_mutation_during_source_io_cannot_change_frozen_coordinator_contract(context):
    from app.trajectory.checkpoint import load_trajectory_checkpoint
    original = deepcopy(context[2])
    rows, _, _ = prepared()

    class MutatingReader(Reader):
        async def read_page(self, *args, **options):
            context[2].source_profile_set.clear()
            return await super().read_page(*args, **options)

    result, _ = await run(context, MutatingReader([[page(rows)]]))
    assert result.state == "READY" and result.completeness.status == "OBSERVED_COMPLETE"
    with context[0].get_session() as session:
        assert load_trajectory_checkpoint(session, context[1], original).acquisition == result


@run_async
@pytest.mark.parametrize("shape", ["conflict", "invalid-root", "wrong-trace"])
async def test_observed_root_failure_after_restore_invalidates_old_candidate(context, shape):
    publish(context, begin(context), evidence=candidate(context))
    rows, _, _ = prepared()
    changed = deepcopy(rows[0])
    if shape == "conflict":
        changed["output"] = {"intent": "different"}
        source = [rows[0], changed]
    elif shape == "invalid-root":
        changed["parentObservationId"] = changed["id"]
        source = [changed]
    else:
        changed["traceId"] = "b" * 32
        source = [changed]
    result, _ = await run(context, Reader([[page(source)]]), elapsed=1)
    assert result.state == "UNAVAILABLE" and result.evidence_ref is None
    assert view(context).candidate_ref is None


@run_async
async def test_retryable_failure_after_restore_preserves_candidate_but_auth_failure_does_not(context):
    saved = publish(context, begin(context), evidence=candidate(context))
    source = Reader([[ObservationReadError("TRAJECTORY_SOURCE_RATE_LIMITED", retryable=True)]])
    result, _ = await run(context, source, elapsed=1)
    assert result.state == "READY" and result.evidence_ref == saved.candidate_ref
    assert result.completeness.status == "ROOT_ONLY"


@run_async
async def test_permanent_source_error_is_unavailable_without_old_candidate_fallback(context):
    publish(context, begin(context), evidence=candidate(context))
    source = Reader([[ObservationReadError("TRAJECTORY_SOURCE_AUTH_FAILED", retryable=False)]])
    result, _ = await run(context, source, elapsed=1)
    assert result.state == "UNAVAILABLE" and result.reason_code == "TRAJECTORY_SOURCE_AUTH_FAILED"
    assert view(context).candidate_ref is None and len(source.calls) == 1


@run_async
async def test_cancel_event_interrupts_reader_and_preserves_conservative_reservation(context):
    entered, stopped = asyncio.Event(), asyncio.Event()

    class Blocking:
        async def read_page(self, *args, **options):
            entered.set()
            await asyncio.Event().wait()

    controller, _ = coordinator(context)
    task = asyncio.create_task(controller.run(context[1], context[2], client=Blocking(), lease_token="lease-1", cancelled=stopped))
    await asyncio.wait_for(entered.wait(), 1)
    stopped.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert view(context).round_token is None
    assert view(context).source_bytes_used == context[2].limits.max_source_bytes


@run_async
async def test_database_work_does_not_block_heartbeat(context, monkeypatch):
    import app.trajectory.coordinator as module
    entered, release = Event(), Event()
    original = module.begin_trajectory_round

    def blocked(*args, **options):
        entered.set()
        assert release.wait(timeout=2)
        return original(*args, **options)

    monkeypatch.setattr(module, "begin_trajectory_round", blocked)
    rows, _, _ = prepared()
    task = asyncio.create_task(run(context, Reader([[page(rows)]])))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(3):
            await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    assert (await task)[0].state == "READY"


@run_async
async def test_cancelled_late_publication_rolls_back_candidate_and_state(context, monkeypatch):
    import app.trajectory.coordinator as module
    entered, release = Event(), Event()
    original = module.publish_trajectory_round

    def delayed_commit(*args, **options):
        result = original(*args, **options)
        entered.set()
        assert release.wait(timeout=2)
        return result

    monkeypatch.setattr(module, "publish_trajectory_round", delayed_commit)
    rows, _, _ = prepared()
    task = asyncio.create_task(run(context, Reader([[page(rows[:1])]])))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0.01)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    restored = view(context)
    assert restored.acquisition.state == "PENDING" and restored.candidate_ref is None
    assert restored.round_token is None and restored.source_bytes_used == context[2].limits.max_source_bytes
    with context[0].get_session() as session:
        assert session.scalar(select(func.count()).select_from(TrajectoryEvidenceArtifactRecord)) == 0


@run_async
async def test_database_timeout_abandons_late_reservation_transaction(context, monkeypatch):
    import app.trajectory.coordinator as module
    entered, release, finished = Event(), Event(), Event()
    original = module.begin_trajectory_round
    manager, scope, contract, now = context
    context = manager, scope, configured(contract, limits={"request_timeout_seconds": Decimal("0.05")}), now
    controller, _ = coordinator(context)

    @contextmanager
    def sessions():
        try:
            with manager.get_session() as session:
                yield session
        finally:
            finished.set()

    controller._session_factory = sessions

    def delayed(*args, **options):
        result = original(*args, **options)
        entered.set()
        assert release.wait(timeout=2)
        return result

    monkeypatch.setattr(module, "begin_trajectory_round", delayed)
    reader = Reader([[page([])]])
    task = asyncio.create_task(controller.run(scope, context[2], client=reader, lease_token="lease-1"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(TrajectoryCoordinatorError, match="STORAGE_TIMEOUT"):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    assert reader.calls == []
    with manager.get_session() as session:
        assert session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_runtime_json is None


@run_async
async def test_lease_replaced_during_read_cannot_publish_or_abort_new_owners_round(context):
    rows, _, _ = prepared()

    class Source(Reader):
        async def read_page(self, *args, **options):
            with context[0].get_session() as session:
                session.get(ExperimentItemExecutionRecord, "item-1").lease_token = "lease-2"
            return await super().read_page(*args, **options)

    with pytest.raises(Exception, match="TRAJECTORY_CHECKPOINT_LEASE_LOST"):
        await run(context, Source([[page(rows)]]))
    restored = view(context)
    assert restored.acquisition.state == "PENDING" and restored.round_token is not None
    assert restored.source_bytes_used == context[2].limits.max_source_bytes and restored.candidate_ref is None


@run_async
async def test_cancel_event_set_by_completed_reader_prevents_candidate_publication(context, monkeypatch):
    stopped = asyncio.Event()
    controller, _ = coordinator(context)

    async def fast_database(_contract, action):
        with context[0].get_session() as session:
            return action(session)

    monkeypatch.setattr(controller, "_transaction", fast_database)
    rows, _, _ = prepared()

    class Source(Reader):
        async def read_page(self, *args, **options):
            result = await super().read_page(*args, **options)
            stopped.set()
            return result

    reader = Source([[page(rows[:1])]])
    with pytest.raises(asyncio.CancelledError):
        await controller.run(context[1], context[2], client=reader, lease_token="lease-1", cancelled=stopped)
    assert len(reader.calls) == 1
    assert view(context).candidate_ref is None and view(context).round_token is None


@run_async
@pytest.mark.parametrize("used", [-1, True, 0.5, "1", None, 8388609])
async def test_restored_runtime_rejects_invalid_budget_seed(context, used):
    from app.trajectory.acquisition import AcquisitionRuntime
    _, clock = coordinator(context)
    with pytest.raises(ValueError, match="TRAJECTORY_ACQUISITION_RUNTIME_INVALID"):
        AcquisitionRuntime(context[1], context[2], session_factory=context[0].get_session,
            started_at=context[3], source_bytes_used=used, clock=clock)


@pytest.fixture
def postgres_engine():
    yield from checkpoint_postgres_engine.__wrapped__()


@pytest.fixture
def postgres_context(postgres_engine):
    manager, scope, contract, now = checkpoint_postgres_context.__wrapped__(postgres_engine)
    return manager, scope, configured(contract, limits={"deadline_seconds": 10, "quiet_window_seconds": 2}), now


@run_async
async def test_postgres_competing_coordinator_cannot_read_while_round_is_reserved(postgres_context):
    from app.trajectory.checkpoint import TrajectoryCheckpointError
    context = postgres_context
    entered = asyncio.Event()

    class Blocking:
        async def read_page(self, *args, **options):
            entered.set()
            await asyncio.Event().wait()

    controller, _ = coordinator(context)
    first = asyncio.create_task(controller.run(context[1], context[2], client=Blocking(), lease_token="lease-1"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        reader = Reader([[page([])]])
        with pytest.raises(TrajectoryCheckpointError, match="ROUND_BUSY"):
            await run(context, reader)
        assert reader.calls == []
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    assert view(context).source_bytes_used == context[2].limits.max_source_bytes
    assert view(context).round_token is None


@run_async
async def test_postgres_locked_launch_has_finite_storage_wait_and_no_source_io(postgres_context):
    from concurrent.futures import ThreadPoolExecutor

    from app.db_models import ExperimentLaunchRecord
    entered, release = Event(), Event()
    manager, scope, contract, now = postgres_context
    context = manager, scope, configured(contract, limits={"request_timeout_seconds": Decimal("0.05")}), now

    def hold():
        with manager.get_session() as session:
            session.scalar(select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == scope.launch_id).with_for_update())
            entered.set()
            assert release.wait(timeout=2)

    reader = Reader([[page([])]])
    with ThreadPoolExecutor(max_workers=1) as pool:
        blocker = pool.submit(hold)
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            with pytest.raises(TrajectoryCoordinatorError) as error:
                await asyncio.wait_for(run(context, reader), 1)
            assert error.value.reason_code in {"TRAJECTORY_COORDINATOR_STORAGE_TIMEOUT", "TRAJECTORY_COORDINATOR_STORAGE_UNAVAILABLE"}
        finally:
            release.set()
        blocker.result(timeout=1)
    assert reader.calls == []
    with manager.get_session() as session:
        assert session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_runtime_json is None


@run_async
async def test_lease_expired_while_getting_rows_is_rechecked_before_source_io(context, monkeypatch):
    import app.trajectory.checkpoint as module
    from app.trajectory.checkpoint import TrajectoryCheckpointError
    controller, clock = coordinator(context)
    with context[0].get_session() as session:
        session.get(ExperimentItemExecutionRecord, "item-1").lease_expires_at = context[3] + timedelta(seconds=1)
    original = module._rows

    def after_lock(*args, **options):
        rows = original(*args, **options)
        clock.advance(2)
        return rows

    monkeypatch.setattr(module, "_rows", after_lock)
    rows, _, _ = prepared()
    reader = Reader([[page(rows)]])
    with pytest.raises(TrajectoryCheckpointError, match="LEASE_LOST"):
        await controller.run(context[1], context[2], client=reader, lease_token="lease-1")
    assert reader.calls == []
    with context[0].get_session() as session:
        assert session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_runtime_json is None


@run_async
async def test_lease_expired_during_candidate_validation_rolls_back_publication(context, monkeypatch):
    import app.trajectory.checkpoint as module
    from app.trajectory.checkpoint import TrajectoryCheckpointError
    controller, clock = coordinator(context)
    with context[0].get_session() as session:
        session.get(ExperimentItemExecutionRecord, "item-1").lease_expires_at = context[3] + timedelta(seconds=1)
    original = module.persist_trajectory_evidence

    def delayed(*args, **options):
        ref = original(*args, **options)
        clock.advance(2)
        return ref

    monkeypatch.setattr(module, "persist_trajectory_evidence", delayed)
    rows, _, _ = prepared()
    with pytest.raises(TrajectoryCheckpointError, match="LEASE_LOST"):
        await controller.run(context[1], context[2], client=Reader([[page(rows[:1])]]), lease_token="lease-1")
    assert view(context).candidate_ref is None
    with context[0].get_session() as session:
        assert session.scalar(select(func.count()).select_from(TrajectoryEvidenceArtifactRecord)) == 0


@run_async
async def test_postgres_lease_expired_during_lock_wait_prevents_reservation_and_source_io(postgres_context, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    import app.trajectory.checkpoint as module
    from app.db_models import ExperimentLaunchRecord
    from app.trajectory.checkpoint import TrajectoryCheckpointError
    context = postgres_context
    manager, scope, contract, now = context
    controller, clock = coordinator(context)
    held, entered, release = Event(), Event(), Event()
    with manager.get_session() as session:
        session.get(ExperimentItemExecutionRecord, "item-1").lease_expires_at = now + timedelta(seconds=1)
    original = module._rows

    def waiting(*args, **options):
        entered.set()
        return original(*args, **options)

    monkeypatch.setattr(module, "_rows", waiting)

    def hold():
        with manager.get_session() as session:
            session.scalar(select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == scope.launch_id).with_for_update())
            held.set()
            assert release.wait(timeout=2)

    rows, _, _ = prepared()
    reader = Reader([[page(rows)]])
    with ThreadPoolExecutor(max_workers=1) as pool:
        blocker = pool.submit(hold)
        try:
            assert await asyncio.to_thread(held.wait, 1)
            task = asyncio.create_task(controller.run(scope, contract, client=reader, lease_token="lease-1"))
            assert await asyncio.to_thread(entered.wait, 1)
            assert not task.done()
            clock.advance(2)
        finally:
            release.set()
        blocker.result(timeout=1)
        with pytest.raises(TrajectoryCheckpointError, match="LEASE_LOST"):
            await task
    assert reader.calls == []
    with manager.get_session() as session:
        assert session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_runtime_json is None
