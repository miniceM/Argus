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
from datetime import UTC, datetime, timedelta
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


def test_drive_until_link_routes_queue_messages_by_work_type():
    """合并 main 后 queue 消息是 4 元组，且必须按 work_type 路由。

    main 把 ``read_group`` 的返回值从 ``(message_id, item_id, generation)``
    扩展为 ``(message_id, item_id, generation, work_type)``（Issue #84：同一条队列
    既跑完整执行也跑仅重新评测）。补链验证脚本若仍按 3 元组解包，会在 CI run
    37712345355 上抛出 ``ValueError: too many values to unpack (expected 3)``；
    若直接把 EVALUATION 消息丢给 ``execute_item_message``，则会重新调用 Agent，
    破坏"仅重新评测不重跑 Agent"的语义。
    """
    script = _load_verification_script()

    class _Worker:
        def __init__(self):
            self.invocations: list[tuple] = []
            self.evaluations: list[tuple] = []

        @staticmethod
        def poll_queue(count=10, block_ms=1000):
            return [
                ("m-1", "item-1", 2, "INVOCATION"),
                ("m-2", "item-2", 2, "EVALUATION"),
            ]

        async def execute_item_message(self, *args):
            self.invocations.append(args)
            return True

        async def execute_evaluation_message(self, *args):
            self.evaluations.append(args)
            return True

    class _Quiescent:
        """已经 SYNCED 且已有链接：让驱动循环在第一轮处理完消息后直接返回。"""

        @staticmethod
        def process_batch(batch_size=1):
            return 0

        @staticmethod
        def run_reconcile_cycle():
            return None

        @staticmethod
        def reconcile_langfuse_links(_service=None, **_kwargs):
            return 0

        @staticmethod
        def ensure_launch_link(_launch_id):
            return SimpleNamespace(status="UNCHANGED", reason=None, url="ok", run_id="run-1")

    class _SyncedLaunch:
        id = "launch-1"
        status = "COMPLETED"
        langfuse_sync_status = "SYNCED"
        langfuse_experiment_id = "run-1"
        langfuse_experiment_url = "https://example.com/run-1"
        langfuse_sync_error = None

    worker = _Worker()
    state = script._drive_until_link(
        {
            "worker": worker,
            "outbox_syncer": _Quiescent(),
            "run_score_syncer": _Quiescent(),
            "reconciler": _Quiescent(),
            "launch_link_service": _Quiescent(),
            "db_manager": _FakeSessionFactory(_SyncedLaunch()),
        },
        "launch-1",
        deadline=time.monotonic() + 5,
    )

    # 4 元组被正确解包，且两类工作分别路由
    assert worker.invocations == [("m-1", "item-1", 2)]
    assert worker.evaluations == [("m-2", "item-2", 2)]
    assert state["langfuse_experiment_url"] == "https://example.com/run-1"


def test_environment_is_configured_before_app_config_is_first_imported(monkeypatch):
    """app.config 必须在环境变量设置完成之后才被首次导入。

    ``app/config.py`` 在模块导入期就执行 ``settings = Settings()``，而
    ``Settings.argus_langfuse_dashboard_url`` 是普通默认值，只在构造时求值一次。
    ``main.py`` 直接 ``from .config import settings`` 取这个已固化的对象，因此若
    凭据净化先触发了 ``import app.config``，LangfuseLinkResolver 会拿到
    dashboard_base=None，补链永远返回 DASHBOARD_UNCONFIGURED。
    """
    script = _load_verification_script()
    calls: list[str] = []

    def _stop(name: str):
        def _inner(*_args, **_kwargs):
            calls.append(name)
            raise RuntimeError("stop")

        return _inner

    monkeypatch.setenv("LANGFUSE_BASE_URL", CLEAN)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PUBLIC_KEY)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SECRET_KEY)
    monkeypatch.setattr(sys, "argv", ["verify-async-langfuse-link.py"])
    monkeypatch.setattr(script, "_configure_environment", _stop("_configure_environment"))
    monkeypatch.setattr(script, "_apply_langfuse_credentials", _stop("_apply_langfuse_credentials"))

    with pytest.raises(RuntimeError):
        script.main()

    assert calls[0] == "_configure_environment", (
        "必须在设置环境变量之后才允许导入 app.config，实际调用顺序为 " + repr(calls)
    )


