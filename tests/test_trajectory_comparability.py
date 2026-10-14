"""S43-07：冻结配置相同，单次观测参数变化不导致 Snapshot 不可比。"""

from copy import deepcopy
from types import SimpleNamespace

import test_trajectory_worker as worker_fixture
from app.baselines import validate_baseline_snapshot
from app.comparison_contracts import assess_comparability
from app.db_models import ExperimentLaunchRecord, RunResultSnapshotRecord
from app.trajectory.inputs import load_snapshot_input
from test_trajectory_required_evidence import required_trajectory_snapshot


def test_different_observed_arguments_keep_persisted_snapshots_comparable(tmp_path, monkeypatch):
    # 这里验证数据库/冻结比较合同；Mock HTTP 来源不作为真实 Reader 准入证据。
    original = worker_fixture.prepared

    def changed_arguments():
        rows, contract, scope = original()
        rows = deepcopy(rows)
        for row in rows:
            if row.get("type") == "TOOL":
                row["input"] = {"customer_id": "synthetic-002"}
        return rows, contract, scope

    snapshots, inputs = [], []
    for name in ("baseline", "candidate"):
        directory = tmp_path / name
        directory.mkdir()
        if name == "candidate":
            monkeypatch.setattr(worker_fixture, "prepared", changed_arguments)
        setup = required_trajectory_snapshot.__wrapped__(SimpleNamespace(param="sqlite"), directory)
        try:
            db, snapshot, _ = next(setup)
            with db.get_session() as session:
                stored = session.get(RunResultSnapshotRecord, snapshot.id)
                launch = session.get(ExperimentLaunchRecord, snapshot.launch_id)
                validate_baseline_snapshot(stored, launch, launch.agent_id)
                assert stored.evidence_state == "COMPLETE"
                value = load_snapshot_input(session, snapshot_id=stored.id, dataset_item_id=stored.items[0]["dataset_item_id"])
                snapshots.append(deepcopy(stored.manifest))
                inputs.append(value)
        finally:
            setup.close()

    before, after = (value.trajectory_evidence_ref for value in inputs)
    assert before.trajectory_digest != after.trajectory_digest
    assert before.trajectory_contract_digest == after.trajectory_contract_digest
    assert any(step.arguments_redacted != other.arguments_redacted
               for step, other in zip(inputs[0].trajectory.steps, inputs[1].trajectory.steps, strict=True))
    assert snapshots[0]["measurement_digest"] == snapshots[1]["measurement_digest"]
    assert snapshots[0]["contract_digests"] == snapshots[1]["contract_digests"]
    verdict = assess_comparability(*snapshots)
    assert verdict.comparable and verdict.reason_codes == ()
    assert all(dimension.status == "MATCH" for dimension in verdict.dimensions)
