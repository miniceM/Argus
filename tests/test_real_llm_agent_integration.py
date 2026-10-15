"""Issue #198 — Real LLM Agent 跨边界回归。

完整链路：真实 openai SDK（httpx.MockTransport 模拟上游）→ real-llm-agent
FastAPI 服务（httpx.ASGITransport 承载）→ 现有 RemoteAgentExecutor.invoke_once
→ Worker 冻结评分持久化。不要求 Langfuse / 数据库服务器 / 真实模型 Key。

覆盖：
- 六条 `data/dataset.json` seed 输入经 map_request 真实进入 Agent；
- 上游 URL/path、Bearer 测试值、model、messages 形状全部真实可见；
- 成功用例真实经过 `answer_present@1.0.0` 冻结评分，结果为 boolean true；
- usage 从 Agent response 按 usage_cost_mapping 提取并落到 Attempt；
- 401 / 502 / 504 三类失败对应 execution failed、eval skipped / quality
  unknown，且不存在假通过；
- 502 与 504 同属 HTTP_5XX，但 http_status 与安全 error_message 可区分。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import openai
import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "services" / "eval-runner", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.db_models import (  # noqa: E402
    ExecutionAttemptRecord,  # noqa: E402
    ExperimentItemExecutionRecord,  # noqa: E402
)
from app.manifest import LaunchService  # noqa: E402
from app.registry import AgentRegistry  # noqa: E402
from test_real_llm_agent import _AsyncMockTransport, _load_service_module  # noqa: E402

MODEL_API_KEY = "test-model-key-198"
MODEL_NAME = "gateway-test-snapshot-17b"
MODEL_BASE_URL = "http://upstream-model.test/v1"
SERVING_MODEL = "gateway-serving-fixed-17b"

SCENARIO_OK = "scenario:ok"
SCENARIO_UNAUTHORIZED = "scenario:unauthorized"
SCENARIO_UNREACHABLE = "scenario:unreachable"
SCENARIO_TIMEOUT = "scenario:timeout"
_SCENARIO_MARKER_TOKEN = "::"

MARKERS = (
    [SCENARIO_OK] * 3 + [SCENARIO_UNAUTHORIZED, SCENARIO_UNREACHABLE, SCENARIO_TIMEOUT]
)

USAGE_OK = {"input_tokens": 12, "output_tokens": 34, "total_tokens": 46}


def _stamp_items(markers: list[str]) -> list[dict[str, Any]]:
    """真实 seed 输入按顺序追加确定性场景标记（不改映射与编排逻辑）。"""
    items = json.loads((ROOT / "data" / "dataset.json").read_text(encoding="utf-8"))["items"]
    assert len(items) >= len(markers)
    for item, marker in zip(items[: len(markers)], markers, strict=True):
        item["input"]["messages"][0]["content"] += f" [{_SCENARIO_MARKER_TOKEN}{marker}]"
    return items[: len(markers)]


def _snapshot_for_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "dataset_id": "local-issue198",
        "dataset_version": "v1",
        "content_digest": "sha256:issue198-integration-fixture",
        "items": items,
    }


class UpstreamModelGateway:
    """自适应上游：按消息中的场景标记决定行为，并记录全部请求证据。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == f"{MODEL_BASE_URL}/chat/completions"
        assert request.headers["authorization"] == f"Bearer {MODEL_API_KEY}"
        body = json.loads(request.content.decode("utf-8"))
        self.calls.append({"model": body["model"], "messages": body["messages"]})
        content = body["messages"][-1]["content"]
        if SCENARIO_OK in content:
            return httpx.Response(
                200,
                json={
                    "id": "upstream-req-integration",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": f"适配后的真实回答 [{SCENARIO_OK}]",
                            }
                        }
                    ],
                    "model": SERVING_MODEL,
                    "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
                },
            )
        if SCENARIO_UNAUTHORIZED in content:
            return httpx.Response(
                401,
                json={"error": {"type": "invalid_request_error", "message": "bad key"}},
            )
        if SCENARIO_UNREACHABLE in content:
            raise httpx.ConnectError("simulated connect failure", request=request)
        if SCENARIO_TIMEOUT in content:
            raise httpx.ReadTimeout("simulated read timeout", request=request)
        raise AssertionError(f"unexpected scenario marker in: {content!r}")


def _build_agent_app(svc, gateway: UpstreamModelGateway):
    def factory(_settings):
        return openai.AsyncOpenAI(
            base_url=MODEL_BASE_URL,
            api_key=MODEL_API_KEY,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=_AsyncMockTransport(gateway.handler)),
        )

    settings = svc.Settings(
        base_url=MODEL_BASE_URL,
        api_key=MODEL_API_KEY,
        model_name=MODEL_NAME,
        connect_timeout=1.0,
        read_timeout=1.0,
        write_timeout=1.0,
        pool_timeout=1.0,
        deadline_seconds=3.0,
    )
    app = svc.create_app(settings=settings, client_factory=factory)
    # ASGITransport 不执行 lifespan。client 由 _drain 在每次调用的事件循环内
    # 惰性创建（openai SDK 的 socket 必须属于同一个 loop），用毕关闭。
    app.state.client_factory = factory
    return app


