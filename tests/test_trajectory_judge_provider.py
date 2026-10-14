"""异步 Judge 的单次有界 HTTP 传输；不以测试 Provider 充当真实校准。"""

import asyncio
import json
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from test_trajectory_judge_definition import definition
from test_trajectory_judge_input import request as build_input
from test_trajectory_judge_input import source as input_source


@pytest.fixture
def prepare_request(tmp_path):
    from copy import deepcopy
    from types import SimpleNamespace

    from app.db_models import AgentVersionRecord, ExecutionCheckpointRecord, ExperimentLaunchRecord
    from app.evaluator_binding import canonical_digest, manifest_measurement_digest
    from app.execution_checkpoint import binding_provenance_from_manifest
    from app.trajectory.judge_input import adjudicate_judge_criteria, prepare_judge_request
    from app.trajectory.manifest import bind_trajectory_manifest, freeze_trajectory_contract
    from test_trajectory_assembler import configured
    from test_trajectory_inputs import context, ready

    setup = context.__wrapped__(SimpleNamespace(param="sqlite"), tmp_path)
    db, scope, contract, started_at = next(setup)
    try:
        contract = configured(contract, redaction_policy=contract.redaction_policy.model_dump() | {
            "content_policy_version": "pii-v1", "string_policies": {"$.customer_id": "identifier", "$.intent": "identifier"}})
        with db.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, scope.launch_id)
            cp = session.get(ExecutionCheckpointRecord, "checkpoint")
            manifest = deepcopy(launch.manifest)
            cp.expected_output = {"intent": "balance"}
            manifest["dataset"]["items"][0]["expected_output"] = cp.expected_output
            manifest["trajectory"] = bind_trajectory_manifest(freeze_trajectory_contract(contract), session.get(AgentVersionRecord, launch.agent_version_id))
            manifest["measurement_digest"] = manifest_measurement_digest(manifest)
            manifest["contract_digests"]["measurement"]["digest"] = manifest["measurement_digest"]
            launch.manifest = manifest
            cp.manifest_digest, cp.binding_provenance = canonical_digest(manifest), binding_provenance_from_manifest(manifest)
        ready.__wrapped__((db, scope, contract, started_at))
        def prepare(frozen):
            approved = adjudicate_judge_criteria({"intent": "balance"}, policy=frozen.criteria_policy, scope=scope, limits=contract.limits)
            with db.get_session() as session:
                return prepare_judge_request(session, item_id=scope.item_execution_id, generation=scope.dispatch_generation,
                    manifest=manifest, definition=frozen, approved_criteria=approved)
        yield prepare
    finally:
        setup.close()


@pytest.fixture
def prepared(prepare_request):
    return prepare_request(definition())


def identity():
    from app.trajectory.judge_provider import JudgeProviderIdentity
    frozen = definition()
    return JudgeProviderIdentity(provider_id=frozen.provider_id, provider_api_version=frozen.provider_api_version,
        resolved_model_id=frozen.resolved_model_id, resolved_model_artifact_digest=frozen.resolved_model_artifact_digest)


def provider(handler, **changes):
    from app.trajectory.judge_provider import OpenAICompatibleJudgeProvider
    values = dict(base_url="http://127.0.0.1:8000/v1", api_key="synthetic-secret", identity=identity(),
        transport=httpx.MockTransport(handler))
    values.update(changes)
    return OpenAICompatibleJudgeProvider(**values)


def response(prepared):
    return {"model": definition().resolved_model_id, "choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps({"schema_version": "judge-output-v1", "verdict": "PASS",
            "confidence": 0.95, "reason_code": "TASK_COMPLETE", "evidence_step_ids": [sorted(prepared.local_step_ids)[0]], "rationale": ""})}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}}


def http_response(body):
    # MockTransport 的 json= 会预读取正文；真实传输是尚未消费的异步 stream。
    return httpx.Response(200, stream=Stream(raw=json.dumps(body).encode()), headers={"content-type": "application/json"})


def test_single_structured_request_checks_model_and_returns_only_safe_result(prepared):
    calls = []
    def handle(request):
        calls.append(request)
        return http_response(response(prepared))
    async def run():
        async with provider(handle) as client:
            value = await client.evaluate(prepared, definition())
        assert client.is_closed
        return value
    value = asyncio.run(run())
    assert value.result.value == "PASS" and value.result.status == "succeeded"
    assert value.usage.input_tokens == 40 and value.usage.output_tokens == 20
    assert value.input_digest == prepared.input_digest and value.definition_digest == prepared.definition_digest
    assert value.duration_ms >= 0
    assert "synthetic-secret" not in repr(value)
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body["stream"] is False and body["model"] == definition().resolved_model_id
    assert body["messages"] == json.loads(prepared.messages_json)
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["properties"]["confidence"]["type"] == "number"
    assert body["max_tokens"] == definition().max_output_tokens
    assert calls[0].url.path == "/v1/chat/completions"


