"""Worker 成功响应后的采集边界；不调用 Agent、不评判质量或启用 Snapshot 资格。"""

import os
import re
from contextlib import AsyncExitStack

from sqlalchemy import select

from ..config import sanitize_langfuse_credential_input, sanitize_langfuse_url_input
from ..db_models import AgentVersionRecord, ExecutionCheckpointRecord, ExperimentLaunchRecord
from ..evaluator_binding import canonical_digest
from .checkpoint import TrajectoryCheckpointError
from .codec import canonical_loads
from .coordinator import TrajectoryCoordinator, TrajectoryCoordinatorError
from .factory import QualifiedReaderFactory, ReaderApproval
from .manifest import TrajectoryManifestError, load_trajectory_contract
from .reader import ObservationReadError
from .schema import TrajectoryScope


class WorkerTrajectoryError(RuntimeError):
    def __init__(self, code, *, abandoned=False):
        self.reason_code = "TRAJECTORY_WORKER_" + code
        self.abandoned = abandoned
        super().__init__(self.reason_code)


def parse_reader_approvals(encoded):
    """只有部署方配置可提供批准；有限 JSON、精确字段及摘要，不读取任意文件。"""
    if encoded is None or encoded == "":
        return ()
    keys = {"trajectory_contract_digest", "reader_artifact_digest", "api_document_digest"}
    try:
        if type(encoded) is not str or len(encoded.encode()) > 16384:
            raise ValueError
        rows = canonical_loads(encoded, max_bytes=16384)
        if type(rows) is not list or len(rows) > 16 or any(type(row) is not dict or set(row) != keys
            or any(type(value) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None
                   for value in row.values()) for row in rows):
            raise ValueError
        return tuple(ReaderApproval(**row) for row in rows)
    except (ValueError, UnicodeError):
        raise ValueError("TRAJECTORY_READER_APPROVAL_CONFIGURATION_INVALID") from None


def _factory_from_environment():
    return QualifiedReaderFactory(
        server_url=sanitize_langfuse_url_input(os.getenv("LANGFUSE_BASE_URL", "http://langfuse-web:3000")),
        public_key=sanitize_langfuse_credential_input(os.getenv("LANGFUSE_PUBLIC_KEY")) or "",
        secret_key=sanitize_langfuse_credential_input(os.getenv("LANGFUSE_SECRET_KEY")) or "",
        approvals=parse_reader_approvals(os.getenv("ARGUS_TRAJECTORY_READER_APPROVALS")),
        trusted_http_hosts=frozenset({"langfuse-web"}),
    )


class _QualifiedReader:
    def __init__(self, stack, contract, factory):
        self._stack, self._contract, self._factory = stack, contract, factory
        self._reader = None

    async def read_page(self, scope, **limits):
        if self._reader is None:
            try:
                self._reader = await self._stack.enter_async_context(self._factory().open(self._contract))
            except ValueError:
                raise ObservationReadError("TRAJECTORY_SOURCE_CONFIGURATION_INVALID", retryable=False) from None
        return await self._reader.read_page(scope, **limits)


class WorkerTrajectoryCollector:
    """每个 Worker 注入一个实例；批准与连接配置首次 1.3 消费时冻结，旧版零访问。"""

    def __init__(self, session_factory, *, factory_provider=None):
        self._session_factory = session_factory
        self._factory_provider = factory_provider or _factory_from_environment
        self._factory = None

    def _get_factory(self):
        if self._factory is None:
            configured = self._factory_provider()
            if type(configured) is not QualifiedReaderFactory:
                raise ValueError("TRAJECTORY_READER_CONFIGURATION_INVALID")
            self._factory = configured
        return self._factory

    def _load_scope(self, session, manifest, item_id, generation, contract):
        checkpoint = session.scalar(select(ExecutionCheckpointRecord).where(
            ExecutionCheckpointRecord.item_execution_id == item_id,
            ExecutionCheckpointRecord.dispatch_generation == generation))
        if checkpoint is None or checkpoint.manifest_digest != canonical_digest(manifest):
            raise TrajectoryCheckpointError("SCOPE_UNAVAILABLE")
        try:
            scope = TrajectoryScope(launch_id=checkpoint.launch_id, item_execution_id=checkpoint.item_execution_id,
                dataset_item_id=checkpoint.dataset_item_id, attempt_id=checkpoint.final_attempt_id,
                dispatch_generation=checkpoint.dispatch_generation, trace_id=checkpoint.trace_id,
                root_observation_id=checkpoint.observation_id)
        except ValueError:
            raise TrajectoryCheckpointError("SCOPE_UNAVAILABLE") from None
        launch = session.get(ExperimentLaunchRecord, checkpoint.launch_id)
        version = session.get(AgentVersionRecord, launch.agent_version_id) if launch else None
        try:
            bound = contract.bind_source_version(version)
            if bound.source_profile != contract.source_profile:
                raise ValueError
        except (ValueError, AttributeError):
            raise TrajectoryCheckpointError("SOURCE_MISMATCH") from None
        return scope, bound

    async def collect(self, manifest, *, item_id, generation, lease_token, cancelled=None):
        try:
            contract = load_trajectory_contract(manifest)
        except TrajectoryManifestError:
            raise WorkerTrajectoryError("CONTRACT_INVALID") from None
        if contract is None:
            return None
        controller = TrajectoryCoordinator(self._session_factory)
        try:
            scope, contract = await controller._transaction(contract, lambda session: self._load_scope(
                session, manifest, item_id, generation, contract))
            # Lazy Reader 直到 Coordinator 完成原时间/租约/CAS 预留才访问配置及网络。
            async with AsyncExitStack() as stack:
                reader = _QualifiedReader(stack, contract, self._get_factory)
                return await controller.run(scope, contract, client=reader, lease_token=lease_token, cancelled=cancelled)
        except TrajectoryCheckpointError as error:
            code = error.reason_code.removeprefix("TRAJECTORY_CHECKPOINT_")
            raise WorkerTrajectoryError(code, abandoned=code in {"LEASE_LOST", "GENERATION_STALE", "CANCELLED"}) from None
        except TrajectoryCoordinatorError:
            raise WorkerTrajectoryError("STORAGE_UNAVAILABLE") from None
        except ObservationReadError:
            raise WorkerTrajectoryError("SOURCE_CLEANUP_UNAVAILABLE") from None
