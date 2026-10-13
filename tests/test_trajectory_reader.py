import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from app.trajectory.reader import LangfuseObservationReader, ObservationReadError
from app.trajectory.schema import TrajectoryScope
from trajectory_fixtures import scope_payload


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, block=False):
        self.chunks = chunks
        self.block = block
        self.started = asyncio.Event()
        self.closed = False
        self.visited = 0

    async def __aiter__(self):
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        for chunk in self.chunks:
            self.visited += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def reader_for(handler):
    return LangfuseObservationReader(
        server_url="http://127.0.0.1:3000",
        public_key="synthetic-public",
        secret_key="synthetic-secret",
        transport=httpx.MockTransport(handler),
    )


async def read(reader):
    return await reader.read_page(
        TrajectoryScope.model_validate(scope_payload()),
        cursor=None,
        page_size=2,
        timeout_seconds=0.1,
        max_bytes=8192,
    )


def test_read_only_v2_query_and_cursor_keep_numbers_exact():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            content=b'{"data":[{"id":"tool-1","amount":0.12345678901234567890123456789}],'
            b'"meta":{"cursor":"next-cursor"}}',
        )

    async def scenario():
        async with reader_for(handler) as reader:
            page = await reader.read_page(
                TrajectoryScope.model_validate(scope_payload()),
                cursor="prior-cursor",
                page_size=2,
                timeout_seconds=1,
                max_bytes=8192,
            )
            assert page.cursor == "next-cursor"
            assert page.rows[0]["amount"] == Decimal("0.12345678901234567890123456789")
            assert page.source_bytes > 0
        assert requests[0].method == "GET"
        assert requests[0].url.path == "/api/public/v2/observations"
        params = requests[0].url.params
        assert params["traceId"] == "1" * 32 and params["cursor"] == "prior-cursor" and params["limit"] == "2"
        assert params["fields"] == "core,basic,time,io,metadata"
        assert "parseIoAsJson" not in params
        assert requests[0].headers["accept-encoding"] == "identity"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "TRAJECTORY_SOURCE_AUTH_FAILED", False),
        (403, "TRAJECTORY_SOURCE_AUTH_FAILED", False),
        (408, "TRAJECTORY_SOURCE_TIMEOUT", True),
        (400, "TRAJECTORY_SOURCE_CONTRACT_ERROR", False),
        (404, "TRAJECTORY_SOURCE_CONTRACT_ERROR", False),
        (429, "TRAJECTORY_SOURCE_RATE_LIMITED", True),
        (500, "TRAJECTORY_SOURCE_TEMPORARY_ERROR", True),
        (503, "TRAJECTORY_SOURCE_TEMPORARY_ERROR", True),
        (302, "TRAJECTORY_SOURCE_REDIRECT_REJECTED", False),
    ],
)
def test_http_failures_do_not_retry_follow_redirects_or_echo_upstream_details(status, code, retryable):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"location": "http://other.example/invoke"},
            content=b"synthetic-secret private observation body",
        )

    async def scenario():
        async with reader_for(handler) as reader:
            with pytest.raises(ObservationReadError) as error:
                await read(reader)
            assert error.value.reason_code == code and error.value.retryable is retryable
            assert str(error.value) == code
            assert "synthetic-secret" not in repr(error.value)
        assert len(requests) == 1

    asyncio.run(scenario())


def test_chunked_oversize_response_stops_and_closes_before_remaining_body():
    stream = TrackingStream([b" " * 100, b" " * 100, b"private-unvisited-body"])

    async def scenario():
        async with reader_for(lambda request: httpx.Response(200, stream=stream)) as reader:
            with pytest.raises(ObservationReadError, match="TRAJECTORY_SOURCE_BYTE_LIMIT"):
                await reader.read_page(
                    TrajectoryScope.model_validate(scope_payload()),
                    cursor=None,
                    page_size=2,
                    timeout_seconds=1,
                    max_bytes=150,
                )
        assert stream.closed and stream.visited == 2

    asyncio.run(scenario())


def test_content_length_over_budget_is_rejected_before_stream_iteration():
    stream = TrackingStream([b"not-visited"])

    async def scenario():
        async with reader_for(
            lambda request: httpx.Response(200, headers={"content-length": "10000"}, stream=stream)
        ) as reader:
            with pytest.raises(ObservationReadError, match="TRAJECTORY_SOURCE_BYTE_LIMIT"):
                await read(reader)
        assert stream.closed and stream.visited == 0

    asyncio.run(scenario())


def test_compressed_responses_are_rejected_before_decompression():
    stream = TrackingStream([b"not-visited"])

    async def scenario():
        async with reader_for(
            lambda request: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
        ) as reader:
            with pytest.raises(ObservationReadError, match="TRAJECTORY_SOURCE_ENCODING_UNSUPPORTED"):
                await read(reader)
        assert stream.closed and stream.visited == 0

    asyncio.run(scenario())


