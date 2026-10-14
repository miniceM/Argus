"""Runner 可信配置的 Reader 准入；批准记录不能来自 Trace 或公开请求。"""

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from .acquisition import AcquisitionLimiter
from .contract import TrajectoryContract
from .evidence import _contract
from .reader import LangfuseObservationReader, ObservationReadError


@dataclass(frozen=True)
class ReaderApproval:
    trajectory_contract_digest: str
    reader_artifact_digest: str
    api_document_digest: str


def reader_artifact_digest() -> str:
    """固定 Reader 与 wire codec 的实现身份；源码不可读即拒绝，不猜版本。"""
    digest = hashlib.sha256()
    try:
        for name in ("reader.py", "codec.py"):
            source = Path(__file__).with_name(name).read_bytes()
            digest.update(name.encode() + b"\0" + len(source).to_bytes(8, "big") + source)
    except OSError:
        raise ObservationReadError("TRAJECTORY_SOURCE_READER_ARTIFACT_UNAVAILABLE", retryable=False) from None
    return "sha256:" + digest.hexdigest()


class QualifiedReaderFactory:
    """依赖注入边界：部署方对实际 Agent/来源校准后批准完整合同，不自动批准。"""

    def __init__(self, *, server_url: str, public_key: str, secret_key: str,
                 approvals: tuple[ReaderApproval, ...] = (),
                 transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
                 trusted_http_hosts: frozenset[str] = frozenset()):
        if (type(approvals) is not tuple or len(approvals) > 16 or any(
            type(record) is not ReaderApproval or any(type(value) is not str
                or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None for value in (
                    record.trajectory_contract_digest, record.reader_artifact_digest, record.api_document_digest))
            for record in approvals)):
            raise ValueError("TRAJECTORY_READER_APPROVAL_CONFIGURATION_INVALID")
        if transport_factory is not None and not callable(transport_factory):
            raise ValueError("TRAJECTORY_READER_CONFIGURATION_INVALID")
        self._approvals = frozenset(approvals)
        self._configuration = dict(server_url=server_url, public_key=public_key, secret_key=secret_key,
            trusted_http_hosts=trusted_http_hosts)
        self._transport_factory = transport_factory
        self._limiter = AcquisitionLimiter()

    @asynccontextmanager
    async def open(self, contract: TrajectoryContract) -> AsyncIterator[LangfuseObservationReader]:
        try:
            frozen, _profile = _contract(contract)
        except ValueError:
            raise ObservationReadError("TRAJECTORY_SOURCE_CONTRACT_ERROR", retryable=False) from None
        artifact, contract_digest = reader_artifact_digest(), frozen.digest
        api_digests = frozenset(record.api_document_digest for record in self._approvals
            if record.trajectory_contract_digest == contract_digest and record.reader_artifact_digest == artifact)
        if not api_digests:
            raise ObservationReadError("TRAJECTORY_SOURCE_UNQUALIFIED", retryable=False)
        reader, transport, failed = None, None, False
        try:
            # 队列等待也计入原单请求预算；probe释放槽后才交给采集，避免嵌套占槽。
            timeout = float(frozen.limits.request_timeout_seconds)
            try:
                async with asyncio.timeout(timeout):
                    async with self._limiter.slot(frozen.limits.max_inflight_acquisitions):
                        transport = self._transport_factory() if self._transport_factory is not None else None
                        reader = LangfuseObservationReader(**self._configuration, transport=transport)
                        await reader.verify_api_contract(api_digests, timeout_seconds=timeout)
            except TimeoutError:
                raise ObservationReadError("TRAJECTORY_SOURCE_TIMEOUT", retryable=True) from None
            yield reader
        except BaseException:
            failed = True
            raise
        finally:
            resource = reader if reader is not None else transport
            if resource is not None:
                try:
                    await resource.aclose()
                except BaseException as error:
                    # 已有消费/探测异常（含取消）保持原样；正常退出的清理故障才转换。
                    if not failed:
                        if not isinstance(error, Exception):
                            raise
                        raise ObservationReadError("TRAJECTORY_SOURCE_TRANSPORT_ERROR", retryable=True) from None
