"""真实来源测试的调度器也必须遵守原采集期限，不额外睡过截止。"""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_trajectory_acquisition import run_async
from test_trajectory_observation_integration import acquire_real_trajectory


@run_async
async def test_real_polling_does_not_sleep_past_the_remaining_acquisition_budget(monkeypatch):
    remaining = 0.013
    calls = 0
    deadline = object()

    class Runtime:
        _wire_bytes = 17

        def __init__(self, *args, **kwargs):
            pass

        def activate(self):
            return nullcontext()

        def remaining(self):
            return remaining

    class Reader:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def acquire(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(state="PENDING", deadline_at=deadline)
        assert remaining == pytest.approx(0), "应在原期限到达时立即收尾"
        return SimpleNamespace(state="READY", deadline_at=deadline)

    async def sleep(seconds):
        nonlocal remaining
        assert seconds <= remaining, "调度器不得给原采集预算追加睡眠"
        remaining -= seconds

    monkeypatch.setattr("app.trajectory.acquisition.AcquisitionRuntime", Runtime)
    monkeypatch.setattr("app.trajectory.acquisition.acquire_trajectory", acquire)
    monkeypatch.setattr("app.trajectory.reader.LangfuseObservationReader", Reader)
    monkeypatch.setattr("test_trajectory_observation_integration.asyncio.sleep", sleep)
    runtime = SimpleNamespace(base_url="http://local.test", env={"S4_PUBLIC_KEY": "fixture", "S4_SECRET_KEY": "fixture"})
    result, source_bytes, first = await acquire_real_trajectory(runtime, SimpleNamespace(get_session=None), None, None)
    assert result.state == "READY" and first.state == "PENDING"
    assert calls == 2 and source_bytes == 17
