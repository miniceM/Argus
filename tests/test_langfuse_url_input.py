"""Langfuse 端点配置的输入净化（Issue #44 Cloud E2E 回归）。

Cloud E2E 曾在 `LANGFUSE_BASE_URL` 带 UTF-8 BOM（U+FEFF）时失败：BOM 不可见、
`str.strip()` 无法清除，导致 SDK 报 "missing an 'http://' or 'https://' protocol"，
而 Dashboard 校验静默失败并降级。容器侧仅因 Compose 的 dotenv 解析器剥离 BOM
才幸免，宿主侧脚本没有这层保护。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services" / "eval-runner")]

BOM = "﻿"
ZWSP = ""
WJ = "⁠"
CLEAN = "https://jp.cloud.langfuse.com"


@pytest.fixture(autouse=True)
def clean_langfuse_client_cache():
    """Keep the process-level Langfuse client singleton out of other tests.

    ``get_client()`` caches by public key, so a client built here would otherwise leak
    into suites that assert on the *unconfigured* client.
    """
    from langfuse._client.resource_manager import LangfuseResourceManager

    def _clear() -> None:
        instances = getattr(LangfuseResourceManager, "_instances", None)
        if isinstance(instances, dict):
            instances.clear()

    _clear()
    yield
    _clear()


@pytest.fixture
def reloaded_config():
    """按当前环境重新加载 app.config，并在结束后恢复。"""
    import app.config as config_module

    yield config_module
    importlib.reload(config_module)


def test_sanitize_removes_invisible_characters():
    from app.config import sanitize_langfuse_url_input

    assert sanitize_langfuse_url_input(CLEAN) == CLEAN
    assert sanitize_langfuse_url_input(f"{BOM}{CLEAN}") == CLEAN
    assert sanitize_langfuse_url_input(f"{CLEAN}{BOM}") == CLEAN
    assert sanitize_langfuse_url_input(f"  {BOM}{CLEAN}\n") == CLEAN
    assert sanitize_langfuse_url_input(f"https://{ZWSP}jp.cloud.langfuse.com") == CLEAN
    assert sanitize_langfuse_url_input(f"https://jp.cloud.langfuse.com{WJ}") == CLEAN


def test_sanitize_keeps_meaning_and_rejects_empty():
    from app.config import sanitize_langfuse_url_input

    # 路径前缀是合法语义，必须保留
    assert sanitize_langfuse_url_input("https://host/langfuse") == "https://host/langfuse"
    assert sanitize_langfuse_url_input(None) is None
    assert sanitize_langfuse_url_input("   ") is None
    assert sanitize_langfuse_url_input(f"{BOM}") is None


def test_dashboard_validator_stays_strict():
    """净化只发生在配置入口；校验函数本身必须继续拒绝非法值。"""
    from app.config import validate_langfuse_dashboard_url

    assert validate_langfuse_dashboard_url(CLEAN) == CLEAN
    assert validate_langfuse_dashboard_url(f"{BOM}{CLEAN}") is None
    assert validate_langfuse_dashboard_url("jp.cloud.langfuse.com") is None


def test_dashboard_url_setting_recovers_from_bom(monkeypatch, reloaded_config):
    monkeypatch.setenv("ARGUS_LANGFUSE_DASHBOARD_URL", f"{BOM}{CLEAN}")
    reloaded = importlib.reload(reloaded_config)
    assert reloaded.settings.argus_langfuse_dashboard_url == CLEAN


def test_base_url_setting_recovers_from_bom(monkeypatch, reloaded_config):
    """SDK 读取 LANGFUSE_BASE_URL，BOM 会直接让 API 调用抛 UnsupportedProtocol。"""
    monkeypatch.setenv("LANGFUSE_BASE_URL", f"{BOM}{CLEAN}")
    reloaded = importlib.reload(reloaded_config)
    assert reloaded.settings.langfuse_base_url == CLEAN


def test_sdk_client_accepts_sanitized_base_url(monkeypatch, reloaded_config):
    """端到端复现 Cloud E2E 故障：SDK 只从环境读取 base URL，且不做任何净化。

    带 BOM 时 `get_client()` 会构造出无法发起请求的 base URL，
    随后每个 API 调用都抛 UnsupportedProtocol。
    """
    from langfuse import get_client

    monkeypatch.setenv("LANGFUSE_BASE_URL", f"{BOM}{CLEAN}")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    reloaded = importlib.reload(reloaded_config)
    os.environ["LANGFUSE_BASE_URL"] = reloaded.settings.langfuse_base_url

    assert get_client()._base_url == CLEAN


def test_genuinely_invalid_url_is_still_rejected(monkeypatch, reloaded_config):
    """净化不得把真正的错误配置变成可用值。"""
    monkeypatch.setenv("LANGFUSE_BASE_URL", "jp.cloud.langfuse.com")
    reloaded = importlib.reload(reloaded_config)
    assert reloaded.settings.langfuse_base_url == "jp.cloud.langfuse.com"
    assert reloaded.settings.argus_langfuse_dashboard_url is None
