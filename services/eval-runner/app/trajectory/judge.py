"""冻结 Judge 输出与独立留出集统计；统计合格不代替 Provider/制品准入。"""

from dataclasses import dataclass
from decimal import Decimal
from math import sqrt
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .codec import canonical_digest, canonical_loads
from .schema import Identity, _ContractModel

Probability = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


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
