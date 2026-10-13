"""Regression tests for the local harness boundary, without starting services."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from trajectory_agents.local_runtime import LocalLangfuse  # noqa: E402


def test_sdk_has_explicit_non_proxy_transports_and_environment(tmp_path, monkeypatch):
    import langfuse

    monkeypatch.setattr("trajectory_agents.local_runtime.require_local_docker", lambda env: None, raising=False)
    monkeypatch.setenv("LANGFUSE_DEBUG", "true")
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
    monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "0")
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:8080")
    observed = {}
    def sdk(**kwargs):
        observed.update(kwargs)
        observed["inherited_debug"] = os.environ.get("LANGFUSE_DEBUG")
        observed["inherited_proxy"] = os.environ.get("HTTP_PROXY")
        return SimpleNamespace(shutdown=lambda: None)
    monkeypatch.setattr(langfuse, "Langfuse", sdk)
    original_client = httpx.Client
    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="openapi: 3.0.0")))
    monkeypatch.setattr(httpx, "Client", client)
    runtime = LocalLangfuse(tmp_path)
    monkeypatch.setattr(runtime, "compose", lambda *args, **kwargs: "127.0.0.1:12345" if args[0] == "port" else "")
    with runtime:
        assert observed["httpx_client"]._trust_env is False
        assert runtime.export_session.trust_env is False
        assert observed["span_exporter"] is not None
        assert observed["sample_rate"] == 1.0
        assert observed["inherited_debug"] is None and observed["inherited_proxy"] is None
    assert os.environ["LANGFUSE_DEBUG"] == "true"
    assert os.environ["HTTP_PROXY"] == "http://192.0.2.1:8080"


@pytest.mark.parametrize("stage", ["sdk", "http"])
def test_cleanup_attempts_down_when_a_client_fails(tmp_path, stage):
    runtime = LocalLangfuse(tmp_path)
    sdk_shutdown, http_close, compose = Mock(), Mock(), Mock()
    failure = RuntimeError("synthetic cleanup failure")
    (sdk_shutdown if stage == "sdk" else http_close).side_effect = failure
    runtime.sdk = SimpleNamespace(shutdown=sdk_shutdown)
    runtime.http = SimpleNamespace(close=http_close)
    runtime._compose_started = True
    runtime.compose = compose
    with pytest.raises(RuntimeError) as caught:
        runtime.__exit__(None, None, None)
    assert caught.value is failure
    sdk_shutdown.assert_called_once()
    http_close.assert_called_once()
    compose.assert_called_once_with("down", "--volumes", "--remove-orphans", timeout=120)


def test_cleanup_preserves_the_original_test_exception(tmp_path, monkeypatch):
    runtime = LocalLangfuse(tmp_path)
    runtime.sdk = SimpleNamespace(shutdown=Mock(side_effect=RuntimeError("synthetic cleanup failure")))
    runtime._compose_started = True
    runtime.compose = Mock()
    monkeypatch.setattr(LocalLangfuse, "__enter__", lambda self: self)
    original = AssertionError("original assertion")
    with pytest.raises(AssertionError) as caught:
        with runtime:
            raise original
    assert caught.value is original
    assert runtime.compose.call_args.args == ("down", "--volumes", "--remove-orphans")


def test_startup_failure_preserved_even_when_cleanup_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("trajectory_agents.local_runtime.require_local_docker", lambda env: None, raising=False)
    runtime = LocalLangfuse(tmp_path)
    original = RuntimeError("original startup failure")
    runtime.compose = Mock(side_effect=[original, RuntimeError("synthetic cleanup failure")])
    with pytest.raises(RuntimeError) as caught:
        with runtime:
            pytest.fail("startup must fail")
    assert caught.value is original


@pytest.mark.parametrize("context", [False, True])
def test_remote_docker_rejected_before_compose(tmp_path, monkeypatch, context):
    import json

    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "tcp://192.0.2.1:2376")
    if context:
        monkeypatch.setenv("DOCKER_CONTEXT", "remote-test")
        monkeypatch.setattr("trajectory_agents.local_runtime.subprocess.run", Mock(return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps("ssh://example.invalid"))))
    runtime = LocalLangfuse(tmp_path)
    runtime.compose = Mock()
    with pytest.raises(ValueError, match="local Unix"):
        runtime.__enter__()
    runtime.compose.assert_not_called()
