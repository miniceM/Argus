"""Issue #258: 新建 Managed 评测时从 Langfuse 自动发现可用 Score 候选，并支持 Agent 绑定 Project 自动继承。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
RUNNER_ROOT = ROOT / "services" / "eval-runner"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

pytest_plugins = ["test_credential_resources"]


def _rule_payload(
    rule_id: str,
    evaluator_id: str,
    *,
    enabled: bool = True,
    sampling: float = 1.0,
    environment: str = "production",
    target_name: str = "remote-agent-http",
) -> dict:
    return {
        "id": rule_id,
        "name": f"name-{rule_id}",
        "enabled": enabled,
        "sampling": sampling,
        "filter": [
            {"type": "stringOptions", "column": "environment", "operator": "any of", "value": [environment]},
            {"type": "stringOptions", "column": "name", "operator": "any of", "value": [target_name]},
        ],
        "evaluatorAssignments": [{"evaluatorId": evaluator_id, "variableMapping": None}],
    }


def _evaluator_payload(
    evaluator_id: str,
    name: str,
    eval_type: str,
    *,
    data_type: str | None = "BOOLEAN",
    status: str = "active",
) -> dict:
    return {
        "id": evaluator_id,
        "name": name,
        "type": eval_type,
        "status": status,
        "pausedAt": None,
        "outputDefinition": {"dataType": data_type} if data_type else None,
    }


def _score_payload(
    score_id: str,
    name: str,
    rule_id: str,
    evaluator_id: str,
    *,
    data_type: str = "BOOLEAN",
    environment: str = "production",
    timestamp: str = "2026-11-13T10:00:00.000Z",
) -> dict:
    return {
        "id": score_id,
        "name": name,
        "source": "EVAL",
        "dataType": data_type,
        "value": True,
        "environment": environment,
        "timestamp": timestamp,
        "metadata": {
            "evaluation_rule_id": rule_id,
            "evaluator_id": evaluator_id,
            "evaluator_version_id": f"{evaluator_id}-v1",
            "job_execution_id": f"job-{score_id}",
        },
    }


@pytest.fixture(autouse=True)
def _reset_candidates_cache():
    try:
        from app.managed_score_candidates import clear_candidates_cache

        clear_candidates_cache()
    except ImportError:
        pass
    yield
    try:
        from app.managed_score_candidates import clear_candidates_cache

        clear_candidates_cache()
    except ImportError:
        pass


def test_discover_candidates_normal_and_unverified_code_rule(setup_runtime, monkeypatch):
    """正常路径：1 条已产生 Score 的 llm_as_judge Rule (verified=true) + 1 条从未产生 Score 的 code Rule (verified=false)。"""
    from app.langfuse_scores_client import ScoresV3Client
    from app.main import app

    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.example")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "proj-258")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/public/v2/evaluation-rules":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _rule_payload("rule-judge", "eval-judge", environment="production"),
                        _rule_payload("rule-code", "eval-code", environment="production"),
                        # 反例：停用、sampling=0、非 remote-agent-http、非当前环境
                        _rule_payload("rule-disabled", "eval-judge", enabled=False),
                        _rule_payload("rule-zero", "eval-judge", sampling=0),
                        _rule_payload("rule-wrong-target", "eval-judge", target_name="argus-run-item-replay"),
                        _rule_payload("rule-wrong-env", "eval-judge", environment="staging"),
                    ],
                    "meta": {"cursor": None},
                },
            )
        if request.url.path == "/api/public/v2/evaluators":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _evaluator_payload("eval-judge", "Judge Accuracy", "llm_as_judge", data_type="BOOLEAN"),
                        _evaluator_payload("eval-code", "Python Custom Check", "code", data_type=None),
                    ],
                    "meta": {"cursor": None},
                },
            )
        if request.url.path == "/api/public/v3/scores":
            assert request.url.params.get("source") == "EVAL"
            assert request.url.params.get("environment") == "production"
            return httpx.Response(
                200,
                json={
                    "data": [
                        _score_payload("score-1", "judge_pass", "rule-judge", "eval-judge", data_type="BOOLEAN"),
                    ],
                    "meta": {"cursor": None},
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.api_launches.get_managed_score_client",
        lambda: ScoresV3Client("https://langfuse.example", "pk-lf-test", "sk-lf-test", transport=transport),
    )

    client = TestClient(app)
    resp = client.get("/api/v1/langfuse/managed-score-candidates", params={"project_id": "proj-258", "environment": "production"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 2

    first = next(item for item in data if item["rule_id"] == "rule-judge")
    assert first == {
        "rule_id": "rule-judge",
        "evaluator_id": "eval-judge",
        "evaluator_name": "Judge Accuracy",
        "evaluator_type": "llm_as_judge",
        "data_type": "boolean",
        "score_name": "judge_pass",
        "verified": True,
        "enabled": True,
        "sampling": 1.0,
    }

    second = next(item for item in data if item["rule_id"] == "rule-code")
    assert second == {
        "rule_id": "rule-code",
        "evaluator_id": "eval-code",
        "evaluator_name": "Python Custom Check",
        "evaluator_type": "code",
        "data_type": None,
        "score_name": None,
        "verified": False,
        "enabled": True,
        "sampling": 1.0,
    }


@pytest.mark.parametrize("status_code,expected_code", [(401, "AUTHENTICATION_FAILED"), (403, "AUTHORIZATION_FAILED")])
def test_discover_candidates_auth_failure_does_not_return_empty_list(setup_runtime, monkeypatch, status_code, expected_code):
    """Langfuse 返回 401/403 时必须返回明确错误，不得伪装成空列表。"""
    from app.langfuse_scores_client import ScoresV3Client
    from app.main import app

    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "proj-258")
    transport = httpx.MockTransport(lambda req: httpx.Response(status_code, json={"message": "Unauthorized"}))
    monkeypatch.setattr(
        "app.api_launches.get_managed_score_client",
        lambda: ScoresV3Client("https://langfuse.example", "pk-lf-test", "sk-lf-test", transport=transport),
    )

    client = TestClient(app)
    resp = client.get("/api/v1/langfuse/managed-score-candidates", params={"project_id": "proj-258", "environment": "production"})
    assert resp.status_code in (400, 409, 502)
    assert resp.json()["detail"]["code"] == expected_code


def test_discover_candidates_ttl_cache_avoids_repeated_remote_hits(setup_runtime, monkeypatch):
    """缓存 TTL 内不重复打远端，避免打开页面产生请求风暴。"""
    from app.langfuse_scores_client import ScoresV3Client
    from app.main import app

    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "proj-258")
    call_count = {"rules": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/public/v2/evaluation-rules":
            call_count["rules"] += 1
        return httpx.Response(200, json={"data": [], "meta": {"cursor": None}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.api_launches.get_managed_score_client",
        lambda: ScoresV3Client("https://langfuse.example", "pk-lf-test", "sk-lf-test", transport=transport),
    )

    client = TestClient(app)
    r1 = client.get("/api/v1/langfuse/managed-score-candidates", params={"project_id": "proj-258", "environment": "production"})
    r2 = client.get("/api/v1/langfuse/managed-score-candidates", params={"project_id": "proj-258", "environment": "production"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert call_count["rules"] == 1


def test_bound_agent_inherits_project_id_and_discovers_candidates_with_bound_credentials(
    credentials, setup_runtime, monkeypatch
):
    """当 Agent 绑定了独立 Langfuse Project (BOUND) 时：
    1) 候选接口可仅凭 agent_id 自动使用绑定凭据和 project_id 拉取候选；
    2) 创建 Managed 评测时若未重复传 project_id（或为空），自动继承 Agent 绑定的 project_id。
    """
    from app.manifest import LaunchService

    client, cred_svc, registry, _ = credentials
    db_mgr = setup_runtime[0]
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "global-other-project")

    cred_rec = cred_svc.create(
        "Bound LF",
        "production",
        {"username": "pk-bound-258", "password": "sk-bound-258"},
        "tester",
        type="basic_auth",
        allowed_origins=["https://bound-lf.example"],
    )

    if not registry.get_agent("agent-bound-258"):
        registry.create_agent("agent-bound-258", "Bound Agent 258")
    registry.set_langfuse_connection(
        "agent-bound-258",
        {
            "base_url": "https://bound-lf.example",
            "project_id": "proj-bound-258",
            "credential_id": cred_rec["id"],
        },
    )
    registry.create_version("agent-bound-258", "v1", "https://agent.example/invoke", environment="production")

    def bound_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/public/projects":
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "proj-bound-258", "name": "bound", "metadata": {}, "organization": {"id": "o", "name": "n"}}],
                    "meta": {"page": 1, "limit": 100, "totalItems": 1, "totalPages": 1},
                },
            )
        if request.url.path == "/api/public/v2/evaluation-rules":
            return httpx.Response(
                200,
                json={"data": [_rule_payload("rule-b", "eval-b", environment="production")], "meta": {"cursor": None}},
            )
        if request.url.path == "/api/public/v2/evaluators":
            return httpx.Response(
                200,
                json={"data": [_evaluator_payload("eval-b", "Bound Judge", "llm_as_judge", data_type="NUMERIC")], "meta": {"cursor": None}},
            )
        if request.url.path == "/api/public/v3/scores":
            return httpx.Response(
                200,
                json={"data": [_score_payload("s-b", "bound_score", "rule-b", "eval-b", data_type="NUMERIC")], "meta": {"cursor": None}},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    orig_client = httpx.Client

    def mock_client(**kw):
        return orig_client(**{**kw, "transport": httpx.MockTransport(bound_handler)})

    with patch("app.langfuse_connection.httpx.Client", side_effect=mock_client), patch(
        "app.langfuse_scores_client.httpx.Client", side_effect=mock_client
    ):
        resp = client.get(
            "/api/v1/langfuse/managed-score-candidates",
            params={"agent_id": "agent-bound-258", "environment": "production"},
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()
        assert len(items) == 1
        assert items[0]["score_name"] == "bound_score"
        assert items[0]["data_type"] == "numeric"
        assert items[0]["verified"] is True

        # 创建 Managed Launch 时，project_id 为空也能自动继承 Agent 绑定的 proj-bound-258
        launch = LaunchService(db_mgr, registry).create_launch(
            agent_id="agent-bound-258",
            agent_version="v1",
            dataset_name="ds-bound",
            environment="production",
            evaluation_source="langfuse_managed",
            managed_evaluation={
                "project_id": "",
                "environment": "production",
                "target": {"name": "remote-agent-http", "type": "TOOL"},
                "scores": [{"name": "bound_score", "data_type": "numeric", "required": True, "rule_id": "rule-b"}],
            },
            quality_policy_rules=[
                {"evaluator_id": "bound_score", "result_type": "numeric", "operator": ">=", "threshold": 0.8}
            ],
            dataset_snapshot={
                "source": "langfuse",
                "dataset_id": "ds-bound",
                "dataset_version": "2026-11-13T00:00:00+00:00",
                "items_count": 1,
                "snapshot_digest": "digest-1",
                "items": [{"id": "item-1", "input": {"q": "hi"}, "expected_output": {"a": "ok"}}],
            },
        )
        assert launch.manifest["managed_evaluation"]["project_id"] == "proj-bound-258"