@pytest.mark.parametrize("profile,thinking", [("openai-compatible-v1", None), ("omlx-0.7-v1", False), ("omlx-0.7-v1", True)])
def test_adapter_sends_exact_frozen_seed_and_template_options(prepare_request, profile, thinking):
    from app.trajectory.judge import JudgeProviderOptions
    from app.trajectory.judge_provider import JudgeProviderIdentity

    frozen = definition(provider_api_version="0.7.0" if profile == "omlx-0.7-v1" else "v1",
        provider_options=JudgeProviderOptions(profile=profile, seed=73, enable_thinking=thinking))
    prepared = prepare_request(frozen)
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        return http_response(response(prepared))
    async def run():
        identity_value = JudgeProviderIdentity(**{key: getattr(frozen, key) for key in JudgeProviderIdentity.model_fields})
        async with provider(handle, identity=identity_value) as client:
            return await client.evaluate(prepared, frozen)
    outcome = asyncio.run(run())
    assert outcome.result.status == "succeeded" and len(calls) == 1
    assert calls[0]["seed"] == 73
    if thinking is None:
        assert "enable_thinking" not in calls[0] and "chat_template_kwargs" not in calls[0]
    else:
        assert calls[0]["enable_thinking"] is thinking
        assert calls[0]["chat_template_kwargs"] == {"enable_thinking": thinking}


def test_adapter_rejects_structured_output_downgrade_without_leaking_warning(prepared):
    def handle(request):
        result = http_response(response(prepared))
        result.headers["Warning"] = '199 omlx "strict schema not enforced; synthetic-secret"'
        return result
    async def run():
        async with provider(handle) as client:
            return await client.evaluate(prepared, definition())
    outcome = asyncio.run(run())
    assert outcome.result.status == "failed" and outcome.result.value is None
    assert outcome.result.reason_code == "JUDGE_PROVIDER_CAPABILITY_UNAVAILABLE"
    assert "synthetic-secret" not in repr(outcome)


@pytest.mark.parametrize("damage,reason", [("model", "JUDGE_MODEL_IDENTITY_MISMATCH"),
    ("finish", "JUDGE_PROVIDER_RESPONSE_INVALID"), ("refusal", "JUDGE_PROVIDER_RESPONSE_INVALID"),
    ("extra_choice", "JUDGE_PROVIDER_RESPONSE_INVALID"), ("usage_bool", "JUDGE_PROVIDER_USAGE_INVALID"),
    ("usage_total", "JUDGE_PROVIDER_USAGE_INVALID"), ("output_budget", "JUDGE_PROVIDER_USAGE_INVALID"),
    ("schema", "JUDGE_OUTPUT_INVALID"), ("extreme_exponent", "JUDGE_OUTPUT_INVALID"),
    ("foreign_step", "JUDGE_EVIDENCE_REF_INVALID")])
def test_provider_never_turns_invalid_identity_transport_or_output_into_score(prepared, damage, reason):
    body = response(prepared)
    if damage == "model":
        body["model"] = "fallback-model"
    elif damage == "finish":
        body["choices"][0]["finish_reason"] = "length"
    elif damage == "refusal":
        body["choices"][0]["message"]["refusal"] = "synthetic private text"
    elif damage == "extra_choice":
        body["choices"].append(body["choices"][0])
    elif damage == "usage_bool":
        body["usage"]["prompt_tokens"] = True
    elif damage == "usage_total":
        body["usage"]["total_tokens"] = 99
    elif damage == "output_budget":
        body["usage"]["completion_tokens"] = definition().max_output_tokens + 1
        body["usage"]["total_tokens"] = 40 + body["usage"]["completion_tokens"]
    elif damage == "extreme_exponent":
        body["choices"][0]["message"]["content"] = body["choices"][0]["message"]["content"].replace("0.95", "1e999999999999999999999999")
    else:
        output = json.loads(body["choices"][0]["message"]["content"])
        if damage == "schema":
            output["confidence"] = "0.95"
        else:
            output["evidence_step_ids"] = ["foreign-item-step"]
        body["choices"][0]["message"]["content"] = json.dumps(output)
    async def run():
        async with provider(lambda request: http_response(body)) as client:
            return await client.evaluate(prepared, definition())
    value = asyncio.run(run())
    assert value.result.value is None and value.result.reason_code == reason
    assert "synthetic private text" not in repr(value)


