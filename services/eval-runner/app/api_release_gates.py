"""发布策略只追加版本，Gate 一旦保存即固定；不提供未经授权的 Override。"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db_models import AgentRecord, ReleaseGateRecord, ReleasePolicyRecord, RunResultSnapshotRecord
from .evaluator_binding import canonical_digest
from .release_gates import ENGINE_VERSION, GateEvaluation, ReleasePolicy, evaluate_gate, frozen_environment

router = APIRouter(prefix="/api/v1", tags=["Release Gates"])


def _db_manager():
    from .main import db_manager
    return db_manager


class ReleasePolicyResponse(ReleasePolicy):
    id: str
    policy_digest: str


class EvaluateGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_name: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    candidate_launch_id: str = Field(min_length=1, max_length=64)
    candidate_snapshot_id: str = Field(min_length=1, max_length=64)


class ReleaseGateResponse(GateEvaluation):
    id: str
    created_at: datetime
    policy: ReleasePolicy
    policy_digest: str
    candidate_launch_id: str
    candidate_revision: int
    candidate_manifest_digest: str
    candidate_source_result_digest: str
    baseline_revision: int | None = None
    baseline_manifest_digest: str | None = None
    baseline_source_result_digest: str | None = None
    report_url: str
    comparison_url: str


def _policy(session, name, version):
    return session.scalars(select(ReleasePolicyRecord).where(
        ReleasePolicyRecord.name == name, ReleasePolicyRecord.version == version,
    )).first()


def _policy_response(record):
    return ReleasePolicyResponse(**record.definition, id=record.id, policy_digest=record.policy_digest)


@router.post("/release-policies", response_model=ReleasePolicyResponse, status_code=201)
def create_release_policy(payload: ReleasePolicy) -> ReleasePolicyResponse:
    manager = _db_manager()
    try:
        with manager.get_session() as session:
            agent = session.scalar(select(AgentRecord).where(AgentRecord.id == payload.agent_id).with_for_update())
            if agent is None:
                raise HTTPException(404, "Agent not found")
            if agent.status != "active":
                raise HTTPException(409, "Agent is inactive or being purged")
            existing = _policy(session, payload.name, payload.version)
            if existing:
                if existing.policy_digest != payload.content_digest:
                    raise HTTPException(409, "ReleasePolicy version is immutable; create a new version")
                return _policy_response(existing)
            record = ReleasePolicyRecord(
                id=str(uuid.uuid4()), name=payload.name, version=payload.version,
                agent_id=payload.agent_id,
                definition=payload.model_dump(mode="json"), policy_digest=payload.content_digest,
            )
            session.add(record)
            session.commit()
            return _policy_response(record)
    except IntegrityError:
        # 并发提交同版本采用相同的幂等/冲突语义，而非 500。
        with manager.get_session() as session:
            winner = _policy(session, payload.name, payload.version)
            if winner and winner.policy_digest == payload.content_digest:
                return _policy_response(winner)
        raise HTTPException(409, "ReleasePolicy version conflict") from None


@router.get("/release-policies", response_model=ReleasePolicyResponse)
def get_release_policy(name: str = Query(...), version: str = Query(...)) -> ReleasePolicyResponse:
    with _db_manager().get_session() as session:
        record = _policy(session, name, version)
        if record is None:
            raise HTTPException(404, "ReleasePolicy version not found")
        return _policy_response(record)


@router.post("/release-gates/evaluate", response_model=ReleaseGateResponse, status_code=201)
def create_release_gate(payload: EvaluateGateRequest) -> ReleaseGateResponse:
    manager = _db_manager()
    request_digest = ""
    try:
        with manager.get_session() as session:
            policy_record = _policy(session, payload.policy_name, payload.policy_version)
            if policy_record is None:
                raise HTTPException(404, "ReleasePolicy version not found")
            candidate = session.get(RunResultSnapshotRecord, payload.candidate_snapshot_id)
            if candidate is None or candidate.launch_id != payload.candidate_launch_id:
                raise HTTPException(404, "Candidate result snapshot not found")
            policy = ReleasePolicy.model_validate(policy_record.definition)
            environment = frozen_environment(candidate)
            if candidate.agent_id != policy.agent_id or (environment is not None and environment != policy.environment):
                raise HTTPException(409, "Candidate Agent/environment does not match ReleasePolicy scope")
            request_digest = canonical_digest({
                "candidate_snapshot_id": candidate.id, "policy_digest": policy.content_digest,
                "engine_version": ENGINE_VERSION,
            })
            existing = session.scalars(select(ReleaseGateRecord).where(
                ReleaseGateRecord.request_digest == request_digest,
            )).first()
            if existing:
                return ReleaseGateResponse.model_validate(existing.result)
            baseline_id = candidate.manifest.get("comparison", {}).get("baseline_snapshot_id")
            baseline = session.get(RunResultSnapshotRecord, baseline_id) if baseline_id else None
            evaluated = evaluate_gate(policy, candidate, baseline)
            console_base = os.getenv("ARGUS_CONSOLE_BASE_URL", "http://localhost:18083").rstrip("/")
            console_origin = urlsplit(console_base)
            if console_origin.scheme not in {"http", "https"} or not console_origin.hostname or console_origin.username or console_origin.password or console_origin.path or console_origin.query or console_origin.fragment:
                raise HTTPException(503, "Console base URL is invalid")
            report_url = f"{console_base}/launches/{quote(candidate.launch_id, safe='')}?snapshot_id={quote(candidate.id, safe='')}"
            result = ReleaseGateResponse(
                **evaluated.model_dump(), id=str(uuid.uuid4()), created_at=datetime.now(UTC),
                policy=policy, policy_digest=policy.content_digest, candidate_launch_id=candidate.launch_id,
                candidate_revision=candidate.revision, candidate_manifest_digest=candidate.manifest_digest,
                candidate_source_result_digest=candidate.source_result_digest,
                baseline_revision=baseline.revision if baseline else None,
                baseline_manifest_digest=baseline.manifest_digest if baseline else None,
                baseline_source_result_digest=baseline.source_result_digest if baseline else None,
                report_url=report_url, comparison_url=report_url + "&tab=comparison",
            )
            session.add(ReleaseGateRecord(
                id=result.id, policy_id=policy_record.id, candidate_snapshot_id=candidate.id,
                baseline_snapshot_id=baseline.id if baseline else None,
                request_digest=request_digest, result=result.model_dump(mode="json"),
            ))
            session.commit()
            return result
    except IntegrityError:
        with manager.get_session() as session:
            winner = session.scalars(select(ReleaseGateRecord).where(
                ReleaseGateRecord.request_digest == request_digest,
            )).first()
            if winner:
                return ReleaseGateResponse.model_validate(winner.result)
        raise HTTPException(409, "Release Gate evidence changed or was removed; retry with a fixed snapshot") from None


@router.get("/release-gates/{gate_id}", response_model=ReleaseGateResponse)
def get_release_gate(gate_id: str) -> ReleaseGateResponse:
    with _db_manager().get_session() as session:
        record = session.get(ReleaseGateRecord, gate_id)
        if record is None:
            raise HTTPException(404, "Release Gate not found")
        return ReleaseGateResponse.model_validate(record.result)
