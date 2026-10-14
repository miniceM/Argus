"""按冻结 required 集合判断资格；不把 optional 诊断当作必要证据。"""

from dataclasses import replace

EVIDENCE_CONTRACT_VERSION = "trajectory-1.0"
COMPARISON_POLICY_VERSION = "comparison-v2-required-evidence"


def uses_required_contract(manifest):
    if not isinstance(manifest, dict):
        return False
    return ("evidence_contract_version" in manifest
        or (manifest.get("comparison") or {}).get("comparison_policy_version") == COMPARISON_POLICY_VERSION
        or ((manifest.get("contract_digests") or {}).get("aggregation_comparison") or {}).get("version") == COMPARISON_POLICY_VERSION)


def frozen_required_policy(manifest):
    from .comparison_contracts import aggregation_comparison_digest
    from .evaluator_binding import bindings_from_manifest, manifest_measurement_digest
    from .quality_policy import QualityPolicy, freeze_quality_policy

    try:
        if manifest.get("evidence_contract_version") != EVIDENCE_CONTRACT_VERSION or manifest.get("schema_version") != "1.3":
            raise ValueError
        bindings = bindings_from_manifest(manifest)
        by_id = {b.evaluator_id: b for b in bindings}
        if not bindings or len(by_id) != len(bindings) or any(
            raw.get("binding_digest") != binding.binding_digest or binding.contract_status != "FROZEN_VERIFIED"
            for raw, binding in zip(manifest["evaluators"], bindings, strict=True)
        ):
            raise ValueError
        raw = manifest["quality_policy"]
        parsed = QualityPolicy.from_payload(raw)
        policy = freeze_quality_policy(policy_id=parsed.policy_id, version=parsed.version, rules=parsed.rules,
            bindings=by_id, unknown_handling=parsed.unknown_handling)
        if raw.get("policy_digest") != policy.policy_digest or parsed.schema_version != policy.schema_version:
            raise ValueError
        contracts = manifest["contract_digests"]
        expected = aggregation_comparison_digest(evidence_contract_version=EVIDENCE_CONTRACT_VERSION)
        if (contracts["aggregation_comparison"] != {"digest": expected, "version": COMPARISON_POLICY_VERSION}
            or manifest["comparison"]["comparison_policy_version"] != COMPARISON_POLICY_VERSION
            or contracts["quality_policy"]["digest"] != policy.policy_digest
            or contracts["measurement"]["digest"] != manifest_measurement_digest(manifest)):
            raise ValueError
        return policy, by_id
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("EVIDENCE_CONTRACT_INVALID") from None


def required_quality_decision(manifest, results, *, trajectory_input=None):
    from .evaluator_results import ResultProvenance, coerce_typed_value
    from .quality_policy import evaluate_quality_policy

    policy, bindings = frozen_required_policy(manifest)
    rows = [r.to_payload() if hasattr(r, "to_payload") else r for r in results]
    valid, complete = [], True
    for rule in policy.rules:
        matches = [r for r in rows if isinstance(r, dict) and r.get("evaluator_id") == rule.evaluator_id]
        binding = bindings[rule.evaluator_id]
        raw = matches[0] if len(matches) == 1 else None
        measurable = False
        if raw is not None:
            try:
                coerce_typed_value(raw.get("value"), result_type=binding.result_type, category_values=binding.category_values)
                measurable = (raw.get("status") == "succeeded" and raw.get("result_type") == binding.result_type
                    and raw.get("provenance") == ResultProvenance.from_binding(binding, manifest_schema_version="1.3").payload()
                    and raw.get("error_code") is None and raw.get("error_message") is None)
                if measurable and rule.required and binding.input_contract.get("input_kind") == "trajectory":
                    from .trajectory.schema import TrajectoryAcquisition
                    acquisition = TrajectoryAcquisition.model_validate(trajectory_input["acquisition"])
                    measurable = (trajectory_input.get("version") == "1.0" and trajectory_input.get("reason_code") is None
                        and trajectory_input.get("trajectory_contract_digest") == manifest["trajectory"]["trajectory_contract_digest"]
                        and acquisition.state == "READY" and acquisition.evidence_ref is not None
                        and acquisition.evidence_ref.trajectory_contract_digest == trajectory_input["trajectory_contract_digest"]
                        and (raw.get("evidence") or {}).get("evidence_ref") == acquisition.evidence_ref.model_dump(mode="json"))
            except (ValueError, TypeError, OverflowError, KeyError, AttributeError):
                measurable = False
            if measurable or raw.get("status") in {"failed", "skipped", "no_result"}:
                valid.append(raw)
        if rule.required and not measurable:
            complete = False
    decision = evaluate_quality_policy(valid, policy)
    return replace(decision, evidence_contract_version=EVIDENCE_CONTRACT_VERSION,
        required_evidence_status="COMPLETE" if complete else "INCOMPLETE")


