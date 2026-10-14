"""动态基线来自有效 Binding，不来自候选引用或兼容默认环境。"""
from copy import deepcopy

import pytest
from app import api_launches
from app.baselines import set_baseline
from app.db_models import BaselineBindingRecord, ExperimentLaunchRecord, RunResultSnapshotRecord
from app.manifest import LaunchService
from app.registry import AgentRegistry
from sqlalchemy import event
from test_issue183_baseline_environment import snapshot


def test_filter_and_projection_use_native_environment_and_frozen_identity(setup_runtime):
    db, _, _, orchestrator, _, _ = setup_runtime
    prod, test = snapshot(db), snapshot(db, "test")
    binding = set_baseline(db, agent_id="test-agent", environment="test", result_snapshot_id=test.id, expected_revision=0)
    svc = LaunchService(db, AgentRegistry(db), runner_version="0.2.0")
    launches = svc.list_launches(environment=" Test ")
    assert [launch.id for launch in launches] == [test.launch_id]
    states = svc.active_baseline_states(launches)
    row = api_launches._enrich_launch(launches[0], orchestrator, states[launches[0].id])
    assert row.environment == "test"
    assert row.is_active_baseline and row.baseline_revision == binding.revision
    assert row.baseline_revision != 0
    with db.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, test.launch_id)
        launch.status = "RUNNING"  # 已绑定快照的资格不由可变 Launch 状态决定。
    assert svc.active_baseline_states(svc.list_launches())[test.launch_id].is_active_baseline
    assert not svc.active_baseline_states(svc.list_launches())[prod.launch_id].is_active_baseline


def test_unknown_frozen_environment_is_not_highlighted_and_query_count_is_bounded(setup_runtime):
    db = setup_runtime[0]
    old = snapshot(db)
    with db.get_session() as session:
        row = session.get(RunResultSnapshotRecord, old.id)
        changed = deepcopy(row.manifest)
        changed["comparison"].pop("environment")
        row.manifest = changed
        session.add(BaselineBindingRecord(agent_id="test-agent", environment="production", result_snapshot_id=old.id, revision=7))
    svc = LaunchService(db, AgentRegistry(db), runner_version="0.2.0")
    launches = svc.list_launches()
    statements = []
    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)
    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        one = svc.active_baseline_states(launches)
        svc.active_baseline_states(launches * 50)
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    assert len(statements) == 2
    assert not one[old.launch_id].is_active_baseline
    assert one[old.launch_id].baseline_revision is None


def test_invalid_environment_filter_rejected(setup_runtime):
    db = setup_runtime[0]
    svc = LaunchService(db, AgentRegistry(db), runner_version="0.2.0")
    with pytest.raises(ValueError):
        svc.list_launches(environment="")
