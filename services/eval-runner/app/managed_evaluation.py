"""Launch 级外部 Score 采纳契约；不管理或执行 Langfuse Evaluator。"""

from __future__ import annotations

import copy
import math
import os
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .evaluator_binding import canonical_digest
from .quality_policy import QUALITY_POLICY_SCHEMA_VERSION, QualityPolicy, QualityRule, freeze_quality_policy


class ManagedObservationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: str


class ManagedScoreExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    data_type: str = Field(description="numeric / boolean / categorical / text；text 仅为可选证据")
    required: bool = Field(default=True, strict=True)
    rule_id: str
    evaluator_id: str | None = None
    project_id: str | None = None
    category_values: list[str] | None = None


class ManagedEvaluationContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    environment: str
    target: ManagedObservationTarget
    scores: list[ManagedScoreExpectation] = Field(min_length=1, max_length=50)


class ManagedEvaluationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_payload(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "recovery": "请修正所选 Langfuse Score 采纳条件和对应 QualityPolicy 后重新提交。",
        }


def _reject(code: str, message: str) -> None:
    raise ManagedEvaluationError(code, message)


def freeze_managed_evaluation(
    payload: dict[str, Any], *, environment: str, policy_rules: list[dict[str, Any]] | None
) -> tuple[dict[str, Any], QualityPolicy]:
    """仅冻结用户确认的身份/条件。外部 Rule 始终使用 Langfuse latest。"""
    contract = ManagedEvaluationContract.model_validate(copy.deepcopy(payload)).model_dump(exclude_none=True)
    project = contract["project_id"]
    if not project.strip() or project != project.strip():
        _reject("MANAGED_IDENTITY_REQUIRED", "必须提供非空且无首尾空白的 Langfuse project_id。")
    configured_project = os.getenv("LANGFUSE_PROJECT_ID")
    if configured_project and configured_project != project:
        _reject("MANAGED_PROJECT_MISMATCH", "所选 project 与服务端 LANGFUSE_PROJECT_ID 不一致。")
    tracing_environment = os.getenv("LANGFUSE_TRACING_ENVIRONMENT")
    if contract["environment"] != environment or (tracing_environment and tracing_environment != environment):
        _reject("MANAGED_ENVIRONMENT_MISMATCH", "Launch、采纳条件及已配置的 Observation environment 必须一致。")
    if contract["target"] != {"name": "remote-agent-http", "type": "TOOL"}:
        _reject("MANAGED_TARGET_UNSUPPORTED", "当前仅支持 remote-agent-http / TOOL，不能选择空 replay span。")
    seen = set()
    bindings = {}
    for score in contract["scores"]:
        name = score["name"]
        if not name.strip() or name != name.strip() or len(name) > 128:
            _reject("MANAGED_SCORE_NAME_INVALID", "Score 名称必须为 1–128 字符，不能包含首尾空白。")
        if name in seen:
            _reject("MANAGED_SCORE_DUPLICATE", f"Score 名称重复：{name}")
        seen.add(name)
        data_type = score["data_type"]
        if data_type not in ("numeric", "boolean", "categorical", "text"):
            _reject("MANAGED_SCORE_TYPE_UNSUPPORTED", f"不支持 Score 类型：{data_type}")
        if data_type == "text" and score["required"]:
            _reject("MANAGED_REQUIRED_TYPE_UNSUPPORTED", "text 只能作为可选证据，不能要求其决定质量。")
        if score.get("project_id", project) != project:
            _reject("MANAGED_PROJECT_MISMATCH", f"Score {name} 与契约 project 不一致。")
        if not score["rule_id"].strip() or score["rule_id"] != score["rule_id"].strip():
            _reject("MANAGED_RULE_REFERENCE_REQUIRED", f"Score {name} 必须显式引用已有 Rule ID。")
        evaluator_ref = score.get("evaluator_id")
        if evaluator_ref is not None and (not evaluator_ref.strip() or evaluator_ref != evaluator_ref.strip()):
            _reject("MANAGED_REFERENCE_INVALID", f"Score {name} 的 Evaluator ID 不能为空或包含首尾空白。")
        categories = score.get("category_values")
        if data_type == "categorical" and (
            not categories
            or any(not v.strip() or v != v.strip() for v in categories)
            or len(set(categories)) != len(categories)
        ):
            _reject("MANAGED_CATEGORIES_INVALID", f"Score {name} 必须提供非空、不重复的分类取值。")
        if data_type != "text":
            # 仅适配既有 QualityPolicy 验证接口，不创建外部 Evaluator Binding/Registry。
            bindings[name] = SimpleNamespace(result_type=data_type, category_values=categories)
    if not policy_rules:
        _reject("MANAGED_QUALITY_POLICY_REQUIRED", "Managed 模式必须显式提供 QualityPolicy。")
    score_by_name = {score["name"]: score for score in contract["scores"]}
    for rule in policy_rules:
        threshold = rule.get("threshold")
        if (
            rule.get("result_type", "numeric") == "numeric"
            and threshold is not None
            and (isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold))
        ):
            # JSON 字符串 NaN/Infinity 可被请求模型解析为非有限浮点；错误响应本身也必须可序列化。
            _reject("QUALITY_POLICY_THRESHOLD_INVALID", "阈值必须是有限数值，不能是 NaN 或 Infinity。")
        score = score_by_name.get(rule["evaluator_id"])
        if score and (
            rule.get("result_type", "numeric") != score["data_type"] or rule.get("required", True) != score["required"]
        ):
            _reject("MANAGED_POLICY_MISMATCH", "QualityPolicy 类型及 required 必须与所选 Score 一致。")
    if any(
        score["required"] and score["name"] not in {r["evaluator_id"] for r in policy_rules}
        for score in contract["scores"]
    ):
        _reject("MANAGED_POLICY_MISMATCH", "每项必选 Score 必须具有对应 QualityPolicy 规则。")
    policy = freeze_quality_policy(
        policy_id="langfuse-managed",
        version=QUALITY_POLICY_SCHEMA_VERSION,
        rules=[QualityRule.from_payload(r) for r in policy_rules],
        bindings=bindings,
    )
    contract.update(schema_version="1.0", source="EVAL", execution_semantics="langfuse_latest")
    contract["acceptance_digest"] = canonical_digest(contract)
    return contract, policy


def evaluation_source(manifest: dict[str, Any] | None) -> str:
    """历史 Manifest 不回填；仅在读取时解释为原有 Builtin 模式。"""
    return (manifest or {}).get("evaluation_source", "builtin")