def _register_agent_version(db_mgr) -> AgentRegistry:
    registry = AgentRegistry(db_mgr)
    registry.create_agent(
        "real-llm-agent",
        "Real LLM Agent",
        description="仅验证真实模型响应及 answer_present",
    )
    registry.create_version(
        "real-llm-agent",
        "v1",
        "http://real-llm-agent:8080/invoke",
        method="POST",
        request_mapping={"messages": "input.messages", "customer_id": "input.customer_id"},
        timeout_seconds=75,
        max_retries=0,
        max_concurrency=1,
        rate_limit_per_minute=60,
        is_idempotent=False,
        environment="poc",
        usage_cost_mapping={
            "input_tokens_path": "usage.input_tokens",
            "output_tokens_path": "usage.output_tokens",
            "total_tokens_path": "usage.total_tokens",
            "source": "provider_reported",
            "measurement_scope": "agent_invocation_total",
        },
    )
    return registry


def _drain(worker, queue, group: str, agent_app) -> None:
    """真实 Worker 执行路径：Executor→Agent 走 ASGITransport，主机名与端口不影响路由。"""
    import app.worker as worker_module

    inner_transport = httpx.ASGITransport(app=agent_app)

    class LoopBoundASGI(httpx.AsyncBaseTransport):
        """在同一事件循环内惰性创建并挂载真实 SDK client。"""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if getattr(agent_app.state, "client", None) is None:
                agent_app.state.client = agent_app.state.client_factory(agent_app.state.settings)
            return await inner_transport.handle_async_request(request)

    real_executor_cls = worker_module.RemoteAgentExecutor

    class TransportedExecutor(real_executor_cls):
        def __init__(self, spec, credential_db_manager=None):
            super().__init__(
                spec, client=httpx.AsyncClient(transport=LoopBoundASGI())
            )

    async def _run_one(message):
        try:
            return await worker.execute_item_message(*message)
        finally:
            client = getattr(agent_app.state, "client", None)
            if client is not None:
                await client.close()
                agent_app.state.client = None

    with (
        patch("app.worker.get_langfuse_client_safe", return_value=None),
        patch.object(worker_module, "RemoteAgentExecutor", TransportedExecutor),
    ):
        while msgs := queue.read_group(group, count=10):
            for message in msgs:
                asyncio.run(_run_one(message))


def run_launch(setup_runtime, svc, gateway, items: list[dict[str, Any]], name: str):
    """冻结 Launch 并用真实 Worker 执行路径执行。返回 (db_mgr, launch)。"""
    db_mgr, queue, _limiter, orchestrator, worker, reconciler = setup_runtime
    registry = _register_agent_version(db_mgr)
    service = LaunchService(db_mgr, registry)
    launch = service.create_launch(
        agent_id="real-llm-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        dataset_version="v1",
        environment="poc",
        name=name,
        max_concurrency=1,
        evaluator_ids=["answer_present"],
        quality_policy_rules=[
            {
                "evaluator_id": "answer_present",
                "operator": "==",
                "expected_value": True,
                "result_type": "boolean",
                "required": True,
            }
        ],
        dataset_snapshot=_snapshot_for_items(items),
    )
    orchestrator.start_launch(launch.id)
    agent_app = _build_agent_app(svc, gateway)
    _drain(worker, queue, f"issue198-{name}", agent_app)
    reconciler.reconcile_launch_states()
    return db_mgr, launch


def _load_launch_view(db_mgr, launch_id) -> dict[str, Any]:
    """在一个会话内取齐需要的持久化快照，避免 lazy-load 踩坑。"""
    from app.db_models import EvaluationResultRecord, ExecutionCheckpointRecord  # noqa: E402

    with db_mgr.get_session() as session:
        records = session.scalars(
            select(ExperimentItemExecutionRecord).where(
                ExperimentItemExecutionRecord.launch_id == launch_id
            )
        ).all()
        view: dict[str, Any] = {"launch_status": None, "items": []}
        for record in records:
            attempt = session.scalars(
                select(ExecutionAttemptRecord).where(
                    ExecutionAttemptRecord.item_execution_id == record.id
                )
            ).one()
            checkpoint = session.scalars(
                select(ExecutionCheckpointRecord).where(
                    ExecutionCheckpointRecord.item_execution_id == record.id
                )
            ).first()
            evals = session.scalars(
                select(EvaluationResultRecord).where(
                    EvaluationResultRecord.item_execution_id == record.id
                )
            ).all()
            view["launch_status"] = record.launch.status
            view["items"].append(
                {
                    "dataset_item_id": record.dataset_item_id,
                    "execution_status": record.execution_status,
                    "eval_status": record.eval_status,
                    "quality_conclusion": record.quality_conclusion,
                    "execution_error": record.execution_error,
                    "scores": record.scores,
                    "attempt": {
                        "status": attempt.status,
                        "http_status": attempt.http_status,
                        "error_type": attempt.error_type,
                        "error_message": attempt.error_message,
                        "usage_cost": attempt.usage_cost,
                        "latency_ms": attempt.latency_ms,
                    },
                    "checkpoint": (
                        checkpoint.agent_output
                        if checkpoint is not None and hasattr(checkpoint, "agent_output")
                        else None
                    ),
                    "typed": [
                        {
                            "evaluator_id": row.evaluator_id,
                            "evaluator_version": row.evaluator_version,
                            "result_type": row.result_type,
                            "status": row.status,
                            "value": row.value,
                            "normalized_value": row.normalized_value,
                            "binding_id": row.binding_id,
                            "definition_digest": row.definition_digest,
                        }
                        for row in evals
                    ],
                }
            )
        return view


