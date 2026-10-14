"""最小证据表的迁移、精度、归属与保留约束；不激活轨迹评测。"""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from app.db import DatabaseManager, MigrationRunner
from app.db_models import (
    AgentRecord,
    AgentVersionRecord,
    Base,
    ExecutionAttemptRecord,
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from app.trajectory.codec import canonical_dumps, canonical_loads
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = "017_trajectory_evidence.sql"
TABLE = "trajectory_evidence_artifacts"
LIMIT = 2 * 1024 * 1024
DIGEST = "sha256:" + "1" * 64


def seed_history(engine):
    with Session(engine) as session, session.begin():
        session.add(AgentRecord(id="agent", name="Synthetic Agent"))
        session.flush()
        session.add(
            AgentVersionRecord(
                id="version",
                agent_id="agent",
                version="v1",
                spec_digest="old-digest",
                endpoint="http://localhost/invoke",
            )
        )
        session.flush()
        for number in (1, 2):
            session.add(
                ExperimentLaunchRecord(
                    id=f"launch-{number}",
                    name="Synthetic Launch",
                    dataset_name="ds",
                    agent_id="agent",
                    agent_version="v1",
                    agent_version_id="version",
                    manifest={"schema_version": "1.2", "frozen": "original"},
                )
            )
        session.flush()
        for number in (1, 2):
            session.add(
                ExperimentItemExecutionRecord(
                    id=f"item-{number}",
                    launch_id=f"launch-{number}",
                    dataset_item_id="case",
                )
            )
        session.flush()
        for number in (1, 2):
            session.add(
                ExecutionAttemptRecord(
                    id=f"attempt-{number}",
                    item_execution_id=f"item-{number}",
                    attempt_no=1,
                )
            )
        session.add(
            ExecutionCheckpointRecord(
                id="checkpoint",
                item_execution_id="item-1",
                launch_id="launch-1",
                dataset_item_id="case",
                dispatch_generation=1,
                output_digest="original-output",
                input_payload={"request": "synthetic"},
                binding_provenance=[],
                manifest_digest="original-manifest",
                final_attempt_id="attempt-1",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )


@pytest.fixture(params=["migration", "orm"])
def engine(request):
    manager = DatabaseManager("sqlite:///:memory:")
    if request.param == "migration":
        applied = MigrationRunner(manager.engine, ROOT / "migrations").apply_all()
        assert MIGRATION in applied
    else:
        Base.metadata.create_all(manager.engine)
    seed_history(manager.engine)
    yield manager.engine
    manager.engine.dispose()


def insert_artifact(conn, **changes):
    values = {
        "id": "evidence",
        "launch_id": "launch-1",
        "item_execution_id": "item-1",
        "attempt_id": "attempt-1",
        "trajectory_contract_digest": DIGEST,
        "trajectory_digest": DIGEST,
        "schema_version": "1.0",
        "payload_json": "{}",
    }
    values.update(changes)
    conn.execute(
        text(
            f"INSERT INTO {TABLE} (id, launch_id, item_execution_id, attempt_id, "
            "trajectory_contract_digest, trajectory_digest, schema_version, payload_json) "
            "VALUES (:id, :launch_id, :item_execution_id, :attempt_id, "
            ":trajectory_contract_digest, :trajectory_digest, :schema_version, :payload_json)"
        ),
        values,
    )


def assert_rejected(engine, action):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        action(conn)


def test_old_rows_keep_nullable_extensions_and_frozen_values(engine):
    columns = {c["name"]: c for c in inspect(engine).get_columns(TABLE)}
    assert set(columns) == {
        "id",
        "launch_id",
        "item_execution_id",
        "attempt_id",
        "trajectory_contract_digest",
        "trajectory_digest",
        "schema_version",
        "payload_json",
        "created_at",
    }
    assert str(columns["payload_json"]["type"]).upper() == "TEXT"
    with engine.connect() as conn:
        version = conn.execute(
            text("SELECT spec_digest, trajectory_source_profile_id FROM agent_versions WHERE id='version'")
        ).one()
        checkpoint = conn.execute(
            text(
                "SELECT output_digest, manifest_digest, trajectory_acquisition_json "
                "FROM execution_checkpoints WHERE id='checkpoint'"
            )
        ).one()
        assert tuple(version) == ("old-digest", None)
        assert tuple(checkpoint) == ("original-output", "original-manifest", None)
    with Session(engine) as session:
        assert session.get(ExperimentLaunchRecord, "launch-1").manifest == {
            "schema_version": "1.2",
            "frozen": "original",
        }


def test_text_payload_keeps_decimal_and_large_integer_exactly(engine):
    payload = canonical_dumps(
        {
            "amount": Decimal("12.000000000000000000000000001"),
            "large": 9007199254740993,
        }
    )
    with engine.begin() as conn:
        insert_artifact(conn, payload_json=payload)
    with engine.connect() as conn:
        stored = conn.execute(text(f"SELECT payload_json FROM {TABLE}")).scalar_one()
    assert stored == payload
    assert canonical_loads(stored) == {
        "amount": Decimal("12.000000000000000000000000001"),
        "large": 9007199254740993,
    }


def test_byte_limit_accepts_exact_limit_and_rejects_multibyte_overflow(engine):
    # 有意只测试数据库容量，合成字符串不代表通过了 StandardTrajectory 校验。
    exact = '"' + "a" * (LIMIT - 2) + '"'
    with engine.begin() as conn:
        insert_artifact(conn, payload_json=exact)
    over = '"' + "中" * (LIMIT // 3 + 1) + '"'
    assert len(over) < LIMIT < len(over.encode("utf-8"))
    assert_rejected(engine, lambda conn: insert_artifact(conn, id="oversize", payload_json=over))
    assert_rejected(engine, lambda conn: insert_artifact(conn, id="empty", payload_json=""))


@pytest.mark.parametrize(
    "changes",
    [
        {"launch_id": "absent"},
        {"item_execution_id": "absent"},
        {"attempt_id": "absent"},
        {"launch_id": "launch-2"},
        {"attempt_id": "attempt-2"},
        {"item_execution_id": "item-2", "attempt_id": "attempt-2"},
    ],
)
def test_artifact_rejects_missing_or_cross_scope_references(engine, changes):
    assert_rejected(engine, lambda conn: insert_artifact(conn, **changes))


def test_unique_key_rejects_duplicate_but_keeps_new_evidence_revision(engine):
    with engine.begin() as conn:
        insert_artifact(conn)
    assert_rejected(engine, lambda conn: insert_artifact(conn, id="duplicate"))
    with engine.begin() as conn:
        insert_artifact(conn, id="new-evidence", trajectory_digest="sha256:" + "2" * 64)
        insert_artifact(conn, id="new-contract", trajectory_contract_digest="sha256:" + "3" * 64)
        assert conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 3
        assert conn.execute(text(f"SELECT payload_json FROM {TABLE} WHERE id='evidence'")).scalar_one() == "{}"


@pytest.mark.parametrize(
    "table,identity",
    [
        ("experiment_launches", "launch-1"),
        ("experiment_item_executions", "item-1"),
        ("execution_attempts", "attempt-1"),
    ],
)
def test_parent_deletion_cannot_silently_remove_evidence(engine, table, identity):
    with engine.begin() as conn:
        insert_artifact(conn)
    assert_rejected(
        engine,
        lambda conn: conn.execute(
            text(f"DELETE FROM {table} WHERE id=:id"),
            {"id": identity},
        ),
    )
    with engine.connect() as conn:
        assert conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 1


def test_checkpoint_ttl_does_not_expire_artifact(engine):
    with engine.begin() as conn:
        insert_artifact(conn)
        conn.execute(text("DELETE FROM execution_checkpoints WHERE id='checkpoint'"))
        assert conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 1


def test_orm_extensions_round_trip_without_global_json_serializer_change(engine):
    with Session(engine) as session, session.begin():
        session.get(AgentVersionRecord, "version").trajectory_source_profile_id = "native-otel-v1"
        session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_acquisition_json = {
            "state": "PENDING",
            "reason_code": None,
        }
    with Session(engine) as session:
        assert session.get(AgentVersionRecord, "version").trajectory_source_profile_id == "native-otel-v1"
        assert session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_acquisition_json == {
            "state": "PENDING",
            "reason_code": None,
        }


def assert_upgrade_preserves_history(engine, tmp_path):
    previous = tmp_path / "previous"
    previous.mkdir()
    for path in (ROOT / "migrations").glob("*.sql"):
        if path.name < MIGRATION:
            (previous / path.name).write_bytes(path.read_bytes())
    MigrationRunner(engine, previous).apply_all()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO agents(id,name) VALUES('old','Original')"))
        conn.execute(
            text(
                "INSERT INTO agent_versions(id,agent_id,version,spec_digest,endpoint) "
                "VALUES('old-v','old','v0','historical','http://localhost/invoke')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO experiment_launches(id,name,dataset_name,agent_id,agent_version,agent_version_id,manifest) "
                "VALUES('old-l','Original','ds','old','v0','old-v','{\"schema_version\":\"1.2\"}')"
            )
        )
        conn.execute(
            text("INSERT INTO experiment_item_executions(id,launch_id,dataset_item_id) VALUES('old-i','old-l','case')")
        )
        conn.execute(text("INSERT INTO execution_attempts(id,item_execution_id,attempt_no) VALUES('old-a','old-i',1)"))
        conn.execute(
            text(
                "INSERT INTO execution_checkpoints(id,item_execution_id,launch_id,dataset_item_id,dispatch_generation,"
                "output_digest,agent_output,input_payload,binding_provenance,manifest_digest,final_attempt_id,expires_at) "
                "VALUES('old-c','old-i','old-l','case',1,'historical-output','{\"result\":\"synthetic\"}',"
                "'{\"request\":\"synthetic\"}','[]','historical-manifest','old-a',CURRENT_TIMESTAMP)"
            )
        )
    runner = MigrationRunner(engine, ROOT / "migrations")
    assert runner.apply_all() == sorted(path.name for path in (ROOT / "migrations").glob("*.sql") if path.name >= MIGRATION)
    assert runner.apply_all() == []
    runner.check_schema_compatibility()
    with engine.connect() as conn:
        assert tuple(
            conn.execute(
                text("SELECT spec_digest,trajectory_source_profile_id FROM agent_versions WHERE id='old-v'")
            ).one()
        ) == ("historical", None)
    with Session(engine) as session:
        checkpoint = session.get(ExecutionCheckpointRecord, "old-c")
        assert checkpoint.trajectory_acquisition_json is None
        assert checkpoint.agent_output == {"result": "synthetic"}
        assert checkpoint.input_payload == {"request": "synthetic"}
        assert checkpoint.output_digest == "historical-output"
        assert checkpoint.manifest_digest == "historical-manifest"
        assert checkpoint.final_attempt_id == "old-a" and checkpoint.dispatch_generation == 1
        assert session.get(ExperimentLaunchRecord, "old-l").manifest == {"schema_version": "1.2"}


def test_017_upgrade_preserves_history_and_is_idempotent(tmp_path):
    manager = DatabaseManager("sqlite:///:memory:")
    try:
        assert_upgrade_preserves_history(manager.engine, tmp_path)
    finally:
        manager.engine.dispose()


def test_postgresql_migration_enforces_same_evidence_contract(tmp_path):
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL absent; requires isolated PostgreSQL")
    admin = create_engine(url)
    schema = "s4_evidence_" + uuid4().hex
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(admin.url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        assert_upgrade_preserves_history(engine, tmp_path)
        seed_history(engine)
        test_old_rows_keep_nullable_extensions_and_frozen_values(engine)
        test_orm_extensions_round_trip_without_global_json_serializer_change(engine)
        for changes in (
            {"launch_id": "absent"},
            {"item_execution_id": "absent"},
            {"attempt_id": "absent"},
            {"launch_id": "launch-2"},
            {"attempt_id": "attempt-2"},
            {"item_execution_id": "item-2", "attempt_id": "attempt-2"},
        ):
            test_artifact_rejects_missing_or_cross_scope_references(engine, changes)
        test_text_payload_keeps_decimal_and_large_integer_exactly(engine)
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {TABLE}"))
        test_byte_limit_accepts_exact_limit_and_rejects_multibyte_overflow(engine)
        for table, identity in (
            ("experiment_launches", "launch-1"),
            ("experiment_item_executions", "item-1"),
            ("execution_attempts", "attempt-1"),
        ):
            with engine.begin() as conn:
                conn.execute(text(f"DELETE FROM {TABLE}"))
            test_parent_deletion_cannot_silently_remove_evidence(engine, table, identity)
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {TABLE}"))
        test_unique_key_rejects_duplicate_but_keeps_new_evidence_revision(engine)
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {TABLE}"))
        test_checkpoint_ttl_does_not_expire_artifact(engine)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()
