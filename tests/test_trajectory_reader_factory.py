import asyncio
import hashlib
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from app.trajectory.factory import QualifiedReaderFactory, ReaderApproval, reader_artifact_digest
from app.trajectory.reader import ObservationReadError
from test_trajectory_acquisition import run_async
from test_trajectory_assembler import configured, prepared
from test_trajectory_reader import TrackingStream

DOCUMENT = b"synthetic public API contract"


def digest(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def approval(contract):
    return ReaderApproval(contract.digest, reader_artifact_digest(), digest(DOCUMENT))


def factory(contract, handler, *, records=None):
    return QualifiedReaderFactory(server_url="http://127.0.0.1:3000", public_key="synthetic-public",
        secret_key="synthetic-secret", approvals=(approval(contract),) if records is None else records,
        transport_factory=lambda: httpx.MockTransport(handler))


@run_async
async def test_approved_factory_probes_fixed_document_before_observation_io_and_closes_reader():
    _, contract, scope = prepared()
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/openapi.yaml":
            assert request.method == "GET" and not request.url.query
            assert request.headers["accept-encoding"] == "identity"
            return httpx.Response(200, content=DOCUMENT)
        assert request.url.path == "/api/public/v2/observations"
        return httpx.Response(200, json={"data": [], "meta": {"cursor": None}})

    async with factory(contract, handler).open(contract) as reader:
        page = await reader.read_page(scope, cursor=None, page_size=2, timeout_seconds=1, max_bytes=1024)
        assert page.rows == () and not reader.is_closed
    assert reader.is_closed and len(requests) == 2


@run_async
@pytest.mark.parametrize("case", ["unapproved", "contract", "artifact"])
async def test_unapproved_contract_or_reader_artifact_has_zero_network_io(case):
    _, contract, _ = prepared()
    record = approval(contract)
    records = () if case == "unapproved" else (replace(record,
        **{("trajectory_contract_digest" if case == "contract" else "reader_artifact_digest"): "sha256:" + "0" * 64}),)
    requests = []
    controller = factory(contract, lambda request: requests.append(request), records=records)
    with pytest.raises(ObservationReadError, match="TRAJECTORY_SOURCE_UNQUALIFIED"):
        async with controller.open(contract):
            pytest.fail("unapproved source was opened")
    assert requests == []


@run_async
@pytest.mark.parametrize("case,reason", [
    ("drift", "API_CONTRACT_MISMATCH"), ("redirect", "REDIRECT_REJECTED"),
    ("encoding", "ENCODING_UNSUPPORTED"), ("declared-size", "API_DOCUMENT_LIMIT"),
])
async def test_api_contract_drift_and_unsafe_probe_fail_closed_without_observation_io(case, reason):
    _, contract, _ = prepared()
    requests = []

    def handler(request):
        requests.append(request)
        if case == "redirect":
            return httpx.Response(302, headers={"location": "https://example.invalid/untrusted"})
        if case == "encoding":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=TrackingStream([DOCUMENT]))
        if case == "declared-size":
            return httpx.Response(200, headers={"content-length": str(1048577)}, content=DOCUMENT)
        return httpx.Response(200, content=b"SYNTHETIC_SECRET_CHANGED_DOCUMENT")

    with pytest.raises(ObservationReadError) as error:
        async with factory(contract, handler).open(contract):
            pytest.fail("changed source was opened")
    assert error.value.reason_code == "TRAJECTORY_SOURCE_" + reason
    assert not error.value.retryable and "SYNTHETIC_SECRET" not in str(error.value)
    assert len(requests) == 1


@run_async
async def test_probe_stream_limit_stops_reading_and_closes_response():
    _, contract, _ = prepared()
    stream = TrackingStream([b"a" * 524288, b"b" * 524289, b"not read"])
    with pytest.raises(ObservationReadError, match="API_DOCUMENT_LIMIT"):
        async with factory(contract, lambda _: httpx.Response(200, stream=stream)).open(contract):
            pytest.fail("oversized source was opened")
    assert stream.closed and stream.visited == 2


