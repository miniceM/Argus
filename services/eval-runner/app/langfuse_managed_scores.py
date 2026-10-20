"""#216：显式契约下的定向采纳。无 Judge、后台轮询、QualityPolicy/Gate 或出站同步。"""

from __future__ import annotations

import copy
import json
import math
from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .db_models import (
    EvaluationResultRecord,
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    ManagedScoreEvidenceRecord,
)
from .evaluation_result_store import result_to_payload
from .evaluator_binding import canonical_digest
from .evaluator_results import (
    ResultContractError,
    ResultProvenance,
    TypedEvaluationResult,
    coerce_typed_value,
    normalized_value_for,
)
from .execution_checkpoint import canonical_output_digest
from .langfuse_scores_client import ScoresReadError, ScoresV3Client  # noqa: F401
from .managed_evaluation import evaluation_source

SCORE_FIELDS = (
    "id",
    "projectId",
    "name",
    "dataType",
    "value",
    "source",
    "subject",
    "environment",
    "timestamp",
    "createdAt",
    "updatedAt",
    "configId",
)
METADATA_FIELDS = (
    "evaluation_rule_id",
    "job_configuration_id",
    "evaluation_rule_assignment_id",
    "evaluator_id",
    "evaluator_version_id",
    "job_execution_id",
    "target_trace_id",
    "target_observation_id",
    "dispatcher_name",
    "code_eval_runtime",
    "input_sha256",
    "output_sha256",
)


def _utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_number": str(value)}
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _score_evidence(row):
    payload = {k: row[k] for k in SCORE_FIELDS if k in row}
    metadata = row.get("metadata")
    payload["metadata"] = (
        {k: metadata[k] for k in METADATA_FIELDS if k in metadata} if isinstance(metadata, dict) else None
    )
    return _json_safe(payload)


