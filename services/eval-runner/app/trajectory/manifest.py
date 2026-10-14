"""Manifest 1.3 的冻结轨迹合同；序列化有效不代表来源已通过运行准入。"""

from types import SimpleNamespace

from .codec import canonical_dumps, canonical_loads
from .contract import TrajectoryContract

TRAJECTORY_MANIFEST_SCHEMA_VERSION = "1.3"
_LEGACY_SCHEMAS = {None, "", "1.0", "1.1", "1.2"}
_CONTRACT_KEYS = {"contract_json", "trajectory_contract_digest"}
_CAPSULE_KEYS = _CONTRACT_KEYS | {"selected_source_profile_id", "selected_source_profile_version"}


class TrajectoryManifestError(ValueError):
    def __init__(self, code):
        self.reason_code = "TRAJECTORY_MANIFEST_" + code
        super().__init__(self.reason_code)


def freeze_trajectory_contract(contract):
    """先冻结为只含字符串的独立载荷，精确保存 Decimal 并防调用方后续变更。"""
    if type(contract) is not TrajectoryContract:
        raise TrajectoryManifestError("CONTRACT_INVALID")
    try:
        checked = TrajectoryContract.model_validate(contract.model_dump(warnings=False))
        return {"contract_json": canonical_dumps(checked), "trajectory_contract_digest": checked.digest}
    except ValueError:
        raise TrajectoryManifestError("CONTRACT_INVALID") from None


def parse_launch_contract(value):
    try:
        contract = TrajectoryContract.model_validate(canonical_loads(value))
        freeze_trajectory_contract(contract)
        return contract
    except (ValueError, TypeError):
        raise TrajectoryManifestError("CONTRACT_INVALID") from None


def _read_contract(payload):
    try:
        if type(payload) is not dict or type(payload.get("contract_json")) is not str:
            raise ValueError
        contract = TrajectoryContract.model_validate(canonical_loads(payload["contract_json"]))
        if canonical_dumps(contract) != payload["contract_json"]:
            raise ValueError
    except ValueError:
        raise TrajectoryManifestError("CONTRACT_INVALID") from None
    if payload.get("trajectory_contract_digest") != contract.digest:
        raise TrajectoryManifestError("DIGEST_MISMATCH")
    return contract


def bind_trajectory_manifest(frozen, version):
    """在 Launch 冻结边界从不可变 AgentVersion 声明选分支；分支不进入合同摘要。"""
    if type(frozen) is not dict or set(frozen) != _CONTRACT_KEYS:
        raise TrajectoryManifestError("CONTRACT_INVALID")
    contract = _read_contract(frozen)
    try:
        bound = contract.bind_source_version(version)
        profile = bound.source_profile
    except (ValueError, AttributeError):
        raise TrajectoryManifestError("SOURCE_MISMATCH") from None
    return {**frozen, "selected_source_profile_id": profile.profile_id,
        "selected_source_profile_version": profile.version}


def load_trajectory_contract(manifest):
    """仅读冻结载荷；不查在线 Trace、不用当前注册表回填历史配置。"""
    if manifest is None:
        return None
    if type(manifest) is not dict:
        raise TrajectoryManifestError("CONTRACT_INVALID")
    schema = manifest.get("schema_version")
    if type(schema) not in {str, type(None)}:
        raise TrajectoryManifestError("SCHEMA_UNSUPPORTED")
    if schema in _LEGACY_SCHEMAS:
        if "trajectory" in manifest:
            raise TrajectoryManifestError("SCHEMA_MISMATCH")
        return None
    if schema != TRAJECTORY_MANIFEST_SCHEMA_VERSION:
        raise TrajectoryManifestError("SCHEMA_UNSUPPORTED")
    payload = manifest.get("trajectory")
    if type(payload) is not dict or set(payload) != _CAPSULE_KEYS:
        raise TrajectoryManifestError("CONTRACT_INVALID")
    contract = _read_contract(payload)
    agent = manifest.get("agent")
    if (type(agent) is not dict or type(agent.get("trajectory_source_profile_id")) is not str
        or agent["trajectory_source_profile_id"] != payload["selected_source_profile_id"]):
        raise TrajectoryManifestError("SOURCE_MISMATCH")
    # 该声明仅来自冻结 agent 区块；真正采集/制品写入仍与实际 DB AgentVersion 复核。
    frozen = {key: payload[key] for key in _CONTRACT_KEYS}
    selected = bind_trajectory_manifest(frozen, SimpleNamespace(
        trajectory_source_profile_id=agent["trajectory_source_profile_id"]))
    if selected["selected_source_profile_version"] != payload["selected_source_profile_version"]:
        raise TrajectoryManifestError("SOURCE_MISMATCH")
    return contract.bind_source_version(SimpleNamespace(
        trajectory_source_profile_id=agent["trajectory_source_profile_id"]))