@run_async
async def test_probe_timeout_is_finite_and_closes_response():
    _, contract, _ = prepared()
    contract = configured(contract, limits={"request_timeout_seconds": Decimal("0.05")})
    stream = TrackingStream(block=True)
    with pytest.raises(ObservationReadError, match="SOURCE_TIMEOUT"):
        async with factory(contract, lambda _: httpx.Response(200, stream=stream)).open(contract):
            pytest.fail("timed out source was opened")
    assert stream.closed


@run_async
async def test_cancel_during_probe_closes_response_and_releases_slot():
    _, contract, _ = prepared()
    contract = configured(contract, limits={"max_inflight_acquisitions": 1})
    stream = TrackingStream(block=True)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=stream) if len(requests) == 1 else httpx.Response(200, content=DOCUMENT)

    controller = factory(contract, handler)

    async def open_one():
        async with controller.open(contract) as reader:
            assert not reader.is_closed

    first = asyncio.create_task(open_one())
    await asyncio.wait_for(stream.started.wait(), 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert stream.closed
    await asyncio.wait_for(open_one(), 1)
    assert len(requests) == 2


@run_async
async def test_probe_concurrency_obeys_frozen_lower_limit():
    _, contract, _ = prepared()
    contract = configured(contract, limits={"max_inflight_acquisitions": 1})
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, content=DOCUMENT)

    controller = factory(contract, handler)

    async def open_one():
        async with controller.open(contract):
            pass

    first = asyncio.create_task(open_one())
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(open_one())
    await asyncio.sleep(0.01)
    assert len(requests) == 1
    release.set()
    await asyncio.gather(first, second)
    assert len(requests) == 2


@run_async
async def test_consumer_timeout_is_not_reclassified_as_probe_failure():
    _, contract, _ = prepared()
    with pytest.raises(TimeoutError, match="SYNTHETIC_CONSUMER_TIMEOUT"):
        async with factory(contract, lambda _: httpx.Response(200, content=DOCUMENT)).open(contract) as reader:
            raise TimeoutError("SYNTHETIC_CONSUMER_TIMEOUT")
    assert reader.is_closed


@run_async
@pytest.mark.parametrize("timeout", [True, 0, -1, 6, float("inf"), None])
async def test_direct_api_probe_rejects_invalid_timeout_without_io(timeout):
    from test_trajectory_reader import reader_for
    requests = []
    async with reader_for(lambda request: requests.append(request) or httpx.Response(200, content=DOCUMENT)) as reader:
        with pytest.raises(ValueError, match="TRAJECTORY_READER_LIMIT_INVALID"):
            await reader.verify_api_contract(frozenset({digest(DOCUMENT)}), timeout_seconds=timeout)
    assert requests == []


@run_async
@pytest.mark.parametrize("digests", [None, [], frozenset(), frozenset({"not-a-digest"}),
    frozenset({"sha256:" + str(value).zfill(64) for value in range(17)})])
async def test_api_probe_rejects_unbounded_or_malformed_approval_configuration_without_io(digests):
    from test_trajectory_reader import reader_for
    requests = []
    async with reader_for(lambda request: requests.append(request) or httpx.Response(200, content=DOCUMENT)) as reader:
        with pytest.raises(ValueError, match="TRAJECTORY_READER_APPROVAL_CONFIGURATION_INVALID"):
            await reader.verify_api_contract(digests, timeout_seconds=1)
    assert requests == []


@run_async
async def test_probe_queue_wait_consumes_the_same_request_budget():
    _, contract, _ = prepared()
    slow = configured(contract, limits={"max_inflight_acquisitions": 1,
        "request_timeout_seconds": Decimal("0.2")})
    fast = configured(contract, limits={"max_inflight_acquisitions": 1,
        "request_timeout_seconds": Decimal("0.02")})
    stream, requests = TrackingStream(block=True), []
    controller = factory(slow, lambda request: requests.append(request) or httpx.Response(200, stream=stream),
                         records=(approval(slow), approval(fast)))
    async def blocked_open():
        with pytest.raises(ObservationReadError, match="SOURCE_TIMEOUT"):
            async with controller.open(slow):
                pytest.fail("blocked source was opened")
    first = asyncio.create_task(blocked_open())
    await asyncio.wait_for(stream.started.wait(), 1)
    with pytest.raises(ObservationReadError, match="SOURCE_TIMEOUT"):
        async with controller.open(fast):
            pytest.fail("queue wait received a fresh request budget")
    assert len(requests) == 1
    await first
    assert stream.closed


