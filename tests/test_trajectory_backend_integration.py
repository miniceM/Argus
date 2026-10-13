"""真实 SDK/API/Reader 门禁；业务 Agent 链路见 test_trajectory_observation_integration.py。"""

import asyncio
import sys
from pathlib import Path

import pytest
from app.trajectory.reader import LangfuseObservationReader
from app.trajectory.schema import TrajectoryScope
from trajectory_fixtures import scope_payload

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from trajectory_agents.local_runtime import LocalLangfuse  # noqa: E402

pytestmark = pytest.mark.trajectory_local


@pytest.fixture(scope="module")
def local_langfuse(tmp_path_factory):
    with LocalLangfuse(tmp_path_factory.mktemp("s40")) as runtime:
        yield runtime


@pytest.mark.parametrize("repeat", range(2))
def test_real_sdk_ingestion_parent_fields_and_cursor(local_langfuse, repeat, monkeypatch):
    runtime = local_langfuse
    with runtime.sdk.start_as_current_observation(name="s4-backend-root", as_type="span"):
        identity = {"trace_id": runtime.sdk.get_current_trace_id(),
                    "root_id": runtime.sdk.get_current_observation_id()}
        children = []
        for index in range(2):
            with runtime.sdk.start_as_current_observation(name=f"s4-backend-child-{index}", as_type="tool",
                                                          input={"synthetic": True}):
                children.append(runtime.sdk.get_current_observation_id())
    runtime.sdk.flush()
    rows = runtime.wait_observations(identity["trace_id"], minimum=3, require_inventory=False)
    by_id = {row["id"]: row for row in rows}
    assert set(by_id) == {identity["root_id"], *children}
    assert by_id[identity["root_id"]]["parentObservationId"] is None
    assert all(by_id[child]["parentObservationId"] == identity["root_id"] for child in children)
    assert all(row["traceId"] == identity["trace_id"] for row in rows)
    assert all(row["type"] in {"SPAN", "TOOL"} for row in rows)
    assert all({"id", "traceId", "parentObservationId", "startTime", "endTime", "type", "name", "input", "output"} <= row.keys()
               for row in rows)
    # 同一真实实例交叉验证生产 Reader；Mock 单测不能替代此 API/分页断言。
    async def read_with_production_adapter():
        scope = TrajectoryScope.model_validate(scope_payload(trace_id=identity["trace_id"],
                                                             root_observation_id=identity["root_id"]))
        observed, cursors = [], []
        cursor = None
        async with LangfuseObservationReader(server_url=runtime.base_url,
                public_key=runtime.env["S4_PUBLIC_KEY"], secret_key=runtime.env["S4_SECRET_KEY"]) as reader:
            for _ in range(20):
                page = await reader.read_page(scope, cursor=cursor, page_size=2, timeout_seconds=5, max_bytes=1024 * 1024)
                assert page.source_bytes > 0 and len(page.rows) <= 2
                observed.extend(page.rows)
                cursor = page.cursor
                if cursor is None:
                    break
                assert cursor not in cursors
                cursors.append(cursor)
            else:
                pytest.fail("Production Reader did not exhaust actual cursor pagination")
        assert reader.is_closed and cursors
        assert {row["id"] for row in observed} == set(by_id)
        assert len(observed) == len(by_id)
        assert all(row["traceId"] == scope.trace_id for row in observed)
        assert all(row["parentObservationId"] == by_id[row["id"]]["parentObservationId"] for row in observed)
        fields = {"id", "traceId", "parentObservationId", "startTime", "endTime", "type", "name", "input", "output"}
        assert all(fields <= row.keys() for row in observed)
        assert all(row[field] == by_id[row["id"]][field] for row in observed for field in fields)
    with monkeypatch.context() as proxy_env:
        # 用不可达代理验证实际传输不读取本机代理环境，而不是检查内部属性。
        proxy_env.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        proxy_env.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        proxy_env.setenv("ALL_PROXY", "http://127.0.0.1:1")
        proxy_env.setenv("NO_PROXY", "")
        asyncio.run(read_with_production_adapter())
    runtime.record("backend_sdk", "pagination", identity, rows)
    assert runtime.reports[-1]["pagination"]["cursor_hops"] >= 1
