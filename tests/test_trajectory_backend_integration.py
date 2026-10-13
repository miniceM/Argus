"""本地 Langfuse 运行环境的真实 SDK/API 门禁；Agent 链路由后续阶段验收。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from trajectory_agents.local_runtime import LocalLangfuse  # noqa: E402

pytestmark = pytest.mark.trajectory_local


@pytest.fixture(scope="module")
def local_langfuse(tmp_path_factory):
    with LocalLangfuse(tmp_path_factory.mktemp("s40")) as runtime:
        yield runtime


@pytest.mark.parametrize("repeat", range(2))
def test_real_sdk_ingestion_parent_fields_and_cursor(local_langfuse, repeat):
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
    runtime.record("backend_sdk", "pagination", identity, rows)
    assert runtime.reports[-1]["pagination"]["cursor_hops"] >= 1