def test_absolute_request_timeout_also_bounds_slow_streams():
    stream = TrackingStream(block=True)

    async def scenario():
        async with reader_for(lambda request: httpx.Response(200, stream=stream)) as reader:
            with pytest.raises(ObservationReadError) as error:
                await read(reader)
            assert error.value.reason_code == "TRAJECTORY_SOURCE_TIMEOUT" and error.value.retryable
        assert stream.closed

    asyncio.run(scenario())


def test_cancellation_is_propagated_and_closes_the_response():
    stream = TrackingStream(block=True)

    async def scenario():
        async with reader_for(lambda request: httpx.Response(200, stream=stream)) as reader:
            task = asyncio.create_task(read(reader))
            await stream.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"data": [], "meta": []},
        {"data": {}, "meta": {}},
        {"data": ["wrong-row"], "meta": {}},
        {"data": [{}, {}, {}], "meta": {}},
        {"data": [], "meta": {"cursor": 12}},
        {"data": [], "meta": {"cursor": "\n"}},
    ],
)
def test_response_shape_is_checked_without_copying_provider_domain_models(payload):
    async def scenario():
        async with reader_for(lambda request: httpx.Response(200, content=json.dumps(payload))) as reader:
            with pytest.raises(ObservationReadError, match="TRAJECTORY_SOURCE_CONTRACT_ERROR"):
                await read(reader)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw,code",
    [
        (b'{"data":[],"data":[],"meta":{}}', "TRAJECTORY_SOURCE_JSON_INVALID"),
        (b'{"data":[],"meta":{},"x":NaN}', "TRAJECTORY_SOURCE_JSON_INVALID"),
        (b"private-invalid-json", "TRAJECTORY_SOURCE_JSON_INVALID"),
    ],
)
def test_invalid_json_is_a_safe_contract_failure(raw, code):
    async def scenario():
        async with reader_for(lambda request: httpx.Response(200, content=raw)) as reader:
            with pytest.raises(ObservationReadError) as error:
                await read(reader)
            assert error.value.reason_code == code and not error.value.retryable
            assert "private" not in str(error.value)

    asyncio.run(scenario())


def test_empty_cursor_means_pagination_exhausted_and_client_closes_on_exit():
    async def scenario():
        async with reader_for(
            lambda request: httpx.Response(200, json={"data": [], "meta": {"cursor": None}})
        ) as reader:
            assert (await read(reader)).cursor is None
        assert reader.is_closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "server_url",
    [
        "file:///tmp/data",
        "http://user:secret@host",
        "https://host/?token=secret",
        "https://host/#secret",
        "http://remote.example",
    ],
)
def test_url_rejects_hidden_credentials_and_unapproved_schemes_without_echo(server_url):
    with pytest.raises(ValueError, match="TRAJECTORY_READER_CONFIGURATION_INVALID") as error:
        LangfuseObservationReader(server_url=server_url, public_key="synthetic", secret_key="synthetic")
    assert server_url not in str(error.value)


def test_plaintext_for_internal_service_requires_an_explicit_trusted_host():
    async def scenario():
        with pytest.raises(ValueError, match="TRAJECTORY_READER_CONFIGURATION_INVALID"):
            LangfuseObservationReader(
                server_url="http://langfuse-web:3000", public_key="synthetic", secret_key="synthetic"
            )
        async with LangfuseObservationReader(
            server_url="http://langfuse-web:3000",
            public_key="synthetic",
            secret_key="synthetic",
            trusted_http_hosts=frozenset({"langfuse-web"}),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [], "meta": {}})),
        ) as reader:
            assert (await read(reader)).rows == ()
        assert reader.is_closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "overrides",
    [
        {"page_size": True},
        {"page_size": 0},
        {"page_size": 101},
        {"max_bytes": False},
        {"max_bytes": 0},
        {"max_bytes": 8 * 1024 * 1024 + 1},
        {"cursor": "\n"},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": 6},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
    ],
)
def test_invalid_read_limits_fail_before_any_network_request(overrides):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [], "meta": {}})

    async def scenario():
        options = {"cursor": None, "page_size": 2, "timeout_seconds": 1, "max_bytes": 8192, **overrides}
        async with reader_for(handler) as reader:
            with pytest.raises(ValueError, match="TRAJECTORY_READER_LIMIT_INVALID"):
                await reader.read_page(TrajectoryScope.model_validate(scope_payload()), **options)
        assert requests == [] and reader.is_closed

    asyncio.run(scenario())


def test_transport_error_does_not_expose_url_or_credentials_and_is_retryable():
    def handler(request):
        raise httpx.ConnectError("synthetic-secret private URL", request=request)

    async def scenario():
        async with reader_for(handler) as reader:
            with pytest.raises(ObservationReadError) as error:
                await read(reader)
            assert error.value.reason_code == "TRAJECTORY_SOURCE_TRANSPORT_ERROR"
            assert error.value.retryable and "synthetic-secret" not in str(error.value)
        assert reader.is_closed

    asyncio.run(scenario())
