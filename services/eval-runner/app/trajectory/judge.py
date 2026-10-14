"""冻结 Judge 输出与独立留出集统计；统计合格不代替 Provider/制品准入。"""

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from math import sqrt
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .codec import canonical_digest, canonical_loads
from .contract import RedactionPolicy
from .schema import Capability, Identity, _ContractModel

Probability = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class JudgeCriteriaPolicy(_ContractModel):
    version: Identity
    paths: Annotated[list[str], Field(max_length=64)]
    string_policies: dict[str, Literal["identifier", "currency"]] = Field(max_length=64)
    content_policy_version: Literal["pii-v1"]

    @model_validator(mode="after")
    def approved_projection(self):
        if len(set(self.paths)) != len(self.paths):
            raise ValueError("JUDGE_CRITERIA_POLICY_INVALID")
        RedactionPolicy(version=self.version, input_paths=[], output_paths=self.paths, argument_paths=[], result_paths=[],
            content_policy_version=self.content_policy_version, string_policies=self.string_policies)
        return self

    @property
    def digest(self):
        return canonical_digest(JudgeCriteriaPolicy.model_validate(self.model_dump()))


class JudgeOutputPolicy(_ContractModel):
    min_confidence: Probability
    max_rationale_bytes: Annotated[int, Field(ge=0, le=4096)]
    max_evidence_refs: Annotated[int, Field(ge=1, le=1024)]
    allow_empty_evidence: bool
    reason_codes: Annotated[list[Identity], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def unique_reasons(self):
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("JUDGE_REASON_CODES_INVALID")
        return self


class JudgeOutput(_ContractModel):
    schema_version: Literal["judge-output-v1"]
    verdict: Literal["PASS", "FAIL", "UNCERTAIN"]
    confidence: Probability
    reason_code: Identity
    evidence_step_ids: Annotated[list[Identity], Field(max_length=1024)]
    rationale: Annotated[str, Field(max_length=4096)]

    @field_validator("confidence", mode="before")
    @classmethod
    def json_probability(cls, value):
        # codec 的 JSON 小数为 Decimal；整数 0/1 是数值，bool/string 不是。
        if type(value) is int:
            return Decimal(value)
        if type(value) is not Decimal:
            raise ValueError("JUDGE_CONFIDENCE_INVALID")
        return value


@dataclass(frozen=True)
class ParsedJudgeResult:
    status: Literal["succeeded", "no_result", "failed"]
    value: Literal["PASS", "FAIL"] | None
    reason_code: str
    evidence_step_ids: tuple[str, ...] = ()
    confidence: Decimal | None = None


def parse_judge_output(raw: str, *, policy: JudgeOutputPolicy, local_step_ids: frozenset[str]) -> ParsedJudgeResult:
    """只接收 Runner 已验证当前 Item/Attempt 后的 Step 集，不相信 Provider provenance。"""
    policy = JudgeOutputPolicy.model_validate(policy.model_dump())
    if type(local_step_ids) is not frozenset or any(type(step) is not str or not step for step in local_step_ids):
        raise ValueError("JUDGE_LOCAL_SCOPE_INVALID")
    try:
        value = JudgeOutput.model_validate(canonical_loads(raw, max_bytes=128 * 1024))
        if value.reason_code not in policy.reason_codes or len(value.rationale.encode("utf-8")) > policy.max_rationale_bytes:
            raise ValueError
    except (ValueError, UnicodeError):
        # 不把原始响应、Pydantic input 或 Provider 任意错误文本带到日志/Score。
        return ParsedJudgeResult("failed", None, "JUDGE_OUTPUT_INVALID")
    refs = tuple(value.evidence_step_ids)
    if (len(refs) > policy.max_evidence_refs or len(set(refs)) != len(refs)
        or not set(refs) <= local_step_ids or (not refs and not policy.allow_empty_evidence)):
        return ParsedJudgeResult("no_result", None, "JUDGE_EVIDENCE_REF_INVALID")
    if value.verdict == "UNCERTAIN":
        return ParsedJudgeResult("no_result", None, "JUDGE_UNCERTAIN", refs, value.confidence)
    if value.confidence < policy.min_confidence:
        return ParsedJudgeResult("no_result", None, "JUDGE_LOW_CONFIDENCE", refs, value.confidence)
    # 首版不保存不能证明安全的自由文本；仅保留枚举原因和本地证据引用。
    return ParsedJudgeResult("succeeded", value.verdict, value.reason_code, refs, value.confidence)


class CalibrationLabel(_ContractModel):
    case_id: Identity
    near_duplicate_group: Identity
    trajectory_digest: Digest
    risk_id: Identity
    truth: Literal["PASS", "FAIL"]


class CalibrationObservation(_ContractModel):
    case_id: Identity
    run_index: Annotated[int, Field(ge=0, le=19)]
    verdict: Literal["PASS", "FAIL", "UNKNOWN"]


class CalibrationPolicy(_ContractModel):
    version: Identity
    risk_ids: Annotated[list[Identity], Field(min_length=1, max_length=64)]
    holdout_digest: Digest
    max_false_accept_rate: Probability
    min_samples_per_critical_slice: Annotated[int, Field(ge=1, le=100000)]
    confidence_level: Decimal
    max_disagreement_rate: Probability
    repeat_runs: Annotated[int, Field(ge=2, le=20)]

    @model_validator(mode="after")
    def supported_policy(self):
        if self.confidence_level != Decimal("0.95") or len(set(self.risk_ids)) != len(self.risk_ids):
            raise ValueError("JUDGE_CALIBRATION_POLICY_INVALID")
        return self


class JudgeProviderOptions(_ContractModel):
    """有限 Provider 参数；不能通过任意 extras 改写已冻结 transport。"""

    profile: Literal["openai-compatible-v1", "omlx-0.7-v1"]
    seed: Annotated[int, Field(ge=0, le=4294967295)]
    enable_thinking: bool | None

    @model_validator(mode="after")
    def approved_options(self):
        if (self.profile == "omlx-0.7-v1" and self.enable_thinking is None
            or self.profile == "openai-compatible-v1" and self.enable_thinking is not None):
            raise ValueError("JUDGE_PROVIDER_PROFILE_INVALID")
        return self


class FrozenJudgeDefinition(_ContractModel):
    """声明身份与冻结预算；Provider 必须另外证明实际模型/能力，Schema 不构成准入。"""

    judge_kind: Literal["task_completion", "plan_reasonableness", "wasted_stage", "complex_policy"]
    risk_id: Identity
    version: Identity
    provider_id: Identity
    provider_api_version: Identity
    resolved_model_id: Identity
    resolved_model_artifact_digest: Digest
    prompt_text: Annotated[str, Field(min_length=1, max_length=4096)]
    prompt_digest: Digest
    rubric_text: Annotated[str, Field(min_length=1, max_length=4096)]
    rubric_digest: Digest
    implementation_ref: Identity
    implementation_artifact_digest: Digest
    dependency_lock_digest: Digest
    provider_options: JudgeProviderOptions | None = None
    temperature: Annotated[Decimal, Field(ge=0, le=2, allow_inf_nan=False)]
    top_p: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
    max_output_tokens: Annotated[int, Field(ge=1, le=16384)]
    request_timeout_seconds: Annotated[Decimal, Field(gt=0, le=300, allow_inf_nan=False)]
    max_attempts: Literal[1]
    max_steps: Annotated[int, Field(ge=1, le=1024)]
    max_input_bytes: Annotated[int, Field(ge=1, le=2 * 1024 * 1024)]
    max_total_calls: Annotated[int, Field(ge=1, le=100000)]
    max_total_input_tokens: Annotated[int, Field(ge=1, le=1000000000)]
    max_total_output_tokens: Annotated[int, Field(ge=1, le=1000000000)]
    mode: Literal["diagnostic", "release"]
    eligible_when: Literal["DETERMINISTIC_FAIL_OR_UNKNOWN"]
    trigger_confidence_threshold: Probability
    sample_rate: Probability
    sampling_seed: Identity
    required_case_policy: Literal["ALL_DATASET_CASES"]
    required_case_ids_digest: Digest
    calibration_set_id: Identity
    calibration_set_version: Identity
    calibration_set_digest: Digest
    calibration_policy: CalibrationPolicy
    output_policy: JudgeOutputPolicy
    criteria_policy: JudgeCriteriaPolicy
    required_capabilities: Annotated[list[Capability], Field(max_length=6)]

    @field_validator("max_attempts", mode="before")
    @classmethod
    def single_integer_attempt(cls, value):
        if type(value) is not int:
            raise ValueError("JUDGE_ATTEMPTS_INVALID")
        return value

    @model_validator(mode="after")
    def verified_texts_and_risk(self):
        if (self.provider_options is not None and self.provider_options.profile == "omlx-0.7-v1"
            and self.provider_api_version != "0.7.0"):
            raise ValueError("JUDGE_PROVIDER_PROFILE_INVALID")
        for text, digest in ((self.prompt_text, self.prompt_digest), (self.rubric_text, self.rubric_digest)):
            if len(text.encode("utf-8")) > 4096 or "sha256:" + sha256(text.encode("utf-8")).hexdigest() != digest:
                raise ValueError("JUDGE_INSTRUCTION_DIGEST_INVALID")
        model = self.resolved_model_id.casefold()
        if model in {"latest", "default", "auto", "alias"} or model.endswith((":latest", "@latest", "/latest")):
            raise ValueError("JUDGE_MODEL_IDENTITY_UNRESOLVED")
        if self.risk_id not in self.calibration_policy.risk_ids:
            raise ValueError("JUDGE_CALIBRATION_RISK_MISMATCH")
        # 能力按冻结 rubric/输入合同声明；例如浪费动作不必是耗时判断。
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("JUDGE_CAPABILITY_CONTRACT_INVALID")
        return self

    @property
    def definition_digest(self) -> str:
        # 输出 Schema 与策略一并固定，修改解析契约必须创建新 Definition。
        value = FrozenJudgeDefinition.model_validate(self.model_dump())
        return canonical_digest({"definition_schema_version": "trajectory-judge-definition-v1",
            "configuration": value.model_dump(), "output_schema": judge_output_schema(value.output_policy)})


def judge_output_schema(policy: JudgeOutputPolicy) -> dict:
    policy = JudgeOutputPolicy.model_validate(policy.model_dump())
    # Decimal 的通用 Pydantic Schema 还允许字符串；Provider 合同明确只允许 JSON number。
    schema = canonical_loads(json.dumps(JudgeOutput.model_json_schema(), allow_nan=False))
    schema["properties"]["confidence"] = {"type": "number", "minimum": 0, "maximum": 1}
    schema["properties"]["reason_code"]["enum"] = list(policy.reason_codes)
    refs = schema["properties"]["evidence_step_ids"]
    refs.update(minItems=0 if policy.allow_empty_evidence else 1, maxItems=policy.max_evidence_refs)
    return schema


def judge_should_run(definition: FrozenJudgeDefinition, *, case_id: str, deterministic_verdicts: list[str],
    deterministic_confidences: list[Decimal] | None = None) -> bool:
    definition = FrozenJudgeDefinition.model_validate(definition.model_dump())
    if (type(case_id) is not str or not case_id or type(deterministic_verdicts) is not list
        or any(type(value) is not str or value not in {"PASS", "FAIL", "UNKNOWN"} for value in deterministic_verdicts)):
        raise ValueError("JUDGE_TRIGGER_INPUT_INVALID")
    if deterministic_confidences is not None and (type(deterministic_confidences) is not list
        or len(deterministic_confidences) != len(deterministic_verdicts)
        or any(type(value) is not Decimal or not value.is_finite() or not 0 <= value <= 1 for value in deterministic_confidences)):
        raise ValueError("JUDGE_TRIGGER_INPUT_INVALID")
    if definition.mode == "release":
        return True  # required cohort 校验是启动前置条件，不应用诊断触发/采样。
    low_confidence = deterministic_confidences is not None and any(value < definition.trigger_confidence_threshold
        for value in deterministic_confidences)
    if not low_confidence and not any(value in {"FAIL", "UNKNOWN"} for value in deterministic_verdicts):
        return False
    # canonical 分隔防止 seed/Case 拼接碰撞；同一冻结配置重跑得到相同抽样。
    digest = canonical_digest([definition.sampling_seed, case_id, definition.definition_digest])
    numerator = int(digest.removeprefix("sha256:"), 16)
    rate_numerator, rate_denominator = definition.sample_rate.as_integer_ratio()
    return numerator * rate_denominator < rate_numerator * 2 ** 256


def validate_required_judge_coverage(definition: FrozenJudgeDefinition, *, dataset_case_ids: list[str]) -> None:
    definition = FrozenJudgeDefinition.model_validate(definition.model_dump())
    if (type(dataset_case_ids) is not list or not dataset_case_ids
        or any(type(value) is not str or not value for value in dataset_case_ids)
        or len(set(dataset_case_ids)) != len(dataset_case_ids)
        or canonical_digest(sorted(dataset_case_ids)) != definition.required_case_ids_digest):
        raise ValueError("JUDGE_REQUIRED_COHORT_INVALID")


@dataclass(frozen=True)
class CalibrationSlice:
    risk_id: str
    sample_count: int
    violation_count: int
    false_accept_counts: tuple[int, ...]
    false_accept_rates: tuple[float | None, ...]
    false_accept_uppers: tuple[float | None, ...]
    false_reject_counts: tuple[int, ...]
    max_false_accept_upper: float | None
    disagreement_rate: float
    eligible: bool


@dataclass(frozen=True)
class CalibrationReport:
    """eligible 仅表示统计合格，执行准入还必须核验冻结身份、真实来源与制品。"""

    eligible: bool
    reason_code: str
    policy_version: str
    holdout_digest: str
    input_coverage: float
    slices: tuple[CalibrationSlice, ...] = ()
    disagreement_rate: float | None = None


def wilson_upper(false_accepts: int, violations: int) -> float:
    if type(false_accepts) is not int or type(violations) is not int or not 0 <= false_accepts <= violations or violations <= 0:
        raise ValueError("JUDGE_CALIBRATION_COUNTS_INVALID")
    z = 1.644854
    p = false_accepts / violations
    return (p + z * z / (2 * violations) + z * sqrt(p * (1 - p) / violations + z * z / (4 * violations * violations))) / (
        1 + z * z / violations)


def calibration_labels_digest(labels: list[CalibrationLabel]) -> str:
    rows = [CalibrationLabel.model_validate(row.model_dump()).model_dump() for row in labels]
    return canonical_digest(sorted(rows, key=lambda row: row["case_id"]))


def evaluate_calibration(policy: CalibrationPolicy, *, development: list[CalibrationLabel],
    holdout: list[CalibrationLabel], observations: list[CalibrationObservation]) -> CalibrationReport:
    """每个风险、每次重复独立计算；输入不全/分期泄漏均禁止准入，不池化重复。"""
    policy = CalibrationPolicy.model_validate(policy.model_dump())
    development = [CalibrationLabel.model_validate(row.model_dump()) for row in development]
    holdout = [CalibrationLabel.model_validate(row.model_dump()) for row in holdout]
    observations = [CalibrationObservation.model_validate(row.model_dump()) for row in observations]
    ids = {row.case_id for row in holdout}
    groups = {row.near_duplicate_group for row in holdout}
    # 近重复的留出样本不能当作 Wilson 的独立样本。开发集内部可用于调优。
    if (not holdout or len(ids) != len(holdout) or len(groups) != len(holdout)
        or len({row.trajectory_digest for row in holdout}) != len(holdout)
        or ids & {row.case_id for row in development} or groups & {row.near_duplicate_group for row in development}
        or {row.trajectory_digest for row in holdout} & {row.trajectory_digest for row in development}
        or calibration_labels_digest(holdout) != policy.holdout_digest
        or {row.risk_id for row in holdout} != set(policy.risk_ids)):
        return CalibrationReport(False, "JUDGE_CALIBRATION_SPLIT_INVALID", policy.version, policy.holdout_digest, 0)
    expected_keys = {(row.case_id, run) for row in holdout for run in range(policy.repeat_runs)}
    actual_keys = {(row.case_id, row.run_index) for row in observations}
    covered_keys = {(row.case_id, row.run_index) for row in observations if row.verdict != "UNKNOWN"} & expected_keys
    input_coverage = len(covered_keys) / len(expected_keys)
    if actual_keys != expected_keys or len(actual_keys) != len(observations) or any(row.verdict == "UNKNOWN" for row in observations):
        return CalibrationReport(False, "JUDGE_CALIBRATION_COVERAGE_INCOMPLETE", policy.version, policy.holdout_digest, input_coverage)
    lookup = {(row.case_id, row.run_index): row.verdict for row in observations}
    disagreements = {row.case_id for row in holdout if len({lookup[row.case_id, run] for run in range(policy.repeat_runs)}) > 1}
    slices = []
    for risk_id in policy.risk_ids:
        rows = [row for row in holdout if row.risk_id == risk_id]
        n = sum(row.truth == "FAIL" for row in rows)
        fa = tuple(sum(row.truth == "FAIL" and lookup[row.case_id, run] == "PASS" for row in rows) for run in range(policy.repeat_runs))
        fr = tuple(sum(row.truth == "PASS" and lookup[row.case_id, run] == "FAIL" for row in rows) for run in range(policy.repeat_runs))
        uppers = tuple(wilson_upper(count, n) for count in fa) if n else (None,) * policy.repeat_runs
        upper = max(uppers) if n else None
        disagreement = sum(row.case_id in disagreements for row in rows) / len(rows)
        eligible = (n >= policy.min_samples_per_critical_slice and upper is not None and upper <= float(policy.max_false_accept_rate)
            and disagreement <= float(policy.max_disagreement_rate))
        slices.append(CalibrationSlice(risk_id, len(rows), n, fa, tuple(count / n if n else None for count in fa),
            uppers, fr, upper, disagreement, eligible))
    eligible = all(row.eligible for row in slices)
    return CalibrationReport(eligible, "JUDGE_CALIBRATION_STATISTICS_PASSED" if eligible else "JUDGE_CALIBRATION_STATISTICS_FAILED",
        policy.version, policy.holdout_digest, input_coverage, tuple(slices), len(disagreements) / len(holdout))