# 远端身份核对用的固定身份：真实 ID 形态，避免测试把 Name 当 ID 用。
DS_NAME = "banking-agent-regression"
DATASET_ID = "cmu5gbnus0006ad0kjc2u7r1k"
PROJECT_ID = "cmu5a6fj500aoad0jvrckiypb"
ACK_RUN_ID = "ce880ed6-8f82-4cc0-9461-746b2e9905e4"


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows


class _FakeVerifySession:
    def __init__(self, launch, tasks):
        self._launch = launch
        self._tasks = tasks

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, _model, _launch_id):
        return self._launch

    def query(self, _model):
        return _FakeQuery(self._tasks)


def _task(run_name="argus-test", ack_run_id=ACK_RUN_ID):
    """模拟 outbox 任务：``_dataset_run_id`` 是 Langfuse create ACK 返回的 Run ID。"""
    return SimpleNamespace(
        dataset_run_name=run_name,
        scores_payload={"_dataset_run_id": ack_run_id} if ack_run_id else {},
    )


def _fake_client(
    *,
    project_id,
    dataset_id,
    experiments=lambda **_kwargs: SimpleNamespace(data=[]),
    experiment_items=lambda **_kwargs: SimpleNamespace(data=[]),
    scores=lambda **_kwargs: SimpleNamespace(data=[]),
):
    """构造只暴露 v4 读路径的假客户端。

    2026-09-16 之后创建的 Organization 上，Langfuse 已关闭全部 v3 读路径
    （datasets.get_run / datasets.get_runs / dataset-run-items / v2 scores 均返回
    410 LEGACY_API_UNAVAILABLE_FOR_NEW_ORGANIZATION），因此这里让任何 legacy 调用
    直接失败，防止验证脚本再次退回已被平台下线的接口。
    """

    def _legacy(name):
        def _boom(*_args, **_kwargs):
            raise AssertionError(f"legacy {name} must not be used")

        return _boom

    class _Datasets:
        get_run = staticmethod(_legacy("datasets.get_run"))
        get_runs = staticmethod(_legacy("datasets.get_runs"))

        @staticmethod
        def get(dataset_name, **_kwargs):
            return SimpleNamespace(id=dataset_id, name=dataset_name)

    class _DatasetRunItems:
        list = staticmethod(_legacy("dataset_run_items.list"))

    return SimpleNamespace(
        api=SimpleNamespace(
            projects=SimpleNamespace(
                get=lambda **_kwargs: SimpleNamespace(data=[SimpleNamespace(id=project_id)])
            ),
            datasets=_Datasets,
            dataset_run_items=_DatasetRunItems,
            experiments=SimpleNamespace(list=experiments, list_items=experiment_items),
            scores_v3=SimpleNamespace(get_many_v3=scores),
        )
    )


def _verify(script, monkeypatch, *, url, tasks, client, run_id=ACK_RUN_ID, dataset_name=DS_NAME):
    launch = SimpleNamespace(dataset_name=dataset_name, langfuse_experiment_url=url)
    monkeypatch.setattr("langfuse.get_client", lambda: client)
    return script._verify_remote_identity(
        SimpleNamespace(get_session=lambda: _FakeVerifySession(launch, tasks)),
        "launch-1",
        run_id,
        CLEAN,
    )