def test_absent_usage_remains_unknown_instead_of_zero(prepared):
    body = response(prepared)
    del body["usage"]
    async def run():
        async with provider(lambda request: http_response(body)) as client:
            return await client.evaluate(prepared, definition())
    assert asyncio.run(run()).usage is None


def test_decoding_decimal_is_sent_exactly_as_frozen(prepared, prepare_request):
    frozen = definition(temperature=Decimal("0.1234567890123456789012345678"))
    fixed = prepare_request(frozen)
    calls = []
    def handle(request):
        calls.append(request)
        return http_response(response(fixed))
    async def run():
        async with provider(handle) as client:
            await client.evaluate(fixed, frozen)
    asyncio.run(run())
    assert json.loads(calls[0].content, parse_float=Decimal)["temperature"] == frozen.temperature


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_http_failure_has_no_retry_or_response_secret(prepared, status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="Bearer synthetic-secret", headers={"location": "https://unapproved.invalid"})
    async def run():
        async with provider(handle) as client:
            return await client.evaluate(prepared, definition())
    value = asyncio.run(run())
    assert len(calls) == 1 and value.result.status == "failed" and value.result.value is None
    assert value.result.reason_code == "JUDGE_PROVIDER_HTTP_ERROR" and "synthetic-secret" not in repr(value)


class Stream(httpx.AsyncByteStream):
    def __init__(self, *, raw=b"", block=False):
        self.raw, self.block = raw, block
        self.started, self.closed = asyncio.Event(), False

    async def __aiter__(self):
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        yield self.raw

    async def aclose(self):
        self.closed = True


