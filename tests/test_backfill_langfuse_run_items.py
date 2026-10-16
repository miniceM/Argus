"""Issue 198 S6.2: one-shot ops script to re-link historical Langfuse run items.

约束：Langfuse 云端凭据由脚本在运行时从 `.env.cloud` 注入（不读取、不打印内容）；
只对显式传入的 Launch ID 生效；重复绑定的报错按 Skip 记录，不算失败。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services" / "eval-runner")]


def _load_script():
    """按路径加载 scripts/backfill_langfuse_run_items.py（保持单文件可执行）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "backfill_langfuse_run_items", ROOT / "scripts" / "backfill_langfuse_run_items.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_LAUNCH = {
    "id": "launch-1",
    "manifest": {
        "name": "argus-real-launch",
        "dataset": {
            "source": "langfuse",
            "dataset_version": "2026-10-15T08:00:00+00:00",
            "items": [
                {"id": "ds-item-1", "input": {}, "expected_output": {}, "metadata": {}},
                {"id": "ds-item-2", "input": {}, "expected_output": {}, "metadata": {}},
                {"id": "ds-item-3", "input": {}, "expected_output": {}, "metadata": {}},
            ],
        },
    },
}

_ITEMS = [
    {"id": "exec-1", "dataset_item_id": "ds-item-1", "trace_id": "t1", "observation_id": "o1", "execution_status": "succeeded"},
    {"id": "exec-2", "dataset_item_id": "ds-item-3", "trace_id": "t3", "observation_id": "o3", "execution_status": "succeeded"},
]


def test_build_run_items_assembles_frozen_payloads():
    script = _load_script()
    payloads, skips = script.build_run_items(_LAUNCH, _ITEMS)
    assert len(payloads) == 2
    first = payloads[0]
    assert first["run_name"] == "argus-real-launch"
    assert first["dataset_item_id"] == "ds-item-1"
    assert first["trace_id"] == "t1"
    assert first["observation_id"] == "o1"
    assert first["dataset_version"] == "2026-10-15T08:00:00+00:00"


def test_build_run_items_skips_items_without_trace_or_not_succeeded():
    script = _load_script()
    items = _ITEMS + [
        {"id": "exec-4", "dataset_item_id": "ds-item-2", "trace_id": None, "observation_id": None, "execution_status": "succeeded"},
        {"id": "exec-5", "dataset_item_id": "ds-item-3", "trace_id": "t9", "observation_id": "o9", "execution_status": "failed"},
    ]
    payloads, skips = script.build_run_items(_LAUNCH, items)
    assert len(payloads) == 2
    reasons = {f"{skip['item_execution_id']}": skip for skip in skips}
    assert reasons["exec-4"]["reason"] == "MISSING_TRACE"
    assert reasons["exec-5"]["reason"] == "EXECUTION_NOT_SUCCEEDED"


class _DupError(Exception):
    status_code = 409


def test_apply_backfill_creates_run_items_and_classifies_duplicates():
    script = _load_script()
    payloads = script.build_run_items(_LAUNCH, _ITEMS)[0]

    class FakeDatasetRunItems:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["dataset_item_id"] == "ds-item-3":
                raise _DupError("dataset run item already exists")

    lf = SimpleNamespace(api=SimpleNamespace(dataset_run_items=FakeDatasetRunItems()))
    outcomes = script.apply_backfill(lf, payloads, dry_run=False)
    assert [(o["status"]) for o in outcomes] == ["CREATED", "SKIPPED_DUPLICATE"]
    assert lf.api.dataset_run_items.calls[0]["dataset_version"] == "2026-10-15T08:00:00+00:00"


def test_apply_backfill_dry_run_makes_no_remote_calls():
    script = _load_script()
    payloads = script.build_run_items(_LAUNCH, _ITEMS)[0]

    class FakeDatasetRunItems:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)

    lf = SimpleNamespace(api=SimpleNamespace(dataset_run_items=FakeDatasetRunItems()))
    outcomes = script.apply_backfill(lf, payloads, dry_run=True)
    assert lf.api.dataset_run_items.calls == []
    assert [(o["status"]) for o in outcomes] == ["DRY_RUN", "DRY_RUN"]


def test_load_langfuse_env_filters_to_langfuse_keys(tmp_path):
    script = _load_script()
    env_file = tmp_path / ".env.cloud"
    env_file.write_text(
        "LANGFUSE_PUBLIC_KEY=pk-test\n"
        "LANGFUSE_SECRET_KEY=sk-test\n"
        "OTHER_NOT_LANGFUSE=secret\n"
        "OPENAI_API_KEY=sk-x\n"
        "# comment\n",
        encoding="utf-8",
    )
    loaded = script.load_langfuse_env(env_file)
    assert sorted(loaded) == ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]


