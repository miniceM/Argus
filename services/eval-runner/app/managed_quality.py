"""仅用 Argus 已采纳/冻结证据判定质量；无远程读取、外部版本管理或新 Gate。"""

from __future__ import annotations

import copy
from typing import Any

from .evaluator_binding import canonical_digest
from .evaluator_results import ResultContractError, coerce_typed_value
from .managed_evaluation import ManagedEvaluationContract, evaluation_source
from .quality_policy import QualityPolicy


def managed_contract(manifest: dict[str, Any]) -> tuple[dict, QualityPolicy]:
    """复验冻结的采纳口径与质量策略，不查询当前 Langfuse Rule。

    托管目标当前仅支持 remote-agent-http/TOOL（与
    langfuse_managed_scores._context 的校验值耦合，#218 范围内
    不扩展其他 Agent 类型；新增目标需同步两处并补契约测试）。
    """
    contract = copy.deepcopy(manifest.get("managed_evaluation") or {})
    digest = contract.pop("acceptance_digest", None)
    if (
        not digest
        or canonical_digest(contract) != digest
        or contract.get("source") != "EVAL"
        or contract.get("execution_semantics") != "langfuse_latest"
        or contract.get("environment") != (manifest.get("comparison") or {}).get("environment")
        or contract.get("target") != {"name": "remote-agent-http", "type": "TOOL"}
    ):
        raise ValueError("MANAGED_CONTRACT_INVALID")
    ManagedEvaluationContract.model_validate(
        {k: contract[k] for k in ("project_id", "environment", "target", "scores")}
    )
    contract["acceptance_digest"] = digest
    specs = contract["scores"]
    names = [s["name"] for s in specs]
    if len(set(names)) != len(names) or not any(s.get("required", True) for s in specs):
        raise ValueError("MANAGED_CONTRACT_INVALID")
    policy = QualityPolicy.from_payload(manifest["quality_policy"])
    frozen_policy = manifest["quality_policy"].get("policy_digest")
    if policy.policy_digest != frozen_policy:
        raise ValueError("QUALITY_POLICY_INVALID")
    block = manifest.get("contract_digests") or {}
    if (
        manifest.get("measurement_digest") != digest
        or (block.get("measurement") or {}).get("digest", digest) != digest
        or (block.get("quality_policy") or {}).get("digest", frozen_policy) != frozen_policy
    ):
        raise ValueError("MANAGED_CONTRACT_DIGEST_MISMATCH")
    for spec in specs:
        rule = policy.rule_for(spec["name"])
        if spec.get("required", True) and rule is None:
            raise ValueError("MANAGED_REQUIRED_POLICY_MISSING")
        if rule and (rule.result_type != spec["data_type"] or rule.required != spec.get("required", True)):
            raise ValueError("MANAGED_POLICY_MISMATCH")
    if any(r.evaluator_id not in names for r in policy.rules):
        raise ValueError("MANAGED_POLICY_MISMATCH")
    return contract, policy


def _verified_result(result: dict, expected: dict, contract: dict, manifest: dict, item: dict, launch_id: str):
    """检查原始 Score、TypedResult、采纳身份三者一致；不重新执行 Judge。

    只有 ``MANAGED_SCORE_VERIFIED`` 可作为可发布证据：#216 的
    ``MANAGED_DIAGNOSTIC_ONLY`` 是诊断采纳（未参与 QualityPolicy），
    追认为 PASS 会违反 fail-closed。

    代际隔离不在此处复验：采纳时以租约/代际围栏 + 按 evaluator
    upsert + 行锁内「报告背书已采纳行」保证单调，Snapshot 复验改为
    钉死 dispatch/attempt/trace 谱系（Snapshot item 不携带
    evaluation_generation，见 result_snapshots.build_result_items）。
    """
    from .langfuse_managed_scores import _utc

    evidence = result.get("evidence") or {}
    provenance = result.get("provenance") or {}
    score = evidence.get("score") or {}
    meta = score.get("metadata") or {}
    obs = evidence.get("observation") or {}
    digest = contract["acceptance_digest"]
    checks = [
        result.get("status") == "succeeded",
        result.get("result_type") == expected["data_type"],
        provenance.get("executor_type") == "langfuse_managed",
        provenance.get("binding_id") == digest,
        provenance.get("definition_digest") == digest,
        provenance.get("evaluator_id") == expected["name"],
        provenance.get("evaluator_version") is None,
        provenance.get("contract_status") == "MANAGED_SCORE_VERIFIED",
        provenance.get("manifest_schema_version") == manifest.get("schema_version"),
        evidence.get("acceptance_digest") == digest,
        evidence.get("manifest_digest") == canonical_digest(manifest),
        evidence.get("launch_id") == launch_id,
        all(
            item.get(k) is not None and evidence.get(k) == item[k]
            for k in (
                "item_execution_id",
                "dataset_item_id",
                "dispatch_generation",
                "final_attempt_id",
                "trace_id",
                "observation_id",
            )
        ),
        all(
            isinstance(evidence.get(k), str) and evidence[k]
            for k in ("checkpoint_id", "input_digest", "output_digest", "observed_at")
        ),
        score.get("source") == "EVAL",
        score.get("projectId") == contract["project_id"],
        isinstance(score.get("id"), str) and bool(score["id"]),
        score.get("environment") == contract["environment"],
        score.get("name") == expected["name"],
        score.get("dataType") == expected["data_type"].upper(),
        score.get("subject")
        == {"kind": "observation", "id": item.get("observation_id"), "traceId": item.get("trace_id")},
        all(
            isinstance(meta.get(k), str) and meta[k].strip()
            for k in (
                "evaluation_rule_id",
                "evaluator_id",
                "job_execution_id",
                "target_trace_id",
                "target_observation_id",
            )
        ),
        meta.get("evaluation_rule_id") == expected["rule_id"],
        meta.get("job_configuration_id", expected["rule_id"]) == expected["rule_id"],
        not expected.get("evaluator_id") or meta.get("evaluator_id") == expected["evaluator_id"],
        meta.get("target_trace_id") == item.get("trace_id"),
        meta.get("target_observation_id") == item.get("observation_id"),
        obs.get("id") == item.get("observation_id"),
        obs.get("traceId") == item.get("trace_id"),
        obs.get("projectId") == contract["project_id"],
        obs.get("environment") == contract["environment"],
        obs.get("name") == contract["target"]["name"],
        obs.get("type") == contract["target"]["type"],
    ]
    timestamp, start, observed = (
        _utc(score.get("timestamp")),
        _utc(obs.get("startTime")),
        _utc(evidence.get("observed_at")),
    )
    checks.append(bool(timestamp and start and observed and start <= timestamp <= observed))
    # 采纳时已核验的 IO 摘要，不保存 Agent 原文，也不依赖 Checkpoint 保留期限。
    io = evidence.get("observation_io_digests") or {}
    for field in ("input", "output"):
        if f"{field}_sha256" in meta:
            checks.append(meta[f"{field}_sha256"] == io.get(field))
    if io.get("output"):
        checks.append(evidence.get("output_digest") == "sha256:" + io["output"])
    if not all(checks):
        raise ValueError("MANAGED_SCORE_EVIDENCE_INVALID")
    value = coerce_typed_value(
        score.get("value"), result_type=expected["data_type"], category_values=expected.get("category_values")
    )
    typed = coerce_typed_value(
        result.get("value"), result_type=expected["data_type"], category_values=expected.get("category_values")
    )
    if typed != value or (expected["data_type"] != "numeric" and type(typed) is not type(value)):
        raise ValueError("MANAGED_SCORE_VALUE_MISMATCH")
    if result.get("normalized_value") != (float(value) if expected["data_type"] == "numeric" else None):
        raise ValueError("MANAGED_SCORE_NORMALIZATION_INVALID")


