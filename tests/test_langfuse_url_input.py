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
import time
from pathlib import Path
from types import SimpleNamespace

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


# --- 凭据净化（CI run 2 的 401 Invalid credentials）-------------------------
#
# CI 的 Compose 版本会清理 env_file 值中的不可见字符，容器因此始终正常；
# 宿主验证脚本用 Python 解析同一个文件，拿到的是原始值。run 1 证明端点需要净化，
# run 2 进一步证明凭据同样需要净化——否则 base64 后的 Authorization 头无效，
# Langfuse 返回 401 "Invalid credentials. Confirm that you've configured the correct host"。

PUBLIC_KEY = "pk-lf-test"
SECRET_KEY = "sk-lf-test"


def test_credential_sanitizer_strips_invisible_and_stray_whitespace():
    from app.config import sanitize_langfuse_credential_input

    assert sanitize_langfuse_credential_input(PUBLIC_KEY) == PUBLIC_KEY
    assert sanitize_langfuse_credential_input(f"{BOM}{PUBLIC_KEY}") == PUBLIC_KEY
    assert sanitize_langfuse_credential_input(f"{ZWSP}{PUBLIC_KEY}{WJ}") == PUBLIC_KEY
    # GitHub Secret 常带结尾换行，同样会让 base64 后的 Authorization 失效
    assert sanitize_langfuse_credential_input(f"{PUBLIC_KEY}\n") == PUBLIC_KEY
    assert sanitize_langfuse_credential_input(f"  {BOM}{PUBLIC_KEY} \r\n") == PUBLIC_KEY


def test_credential_sanitizer_rejects_empty_result():
    from app.config import sanitize_langfuse_credential_input

    assert sanitize_langfuse_credential_input(None) is None
    assert sanitize_langfuse_credential_input(f"{BOM}\n") is None