def test_load_langfuse_env_requires_core_credentials(tmp_path):
    script = _load_script()
    env_file = tmp_path / ".env.cloud"
    env_file.write_text("OTHER=1\n", encoding="utf-8")
    import pytest

    with pytest.raises(SystemExit):
        script.load_langfuse_env(env_file)


# ---------------------------------------------------------------------------
# OTel 投影路径（v4 云端，legacy `/dataset-run-items` 不可投影时的唯一合法写入）
# ---------------------------------------------------------------------------


def test_apply_backfill_otel_strategy_uses_projection_and_never_calls_legacy(monkeypatch, tmp_path):
    script = _load_script()
    payload_items = script.build_run_items(_LAUNCH, _ITEMS)[0]
    payload_items = [{**payload, "dataset_id": "ds-test"} for payload in payload_items]

    legacy_calls: list[dict] = []

    class _LegacyApi:
        class dataset_run_items:
            def create(self, **kwargs):
                legacy_calls.append(kwargs)

    projection_calls: list[dict] = []

    def _fake_replay(**kwargs):
        projection_calls.append(kwargs)

    monkeypatch.setattr(script, "replay_experiment_item", _fake_replay)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-fake")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-fake")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.example.test")

    outcomes = script.apply_backfill(
        SimpleNamespace(api=_LegacyApi()),
        payload_items,
        dry_run=False,
        strategy="otel",
        launch_id="launch-1",
        env_path=tmp_path / ".env.cloud",
    )
    assert [o["status"] for o in outcomes] == ["CREATED", "CREATED"]
    assert legacy_calls == []
    assert len(projection_calls) == 2
    call = projection_calls[0]
    assert call["trace_id"]
    assert call["span_id"] == call.get("span_id")  # observation id travels as span id
    assert call["dataset_id"] == "ds-test"
    assert call["credentials"].base_url == "https://langfuse.example.test"


def test_apply_backfill_otel_dry_run_makes_no_calls(monkeypatch, tmp_path):
    script = _load_script()
    payload_items = script.build_run_items(_LAUNCH, _ITEMS)[0]
    payload_items = [{**payload, "dataset_id": "ds-test"} for payload in payload_items]
    legacy_calls: list[dict] = []
    projection_calls: list[dict] = []

    class _LegacyApi:
        class dataset_run_items:
            def create(self, **kwargs):
                legacy_calls.append(kwargs)

    monkeypatch.setattr(script, "replay_experiment_item", lambda **kwargs: projection_calls.append(kwargs))

    outcomes = script.apply_backfill(
        SimpleNamespace(api=_LegacyApi()),
        payload_items,
        dry_run=True,
        strategy="otel",
    )
    assert [o["status"] for o in outcomes] == ["DRY_RUN", "DRY_RUN"]
    assert legacy_calls == []
    assert projection_calls == []


def test_build_run_items_respects_skip_dataset_item_ids():
    script = _load_script()
    skip_filter = {"ds-item-1"}
    payloads, skips = script.build_run_items(_LAUNCH, _ITEMS, skip_dataset_item_ids=skip_filter)
    ids = {payload["dataset_item_id"] for payload in payloads}
    assert ids == {"ds-item-3"}
    assert any(s["reason"] == "SKIPPED_BY_FILTER" for s in skips)


def test_apply_backfill_otel_run_id_override_reuses_existing_run(monkeypatch, tmp_path):
    script = _load_script()
    payload_items = script.build_run_items(_LAUNCH, _ITEMS)[0]
    payload_items = [{**payload, "dataset_id": "ds-test"} for payload in payload_items]
    projection_calls: list[dict] = []

    def _fake_replay(**kwargs):
        projection_calls.append(kwargs)

    monkeypatch.setattr(script, "replay_experiment_item", _fake_replay)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-fake")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-fake")

    script.apply_backfill(
        SimpleNamespace(api=SimpleNamespace(dataset_run_items=SimpleNamespace(create=lambda **kw: None))),
        payload_items,
        dry_run=False,
        strategy="otel",
        launch_id="launch-1",
        run_id_override="fb7f61b8-761e-4ce7-b980-880c58fa51d4",
        env_path=tmp_path / ".env.cloud",
    )
    assert projection_calls, "otel strategy must post at least one OTel payload"
    run_ids = {call["run_id"] for call in projection_calls}
    assert run_ids == {"fb7f61b8-761e-4ce7-b980-880c58fa51d4"}