def test_remote_identity_confirms_run_when_platform_exposes_it(monkeypatch):
    """平台能读回 Run 时，身份核对必须是最强的全等断言。

    Langfuse Cloud 对 2026-09-16 之后创建的 Organization 关闭了 v3 的
    datasets.get_run（410 LEGACY_API_UNAVAILABLE_FOR_NEW_ORGANIZATION）；
    Dataset Run 在 v4 中即 Experiment，只能经 /api/public/experiments 读回。
    """
    script = _load_verification_script()
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_INTERVAL_SECONDS", 0.0, raising=False)

    url = f"{CLEAN}/project/{PROJECT_ID}/datasets/{DATASET_ID}/runs/{ACK_RUN_ID}"
    seen: list[dict] = []

    def _list(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(id=ACK_RUN_ID, name="argus-test", dataset_id=DATASET_ID)])

    client = _fake_client(
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        experiments=_list,
    )

    evidence = _verify(script, monkeypatch, url=url, tasks=[_task()], client=client)

    lookup = evidence["remote_run_lookup"]
    assert lookup["status"] == "CONFIRMED"
    assert "experiments.list" in lookup["via"]
    assert evidence["remote_run_id"] == ACK_RUN_ID
    assert evidence["remote_dataset_id"] == DATASET_ID
    assert evidence["key_project_id"] == PROJECT_ID
    # 必须按 ID 精确查询：name 过滤在 Langfuse 侧不保证命中
    assert seen and all(kwargs.get("id") == ACK_RUN_ID for kwargs in seen)


def test_remote_identity_stays_green_when_platform_cannot_expose_the_run(monkeypatch, capsys):
    """平台读不回 legacy 写入的 Dataset Run 时，身份核对不能靠"猜"。

    真实 Cloud 证据（PR #79 CI run 36712685541）：异步 Outbox 通过
    POST /api/public/dataset-run-items 写入的 Run，在该 Organization 上
    v3 读路径全部 410，v4 读路径（experiments / experiment-items / v3 scores）
    一律查不到——同一 Key 下同步路径创建的 Experiment 却可以查到。
    此时脚本必须仍然证明"链接指向真实身份"，而不是把平台限制当成回归。
    """
    script = _load_verification_script()
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_INTERVAL_SECONDS", 0.0, raising=False)

    url = f"{CLEAN}/project/{PROJECT_ID}/datasets/{DATASET_ID}/runs/{ACK_RUN_ID}"

    def _legacy_gone(**_kwargs):
        raise RuntimeError(
            "status_code: 410, body: {'error': 'LEGACY_API_UNAVAILABLE_FOR_NEW_ORGANIZATION'}"
        )

    client = _fake_client(
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        experiments=lambda **_kw: SimpleNamespace(
            data=[SimpleNamespace(id="other-run", name="argus-ci-v1-unrelated")]
        ),
        experiment_items=_legacy_gone,
        scores=lambda **_kw: SimpleNamespace(data=[]),
    )

    evidence = _verify(script, monkeypatch, url=url, tasks=[_task()], client=client)

    lookup = evidence["remote_run_lookup"]
    assert lookup["status"] == "NOT_EXPOSED"
    assert lookup["attempts"] == 2
    assert evidence["remote_run_id"] is None
    # 已核对的事实一个都不能少
    assert evidence["key_project_id"] == PROJECT_ID
    assert evidence["remote_dataset_id"] == DATASET_ID
    assert evidence["url_project_id"] == PROJECT_ID
    assert evidence["url_dataset_id"] == DATASET_ID
    assert evidence["url_run_id"] == ACK_RUN_ID
    # Persisted Run ID 必须能被 Langfuse 的 create ACK 追溯，而不是 Argus 合成
    assert evidence["ack_run_ids"] == [ACK_RUN_ID]
    # 读不到也要留下可诊断的证据
    probed = {probe["via"] for probe in lookup["probes"]}
    assert {"experiments.list", "experiment-items", "v3-scores"} <= probed
    assert any(probe["status"] == "ERROR" for probe in lookup["probes"])
    assert any("other-run" in str(row) for row in lookup["observed"])

    notice = capsys.readouterr()
    notice = notice.out + notice.err
    assert ACK_RUN_ID in notice
    assert "NOT_EXPOSED" in notice


def test_remote_identity_fails_when_task_evidence_contradicts_the_link(monkeypatch):
    """Persisted Run ID 与任务 ACK 不一致时必须失败：链接可能指向另一个 Run。"""
    script = _load_verification_script()
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_INTERVAL_SECONDS", 0.0, raising=False)

    url = f"{CLEAN}/project/{PROJECT_ID}/datasets/{DATASET_ID}/runs/{ACK_RUN_ID}"
    client = _fake_client(project_id=PROJECT_ID, dataset_id=DATASET_ID)

    with pytest.raises(SystemExit):
        _verify(
            script,
            monkeypatch,
            url=url,
            tasks=[_task(ack_run_id="99999999-9999-9999-9999-999999999999")],
            client=client,
        )