@run_async
async def test_invalid_frozen_contract_does_not_expose_raw_values_in_serializer_warnings():
    import warnings

    _, contract, _ = prepared()
    controller = factory(contract, lambda _: pytest.fail("invalid contract caused network IO"))
    object.__setattr__(contract.limits, "max_steps", "SYNTHETIC_SECRET_INVALID_CONTRACT")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ObservationReadError, match="SOURCE_CONTRACT_ERROR"):
            async with controller.open(contract):
                pytest.fail("invalid contract was accepted")
    assert not caught, "serializer emitted raw invalid contract values"


class FailingCloseTransport(httpx.MockTransport):
    async def aclose(self):
        raise RuntimeError("SYNTHETIC_SECRET_CLOSE_ERROR")


@run_async
@pytest.mark.parametrize("consumer_error", [TimeoutError("SYNTHETIC_CONSUMER"), asyncio.CancelledError()])
async def test_close_failure_preserves_active_consumer_exception(consumer_error):
    _, contract, _ = prepared()
    controller = QualifiedReaderFactory(server_url="http://127.0.0.1:3000", public_key="synthetic-public",
        secret_key="synthetic-secret", approvals=(approval(contract),),
        transport_factory=lambda: FailingCloseTransport(lambda _: httpx.Response(200, content=DOCUMENT)))
    with pytest.raises(type(consumer_error)) as caught:
        async with controller.open(contract):
            raise consumer_error
    assert caught.value is consumer_error


@run_async
async def test_close_failure_after_normal_consumer_exit_is_finite_and_does_not_expose_transport_detail():
    _, contract, _ = prepared()
    controller = QualifiedReaderFactory(server_url="http://127.0.0.1:3000", public_key="synthetic-public",
        secret_key="synthetic-secret", approvals=(approval(contract),),
        transport_factory=lambda: FailingCloseTransport(lambda _: httpx.Response(200, content=DOCUMENT)))
    with pytest.raises(ObservationReadError, match="TRAJECTORY_SOURCE_TRANSPORT_ERROR") as caught:
        async with controller.open(contract):
            pass
    assert "SYNTHETIC_SECRET" not in str(caught.value)


class SingleUseTransport(httpx.MockTransport):
    closed = False

    async def handle_async_request(self, request):
        assert not self.closed, "a Reader reused another Reader's closed transport"
        return await super().handle_async_request(request)

    async def aclose(self):
        self.closed = True


@run_async
async def test_each_open_owns_an_independent_transport_lifetime():
    _, contract, _ = prepared()
    transports = []
    def create_transport():
        transport = SingleUseTransport(lambda _: httpx.Response(200, content=DOCUMENT))
        transports.append(transport)
        return transport
    controller = QualifiedReaderFactory(server_url="http://127.0.0.1:3000", public_key="synthetic-public",
        secret_key="synthetic-secret", approvals=(approval(contract),), transport_factory=create_transport)
    for _ in range(2):
        async with controller.open(contract) as reader:
            assert not reader.is_closed
    assert len(transports) == 2 and transports[0] is not transports[1] and all(t.closed for t in transports)


@run_async
async def test_rejected_client_configuration_closes_the_new_owned_transport():
    _, contract, _ = prepared()
    transports = []
    def create_transport():
        transport = SingleUseTransport(lambda _: pytest.fail("invalid configuration caused network IO"))
        transports.append(transport)
        return transport
    controller = QualifiedReaderFactory(server_url="http://external.invalid", public_key="synthetic-public",
        secret_key="synthetic-secret", approvals=(approval(contract),), transport_factory=create_transport)
    with pytest.raises(ValueError, match="TRAJECTORY_READER_CONFIGURATION_INVALID"):
        async with controller.open(contract):
            pytest.fail("untrusted HTTP configuration was opened")
    assert len(transports) == 1 and transports[0].closed