def _parse_io(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _check(condition, code):
    if not condition:
        raise ScoresReadError(code)


class ManagedScoreCollector:
    def __init__(self, db_manager, client):
        self.db = db_manager
        self.client = client

    def _context(self, session, launch_id, item_id):
        launch = session.get(ExperimentLaunchRecord, launch_id)
        item = session.get(ExperimentItemExecutionRecord, item_id)
        _check(launch is not None and item is not None and item.launch_id == launch_id, "ITEM_NOT_FOUND")
        _check(evaluation_source(launch.manifest) == "langfuse_managed", "MANAGED_NOT_SELECTED")
        context = {
            "launch_id": launch_id,
            "item_execution_id": item_id,
            "dataset_item_id": item.dataset_item_id,
            "trace_id": item.trace_id,
            "observation_id": item.observation_id,
            "dispatch_generation": item.dispatch_generation,
            "evaluation_generation": item.evaluation_generation,
            "final_attempt_id": item.final_attempt_id,
            "manifest_digest": canonical_digest(launch.manifest),
            "manifest_schema_version": launch.manifest.get("schema_version"),
        }
        contract = copy.deepcopy(launch.manifest.get("managed_evaluation", {}))
        digest = contract.pop("acceptance_digest", None)
        _check(
            digest
            and canonical_digest(contract) == digest
            and contract.get("source") == "EVAL"
            and contract.get("environment") == launch.environment
            and contract.get("target") == {"name": "remote-agent-http", "type": "TOOL"},
            "CONTRACT_INVALID",
        )
        contract["acceptance_digest"] = digest
        context["acceptance_digest"] = digest
        _check(item.execution_status == "succeeded" and item.trace_id and item.observation_id, "EXECUTION_NOT_READY")
        cp = session.scalar(
            select(ExecutionCheckpointRecord).where(
                ExecutionCheckpointRecord.item_execution_id == item_id,
                ExecutionCheckpointRecord.dispatch_generation == item.dispatch_generation,
            )
        )
        _check(cp is not None, "CHECKPOINT_MISSING")
        _check(_utc(cp.expires_at) > datetime.now(UTC), "CHECKPOINT_EXPIRED")
        _check(
            cp.launch_id == launch_id
            and cp.dataset_item_id == item.dataset_item_id
            and cp.trace_id == item.trace_id
            and cp.observation_id == item.observation_id
            and cp.final_attempt_id == item.final_attempt_id
            and cp.manifest_digest == context["manifest_digest"]
            and cp.agent_output is not None
            and cp.output_digest == canonical_output_digest(cp.agent_output),
            "CHECKPOINT_MISMATCH",
        )
        context.update(
            checkpoint_id=cp.id, output_digest=cp.output_digest, input_digest=canonical_output_digest(cp.input_payload)
        )
        return context, contract

    def _observation(self, ctx, contract):
        _check(self.client is not None, "CLIENT_NOT_CONFIGURED")
        rows = self.client.observations(ctx["trace_id"])
        matches = [
            r
            for r in rows
            if r.get("id") == ctx["observation_id"]
            and r.get("traceId") == ctx["trace_id"]
            and r.get("name") == contract["target"]["name"]
            and r.get("type") == contract["target"]["type"]
        ]
        # 空 replay SPAN 与业务 TOOL 可共享 ID，不能选第一条或忽略重复 TOOL。
        _check(len(matches) == 1, "OBSERVATION_AMBIGUOUS_OR_MISSING")
        obs = matches[0]
        _check(obs.get("projectId") == contract["project_id"], "OBSERVATION_PROJECT_MISMATCH")
        _check(obs.get("environment") == contract["environment"], "OBSERVATION_ENVIRONMENT_MISMATCH")
        original_input = _parse_io(obs.get("input"))
        _check(
            isinstance(original_input, dict)
            and "request" in original_input
            and canonical_output_digest(original_input["request"]) == ctx["input_digest"]
            and canonical_output_digest(_parse_io(obs.get("output"))) == ctx["output_digest"],
            "OBSERVATION_IO_MISMATCH",
        )
        _check(_utc(obs.get("startTime")) is not None, "OBSERVATION_TIME_MISSING")
        return obs

    def _verify(self, row, expected, ctx, contract, obs):
        _check(isinstance(row.get("id"), str) and 0 < len(row["id"]) <= 128, "SCORE_ID_MISSING")
        _check(row.get("projectId") == contract["project_id"], "PROJECT_MISMATCH")
        _check(row.get("source") == "EVAL", "SOURCE_MISMATCH")
        _check(row.get("name") == expected["name"], "NAME_MISMATCH")
        _check(row.get("environment") == contract["environment"], "ENVIRONMENT_MISMATCH")
        _check(row.get("dataType") == expected["data_type"].upper(), "TYPE_MISMATCH")
        _check(
            row.get("subject") == {"kind": "observation", "id": ctx["observation_id"], "traceId": ctx["trace_id"]},
            "SUBJECT_MISMATCH",
        )
        meta = row.get("metadata")
        _check(
            isinstance(meta, dict)
            and all(
                isinstance(meta.get(k), str) and meta[k].strip()
                for k in (
                    "evaluation_rule_id",
                    "evaluator_id",
                    "job_execution_id",
                    "target_trace_id",
                    "target_observation_id",
                )
            ),
            "PROVENANCE_MISSING",
        )
        _check(meta["evaluation_rule_id"] == expected["rule_id"], "RULE_MISMATCH")
        _check(
            "job_configuration_id" not in meta or meta["job_configuration_id"] == expected["rule_id"], "RULE_MISMATCH"
        )
        _check(
            not expected.get("evaluator_id") or meta["evaluator_id"] == expected["evaluator_id"], "EVALUATOR_MISMATCH"
        )
        _check(
            meta["target_trace_id"] == ctx["trace_id"] and meta["target_observation_id"] == ctx["observation_id"],
            "TARGET_MISMATCH",
        )
        for field in ("input", "output"):
            _check(
                f"{field}_sha256" not in meta
                or meta[f"{field}_sha256"] == ctx[f"{field}_digest"].removeprefix("sha256:"),
                "SCORE_IO_MISMATCH",
            )
        stamp = _utc(row.get("timestamp"))
        _check(stamp is not None and _utc(obs["startTime"]) <= stamp <= datetime.now(UTC), "SCORE_TIME_INVALID")
        return coerce_typed_value(
            row.get("value"), result_type=expected["data_type"], category_values=expected.get("category_values")
        )

    def collect(self, launch_id: str, item_id: str) -> dict:
        try:
            with self.db.get_session() as session:
                ctx, contract = self._context(session, launch_id, item_id)
        except ScoresReadError as exc:
            # 无有效 Context 时不伪造关联证据；已知 Item 的诊断仍可读取。
            return self._failure(launch_id, item_id, exc.code)
        decisions = []
        try:
            obs = self._observation(ctx, contract)
            # 全部读取成功才开始采纳；后页失败不得留下部分有效结果。
            candidates = [
                (expected, self.client.scores_for(ctx["trace_id"], ctx["observation_id"], expected["name"]))
                for expected in contract["scores"]
            ]
        except ScoresReadError as exc:
            return self._save(ctx, contract, [("REJECTED", exc.code, "", None, None)], retry_after=exc.retry_after)
        for expected, rows in candidates:
            unique, eligible, conflicts = {}, [], set()
            for row in rows:
                fingerprint = canonical_digest(_score_evidence(row))
                key = row.get("id") if isinstance(row.get("id"), str) else fingerprint
                if key in unique and canonical_digest(_score_evidence(unique[key])) != fingerprint:
                    conflicts.add(key)
                unique[key] = row
            for key, row in unique.items():
                try:
                    _check(key not in conflicts, "SCORE_ID_CONFLICT")
                    value = self._verify(row, expected, ctx, contract, obs)
                    eligible.append((row, value))
                except (ScoresReadError, ResultContractError) as exc:
                    decisions.append(("REJECTED", exc.code, expected["name"], row, None))
            if conflicts:
                for row, _ in eligible:
                    decisions.append(("REJECTED", "SCORE_ID_CONFLICT", expected["name"], row, None))
            elif len(eligible) > 1:
                for row, _ in eligible:
                    decisions.append(("REJECTED", "SCORE_AMBIGUOUS", expected["name"], row, None))
            elif eligible:
                row, value = eligible[0]
                evidence = {
                    **ctx,
                    "score": _score_evidence(row),
                    "observed_at": datetime.now(UTC).isoformat(),
                    "observation": {
                        k: obs[k] for k in ("id", "traceId", "name", "type", "projectId", "environment", "startTime")
                    },
                }
                base_url = getattr(self.client, "base_url", None)
                if base_url:
                    evidence["score_url"] = (
                        f"{base_url}/project/{quote(contract['project_id'], safe='')}/traces/"
                        f"{quote(ctx['trace_id'], safe='')}?observation={quote(ctx['observation_id'], safe='')}"
                    )
                result = TypedEvaluationResult(
                    evaluator_id=expected["name"],
                    result_type=expected["data_type"],
                    status="succeeded",
                    value=value,
                    normalized_value=normalized_value_for(
                        value, result_type=expected["data_type"], normalization_rule=None
                    ),
                    provenance=ResultProvenance(
                        binding_id=contract["acceptance_digest"],
                        evaluator_id=expected["name"],
                        evaluator_version=None,
                        definition_digest=contract["acceptance_digest"],
                        executor_type="langfuse_managed",
                        manifest_schema_version=ctx["manifest_schema_version"],
                        contract_status="MANAGED_DIAGNOSTIC_ONLY",
                    ),
                    evidence=evidence,
                )
                decisions.append(("ADOPTED", "VERIFIED", expected["name"], row, result))
            if not rows:
                decisions.append(("MISSING", "SCORE_MISSING", expected["name"], None, None))
        return self._save(ctx, contract, decisions)

    def _failure(self, launch_id, item_id, code):
        with self.db.get_session() as session:
            item = session.get(ExperimentItemExecutionRecord, item_id)
            if item is None or item.launch_id != launch_id:
                raise ScoresReadError("ITEM_NOT_FOUND")
            ctx = {
                "launch_id": launch_id,
                "item_execution_id": item_id,
                "dispatch_generation": item.dispatch_generation,
            }
        return self._save(ctx, {}, [("REJECTED", code, "", None, None)])

    def _save(self, ctx, contract, decisions, retry_after=None):
        report = []
        with self.db.get_session() as session:
            # UPDATE 在 PostgreSQL / SQLite 均取得写锁；再读 Context 防 TOCTOU 和旧 generation。
            item = session.get(ExperimentItemExecutionRecord, ctx["item_execution_id"])
            _check(item is not None and item.launch_id == ctx["launch_id"], "ITEM_NOT_FOUND")
            locked = session.execute(
                update(ExperimentItemExecutionRecord)
                .where(ExperimentItemExecutionRecord.id == item.id)
                .values(id=item.id, updated_at=ExperimentItemExecutionRecord.updated_at)
            )
            _check(locked.rowcount == 1, "ITEM_NOT_FOUND")
            session.expire_all()
            current = None
            if contract:
                try:
                    current, _ = self._context(session, ctx["launch_id"], ctx["item_execution_id"])
                except ScoresReadError:
                    pass
            stale = bool(contract) and current != ctx
            for decision, reason, name, raw, result in decisions:
                if stale:
                    decision, reason, result = "REJECTED", "COLLECTION_CONTEXT_CHANGED", None
                payload = {**ctx, "score": _score_evidence(raw) if raw else None}
                if result is not None:
                    decision, reason = self._insert_result(session, ctx, contract, result)
                    payload = result.evidence
                audit_key = canonical_digest(
                    {
                        **ctx,
                        "name": name,
                        "score": payload.get("score"),
                        "decision": "ADOPTED" if decision == "ALREADY_ADOPTED" else decision,
                        "reason": reason,
                    }
                )
                audit_id = "mse_" + audit_key.removeprefix("sha256:")[:56]
                if session.get(ManagedScoreEvidenceRecord, audit_id) is None:
                    session.add(
                        ManagedScoreEvidenceRecord(
                            id=audit_id,
                            launch_id=ctx["launch_id"],
                            item_execution_id=ctx["item_execution_id"],
                            dispatch_generation=ctx["dispatch_generation"],
                            score_name=name,
                            score_id=raw.get("id")
                            if raw and isinstance(raw.get("id"), str) and len(raw["id"]) <= 128
                            else None,
                            decision="ADOPTED" if decision == "ALREADY_ADOPTED" else decision,
                            reason=reason,
                            evidence=payload,
                        )
                    )
                    session.flush()
                score_id = raw.get("id") if raw else None
                score_id = score_id if isinstance(score_id, str) and len(score_id) <= 128 else None
                report.append(
                    {
                        "score_name": name,
                        "score_id": score_id,
                        "decision": decision,
                        "reason": reason,
                        "evidence_id": audit_id,
                    }
                )
        return {
            "launch_id": ctx["launch_id"],
            "item_execution_id": ctx["item_execution_id"],
            "quality_conclusion": "unknown",
            "releasable": False,
            "retry_after": retry_after,
            "decisions": report,
        }

    def _insert_result(self, session, ctx, contract, result):
        existing = session.scalar(
            select(EvaluationResultRecord).where(
                EvaluationResultRecord.item_execution_id == ctx["item_execution_id"],
                EvaluationResultRecord.evaluator_id == result.evaluator_id,
            )
        )
        if existing:
            proof = existing.evidence or {}
            same = (
                existing.executor_type == "langfuse_managed"
                and proof.get("score") == result.evidence["score"]
                and all(proof.get(k) == ctx[k] for k in ctx)
            )
            return ("ALREADY_ADOPTED", "VERIFIED") if same else ("REJECTED", "RESULT_CONFLICT")
        row = EvaluationResultRecord(
            id="er_"
            + canonical_digest({"item": ctx["item_execution_id"], "name": result.evaluator_id}).removeprefix("sha256:")[
                :24
            ],
            item_execution_id=ctx["item_execution_id"],
            launch_id=ctx["launch_id"],
            external_project_id=contract["project_id"],
            external_score_id=result.evidence["score"]["id"],
            evaluator_id=result.evaluator_id,
            evaluator_version=None,
            result_type=result.result_type,
            status=result.status,
            value=result.value,
            normalized_value=result.normalized_value,
            evidence=result.evidence,
            binding_id=result.provenance.binding_id,
            definition_digest=result.provenance.definition_digest,
            executor_type="langfuse_managed",
            manifest_schema_version=result.provenance.manifest_schema_version,
            contract_status=result.provenance.contract_status,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            return "REJECTED", "SCORE_ALREADY_CLAIMED"
        return "ADOPTED", "VERIFIED"

    def diagnostics(self, launch_id, item_id):
        with self.db.get_session() as session:
            item = session.get(ExperimentItemExecutionRecord, item_id)
            _check(item is not None and item.launch_id == launch_id, "ITEM_NOT_FOUND")
            evidence = list(
                session.scalars(
                    select(ManagedScoreEvidenceRecord)
                    .where(ManagedScoreEvidenceRecord.item_execution_id == item_id)
                    .order_by(ManagedScoreEvidenceRecord.created_at)
                )
            )
            results = list(
                session.scalars(
                    select(EvaluationResultRecord).where(
                        EvaluationResultRecord.item_execution_id == item_id,
                        EvaluationResultRecord.executor_type == "langfuse_managed",
                    )
                )
            )
            return {
                "launch_id": launch_id,
                "item_execution_id": item_id,
                "quality_conclusion": "unknown",
                "releasable": False,
                "results": [result_to_payload(r) for r in results],
                "evidence": [
                    {
                        "id": r.id,
                        "decision": r.decision,
                        "reason": r.reason,
                        "score_name": r.score_name,
                        "score_id": r.score_id,
                        "created_at": r.created_at,
                        "evidence": r.evidence,
                    }
                    for r in evidence
                ],
            }