def test_remote_identity_fails_when_url_carries_dataset_name_instead_of_id(monkeypatch):
    """URL 里的 Dataset 段必须是远端真实 ID，而不是 Dataset Name。"""
    script = _load_verification_script()
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_INTERVAL_SECONDS", 0.0, raising=False)

    url = f"{CLEAN}/project/{PROJECT_ID}/datasets/{DS_NAME}/runs/{ACK_RUN_ID}"
    client = _fake_client(project_id=PROJECT_ID, dataset_id=DATASET_ID)

    with pytest.raises(SystemExit):
        _verify(script, monkeypatch, url=url, tasks=[_task()], client=client)


def test_remote_identity_fails_when_project_in_url_is_not_the_key_project(monkeypatch):
    """URL 里的 Project 必须是当前 Key 对应的 Project（曾错误拼接 poc-project）。"""
    script = _load_verification_script()
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_INTERVAL_SECONDS", 0.0, raising=False)

    url = f"{CLEAN}/project/poc-project/datasets/{DATASET_ID}/runs/{ACK_RUN_ID}"
    client = _fake_client(project_id=PROJECT_ID, dataset_id=DATASET_ID)

    with pytest.raises(SystemExit):
        _verify(script, monkeypatch, url=url, tasks=[_task()], client=client)


def test_remote_identity_retries_within_a_bounded_recent_window(monkeypatch):
    """实验刚创建，Cloud 读路径可能滞后：查询必须收敛到最近窗口并有限重试。

    from_start_time 若从 2020 年起算且 limit=100，返回的是最早的一页，
    永远不会包含本次刚创建的 Experiment。
    """
    script = _load_verification_script()
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_INTERVAL_SECONDS", 0.0, raising=False)

    url = f"{CLEAN}/project/{PROJECT_ID}/datasets/{DATASET_ID}/runs/{ACK_RUN_ID}"
    seen: list[dict] = []

    def _list(**kwargs):
        seen.append(kwargs)
        recent = datetime.now(UTC) - timedelta(hours=7)
        assert kwargs["from_start_time"] >= recent, kwargs
        assert kwargs["to_start_time"] > datetime.now(UTC) - timedelta(minutes=1), kwargs
        if len(seen) < 3:
            return SimpleNamespace(data=[])
        return SimpleNamespace(
            data=[SimpleNamespace(id=ACK_RUN_ID, name="argus-test", dataset_id=DATASET_ID)]
        )

    client = _fake_client(project_id=PROJECT_ID, dataset_id=DATASET_ID, experiments=_list)

    evidence = _verify(script, monkeypatch, url=url, tasks=[_task()], client=client)

    assert evidence["remote_run_id"] == ACK_RUN_ID
    assert evidence["remote_run_lookup"]["attempts"] == 3
    assert len(seen) >= 3


def test_remote_identity_confirms_run_via_experiment_items(monkeypatch):
    """Run 本身未出现在 experiments 时，v4 的 experiment-items 仍可确认其身份。"""
    script = _load_verification_script()
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(script, "REMOTE_RUN_LOOKUP_INTERVAL_SECONDS", 0.0, raising=False)

    url = f"{CLEAN}/project/{PROJECT_ID}/datasets/{DATASET_ID}/runs/{ACK_RUN_ID}"
    client = _fake_client(
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        experiment_items=lambda **_kw: SimpleNamespace(
            data=[
                SimpleNamespace(
                    experiment_id=ACK_RUN_ID,
                    experiment_name="argus-test",
                    experiment_dataset_id=DATASET_ID,
                    trace_id="trace-1",
                )
            ]
        ),
    )

    evidence = _verify(script, monkeypatch, url=url, tasks=[_task()], client=client)

    assert evidence["remote_run_lookup"]["status"] == "CONFIRMED"
    assert "experiment-items" in evidence["remote_run_lookup"]["via"]
    assert evidence["remote_run_id"] == ACK_RUN_ID
    assert evidence["remote_dataset_id"] == DATASET_ID