def _load_verification_script():
    """按路径加载 scripts/verify-async-langfuse-link.py（文件名含连字符，非合法模块名）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "verify_async_langfuse_link", ROOT / "scripts" / "verify-async-langfuse-link.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verification_script_exports_sanitized_credentials(monkeypatch):
    """脚本解析出的凭据必须写回 os.environ，否则 SDK 仍读到原始 Secret。"""
    script = _load_verification_script()

    # shell 环境仍是原始 Secret（与 CI 中 GitHub Secret 一致，带不可见字符）
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", f"{BOM}{PUBLIC_KEY}")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", f"{SECRET_KEY}\n")
    # .env.cloud 是容器实际使用的配置，同样含不可见字符
    env_file_values = {
        "LANGFUSE_PUBLIC_KEY": f"{BOM}{PUBLIC_KEY}",
        "LANGFUSE_SECRET_KEY": f"{SECRET_KEY}\n",
    }

    script._apply_langfuse_credentials(env_file_values)

    assert os.environ["LANGFUSE_PUBLIC_KEY"] == PUBLIC_KEY
    assert os.environ["LANGFUSE_SECRET_KEY"] == SECRET_KEY


def test_verification_script_prefers_env_file_credentials(monkeypatch):
    """凭据以 .env.cloud 为准：必须验证并使用容器实际拿到的值。"""
    script = _load_verification_script()

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", f"{BOM}{PUBLIC_KEY}-from-shell")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", f"{SECRET_KEY}-from-shell")
    env_file_values = {
        "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
        "LANGFUSE_SECRET_KEY": SECRET_KEY,
    }

    script._apply_langfuse_credentials(env_file_values)

    assert os.environ["LANGFUSE_PUBLIC_KEY"] == PUBLIC_KEY
    assert os.environ["LANGFUSE_SECRET_KEY"] == SECRET_KEY


def test_sdk_client_uses_sanitized_credentials(monkeypatch):
    """端到端复现 run 2：净化后的凭据必须真正进入 SDK 使用的 Basic 认证材料。"""
    from base64 import b64decode

    from langfuse import get_client
    from langfuse._utils.request import LangfuseClient

    script = _load_verification_script()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", f"{BOM}{PUBLIC_KEY}")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", f"{SECRET_KEY}\n")
    monkeypatch.setenv("LANGFUSE_BASE_URL", CLEAN)

    script._apply_langfuse_credentials({})

    resources = get_client()._resources
    assert resources.public_key == PUBLIC_KEY
    assert resources.secret_key == SECRET_KEY

    # SDK 最终把 "<public>:<secret>" base64 后放进 Authorization 头
    http_client = LangfuseClient(
        public_key=resources.public_key,
        secret_key=resources.secret_key,
        base_url=CLEAN,
        version="test",
        timeout=1,
        session=None,
    )
    auth_header = http_client.generate_headers()["Authorization"]
    assert b64decode(auth_header.removeprefix("Basic ")).decode("utf-8") == (
        f"{PUBLIC_KEY}:{SECRET_KEY}"
    )


def test_env_file_parser_tolerates_leading_bom(tmp_path):
    """人工编辑的 .env.cloud 可能整体带 BOM，必须落在第一个变量名而非其值上。"""
    script = _load_verification_script()

    env_file = tmp_path / ".env.cloud"
    env_file.write_bytes(
        f"{BOM}LANGFUSE_PUBLIC_KEY={PUBLIC_KEY}\nLANGFUSE_SECRET_KEY={SECRET_KEY}\n".encode()
    )

    values = script._read_env_file(env_file)

    assert values == {
        "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
        "LANGFUSE_SECRET_KEY": SECRET_KEY,
    }


def test_preflight_reports_rejected_credentials_without_leaking_them(monkeypatch, capsys):
    """凭据被拒时必须立刻失败，并指向不可见字符/空白，而不是抛出 opaque 的 401。"""
    script = _load_verification_script()

    class _FakeApi:
        @staticmethod
        def get(**_kwargs):
            raise RuntimeError(
                "headers: {...}, status_code: 401, body: {'message': 'Invalid credentials.'}"
            )

    class _FakeClient:
        api = type("_Api", (), {"projects": _FakeApi})()

    monkeypatch.setattr("langfuse.get_client", lambda: _FakeClient())

    with pytest.raises(SystemExit) as excinfo:
        script._preflight_langfuse_auth()

    assert excinfo.value.code == 1
    message = capsys.readouterr().err
    assert "rejected the configured credentials" in message
    assert "401" in message
    assert "U+FEFF" in message
    # 诊断信息不得回显任何凭据内容
    assert PUBLIC_KEY not in message
    assert SECRET_KEY not in message


def test_registered_agent_version_maps_every_real_dataset_item():
    """脚本注册的 request_mapping 必须能映射真实 Dataset item。

    Cloud E2E 曾用 `{"input": "text"}` 注册 Agent，而 Langfuse 数据集 item 的输入是
    `{"messages": [...], "customer_id": ...}`，Worker 在 map_request 处直接 KeyError。
    这里对 data/dataset.json 的每条 item 真实调用 map_request，防止再次写错形状。
    """
    import json

    from app.registry import map_request

    script = _load_verification_script()
    captured: dict = {}

    class _FakeResponse:
        status_code = 201
        text = ""

    class _FakeClient:
        def post(self, path, json=None, **_kwargs):
            captured[path] = json
            return _FakeResponse()

    script._register_agent_and_version(_FakeClient(), "http://agent/invoke")

    mapping = captured["/api/v1/agent-versions"]["request_mapping"]
    seed = json.loads((ROOT / "data" / "dataset.json").read_text(encoding="utf-8"))
    for item in seed["items"]:
        payload = map_request(item["input"], mapping)
        assert payload["messages"] == item["input"]["messages"]
        assert payload["customer_id"] == item["input"]["customer_id"]


class _FakeSession:
    def __init__(self, launch):
        self._launch = launch

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, _model, _launch_id):
        return self._launch


class _FakeSessionFactory:
    def __init__(self, launch):
        self._launch = launch

    def get_session(self):
        return _FakeSession(self._launch)


def test_drive_until_link_surfaces_link_backfill_reason(capsys):
    """补链没成功时，验证脚本必须给出原因码，而不是只报"没等到链接"。

    Reconciler 在候选之间有退避，永久性原因（DASHBOARD_UNCONFIGURED、
    DATASET_ID_CONFLICT 等）不会出现在任何日志里，只会表现为超时。
    """
    script = _load_verification_script()

    class _Launch:
        id = "launch-1"
        status = "COMPLETED"
        langfuse_sync_status = "SYNCED"
        langfuse_experiment_id = "run-1"
        langfuse_experiment_url = None
        langfuse_sync_error = None

    class _LinkService:
        def __init__(self):
            self.calls = 0

        def ensure_launch_link(self, _launch_id):
            self.calls += 1
            return SimpleNamespace(status="UNAVAILABLE", reason="DATASET_ID_CONFLICT", url=None, run_id="run-1")

    link_service = _LinkService()

    class _NoWork:
        @staticmethod
        def poll_queue(count=10, block_ms=1000):
            return []

        @staticmethod
        def process_batch(batch_size=1):
            return 0

        @staticmethod
        def run_reconcile_cycle():
            return None

        @staticmethod
        def reconcile_langfuse_links(_service=None, **_kwargs):
            return 0

    with pytest.raises(SystemExit):
        script._drive_until_link(
            {
                "worker": _NoWork(),
                "outbox_syncer": _NoWork(),
                "run_score_syncer": _NoWork(),
                "reconciler": _NoWork(),
                "launch_link_service": link_service,
                "db_manager": _FakeSessionFactory(_Launch()),
            },
            "launch-1",
            deadline=time.monotonic() + 0.01,
        )

    output = capsys.readouterr()
    assert "DATASET_ID_CONFLICT" in (output.out + output.err)
    assert link_service.calls >= 1
