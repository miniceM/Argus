"""Layered comparison contracts, and why a comparison is or is not formal.

A comparison is only *formal* when both sides were measured and judged under
identical contracts. Measurement (how a number is produced), Quality Policy (how
it becomes PASS/FAIL) and Aggregation/Comparison (how cases become a run-level
verdict) move independently, so each gets its own versioned digest.

The point is not to forbid comparisons when something moved -- it is to say
*which* contract moved, so that tightening a threshold is never reported as an
Agent regression.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .evaluator_binding import canonical_digest

COMPARISON_CONTRACT_SCHEMA_VERSION = "comparison-contract-1"

MEASUREMENT = "MEASUREMENT"
QUALITY_POLICY = "QUALITY_POLICY"
AGGREGATION_COMPARISON = "AGGREGATION_COMPARISON"

DIMENSIONS: tuple[str, ...] = (MEASUREMENT, QUALITY_POLICY, AGGREGATION_COMPARISON)

# The Manifest block spells the dimensions in snake_case.
_BLOCK_KEYS: dict[str, str] = {
    MEASUREMENT: "measurement",
    QUALITY_POLICY: "quality_policy",
    AGGREGATION_COMPARISON: "aggregation_comparison",
}

STATUS_MATCH = "MATCH"
STATUS_CHANGED = "CHANGED"
STATUS_UNKNOWN = "UNKNOWN"

REASON_DATASET_CHANGED = "DATASET_CHANGED"
REASON_CONTRACT_PROVENANCE_UNKNOWN = "CONTRACT_PROVENANCE_UNKNOWN"

_DIMENSION_REASONS: dict[str, str] = {
    MEASUREMENT: "MEASUREMENT_CHANGED",
    QUALITY_POLICY: "QUALITY_POLICY_CHANGED",
    AGGREGATION_COMPARISON: "AGGREGATION_COMPARISON_CHANGED",
}

# What the run-level verdict is built from: every required case must be decided
# under a complete evidence state. A pretty average over a handful of comparable
# cases is never a release-grade "no regression".
AGGREGATION_CONTRACT: dict[str, Any] = {
    "denominator": "all_required_cases",
    "coverage_requirement": "all_required_cases_decided",
    "classification_algorithm": "quality_conclusion_then_typed_score_deltas",
    "direction_semantics": "per_evaluator_frozen_direction",
    "category_ordering": "frozen_normalization_mapping_only",
    "text_handling": "diagnostic_only_no_numeric_delta",
    "unknown_handling": "blocks_formal_comparison",
}


def aggregation_comparison_digest() -> str:
    """Digest of the comparison semantics themselves.

    Changing the denominator, the coverage requirement or the classification
    algorithm changes this digest, and therefore any comparison that spans the
    change is no longer formal.
    """
    return canonical_digest(AGGREGATION_CONTRACT)


def _version_of(entry: Any) -> str | None:
    if isinstance(entry, Mapping):
        value = entry.get("version")
        if isinstance(value, str) and value.strip():
            return value
    return None


def _digest_of(entry: Any) -> str | None:
    if isinstance(entry, Mapping):
        value = entry.get("digest")
        if isinstance(value, str) and value.strip():
            return value
    return None


def _dataset_identity(manifest: Mapping[str, Any] | None) -> tuple[str, str] | None:
    dataset = (manifest or {}).get("dataset") or {}
    source = dataset.get("source")
    dataset_id = dataset.get("dataset_id")
    if not isinstance(source, str) or not source.strip():
        return None
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        return None
    return source.strip(), dataset_id.strip()


@dataclass(frozen=True)
class ContractDimension:
    dimension: str
    status: str
    baseline_digest: str | None = None
    candidate_digest: str | None = None
    baseline_version: str | None = None
    candidate_version: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "status": self.status,
            "baseline_digest": self.baseline_digest,
            "candidate_digest": self.candidate_digest,
            "baseline_version": self.baseline_version,
            "candidate_version": self.candidate_version,
        }


@dataclass(frozen=True)
class ComparabilityVerdict:
    comparable: bool
    reason_codes: tuple[str, ...]
    dimensions: tuple[ContractDimension, ...]
    suggestions: tuple[str, ...] = ()

    @property
    def provenance(self) -> str:
        if any(dim.status == STATUS_UNKNOWN for dim in self.dimensions):
            return "LEGACY_PARTIAL" if any(
                dim.status != STATUS_UNKNOWN for dim in self.dimensions
            ) else "UNKNOWN"
        return "FROZEN"

    def dimension(self, name: str) -> ContractDimension:
        return next(dim for dim in self.dimensions if dim.dimension == name)

    def to_payload(self) -> dict[str, Any]:
        return {
            "comparable": self.comparable,
            "reason_codes": list(self.reason_codes),
            "provenance": self.provenance,
            "dimensions": [dim.to_payload() for dim in self.dimensions],
            "suggestions": list(self.suggestions),
        }


def contract_digests_for(manifest: Mapping[str, Any] | None) -> dict[str, ContractDimension]:
    """Read the three contract digests out of a frozen Manifest.

    A pre-#86 Manifest carries the #81 measurement digest and the #83 policy
    digest, but never digested its comparison semantics, so that dimension is
    reported UNKNOWN rather than back-filled from the current catalog.
    """
    manifest = manifest or {}
    block = manifest.get("contract_digests")
    versions = {
        MEASUREMENT: manifest.get("schema_version"),
        QUALITY_POLICY: (manifest.get("quality_policy") or {}).get("version"),
        AGGREGATION_COMPARISON: (manifest.get("comparison") or {}).get("comparison_policy_version"),
    }

    def entry(name: str) -> Any:
        if isinstance(block, Mapping):
            return block.get(_BLOCK_KEYS[name])
        return None

    digests: dict[str, ContractDimension] = {}
    for name in DIMENSIONS:
        digest = _digest_of(entry(name))
        if digest is None:
            if name == MEASUREMENT:
                digest = _legacy_measurement_digest(manifest)
            elif name == QUALITY_POLICY:
                digest = _legacy_policy_digest(manifest)
        digests[name] = ContractDimension(
            dimension=name,
            status=STATUS_MATCH if digest else STATUS_UNKNOWN,
            baseline_digest=digest,
            candidate_digest=digest,
            baseline_version=_version_of(entry(name)) or (
                versions[name] if isinstance(versions[name], str) else None
            ),
            candidate_version=_version_of(entry(name)) or (
                versions[name] if isinstance(versions[name], str) else None
            ),
        )
    return digests


def _legacy_measurement_digest(manifest: Mapping[str, Any]) -> str | None:
    """The #81 measurement digest, or None when it cannot be proven."""
    digest = manifest.get("measurement_digest")
    if isinstance(digest, str) and digest.strip():
        return digest
    # Pre-#81 Manifests have no binding identity at all, and the current
    # catalog must never be used to guess what they measured.
    return None


