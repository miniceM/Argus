"""Issue 198 S6.2 方向 A：run-item 写入策略分叉（legacy / OTel 投影）。

边界：
- `legacy` 是默认值 —— v3 自托管 Langfuse 与现有 compose 栈行为完全不改变（不动 CI 路径）。
- `otel` 是显式 opt-in（云端 v4 组织）；`lf.api.dataset_run_items.create` 该分支绝不调用，
  避免隐式 200-但不-投影。
- 策略每次同步重新读取（不缓存），测试用 `monkeypatch.setenv` 切换。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services" / "eval-runner")]

from app.langfuse_run_items import (  # noqa: E402
    link_dataset_run_item,
    resolve_run_item_strategy,
    self_assigned_run_id,
)

_FAKE_LF = SimpleNamespace(
    api=SimpleNamespace(dataset_run_items=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(dataset_run_id="legacy-run-id")))
)


def _kwargs(**overrides):
    defaults = dict(
        strategy="legacy",
        lf=_FAKE_LF,
        launch_id="aa53cf52-ee2b-4268-8a5e-9c192a86e253",
        run_name="issue198-token-e2e-v3",
        dataset_id="cmu5gbnus0006ad0kjc2u7r1k",
        dataset_item_id="00000000-0000-4000-8000-000000000001",
        dataset_version="2026-10-15T08:00:00+00:00",
        trace_id="8dbe7811b119e45872bb29ce30d9b7e5",
        observation_id="88b26ef71792fbe9",
        langfuse_base_url="https://langfuse.example.test",
        langfuse_public_key="pk-fake",
        langfuse_secret_key="sk-fake",
        timeout_seconds=15.0,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# resolve_run_item_strategy
# ---------------------------------------------------------------------------


def test_resolve_run_item_strategy_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("ARGUS_LANGFUSE_RUN_ITEM_STRATEGY", raising=False)
    assert resolve_run_item_strategy() == "legacy"


# ---------------------------------------------------------------------------
# self_assigned_run_id
# ---------------------------------------------------------------------------


def test_self_assigned_run_id_stable_and_valid_uuid():
    a = self_assigned_run_id(launch_id=_kwargs()["launch_id"], run_name=_kwargs()["run_name"])
    b = self_assigned_run_id(launch_id=_kwargs()["launch_id"], run_name=_kwargs()["run_name"])
    assert a == b
    assert uuid.UUID(a) == uuid.UUID(a)


def test_self_assigned_run_id_varies_by_run_name():
    a = self_assigned_run_id(launch_id=_kwargs()["launch_id"], run_name="run-a")
    b = self_assigned_run_id(launch_id=_kwargs()["launch_id"], run_name="run-b")
    assert a != b


# ---------------------------------------------------------------------------
# legacy 分支 —— 现有行为保持不变
# ---------------------------------------------------------------------------


def test_legacy_strategy_calls_dataset_run_items_create(monkeypatch):
    strategy = "legacy"
    calls: list[dict] = []

    class _LF:
        class api:
            class dataset_run_items:
                create_calls: list = []

    lf = _LF()

    def _legacy_create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(dataset_run_id="legacy-run-id")

    lf.api.dataset_run_items.create = _legacy_create
    result = link_dataset_run_item(**_kwargs(strategy=strategy, lf=lf))
    assert result == "legacy-run-id"
    assert len(calls) == 1
    assert calls[0]["run_name"] == _kwargs()["run_name"]
    assert calls[0]["dataset_item_id"] == _kwargs()["dataset_item_id"]
    assert calls[0]["trace_id"] == _kwargs()["trace_id"]
    assert calls[0]["observation_id"] == _kwargs()["observation_id"]
    assert calls[0]["dataset_version"] == _kwargs()["dataset_version"]


# ---------------------------------------------------------------------------
# otel 分支 —— 走 OTel 投影，不再调用 legacy REST
# ---------------------------------------------------------------------------


def test_otel_strategy_uses_projection_and_never_calls_legacy(*, monkeypatch):
    legacy_calls: list[dict] = []

    def _legacy_create(**kwargs):
        legacy_calls.append(kwargs)
        return SimpleNamespace(dataset_run_id="should-not-be-called")

    projection_calls: list[dict] = []

    def _fake_replay(**kwargs):
        projection_calls.append(kwargs)
        return (200, b"ok")

    monkeypatch.setattr("app.langfuse_run_items.replay_experiment_item", _fake_replay)

    lf = SimpleNamespace(
        api=SimpleNamespace(dataset_run_items=SimpleNamespace(create=_legacy_create))
    )
    result = link_dataset_run_item(**_kwargs(strategy="otel", lf=lf))
    assert result and uuid.UUID(result) == uuid.UUID(result)
    assert legacy_calls == []
    assert len(projection_calls) == 1
    call = projection_calls[0]
    assert call["trace_id"] == _kwargs()["trace_id"]
    assert call["span_id"] == _kwargs()["observation_id"]
    assert call["run_name"] == _kwargs()["run_name"]
    assert call["dataset_item_id"] == _kwargs()["dataset_item_id"]
    assert call["credentials"].base_url == _kwargs()["langfuse_base_url"]


def test_otel_strategy_requires_dataset_id(monkeypatch, *a, **kw):

    monkeypatch.setattr(
        "app.langfuse_run_items.replay_experiment_item", lambda **kw: (200, b"ok")
    )
    with pytest.raises(ValueError):
        link_dataset_run_item(**_kwargs(strategy="otel", dataset_id=None))


def test_otel_strategy_requires_langfuse_credentials():
    with pytest.raises(ValueError):
        link_dataset_run_item(**_kwargs(strategy="otel", langfuse_public_key=None))


def test_resolve_run_item_strategy_otel_is_explicit(monkeypatch):
    monkeypatch.setenv("ARGUS_LANGFUSE_RUN_ITEM_STRATEGY", "otel")
    assert resolve_run_item_strategy() == "otel"


def test_resolve_run_item_strategy_unknown_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("ARGUS_LANGFUSE_RUN_ITEM_STRATEGY", "bogus")
    assert resolve_run_item_strategy() == "legacy"