def test_cancellation_closes_actual_response_and_releases_capacity(prepared):
    async def run():
        stream = Stream(block=True)
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, stream=stream, headers={"content-type": "application/json"}) if len(calls) == 1 else http_response(response(prepared))
        async with provider(handle) as client:
            task = asyncio.create_task(client.evaluate(prepared, definition()))
            await asyncio.wait_for(stream.started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stream.closed
            value = await client.evaluate(prepared, definition())
            assert value.result.value == "PASS"
    asyncio.run(run())


def test_timeout_closes_response_without_a_score(prepared, prepare_request):
    async def run():
        stream = Stream(block=True)
        frozen = definition(request_timeout_seconds=Decimal("0.03"))
        fixed = prepare_request(frozen)
        async with provider(lambda request: httpx.Response(200, stream=stream, headers={"content-type": "application/json"})) as client:
            value = await client.evaluate(fixed, frozen)
        assert stream.closed and value.result.value is None and value.result.reason_code == "JUDGE_TIMEOUT"
    asyncio.run(run())


def test_concurrency_two_does_not_block_event_loop(prepared):
    async def run():
        active = maximum = ticks = 0
        async def handle(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.03)
            active -= 1
            return http_response(response(prepared))
        async def heartbeat():
            nonlocal ticks
            for _ in range(20):
                ticks += 1
                await asyncio.sleep(0.005)
        async with provider(handle) as client:
            values, _ = await asyncio.gather(asyncio.gather(*(client.evaluate(prepared, definition()) for _ in range(6))), heartbeat())
        assert maximum == 2 and ticks == 20 and all(value.result.value == "PASS" for value in values)
    asyncio.run(run())


def test_waiting_for_capacity_is_in_total_timeout_and_never_sends(prepared, prepare_request):
    async def run():
        streams, calls = [], []
        def handle(request):
            calls.append(request)
            stream = Stream(block=True)
            streams.append(stream)
            return httpx.Response(200, stream=stream, headers={"content-type": "application/json"})
        async with provider(handle) as client:
            tasks = [asyncio.create_task(client.evaluate(prepared, definition())) for _ in range(2)]
            while len(streams) < 2:
                await asyncio.sleep(0)
            await asyncio.gather(*(stream.started.wait() for stream in streams))
            frozen = definition(request_timeout_seconds=Decimal("0.02"))
            value = await client.evaluate(prepare_request(frozen), frozen)
            assert value.result.reason_code == "JUDGE_TIMEOUT" and len(calls) == 2
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(value, asyncio.CancelledError) for value in results)
            assert all(stream.closed for stream in streams)
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [True, False])
def test_real_loopback_tcp_response_is_closed_on_cancel_or_timeout(prepared, cancel, prepare_request):
    async def run():
        started, disconnected = asyncio.Event(), asyncio.Event()
        async def handle(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:"))
                await reader.readexactly(length)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n")
                await writer.drain()
                started.set()
                assert await reader.read(1) == b""
                disconnected.set()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        frozen = definition(request_timeout_seconds=Decimal("0.2"))
        fixed = prepare_request(frozen)
        async with server:
            async with provider(lambda request: None, base_url=f"http://127.0.0.1:{port}/v1", transport=None) as client:
                task = asyncio.create_task(client.evaluate(fixed, frozen))
                await asyncio.wait_for(started.wait(), 1)
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    value = await task
                    assert value.result.reason_code == "JUDGE_TIMEOUT" and value.result.value is None
                await asyncio.wait_for(disconnected.wait(), 1)
    asyncio.run(run())


def test_network_error_does_not_expose_provider_exception(prepared):
    def handle(request):
        raise httpx.ConnectError("Bearer synthetic-secret", request=request)
    async def run():
        async with provider(handle) as client:
            return await client.evaluate(prepared, definition())
    value = asyncio.run(run())
    assert value.result.reason_code == "JUDGE_PROVIDER_NETWORK_ERROR" and "synthetic-secret" not in repr(value)


@pytest.mark.parametrize("raw", [b"x" * (128 * 1024 + 1), b'{"model":"a","model":"b"}', b'{"x":NaN}',
    b'{"x":1e999999999999999999999999}', b'[' * 40 + b'0' + b']' * 40])
def test_bounded_response_and_strict_envelope_close_stream(prepared, raw):
    async def run():
        stream = Stream(raw=raw)
        async with provider(lambda request: httpx.Response(200, stream=stream, headers={"content-type": "application/json"})) as client:
            value = await client.evaluate(prepared, definition())
        assert stream.closed and value.result.status == "failed" and value.result.value is None
    asyncio.run(run())


def test_changed_request_or_frozen_identity_never_sends(prepared, prepare_request):
    async def run():
        calls = []
        async with provider(lambda request: calls.append(request)) as client:
            value = await client.evaluate(replace(prepared, messages_json=prepared.messages_json + " "), definition())
            assert value.result.reason_code == "JUDGE_PREPARED_REQUEST_INVALID"
            changed = definition(resolved_model_artifact_digest="sha256:" + "b" * 64)
            value = await client.evaluate(prepare_request(changed), changed)
            assert value.result.reason_code == "JUDGE_PROVIDER_IDENTITY_MISMATCH"
        assert not calls
    asyncio.run(run())


def test_recomputed_public_hash_cannot_authorize_added_secret(prepared):
    from app.trajectory.codec import canonical_digest
    from app.trajectory.judge_input import _text_digest
    messages = json.loads(prepared.messages_json)
    data = json.loads(messages[1]["content"])
    data["leak"] = "Bearer synthetic-secret"
    messages[1]["content"] = json.dumps(data)
    text = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = canonical_digest({"input_schema_version": "judge-input-v1", "scope": prepared.scope.model_dump(),
        "evidence_ref": prepared.evidence_ref.model_dump(), "definition_digest": prepared.definition_digest,
        "criteria_digest": prepared.criteria_digest, "messages_digest": _text_digest(text)})
    forged = replace(prepared, messages_json=text, input_digest=digest)
    async def run():
        calls = []
        def handle(request):
            calls.append(request)
            return http_response(response(prepared))
        async with provider(handle) as client:
            value = await client.evaluate(forged, definition())
        assert not calls and value.result.reason_code == "JUDGE_PREPARED_REQUEST_INVALID"
    asyncio.run(run())


def test_pure_builder_cannot_bypass_database_and_criteria_source(prepared):
    from test_trajectory_judge_input import criteria
    source = input_source.__wrapped__()
    forged = build_input(source, approved=criteria(source, {"intent": "escalate"}))
    async def run():
        calls = []
        def handle(request):
            calls.append(request)
            return http_response(response(forged))
        async with provider(handle) as client:
            value = await client.evaluate(forged, definition())
        assert not calls and value.result.reason_code == "JUDGE_PREPARED_REQUEST_INVALID"
    asyncio.run(run())


def test_invalid_digest_metadata_is_not_returned_as_sensitive_text(prepared):
    async def run():
        async with provider(lambda request: None) as client:
            value = await client.evaluate(replace(prepared, input_digest="Bearer synthetic-secret"), definition())
        assert value.result.reason_code == "JUDGE_PREPARED_REQUEST_INVALID"
        assert "synthetic-secret" not in repr(value) and value.input_digest is None
    asyncio.run(run())


@pytest.mark.parametrize("url", ["http://untrusted.invalid/v1", "https://name:secret@example.com/v1", "https://example.com/v1?q=1"])
def test_unapproved_or_credential_embedded_url_is_rejected(url):
    with pytest.raises(ValueError, match="JUDGE_PROVIDER_CONFIGURATION_INVALID"):
        provider(lambda request: None, base_url=url)
