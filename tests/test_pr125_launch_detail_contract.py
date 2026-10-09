"""PR #125 LaunchDetail 后端读模型契约验证（BE-01 / BE-02 / BE-05）。

这些测试从 Console 视角固定 PR #125 前端重构所依赖的后端契约：

1. BE-01 —— 一个 ``(launch_id, snapshot_id)`` 贯穿 launch manifest、Snapshot
   detail、summary、comparison、comparison/case 与 revision history；显式无效
   或跨 Launch 的 Snapshot 在所有固定读取端点上都 404，绝不回退 latest。
2. BE-01 —— 固冻字段的覆盖映射：冻结 Policy 与 Dataset input 来自 launch
   manifest（冻结副本），KPI 计数、逐用例规则理由、证据状态来自 Snapshot
   detail，正式结论来自 comparison。前端无需等待新端点即可完成 F1/F3/F4。
3. BE-02 —— UNKNOWN 安全的分母（全量 PASS/总数、decided、coverage 三个口径
   分开）、冻结阈值保持原值（0.8 而非 80%）、含 UNKNOWN 的冻结证据 fail-closed。
4. BE-05 —— 真实契约形状的场景样本（正常、退化、UNKNOWN、无 Baseline、
   输出上游不可用），响应由 FastAPI response_model 校验。

语义级退化判定、formal withheld、CAS 409、单侧输出、成本覆盖等已有专门回归
（test_issue_86 / test_issue_85 / test_api_results_s3 / test_result_outputs），
这里不重复其判定逻辑，只验证 Console 拼装一个正确报告所需的字段可达性。
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "services" / "eval-runner") not in sys.path:
    sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app import api_results, main  # noqa: E402
from app.db_models import ExperimentItemExecutionRecord as Item  # noqa: E402
from app.db_models import ExperimentLaunchRecord  # noqa: E402
from app.manifest import LaunchService  # noqa: E402
from app.registry import AgentRegistry  # noqa: E402
from app.result_snapshots import _canonical_digest, create_result_snapshot  # noqa: E402

# ---------------------------------------------------------------------------
# 契约形状真实的夹具（BE-05）
# ---------------------------------------------------------------------------


def _manifest(case_ids: list[str], *, baseline_snapshot_id: str | None = None) -> dict[str, Any]:
    """与生产 manifest 同形状：冻结 Policy + 冻结 Dataset 副本 + 比较身份。"""
    return {
        "schema_version": "1.2",
        "dataset": {
            "source": "seed",
            "dataset_id": "dataset-pr125",
            "dataset_name": "golden",
            "dataset_version": "v1",
            "snapshot_digest": "sha256:dataset-pr125",
            "items": [
                {"id": case_id, "input": {"q": case_id}, "expected_output": {"a": 1}, "metadata": {}}
                for case_id in case_ids
            ],
        },
        "agent": {"agent_id": "test-agent", "version": "v1", "spec_digest": "sha256:spec"},
        "quality_policy": {
            "policy_id": "default",
            "version": "1.0",
            "schema_version": "1.0",
            "unknown_handling": "unknown_not_releasable",
            "policy_digest": "sha256:" + "a" * 64,
            "rules": [
                {
                    "evaluator_id": "correctness",
                    "operator": ">=",
                    "threshold": 0.8,
                    "result_type": "numeric",
                    "required": True,
                    "critical": True,
                }
            ],
        },
        "evaluators": [
            {"id": "correctness", "version": "1.0.0", "threshold": 0.8, "direction": "higher_is_better"}
        ],
        "measurement_digest": "sha256:" + "b" * 64,
        "runner": {"runner_version": "0.2.0", "build_id": "test"},
        "comparison": {
            "environment": "production",
            "baseline_snapshot_id": baseline_snapshot_id,
            "baseline_binding_revision": 1 if baseline_snapshot_id else None,
            "comparison_policy_version": "comparison-v1",
        },
    }


def _quality_evaluation(conclusion: str) -> dict[str, Any]:
    """QualityDecision.payload() 的真实形状（逐规则理由挂在 rules 下）。"""
    if conclusion == "unknown":
        return {
            "conclusion": "unknown",
            "policy_id": "default",
            "policy_version": "1.0",
            "policy_digest": "sha256:" + "a" * 64,
            "decided_by": "quality_policy",
            "releasable": False,
            "unknown_reasons": ["REQUIRED_EVIDENCE_MISSING"],
            "rules": [],
        }
    passed = conclusion == "pass"
    return {
        "conclusion": conclusion,
        "policy_id": "default",
        "policy_version": "1.0",
        "policy_digest": "sha256:" + "a" * 64,
        "decided_by": "quality_policy",
        "releasable": passed,
        "unknown_reasons": [],
        "rules": [
            {
                "evaluator_id": "correctness",
                "result_type": "numeric",
                "required": True,
                "critical": True,
                "operator": ">=",
                "expected": 0.8,
                "observed_value": 1.0 if passed else 0.2,
                "observed_status": "known",
                "conclusion": conclusion,
                "reason_code": "REQUIRED_RULE_PASSED" if passed else "REQUIRED_RULE_VIOLATED",
                "explanation": "correctness >= 0.8",
            }
        ],
    }


def _seed(
    db_mgr,
    conclusions: dict[str, str],
    *,
    baseline_snapshot_id: str | None = None,
    inputs: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """冻结一个终态 Launch，返回 ``(launch_id, snapshot_id)``。"""
    case_ids = list(conclusions)
    launch_id = str(uuid.uuid4())
    launch_conclusion = (
        "unknown" if "unknown" in conclusions.values()
        else ("pass" if all(c == "pass" for c in conclusions.values()) else "fail")
    )
    with db_mgr.get_session() as session:
        launch = ExperimentLaunchRecord(
            id=launch_id,
            name="pr125-contract",
            status="COMPLETED",
            quality_conclusion=launch_conclusion,
            dataset_name="golden",
            dataset_version="v1",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            manifest=_manifest(case_ids, baseline_snapshot_id=baseline_snapshot_id),
            completed_at=datetime.now(UTC),
            langfuse_experiment_url=f"https://langfuse.example/runs/{launch_id}",
        )
        session.add(launch)
        session.flush()
        if inputs:
            dataset = dict(launch.manifest["dataset"])
            dataset["items"] = [
                {
                    "id": case_id,
                    "input": inputs.get(case_id, {"q": case_id}),
                    "expected_output": {"a": 1},
                    "metadata": {},
                }
                for case_id in case_ids
            ]
            launch.manifest = {**launch.manifest, "dataset": dataset}
        for case_id, conclusion in conclusions.items():
            session.add(
                Item(
                    id=str(uuid.uuid4()),
                    launch_id=launch_id,
                    dataset_item_id=case_id,
                    execution_status="succeeded",
                    eval_status="succeeded",
                    quality_conclusion=conclusion,
                    scores=(
                        {"correctness": 1.0 if conclusion == "pass" else 0.2}
                        if conclusion != "unknown" else {}
                    ),
                    quality_evaluation=_quality_evaluation(conclusion),
                    dispatch_generation=1,
                    trace_id=f"trace-{launch_id}",
                    observation_id=f"obs-{launch_id}",
                    langfuse_trace_url=f"https://langfuse.example/traces/trace-{launch_id}",
                )
            )
        session.flush()
        snapshot = create_result_snapshot(session, launch)
        assert snapshot is not None
        session.commit()
        return launch_id, snapshot.id


@pytest.fixture
def client(setup_runtime, monkeypatch):
    db_mgr, _, _, orchestrator, _, _ = setup_runtime
    # get_services 每次请求都会从 main 取 db_manager/launch_service/orchestrator，
    # 四者必须一起指向测试库，否则 launch 详情与 result 端点会读不同的库。
    registry = AgentRegistry(db_mgr)
    monkeypatch.setattr(main, "db_manager", db_mgr)
    monkeypatch.setattr(main, "registry", registry)
    monkeypatch.setattr(main, "launch_service", LaunchService(db_mgr, registry))
    monkeypatch.setattr(main, "orchestrator", orchestrator)
    monkeypatch.setattr(api_results, "_db_manager", lambda: db_mgr)
    # 无 Langfuse 时读取必须立即返回可重试的 FETCH_FAILED，而非挂起或伪造空成功。
    monkeypatch.setattr("app.result_outputs.get_langfuse_client_safe", lambda: None)
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def _item(body: dict[str, Any], case_id: str) -> dict[str, Any]:
    return next(row for row in body["items"] if row["dataset_item_id"] == case_id)


# ---------------------------------------------------------------------------
# BE-01：一个身份贯穿所有读取端点，字段足够前端拼装报告
# ---------------------------------------------------------------------------


def test_console_report_contract_reaches_every_required_frozen_field(setup_runtime, client):
    db_mgr, *_ = setup_runtime
    _, baseline_snapshot = _seed(db_mgr, {"c1": "pass"})
    launch_id, snapshot_id = _seed(db_mgr, {"c1": "fail", "c2": "pass"}, baseline_snapshot_id=baseline_snapshot)

    # 1) 冻结 Policy 与 Dataset input：launch manifest（Launch 冻结副本）。
    launch = client.get(f"/api/v1/experiment-launches/{launch_id}")
    assert launch.status_code == 200, launch.text
    manifest = launch.json()["manifest"]
    assert manifest["quality_policy"]["rules"][0]["threshold"] == 0.8
    assert manifest["quality_policy"]["rules"][0]["operator"] == ">="
    assert manifest["dataset"]["items"][0]["input"] == {"q": "c1"}

    # 2) Snapshot detail：KPI 计数、逐用例规则理由、证据状态、冻结引用。
    detail_resp = client.get(f"/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}")
    assert detail_resp.status_code == 200, detail_resp.text
    detail = detail_resp.json()
    assert detail["launch_id"] == launch_id
    assert detail["snapshot_id"] == snapshot_id
    assert detail["revision"] == 1
    assert detail["summary"]["total_cases"] == 2
    case_c1 = _item(detail, "c1")
    evaluation = case_c1["quality_evaluation"]
    assert evaluation["policy_digest"] == manifest["quality_policy"]["policy_digest"]
    assert evaluation["rules"][0]["expected"] == 0.8          # 原始数值，不是 80%
    assert evaluation["rules"][0]["operator"] == ">="
    assert len(case_c1["case_digest"]) == 64
    assert case_c1["output_ref"] == {
        "trace_id": case_c1["trace_id"],
        "observation_id": case_c1["observation_id"],
    }

    # 3) summary 与 detail 同一身份；同步状态独立存在（不混入冻结结果）。
    summary = client.get(
        f"/api/v1/experiment-launches/{launch_id}/summary",
        params={"snapshot_id": snapshot_id},
    ).json()
    assert summary["snapshot_id"] == snapshot_id
    assert summary["manifest_digest"] == detail["manifest_digest"]
    assert summary["source_result_digest"] == detail["source_result_digest"]
    assert summary["langfuse_sync"]["overall"]
    assert summary["langfuse_score_sync_status"]

    # 4) comparison：正式结论与可比性由后端给出，身份指向同一 Candidate。
    comparison = client.get(
        f"/api/v1/experiment-launches/{launch_id}/comparison",
        params={"snapshot_id": snapshot_id},
    )
    assert comparison.status_code == 200, comparison.text
    comp = comparison.json()
    assert comp["launch_id"] == launch_id
    assert comp["candidate_snapshot_id"] == snapshot_id
    assert comp["baseline_snapshot_id"] == baseline_snapshot
    assert "available" in comp["formal"] and "withheld_reasons" in comp["formal"]
    assert "comparable" in comp["comparability"]
    assert comp["summary"]["cost_comparison"]["status"]
    # comparison 的 versions 按候选/基线分侧嵌套（summary/detail 是扁平结构）。
    assert comp["versions"]["candidate"]["dataset"]["version"] == "v1"
    assert comp["versions"]["baseline"]["dataset"]["version"] == "v1"
    # 语义判定由 test_issue_86 正式覆盖；此处只证明 Console 能取到正式裁决对象。
    assert isinstance(comp["formal"]["available"], bool)

    # 5) comparison/case：单用例双侧分数；无 Langfuse 时可重试的上游故障。
    case = client.get(
        f"/api/v1/experiment-launches/{launch_id}/comparison/case",
        params={"snapshot_id": snapshot_id, "dataset_item_id": "c1"},
    )
    assert case.status_code == 200, case.text
    case_body = case.json()
    assert case_body["launch_id"] == launch_id
    assert case_body["candidate_snapshot_id"] == snapshot_id
    assert case_body["baseline_snapshot_id"] == baseline_snapshot
    assert case_body["candidate"]["output_status"] == "FETCH_FAILED"
    assert case_body["candidate"]["reason"] == "LANGFUSE_UNAVAILABLE"
    assert case_body["candidate"]["retryable"] is True
    assert case_body["candidate"]["scores"]["correctness"] == pytest.approx(0.2)
    assert case_body["baseline"]["scores"]["correctness"] == pytest.approx(1.0)

    # 6) revision history：同一快照、真实计数、latest 标记。
    history = client.get(f"/api/v1/experiment-launches/{launch_id}/result-snapshots").json()
    assert history["latest_snapshot_id"] == snapshot_id
    row = next(r for r in history["revisions"] if r["snapshot_id"] == snapshot_id)
    assert row["is_latest"] is True
    assert row["total_cases"] == 2
    assert row["quality_pass_count"] == 1
    assert row["quality_fail_count"] == 1


def test_pinned_endpoints_reject_unknown_and_foreign_snapshot_ids(setup_runtime, client):
    """无效/跨 Launch ID 全端点 fail closed，绝不退回 latest（F2 后端侧保护）。"""
    db_mgr, *_ = setup_runtime
    _, other_snapshot = _seed(db_mgr, {"c1": "pass"})
    launch_id, snapshot_id = _seed(db_mgr, {"c1": "pass", "c2": "fail"})

    def endpoints(bad_id: str) -> list[str]:
        return [
            f"/api/v1/experiment-launches/{launch_id}/result-snapshots/{bad_id}",
            f"/api/v1/experiment-launches/{launch_id}/summary?snapshot_id={bad_id}",
            f"/api/v1/experiment-launches/{launch_id}/comparison?snapshot_id={bad_id}",
            (
                f"/api/v1/experiment-launches/{launch_id}/comparison/case"
                f"?snapshot_id={bad_id}&dataset_item_id=c1"
            ),
        ]

    for bad_id in ("does-not-exist", other_snapshot):
        for url in endpoints(bad_id):
            response = client.get(url)
            assert response.status_code == 404, (url, response.status_code, response.text)
            # 404 不能夹带本 Launch 的 latest 结果（无静默回退）。
            assert snapshot_id not in response.text


def test_snapshot_bytes_are_independent_of_live_manifest_and_item_mutation(setup_runtime, client):
    """S1 在 live 数据被改写（或 S2 出现）后仍逐字节返回同一份冻结结果。"""
    db_mgr, *_ = setup_runtime
    launch_id, snapshot_id = _seed(
        db_mgr,
        {"c1": "pass", "c2": "fail"},
        inputs={"c1": {"q": "original input"}},
    )
    before = client.get(f"/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}").json()
    original_content = {"input": {"q": "original input"}, "expected_output": {"a": 1}, "metadata": {}}
    assert _item(before, "c1")["case_digest"] == _canonical_digest(original_content)

    # 模拟 live Launch 的 manifest 与 item 被改写（例如 S2 重评后 live 变绿）。
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        dataset = dict(launch.manifest["dataset"])
        dataset["dataset_version"] = "v2-live"
        dataset["items"] = [
            {**row, "input": {"q": "MUTATED LIVE INPUT"}} if row["id"] == "c1" else row
            for row in dataset["items"]
        ]
        launch.manifest = {**launch.manifest, "dataset": dataset}
        live_item = session.query(Item).filter_by(launch_id=launch_id, dataset_item_id="c2").one()
        live_item.quality_conclusion = "pass"
        live_item.scores = {"correctness": 1.0}
        session.commit()

    after = client.get(f"/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}").json()
    # 冻结结果逐字节不变：c2 仍然是 FAIL，KPI 仍是 1 PASS / 1 FAIL。
    assert after == before
    assert after["versions"]["dataset"]["version"] == "v1"
    assert _item(after, "c1")["case_digest"] == _canonical_digest(original_content)
    assert _item(after, "c2")["quality_conclusion"] == "fail"
    assert after["summary"]["quality_fail_count"] == 1

    # 而 live 数据确实已经变了——证明上面的不变性不是“没改数据”。
    live = client.get(f"/api/v1/experiment-launches/{launch_id}").json()
    assert live["manifest"]["dataset"]["dataset_version"] == "v2-live"
    live_items = client.get(f"/api/v1/experiment-launches/{launch_id}/items").json()
    assert next(row for row in live_items if row["dataset_item_id"] == "c2")["quality_conclusion"] == "pass"


# ---------------------------------------------------------------------------
# BE-02：UNKNOWN 安全分母 + 冻结阈值保持原值
# ---------------------------------------------------------------------------


def test_unknown_safe_counts_frozen_thresholds_and_fail_closed_evidence(setup_runtime, client):
    db_mgr, *_ = setup_runtime
    launch_id, snapshot_id = _seed(db_mgr, {"c1": "pass", "c2": "fail", "c3": "unknown"})

    detail = client.get(
        f"/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}"
    ).json()
    summary = detail["summary"]
    # 三个口径必须分开：全量 PASS/总数、decided 分母、coverage。
    assert summary["total_cases"] == 3
    assert summary["quality_pass_count"] == 1
    assert summary["quality_fail_count"] == 1
    assert summary["quality_unknown_count"] == 1
    assert summary["decided_case_count"] == 2
    assert summary["decided_pass_rate"] == pytest.approx(0.5)
    assert summary["decision_coverage"] == pytest.approx(2 / 3)
    assert summary["quality_pass_count"] / summary["total_cases"] == pytest.approx(1 / 3)
    # 真实零与缺失不混淆：未知不进 pass，也不变成 0 分母。
    assert summary["score_means"]["correctness"] is not None

    # 含 UNKNOWN 的冻结证据必须 fail-closed：DIAGNOSTIC，不可作为正式证据。
    assert detail["evidence_state"] == "DIAGNOSTIC"
    assert detail["releasable"] is False
    assert detail["evidence_reasons"]

    unknown_eval = _item(detail, "c3")["quality_evaluation"]
    assert unknown_eval["conclusion"] == "unknown"
    assert unknown_eval["unknown_reasons"]
    assert unknown_eval["releasable"] is False

    # 冻结阈值保持原始数值与比较符（0.8 / >=），前端不得百分比化。
    rule = _item(detail, "c1")["quality_evaluation"]["rules"][0]
    assert rule["expected"] == 0.8
    assert rule["operator"] == ">="
    assert rule["expected"] != 80

    history = client.get(f"/api/v1/experiment-launches/{launch_id}/result-snapshots").json()
    row = next(r for r in history["revisions"] if r["snapshot_id"] == snapshot_id)
    assert row["quality_unknown_count"] == 1
    assert row["evidence_state"] == "DIAGNOSTIC"


def test_run_summary_keeps_unknown_safe_denominators_for_kpi(setup_runtime, client):
    """KPI 走 summary 端点时必须拿到与 detail 完全一致的冻结计数。"""
    db_mgr, *_ = setup_runtime
    launch_id, snapshot_id = _seed(db_mgr, {"c1": "pass", "c2": "fail", "c3": "unknown"})

    summary = client.get(
        f"/api/v1/experiment-launches/{launch_id}/summary",
        params={"snapshot_id": snapshot_id},
    ).json()
    assert summary["snapshot_id"] == snapshot_id
    assert summary["summary"]["quality_unknown_count"] == 1
    assert summary["summary"]["decided_pass_rate"] == pytest.approx(0.5)
    # RunSummaryResponse 只带 evidence_state；releasable 是 Snapshot detail 字段。
    assert summary["evidence_state"] == "DIAGNOSTIC"