def _legacy_policy_digest(manifest: Mapping[str, Any]) -> str | None:
    policy = manifest.get("quality_policy")
    if not isinstance(policy, Mapping):
        return None
    digest = policy.get("policy_digest")
    if isinstance(digest, str) and digest.strip():
        return digest
    return None


_DIMENSION_SUGGESTIONS: dict[str, str] = {
    MEASUREMENT: "使用相同的测量版本（Evaluator 实现、输入输出契约与参数）重新评测两侧。",
    QUALITY_POLICY: "使用相同质量策略（阈值、operator、critical、UNKNOWN 处置）重新评测后再比较。",
    AGGREGATION_COMPARISON: "两侧使用相同的比较口径（分母、覆盖率要求与分类算法）后重新比较。",
}


def assess_comparability(
    baseline_manifest: Mapping[str, Any] | None,
    candidate_manifest: Mapping[str, Any] | None,
) -> ComparabilityVerdict:
    """Explain whether two frozen results may be compared formally."""
    baseline = contract_digests_for(baseline_manifest)
    candidate = contract_digests_for(candidate_manifest)

    dimensions: list[ContractDimension] = []
    reasons: list[str] = []
    suggestions: list[str] = []

    for name in DIMENSIONS:
        before = baseline[name]
        after = candidate[name]
        if before.baseline_digest is None or after.candidate_digest is None:
            status = STATUS_UNKNOWN
        elif before.baseline_digest == after.candidate_digest:
            status = STATUS_MATCH
        else:
            status = STATUS_CHANGED
        dimensions.append(
            ContractDimension(
                dimension=name,
                status=status,
                baseline_digest=before.baseline_digest,
                candidate_digest=after.candidate_digest,
                baseline_version=before.baseline_version,
                candidate_version=after.candidate_version,
            )
        )
        if status == STATUS_CHANGED:
            reasons.append(_DIMENSION_REASONS[name])
            suggestions.append(_DIMENSION_SUGGESTIONS[name])
        elif status == STATUS_UNKNOWN:
            reasons.append(REASON_CONTRACT_PROVENANCE_UNKNOWN)

    identities = {
        _dataset_identity(baseline_manifest),
        _dataset_identity(candidate_manifest),
    }
    known_identities = {identity for identity in identities if identity is not None}
    if len(known_identities) > 1:
        reasons.append(REASON_DATASET_CHANGED)
        suggestions.append("两侧 Dataset 身份不同，请在同一 Dataset 版本上重新评测后比较。")

    # Deterministic, de-duplicated ordering: dataset first, then the three
    # contract dimensions in their declared order.
    ordered = [REASON_DATASET_CHANGED]
    ordered.extend(_DIMENSION_REASONS[name] for name in DIMENSIONS)
    ordered.append(REASON_CONTRACT_PROVENANCE_UNKNOWN)
    reason_codes = tuple(code for code in ordered if code in reasons)

    return ComparabilityVerdict(
        comparable=not reason_codes,
        reason_codes=reason_codes,
        dimensions=tuple(dimensions),
        suggestions=tuple(dict.fromkeys(suggestions)),
    )
