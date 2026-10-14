"""Launch 持久预算与崩溃边界；生产争抢使用真正 PostgreSQL 行锁。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from threading import Event as ThreadEvent
from types import SimpleNamespace

import pytest
from app.db import DatabaseManager, MigrationRunner
from app.db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_trajectory_evidence import postgres_engine as evidence_postgres_engine
from test_trajectory_judge_definition import definition
from test_trajectory_judge_provider import prepare_request as provider_prepare_request
from test_trajectory_judge_provider import prepared as provider_prepared
from test_trajectory_storage_schema import seed_history

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepare_request(tmp_path):
    yield from provider_prepare_request.__wrapped__(tmp_path)


@pytest.fixture
def prepared(prepare_request):
    return provider_prepared.__wrapped__(prepare_request)


@pytest.fixture
def postgres_engine():
    yield from evidence_postgres_engine.__wrapped__()


@pytest.fixture(params=["sqlite", "postgres"])
def database(request, tmp_path):
    if request.param == "sqlite":
        db = DatabaseManager(f"sqlite:///{tmp_path / 'budget.db'}")
        MigrationRunner(db.engine, ROOT / "migrations").apply_all()
    else:
        engine = request.getfixturevalue("postgres_engine")

        @contextmanager
        def sessions():
            with Session(engine, expire_on_commit=False) as session:
                with session.begin():
                    yield session

        db = SimpleNamespace(engine=engine, get_session=sessions)
    seed_history(db.engine)
    yield db
    if request.param == "sqlite":
        db.engine.dispose()


def configure(db, *, calls=2, inputs=200, outputs=512):
    from app.trajectory.judge_budget import FrozenJudgeBudget, JudgeBindingBudget

    budget = FrozenJudgeBudget(
        max_total_calls=calls,
        max_total_input_tokens=inputs,
        max_total_output_tokens=outputs,
        bindings={
            "binding": JudgeBindingBudget(
                definition_digest=definition().definition_digest, max_input_tokens=100, max_output_tokens=256
            )
        },
    )
    with db.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, "launch-1")
        launch.status = "RUNNING"
        launch.manifest = {
            "schema_version": "1.3",
            "judge_budget": {"profile": budget.model_dump(mode="json"), "digest": budget.digest},
        }
        item = session.get(ExperimentItemExecutionRecord, "item-1")
        item.execution_status = "running"
        item.lease_token, item.lease_expires_at = "lease-1", datetime.now(UTC) + timedelta(minutes=5)
    return budget


def call(**changes):
    from app.trajectory.judge_budget import JudgeBudgetCall

    return JudgeBudgetCall.model_validate(
        dict(
            launch_id="launch-1",
            item_id="item-1",
            evaluation_generation=0,
            binding_id="binding",
            input_digest="sha256:" + "a" * 64,
            definition_digest=definition().definition_digest,
            max_output_tokens=256,
        )
        | changes
    )


def owner(**changes):
    from app.trajectory.judge_budget import JudgeBudgetOwner

    return JudgeBudgetOwner.model_validate(dict(kind="execution", generation=1, lease_token="lease-1") | changes)


def store(db):
    from app.trajectory.judge_budget import JudgeBudgetStore

    return JudgeBudgetStore(db.get_session)


def ledger(db):
    with db.get_session() as session:
        return deepcopy(session.get(ExperimentLaunchRecord, "launch-1").judge_budget_state_json)


def test_migration_keeps_old_launch_null_and_manifest_unchanged(database):
    with database.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, "launch-1")
        assert launch.judge_budget_state_json is None
        assert launch.manifest == {"schema_version": "1.2", "frozen": "original"}


def test_reservation_persists_before_send_and_duplicate_cannot_claim(database):
    configure(database)
    a = store(database).reserve(call(), owner())
    assert ledger(database)["reservations"][a.key]["state"] == "RESERVED"
    with pytest.raises(ValueError, match="JUDGE_CALL_ALREADY_RESERVED"):
        store(database).reserve(call(), owner())
    assert len(ledger(database)["reservations"]) == 1
    store(database).mark_sent(a)
    with pytest.raises(ValueError, match="JUDGE_CALL_OUTCOME_UNKNOWN"):
        store(database).reserve(call(), owner())


def test_sent_unknown_usage_keeps_worst_reservation_and_never_resends(database):
    configure(database, calls=2, inputs=100)
    a = store(database).reserve(call(), owner())
    store(database).mark_sent(a)
    store(database).settle(a, usage=None)
    entry = ledger(database)["reservations"][a.key]
    assert entry["charged_input_tokens"] == 100 and entry["charged_output_tokens"] == 256
    with database.get_session() as session:
        session.get(ExperimentItemExecutionRecord, "item-1").evaluation_generation = 1
    with pytest.raises(ValueError, match="JUDGE_BUDGET_EXHAUSTED"):
        store(database).reserve(call(evaluation_generation=1), owner())


def test_known_usage_refunds_only_token_difference_and_is_idempotent(database):
    from app.trajectory.judge_provider import JudgeUsage

    configure(database)
    a = store(database).reserve(call(), owner())
    store(database).mark_sent(a)
    store(database).settle(a, usage=JudgeUsage(40, 20))
    before = ledger(database)
    store(database).settle(a, usage=JudgeUsage(40, 20))
    assert ledger(database) == before
    assert before["reservations"][a.key]["charged_input_tokens"] == 40
    with pytest.raises(ValueError, match="JUDGE_CALL_OUTCOME_UNKNOWN"):
        store(database).reserve(call(), owner())


def test_unsent_cancel_releases_but_sent_cancel_never_refunds(database):
    configure(database, calls=1, inputs=100, outputs=256)
    a = store(database).reserve(call(), owner())
    store(database).release_unsent(a)
    assert ledger(database)["reservations"][a.key]["state"] == "CANCELLED"
    b = store(database).reserve(call(), owner())
    assert a.token != b.token
    with pytest.raises(ValueError, match="JUDGE_RESERVATION_STALE"):
        store(database).mark_sent(a)
    store(database).mark_sent(b)
    before = ledger(database)
    store(database).release_unsent(b)
    assert ledger(database) == before


def test_new_lease_reclaims_unsent_without_double_charge(database):
    configure(database, calls=1)
    a = store(database).reserve(call(), owner())
    with database.get_session() as session:
        session.get(ExperimentItemExecutionRecord, "item-1").lease_token = "lease-2"
    b = store(database).reserve(call(), owner(lease_token="lease-2"))
    assert b.key == a.key and b.token != a.token and len(ledger(database)["reservations"]) == 1
    with pytest.raises(ValueError, match="JUDGE_RESERVATION_STALE"):
        store(database).mark_sent(a)
    store(database).mark_sent(b)


@pytest.mark.parametrize("damage", ["generation", "lease", "expired", "cancelled", "scope", "definition"])
def test_invalid_owner_or_frozen_identity_never_charges(database, damage):
    configure(database)
    identity, lease = call(), owner()
    if damage == "generation":
        identity = call(evaluation_generation=1)
    elif damage == "lease":
        lease = owner(lease_token="foreign")
    elif damage == "scope":
        identity = call(item_id="item-2")
    elif damage == "definition":
        identity = call(definition_digest="sha256:" + "b" * 64)
    else:
        with database.get_session() as session:
            if damage == "expired":
                session.get(ExperimentItemExecutionRecord, "item-1").lease_expires_at = datetime.now(UTC) - timedelta(
                    seconds=1
                )
            else:
                session.get(ExperimentLaunchRecord, "launch-1").cancel_requested_at = datetime.now(UTC)
    with pytest.raises(ValueError, match="JUDGE_"):
        store(database).reserve(identity, lease)
    assert ledger(database) is None


def test_sent_late_usage_is_accounted_after_generation_change_without_quality_write(database):
    from app.trajectory.judge_provider import JudgeUsage

    configure(database)
    a = store(database).reserve(call(), owner())
    store(database).mark_sent(a)
    with database.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, "item-1")
        item.evaluation_generation, item.quality_conclusion, item.lease_token = 1, "unknown", "new-lease"
    store(database).settle(a, usage=JudgeUsage(40, 20))
    assert ledger(database)["reservations"][a.key]["charged_input_tokens"] == 40
    with database.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, "item-1")
        assert item.evaluation_generation == 1 and item.quality_conclusion == "unknown"


def test_corrupt_ledger_and_changed_budget_fail_closed(database):
    configure(database)
    store(database).reserve(call(), owner())
    with database.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, "launch-1")
        damaged = deepcopy(launch.judge_budget_state_json)
        damaged["budget_digest"] = "sha256:" + "b" * 64
        launch.judge_budget_state_json = damaged
    before = ledger(database)
    with pytest.raises(ValueError, match="JUDGE_BUDGET_STATE_INVALID"):
        store(database).reserve(call(), owner())
    assert ledger(database) == before


@pytest.mark.parametrize("limits", [{"calls": 1}, {"calls": 2, "inputs": 100}, {"calls": 2, "outputs": 256}])
def test_two_workers_cannot_spend_last_call_twice(postgres_engine, limits):
    from app.trajectory.judge_budget import JudgeBudgetStore

    @contextmanager
    def sessions():
        with Session(postgres_engine, expire_on_commit=False) as session, session.begin():
            yield session

    db = SimpleNamespace(get_session=sessions)
    seed_history(postgres_engine)
    configure(db, **limits)
    with sessions() as session:
        session.add(
            ExperimentItemExecutionRecord(
                id="item-3",
                execution_status="running",
                launch_id="launch-1",
                dataset_item_id="case-3",
                lease_token="lease-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    barrier = Barrier(2)

    def run(item_id):
        barrier.wait(timeout=5)
        try:
            return JudgeBudgetStore(sessions).reserve(call(item_id=item_id), owner())
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(run, ["item-1", "item-3"]))
    assert sum(not isinstance(v, str) for v in values) == 1
    assert "JUDGE_BUDGET_EXHAUSTED" in values and len(ledger(db)["reservations"]) == 1


def test_budgeted_provider_commits_send_before_await_and_duplicate_has_zero_calls(database, prepared):
    from app.trajectory.judge import ParsedJudgeResult
    from app.trajectory.judge_budget import BudgetedJudgeProvider
    from app.trajectory.judge_provider import JudgeProviderOutcome, JudgeUsage

    configure(database)
    calls = []

    class Provider:
        async def evaluate(self, request, frozen):
            calls.append(request.input_digest)
            assert next(iter(ledger(database)["reservations"].values()))["state"] == "SENT"
            await asyncio.sleep(0)
            return JudgeProviderOutcome(
                ParsedJudgeResult("succeeded", "PASS", "TASK_COMPLETE"),
                JudgeUsage(40, 20),
                request.input_digest,
                frozen.definition_digest,
                1,
            )

    async def run():
        adapter = BudgetedJudgeProvider(Provider(), store(database))
        a = await adapter.evaluate(prepared, definition(), binding_id="binding", evaluation_generation=0, owner=owner())
        b = await adapter.evaluate(prepared, definition(), binding_id="binding", evaluation_generation=0, owner=owner())
        assert a.result.status == "succeeded" and b.result.value is None
        assert b.result.reason_code == "JUDGE_CALL_OUTCOME_UNKNOWN"

    asyncio.run(run())
    assert len(calls) == 1


def test_finished_owner_cannot_send_previously_reserved_call(database):
    configure(database)
    receipt = store(database).reserve(call(), owner())
    with database.get_session() as session:
        session.get(ExperimentItemExecutionRecord, "item-1").execution_status = "succeeded"
    with pytest.raises(ValueError, match="JUDGE_BUDGET_LEASE_LOST"):
        store(database).mark_sent(receipt)
    assert ledger(database)["reservations"][receipt.key]["state"] == "RESERVED"


@pytest.mark.parametrize("usage", [(101, 0), (0, 257), (-1, 0), (True, 0)])
def test_invalid_usage_never_refunds_or_changes_unknown_reservation(database, usage):
    from app.trajectory.judge_provider import JudgeUsage

    configure(database)
    receipt = store(database).reserve(call(), owner())
    store(database).mark_sent(receipt)
    before = ledger(database)
    with pytest.raises(ValueError, match="JUDGE_USAGE_BOUND_EXCEEDED"):
        store(database).settle(receipt, usage=JudgeUsage(*usage))
    assert ledger(database) == before


def test_budgeted_cancellation_after_send_preserves_charge_and_blocks_replay(database, prepared):
    from app.trajectory.judge_budget import BudgetedJudgeProvider

    configure(database)
    started = asyncio.Event()
    calls = []

    class Provider:
        async def evaluate(self, request, frozen):
            calls.append(request.input_digest)
            started.set()
            await asyncio.Event().wait()

    async def run():
        adapter = BudgetedJudgeProvider(Provider(), store(database))
        task = asyncio.create_task(
            adapter.evaluate(prepared, definition(), binding_id="binding", evaluation_generation=0, owner=owner())
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        value = await adapter.evaluate(
            prepared, definition(), binding_id="binding", evaluation_generation=0, owner=owner()
        )
        assert value.result.reason_code == "JUDGE_CALL_OUTCOME_UNKNOWN" and value.result.value is None

    asyncio.run(run())
    assert len(calls) == 1
    entry = next(iter(ledger(database)["reservations"].values()))
    assert entry["charged_input_tokens"] == 100 and entry["charged_output_tokens"] == 256


def test_budgeted_provider_output_limit_must_fit_frozen_reservation(database, prepared):
    from app.trajectory.judge_budget import BudgetedJudgeProvider

    configure(database)
    with database.get_session() as session:
        from app.trajectory.judge_budget import FrozenJudgeBudget

        launch = session.get(ExperimentLaunchRecord, "launch-1")
        payload = deepcopy(launch.manifest)
        payload["judge_budget"]["profile"]["bindings"]["binding"]["max_output_tokens"] = 1
        payload["judge_budget"]["digest"] = FrozenJudgeBudget.model_validate(payload["judge_budget"]["profile"]).digest
        launch.manifest = payload

    class Provider:
        async def evaluate(self, request, frozen):
            pytest.fail("Provider output limit exceeded reservation before sending")

    value = asyncio.run(
        BudgetedJudgeProvider(Provider(), store(database)).evaluate(
            prepared, definition(), binding_id="binding", evaluation_generation=0, owner=owner()
        )
    )
    assert value.result.reason_code == "JUDGE_BUDGET_BINDING_MISMATCH"
    assert ledger(database) is None


def test_cancelled_old_task_preserves_cancellation_and_new_unsent_owner(database, prepared):
    from app.trajectory.judge_budget import BudgetedJudgeProvider, JudgeBudgetStore

    configure(database)
    replacements = []

    class Store(JudgeBudgetStore):
        def mark_sent(self, receipt):
            with database.get_session() as session:
                session.get(ExperimentItemExecutionRecord, "item-1").lease_token = "lease-2"
            replacements.append(self.reserve(receipt.call, owner(lease_token="lease-2")))
            raise asyncio.CancelledError

    class Provider:
        async def evaluate(self, request, frozen):
            pytest.fail("cancelled task sent Provider request")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            BudgetedJudgeProvider(Provider(), Store(database.get_session)).evaluate(
                prepared, definition(), binding_id="binding", evaluation_generation=0, owner=owner()
            )
        )
    row = ledger(database)["reservations"][replacements[0].key]
    assert row["state"] == "RESERVED" and row["token"] == replacements[0].token


def test_evaluation_lease_allows_eval_only_on_failed_launch_and_shares_budget(database):
    configure(database, calls=1)
    with database.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, "launch-1")
        launch.status = "FAILED"
        item = session.get(ExperimentItemExecutionRecord, "item-1")
        item.execution_status, item.evaluation_status, item.evaluation_generation = "succeeded", "evaluating", 1
        item.evaluation_lease_token, item.evaluation_lease_expires_at = (
            "eval-lease",
            datetime.now(UTC) + timedelta(minutes=5),
        )
    receipt = store(database).reserve(
        call(evaluation_generation=1), owner(kind="evaluation", generation=1, lease_token="eval-lease")
    )
    store(database).mark_sent(receipt)
    store(database).settle(receipt, usage=None)
    with database.get_session() as session:
        session.get(ExperimentItemExecutionRecord, "item-1").evaluation_generation = 2
    with pytest.raises(ValueError, match="JUDGE_BUDGET_EXHAUSTED"):
        store(database).reserve(
            call(evaluation_generation=2), owner(kind="evaluation", generation=2, lease_token="eval-lease")
        )


def test_unsent_cancel_returns_call_capacity_to_different_item(database):
    configure(database, calls=1)
    receipt = store(database).reserve(call(), owner())
    store(database).release_unsent(receipt)
    with database.get_session() as session:
        session.add(
            ExperimentItemExecutionRecord(
                id="item-3",
                launch_id="launch-1",
                dataset_item_id="case-3",
                execution_status="running",
                lease_token="lease-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    replacement = store(database).reserve(call(item_id="item-3"), owner())
    assert len(ledger(database)["reservations"]) == 1
    store(database).mark_sent(replacement)
    with pytest.raises(ValueError, match="JUDGE_RESERVATION_STALE"):
        store(database).mark_sent(receipt)


@pytest.mark.parametrize("cancel_count", [1, 2])
def test_cancel_during_reserve_commit_recovers_receipt_and_refunds(database, prepared, cancel_count):
    from app.trajectory.judge_budget import BudgetedJudgeProvider, JudgeBudgetStore

    configure(database)
    committed, finish = ThreadEvent(), ThreadEvent()

    class Store(JudgeBudgetStore):
        def reserve(self, identity, lease):
            receipt = super().reserve(identity, lease)
            committed.set()
            assert finish.wait(timeout=5)
            return receipt

    class Provider:
        async def evaluate(self, request, frozen):
            pytest.fail("request sent after cancellation during reserve")

    async def run():
        task = asyncio.create_task(
            BudgetedJudgeProvider(Provider(), Store(database.get_session)).evaluate(
                prepared, definition(), binding_id="binding", evaluation_generation=0, owner=owner()
            )
        )
        assert await asyncio.to_thread(committed.wait, 5)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    row = next(iter(ledger(database)["reservations"].values()))
    assert row["state"] == "CANCELLED" and row["charged_input_tokens"] == row["charged_output_tokens"] == 0


@pytest.mark.parametrize("damage", ["cancelled", "lease"])
def test_failed_send_mark_refunds_unsent_budget(database, prepared, damage):
    from app.trajectory.judge_budget import BudgetedJudgeProvider, JudgeBudgetStore

    configure(database)

    class Store(JudgeBudgetStore):
        def mark_sent(self, receipt):
            with database.get_session() as session:
                if damage == "cancelled":
                    session.get(ExperimentLaunchRecord, "launch-1").cancel_requested_at = datetime.now(UTC)
                else:
                    session.get(ExperimentItemExecutionRecord, "item-1").lease_token = "new-lease"
            return super().mark_sent(receipt)

    class Provider:
        async def evaluate(self, request, frozen):
            pytest.fail("invalid owner sent a Provider request")

    value = asyncio.run(
        BudgetedJudgeProvider(Provider(), Store(database.get_session)).evaluate(
            prepared, definition(), binding_id="binding", evaluation_generation=0, owner=owner()
        )
    )
    assert value.result.reason_code == (
        "JUDGE_BUDGET_CANCELLED" if damage == "cancelled" else "JUDGE_BUDGET_LEASE_LOST"
    )
    row = next(iter(ledger(database)["reservations"].values()))
    assert row["state"] == "CANCELLED" and row["charged_input_tokens"] == row["charged_output_tokens"] == 0


def test_cancelled_launch_rejects_before_waiting_on_item_lock(postgres_engine):
    @contextmanager
    def sessions():
        with Session(postgres_engine, expire_on_commit=False) as session, session.begin():
            yield session

    db = SimpleNamespace(get_session=sessions)
    seed_history(postgres_engine)
    configure(db)
    with sessions() as session:
        session.get(ExperimentLaunchRecord, "launch-1").status = "CANCELLING"
    with Session(postgres_engine) as holder, holder.begin():
        holder.scalar(
            select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.id == "item-1").with_for_update()
        )
        with pytest.raises(ValueError, match="JUDGE_BUDGET_CANCELLED"):
            store(db).reserve(call(), owner())
    assert ledger(db) is None