def item_is_evaluated(item):
    if str(item.get("execution_status", "")).lower() != "succeeded" or str(item.get("quality_conclusion", "")).lower() not in {"pass", "fail"}:
        return False
    if "evidence_contract_version" in item or "required_evidence_status" in item:
        return item.get("evidence_contract_version") == EVIDENCE_CONTRACT_VERSION and item.get("required_evidence_status") == "COMPLETE"
    return str(item.get("eval_status", "")).lower() == "succeeded"


def validate_snapshot_contract(snapshot, *, session=None):
    """验证冻结合同及 required 制品归属，不修补历史快照。"""
    from .result_snapshots import _canonical_digest

    try:
        manifest, summary, items = snapshot.manifest, getattr(snapshot, "summary", None), snapshot.items
        if not isinstance(manifest, dict) or not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
            raise ValueError
        if summary is None and not uses_required_contract(manifest):
            summary = {}
        if not isinstance(summary, dict):
            raise ValueError
        if "evidence_contract_version" not in manifest:
            if uses_required_contract(manifest) or "evidence_contract_version" in summary or any("evidence_contract_version" in i or "required_evidence_status" in i for i in items):
                raise ValueError
            return
        policy, bindings = frozen_required_policy(manifest)
        if (snapshot.manifest_digest != _canonical_digest(manifest) or snapshot.source_result_digest != _canonical_digest(items)
            or summary.get("evidence_contract_version") != EVIDENCE_CONTRACT_VERSION):
            raise ValueError
        for item in items:
            if not isinstance(item.get("evaluation_results"), list) or any(not isinstance(r, dict) for r in item["evaluation_results"]):
                raise ValueError
            decision = required_quality_decision(manifest, item["evaluation_results"], trajectory_input=item.get("trajectory_input") or {})
            if (item.get("evidence_contract_version") != EVIDENCE_CONTRACT_VERSION
                or item.get("required_evidence_status") != decision.required_evidence_status
                or item.get("quality_conclusion") != decision.conclusion
                or item.get("quality_evaluation") != decision.payload()):
                raise ValueError
            if decision.required_evidence_status == "COMPLETE" and any(
                rule.required and bindings[rule.evaluator_id].input_contract.get("input_kind") == "trajectory"
                for rule in policy.rules
            ):
                from sqlalchemy import inspect

                from .trajectory.inputs import load_frozen_snapshot_input

                state = inspect(snapshot, raiseerr=False)
                owning_session = session if session is not None else getattr(state, "session", None)
                if owning_session is None:
                    raise ValueError
                loaded = load_frozen_snapshot_input(owning_session, snapshot=snapshot, dataset_item_id=item["dataset_item_id"])
                if loaded is None or loaded.reason_code is not None or loaded.trajectory is None:
                    raise ValueError
        complete = sum(item_is_evaluated(i) for i in items)
        expected = [i["id"] for i in manifest["dataset"]["items"]]
        actual = [i["dataset_item_id"] for i in items]
        if len(set(expected)) != len(expected) or len(set(actual)) != len(actual) or not set(actual).issubset(expected):
            raise ValueError
        total = len(expected)
        if (summary.get("required_evidence_complete_cases") != complete
            or summary.get("required_evidence_incomplete_cases") != total - complete
            or summary.get("required_evidence_status") != ("COMPLETE" if total and complete == total else "INCOMPLETE")
            or (snapshot.evidence_state == "COMPLETE") != (bool(total) and complete == total)):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration):
        raise ValueError("EVIDENCE_CONTRACT_INVALID") from None