def managed_quality_decision(manifest: dict, item: dict, launch_id: str):
    """复用 TypedResult / QualityPolicy；缺失或不可信必选证据只产生 UNKNOWN。"""
    from .quality_policy import evaluate_quality_policy

    try:
        contract, policy = managed_contract(manifest)
    except (ValueError, KeyError, TypeError, AttributeError):
        return evaluate_quality_policy([], None), ["MANAGED_CONTRACT_INVALID"]
    results = item.get("evaluation_results") or []
    accepted, reasons, ids = [], [], set()
    for expected in contract["scores"]:
        matches = [r for r in results if isinstance(r, dict) and r.get("evaluator_id") == expected["name"]]
        try:
            if len(matches) != 1:
                raise ValueError("MANAGED_SCORE_MISSING_OR_AMBIGUOUS")
            result = matches[0]
            _verified_result(result, expected, contract, manifest, item, launch_id)
            identity = result["evidence"]["score"]["id"]
            if identity in ids:
                raise ValueError("MANAGED_SCORE_ID_CONFLICT")
            ids.add(identity)
            accepted.append(result)
        except (ValueError, KeyError, TypeError, AttributeError, ResultContractError):
            if expected.get("required", True):
                reasons.append(f"MANAGED_SCORE_UNVERIFIED:{expected['name']}")
    if item.get("execution_status") != "succeeded" or item.get("eval_status") != "succeeded":
        return evaluate_quality_policy([], policy), [*reasons, "MANAGED_COLLECTION_NOT_COMPLETED"]
    return evaluate_quality_policy(accepted, policy), reasons


def evaluation_specs(manifest: dict) -> list[dict]:
    """仅为既有 Aggregator 提供 Score 类型/方向，不伪造外部 Evaluator 版本。"""
    if evaluation_source(manifest) != "langfuse_managed":
        return manifest.get("evaluators") or []
    rules = {r["evaluator_id"]: r for r in (manifest.get("quality_policy") or {}).get("rules", [])}
    return [
        dict(
            id=s["name"],
            result_type=s["data_type"],
            scope="item",
            required=s.get("required", True),
            executor_type="langfuse_managed",
            direction="lower_is_better" if rules.get(s["name"], {}).get("operator") == "<=" else "higher_is_better",
        )
        for s in (manifest.get("managed_evaluation") or {}).get("scores", [])
    ]


def managed_case_comparison_reason(baseline: dict, candidate: dict, specs: list[dict]) -> str | None:
    """相同 Rule ID 不证明 latest 实现相同；仅比较已采纳的外部版本引用。"""
    for spec in specs:
        if spec.get("executor_type") != "langfuse_managed" or not spec.get("required", True):
            continue
        references = []
        for item in (baseline, candidate):
            matches = [r for r in item.get("evaluation_results", []) if r.get("evaluator_id") == spec["id"]]
            if len(matches) != 1:
                return "MANAGED_MEASUREMENT_UNVERIFIED"
            metadata = ((matches[0].get("evidence") or {}).get("score") or {}).get("metadata") or {}
            version = metadata.get("evaluator_version_id")
            if not isinstance(version, str) or not version.strip():
                return "MANAGED_EXECUTION_VERSION_UNVERIFIED"
            references.append((metadata.get("evaluator_id"), version, metadata.get("code_eval_runtime")))
        if references[0] != references[1]:
            return "MEASUREMENT_CHANGED"
    return None
