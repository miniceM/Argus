"""固定 Snapshot 诊断投影；不采集、不重评、不修补历史。"""

import json
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Response

from ..evaluator_binding import bindings_from_manifest
from ..evaluator_results import ResultProvenance
from ..models import TrajectoryDiagnosisResponse, TrajectoryDisplayResponse, TrajectoryRuleDiagnosisResponse
from ..required_evidence import required_quality_decision, uses_required_contract
from ..result_snapshots import _canonical_digest
from .codec import canonical_dumps
from .inputs import load_snapshot_input


def _reason(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) else None


def _online_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


def _rules(manifest, item, loaded):
    rows = item.get("evaluation_results") or []
    decisions = (item.get("quality_evaluation") or {}).get("rules", [])
    if not isinstance(rows, list) or not isinstance(decisions, list):
        return []
    available = loaded is not None and loaded.trajectory is not None and loaded.reason_code is None
    step_ids = {s.step_id for s in loaded.trajectory.steps} if available else set()
    output = []
    for binding in bindings_from_manifest(manifest):
        if binding.input_contract.get("input_kind") != "trajectory":
            continue
        matches = [r for r in rows if isinstance(r, dict) and r.get("evaluator_id") == binding.evaluator_id]
        row = matches[0] if len(matches) == 1 else {}
        policies = [r for r in decisions if isinstance(r, dict) and r.get("evaluator_id") == binding.evaluator_id]
        policy = policies[0] if len(policies) == 1 else {}
        verified = row.get("provenance") == ResultProvenance.from_binding(binding, manifest_schema_version=manifest["schema_version"]).payload()
        status = row.get("status", "no_result") if verified else "no_result"
        if status not in {"succeeded", "failed", "skipped", "no_result"}:
            status = "no_result"
        value = row.get("value") if status == "succeeded" and type(row.get("value")) is bool else None
        frozen = policy.get("conclusion", "unknown")
        if frozen not in {"pass", "fail", "unknown"}:
            frozen = "unknown"
        conclusion = frozen if available and verified and value is not None else "unknown"
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        ids = evidence.get("evidence_step_ids", [])
        if available and verified and (value is not None or ids):
            if evidence.get("evidence_ref") != loaded.trajectory_evidence_ref.model_dump(mode="json"):
                raise ValueError("EVIDENCE_CONTRACT_INVALID")
        ids = sorted({s for s in ids if isinstance(s, str) and s in step_ids}) if isinstance(ids, list) else []
        output.append(TrajectoryRuleDiagnosisResponse(evaluator_id=binding.evaluator_id, evaluator_version=binding.version,
            binding_id=binding.binding_id, typed_status=status, value=value, conclusion=conclusion,
            frozen_conclusion=frozen if verified else "unknown", required=bool(policy.get("required", False)),
            critical=bool(policy.get("critical", False)), reason_code=(loaded.reason_code if not available and loaded else None)
                or _reason(evidence.get("reason_code")) or _reason(policy.get("reason_code")), evidence_step_ids=ids))
    return output


def build_diagnosis(session, snapshot, item):
    try:
        if (not isinstance(snapshot.manifest, dict) or snapshot.manifest_digest != _canonical_digest(snapshot.manifest)
            or snapshot.source_result_digest != _canonical_digest(snapshot.items)):
            raise ValueError
        version = snapshot.manifest.get("schema_version")
        historical = version in {None, "1.0", "1.1", "1.2"} or version == "1.3" and not snapshot.manifest.get("trajectory")
        unsupported = version not in {None, "1.0", "1.1", "1.2", "1.3"}
        loaded = None if historical or unsupported else load_snapshot_input(session, snapshot_id=snapshot.id, dataset_item_id=item["dataset_item_id"])
        trajectory = loaded.trajectory if loaded else None
        reason = "HISTORICAL_TRAJECTORY_UNAVAILABLE" if historical else loaded.reason_code if loaded else "TRAJECTORY_INPUT_MANIFEST_INVALID"
        available = trajectory is not None and reason is None
        if available and uses_required_contract(snapshot.manifest):
            expected = required_quality_decision(snapshot.manifest, item["evaluation_results"],
                trajectory_input=item["trajectory_input"])
            if (item.get("quality_evaluation") != expected.payload()
                or item.get("required_evidence_status") != expected.required_evidence_status
                or item.get("quality_conclusion") != expected.conclusion):
                raise ValueError
        rules = [] if historical or unsupported else _rules(snapshot.manifest, item, loaded)
        quality = item.get("quality_conclusion", "unknown")
        quality = quality if quality in {"pass", "fail", "unknown"} else "unknown"
        return TrajectoryDiagnosisResponse(launch_id=snapshot.launch_id, snapshot_id=snapshot.id,
            dataset_item_id=item["dataset_item_id"], acquisition_state="READY" if available else
                "PENDING" if loaded and loaded.acquisition and loaded.acquisition.state == "PENDING" else "UNAVAILABLE",
            completeness=trajectory.completeness if available else None,
            trajectory=TrajectoryDisplayResponse.model_validate(trajectory.model_dump(mode="python")) if available else None,
            rule_results=rules, reason_code=reason, evidence_ref=loaded.trajectory_evidence_ref if available else None,
            step_argument_display_json={s.step_id: canonical_dumps(s.arguments_redacted) for s in trajectory.steps} if available else {},
            langfuse_trace_url=_online_url(item.get("trace_url")), execution_status=item.get("execution_status", "unknown"),
            quality_conclusion=quality, required_evidence_status=item.get("required_evidence_status") if not historical and not unsupported else None)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(status_code=409, detail="EVIDENCE_CONTRACT_INVALID") from None


def diagnosis_response(value: TrajectoryDiagnosisResponse):
    # 字符串显示参数可大于普通业务字符串限制；数值来自同一个已验证 artifact。
    # 分别编码获准轨迹，避免 Pydantic/浏览器把 Decimal 转成 float 或 string。
    rest = value.model_dump(mode="json", exclude={"trajectory"})
    encoded = json.dumps(rest, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    trajectory = canonical_dumps(value.trajectory) if value.trajectory is not None else "null"
    return Response(encoded[:-1] + ',"trajectory":' + trajectory + "}", media_type="application/json")