def _ok_view_records(view) -> list[dict[str, Any]]:
    return [row for row in view["items"] if row["execution_status"] == "succeeded"]


def _failed_view_records(view) -> list[dict[str, Any]]:
    return [row for row in view["items"] if row["execution_status"] == "failed"]


@pytest.mark.parametrize(
    "idx, marker",
    [(0, SCENARIO_UNAUTHORIZED), (1, SCENARIO_UNREACHABLE), (2, SCENARIO_TIMEOUT)],
)
def test_single_failure_item_maps_http_status_and_never_fake_pass(
    setup_runtime, idx: int, marker: str
):
    svc = _load_service_module()
    gateway = UpstreamModelGateway()
    items = _stamp_items([marker])
    db_mgr, launch = run_launch(setup_runtime, svc, gateway, items, f"failure-{marker}")
    view = _load_launch_view(db_mgr, launch.id)
    failed = _failed_view_records(view)
    assert len(failed) == 1
    record = failed[0]
    assert record["eval_status"] == "skipped"
    assert record["quality_conclusion"] == "unknown"
    assert record["typed"] == []
    attempt = record["attempt"]
    assert attempt["status"] == "FAILED"
    expected = {"scenario:unauthorized": (401, "HTTP_4XX"), "scenario:unreachable": (502, "HTTP_5XX"), "scenario:timeout": (504, "HTTP_5XX")}
    http_status, error_type = expected[marker]
    assert attempt["http_status"] == http_status
    assert attempt["error_type"] == error_type
    assert str(http_status) in attempt["error_message"]


def test_full_flight_mixes_success_failure_and_real_upstream_contract(setup_runtime):
    svc = _load_service_module()
    gateway = UpstreamModelGateway()
    db_mgr, launch = run_launch(setup_runtime, svc, gateway, _stamp_items(list(MARKERS)), "full-flight")
    view = _load_launch_view(db_mgr, launch.id)

    assert view["launch_status"] == "PARTIAL_FAILED"
    assert len(view["items"]) == 6
    assert len(_ok_view_records(view)) == 3
    assert len(_failed_view_records(view)) == 3

    # 上游收到 6 次真实 SDK 调用；路径/鉴权/model/messages 全部按契约。
    assert len(gateway.calls) == 6
    for call in gateway.calls:
        assert call["model"] == MODEL_NAME
        assert all(set(msg) == {"role", "content"} for msg in call["messages"])


def test_success_items_receive_frozen_answer_present_boolean_true_and_usage(setup_runtime):
    svc = _load_service_module()
    gateway = UpstreamModelGateway()
    db_mgr, launch = run_launch(setup_runtime, svc, gateway, _stamp_items(list(MARKERS)), "success-contract")
    view = _load_launch_view(db_mgr, launch.id)

    for record in _ok_view_records(view):
        assert record["eval_status"] == "succeeded"
        assert record["quality_conclusion"] == "pass"
        # 冻结身份必须落入 provenance，不能用 latest。
        typed = record["typed"]
        assert len(typed) == 1
        result = typed[0]
        assert result["evaluator_id"] == "answer_present"
        assert result["evaluator_version"] == "1.0.0"
        assert result["result_type"] == "boolean"
        assert result["status"] == "succeeded"
        assert result["value"] is True
        assert result["normalized_value"] == 1.0
        assert result["binding_id"] and result["definition_digest"]
        assert record["scores"] == {}
        checkpoint = record["checkpoint"]
        assert checkpoint is not None
        assert checkpoint["model"] == SERVING_MODEL
        assert isinstance(checkpoint["answer"], str)
        assert checkpoint["answer"].strip()
        assert checkpoint["usage"] == dict(
            USAGE_OK
        )
        attempt = record["attempt"]
        assert attempt["http_status"] == 200
        assert (attempt["usage_cost"] or {}).get("usage") == dict(USAGE_OK)
