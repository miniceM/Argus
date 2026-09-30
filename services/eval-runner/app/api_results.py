from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from .aggregation import aggregate_run, compare_case_results
from .comparison_contracts import assess_comparability
from .costs import compare_costs
from .db_models import ExperimentLaunchRecord, RunResultSnapshotRecord
from .models import (
    ComparisonComparability,
    ComparisonContractDimension,
    ComparisonDiagnostic,
    ComparisonFormalVerdict,
    ComparisonResponse,
    ResultSnapshotDetailResponse,
    ResultSnapshotListResponse,
    ResultSnapshotRevisionResponse,
    RunSummaryResponse,
)
from .result_outputs import ComparisonCaseOutputResponse, fetch_observation_output
from .result_snapshots import (
    EVIDENCE_COMPLETE,
    create_result_snapshot,
    latest_result_snapshot,
    list_result_snapshots,
)

router = APIRouter(prefix="/api/v1/experiment-launches", tags=["Evaluation Results"])


def _db_manager():
    from .main import db_manager

    return db_manager


def _versions(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset": {
            "name": manifest.get("dataset", {}).get("dataset_name"),
            "version": manifest.get("dataset", {}).get("dataset_version"),
            "digest": manifest.get("dataset", {}).get("snapshot_digest"),
        },
        "agent": {
            "id": manifest.get("agent", {}).get("agent_id"),
            "version": manifest.get("agent", {}).get("version"),
            "spec_digest": manifest.get("agent", {}).get("spec_digest"),
            "artifact_ref": manifest.get("agent", {}).get("artifact_ref"),
        },
        "evaluators": manifest.get("evaluators", []),
        "runner": manifest.get("runner", {}),
        "environment": manifest.get("comparison", {}).get("environment", "production"),
    }


def _get_snapshot(
    session, launch_id: str, snapshot_id: str | None = None
) -> tuple[ExperimentLaunchRecord, RunResultSnapshotRecord]:
    launch = session.get(ExperimentLaunchRecord, launch_id)
    if not launch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Launch '{launch_id}' not found")
    if snapshot_id is not None:
        snapshot = session.scalars(
            select(RunResultSnapshotRecord).where(
                RunResultSnapshotRecord.id == snapshot_id,
                RunResultSnapshotRecord.launch_id == launch_id,
            )
        ).first()
        if snapshot is None:
            # Do not reveal whether an ID exists under another Launch.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Result snapshot not found")
        return launch, snapshot

    snapshot = latest_result_snapshot(session, launch_id)
    if snapshot is None and launch.status in {"COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLED"}:
        snapshot = create_result_snapshot(session, launch)
        session.commit()
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Launch result snapshot is not ready")
    return launch, snapshot


def _dataset_identity(manifest: dict[str, Any]) -> tuple[str, str] | None:
    dataset = manifest.get("dataset") or {}
    source = dataset.get("source")
    dataset_id = dataset.get("dataset_id")
    if not isinstance(source, str) or not source.strip() or not isinstance(dataset_id, str) or not dataset_id.strip():
        return None
    return source.strip(), dataset_id.strip()


@router.get("/{launch_id}/summary", response_model=RunSummaryResponse, summary="Get a stable run-level evaluation summary")
def get_run_summary(
    launch_id: str, snapshot_id: str | None = None
) -> RunSummaryResponse:
    with _db_manager().get_session() as session:
        _, snapshot = _get_snapshot(session, launch_id, snapshot_id)
        from .db_models import LangfuseRunScoreTaskRecord

        score_task = session.scalars(select(LangfuseRunScoreTaskRecord).where(
            LangfuseRunScoreTaskRecord.snapshot_id == snapshot.id
        )).first()
        score_sync_status = score_task.status if score_task else (
            "NOT_APPLICABLE" if snapshot.manifest.get("dataset", {}).get("source") != "langfuse" else "PENDING"
        )
        return RunSummaryResponse(
            launch_id=snapshot.launch_id,
            snapshot_id=snapshot.id,
            revision=snapshot.revision,
            created_at=snapshot.created_at,
            manifest_digest=snapshot.manifest_digest,
            source_result_digest=snapshot.source_result_digest,
            evidence_state=(snapshot.evidence_state or EVIDENCE_COMPLETE).upper(),
            evidence_reasons=list(snapshot.evidence_reasons or []),
            versions=_versions(snapshot.manifest),
            summary=snapshot.summary,
            langfuse_score_sync_status=score_sync_status,
        )


def _revision_row(snapshot: RunResultSnapshotRecord, is_latest: bool) -> ResultSnapshotRevisionResponse:
    summary = snapshot.summary or {}
    return ResultSnapshotRevisionResponse(
        snapshot_id=snapshot.id,
        revision=snapshot.revision,
        created_at=snapshot.created_at,
        source_result_digest=snapshot.source_result_digest,
        manifest_digest=snapshot.manifest_digest,
        evidence_state=(snapshot.evidence_state or EVIDENCE_COMPLETE).upper(),
        evidence_reasons=list(snapshot.evidence_reasons or []),
        total_cases=int(summary.get("total_cases", 0) or 0),
        quality_pass_count=int(summary.get("quality_pass_count", 0) or 0),
        quality_fail_count=int(summary.get("quality_fail_count", 0) or 0),
        quality_unknown_count=int(summary.get("quality_unknown_count", 0) or 0),
        is_latest=is_latest,
    )


@router.get(
    "/{launch_id}/result-snapshots",
    response_model=ResultSnapshotListResponse,
    summary="List every frozen result revision of a Launch (Issue #85)",
)
def list_launch_result_snapshots(launch_id: str) -> ResultSnapshotListResponse:
    """Every revision ever frozen for this Launch, newest first.

    Issue #85: a re-evaluation creates a new revision and never rewrites an old
    one, so the history is the unit a user browses and shares.
    """
    with _db_manager().get_session() as session:
        if session.get(ExperimentLaunchRecord, launch_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Launch '{launch_id}' not found")
        snapshots = list_result_snapshots(session, launch_id)
        return ResultSnapshotListResponse(
            launch_id=launch_id,
            latest_snapshot_id=snapshots[0].id if snapshots else None,
            latest_revision=snapshots[0].revision if snapshots else None,
            revisions=[_revision_row(snap, index == 0) for index, snap in enumerate(snapshots)],
        )


@router.get(
    "/{launch_id}/result-snapshots/{snapshot_id}",
    response_model=ResultSnapshotDetailResponse,
    summary="Read one immutable result revision by its own id (Issue #85)",
)
def get_launch_result_snapshot(launch_id: str, snapshot_id: str) -> ResultSnapshotDetailResponse:
    """The shareable, fixed view: this id always returns these exact results.

    Nothing here falls back to "latest", so a shared link cannot drift when the
    Launch is re-evaluated and a newer revision is frozen.
    """
    with _db_manager().get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        if not launch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Launch '{launch_id}' not found")
        snapshot = session.scalars(
            select(RunResultSnapshotRecord).where(
                RunResultSnapshotRecord.id == snapshot_id,
                RunResultSnapshotRecord.launch_id == launch_id,
            )
        ).first()
        if snapshot is None:
            # Do not reveal whether an ID exists under another Launch.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Result snapshot not found")
        evidence_state = (snapshot.evidence_state or EVIDENCE_COMPLETE).upper()
        return ResultSnapshotDetailResponse(
            launch_id=launch_id,
            snapshot_id=snapshot.id,
            revision=snapshot.revision,
            created_at=snapshot.created_at,
            source_result_digest=snapshot.source_result_digest,
            manifest_digest=snapshot.manifest_digest,
            evidence_state=evidence_state,
            evidence_reasons=list(snapshot.evidence_reasons or []),
            releasable=evidence_state == EVIDENCE_COMPLETE,
            versions=_versions(snapshot.manifest),
            summary=snapshot.summary,
            items=list(snapshot.items or []),
        )



def _formal_verdict(
    *,
    comparability,
    classification_counts: Counter,
    required_cases: int,
    baseline_snapshot,
    candidate_snapshot,
    baseline_bound: bool,
) -> ComparisonFormalVerdict:
    """Withhold a run-level verdict unless every required case is supported.

    Issue #86: a formal 'no regression' must never be inferred from a handful of
    comparable cases, a moved contract, or an incomplete evidence state.
    """
    comparable_cases = sum(
        value for key, value in classification_counts.items() if key != "NOT_COMPARABLE"
    )
    withheld: list[str] = list(comparability.reason_codes)
    if not baseline_bound:
        withheld.append("BASELINE_NOT_BOUND")
    for label, snapshot in (("BASELINE", baseline_snapshot), ("CANDIDATE", candidate_snapshot)):
        state = ((snapshot.evidence_state if snapshot else None) or EVIDENCE_COMPLETE).upper()
        if state != EVIDENCE_COMPLETE:
            withheld.append(f"{label}_EVIDENCE_INCOMPLETE")
    if required_cases and comparable_cases < required_cases:
        withheld.append("COVERAGE_INCOMPLETE")

    coverage = (comparable_cases / required_cases) if required_cases else 0.0
    if withheld:
        return ComparisonFormalVerdict(
            available=False,
            verdict=None,
            reason=None,
            required_cases=required_cases,
            comparable_cases=comparable_cases,
            coverage=coverage,
            withheld_reasons=list(dict.fromkeys(withheld)),
        )
    if classification_counts.get("REGRESSION"):
        verdict, reason = "REGRESSION", "CASE_REGRESSION"
    elif classification_counts.get("IMPROVEMENT"):
        verdict, reason = "IMPROVEMENT", "CASE_IMPROVEMENT"
    else:
        verdict, reason = "UNCHANGED", "NO_QUALITY_CHANGE"
    return ComparisonFormalVerdict(
        available=True,
        verdict=verdict,
        reason=reason,
        required_cases=required_cases,
        comparable_cases=comparable_cases,
        coverage=coverage,
        withheld_reasons=[],
    )


@router.get("/{launch_id}/comparison", response_model=ComparisonResponse, summary="Compare a Candidate against its frozen Baseline")
def get_launch_comparison(
    launch_id: str,
    snapshot_id: str | None = None,
    classification: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: int = Query(default=0, ge=0),
) -> ComparisonResponse:
    allowed = {"REGRESSION", "IMPROVEMENT", "UNCHANGED", "NOT_COMPARABLE"}
    if classification and classification.upper() not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"classification must be one of {sorted(allowed)}")

    with _db_manager().get_session() as session:
        candidate_launch, candidate_snapshot = _get_snapshot(session, launch_id, snapshot_id)
        comparison_manifest = candidate_snapshot.manifest.get("comparison", {})
        baseline_id = comparison_manifest.get("baseline_snapshot_id")
        baseline_snapshot = session.get(RunResultSnapshotRecord, baseline_id) if baseline_id else None
        if baseline_id and baseline_snapshot is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Frozen Baseline result snapshot is unavailable")
        baseline_launch = session.get(ExperimentLaunchRecord, baseline_snapshot.launch_id) if baseline_snapshot else None

        baseline_items = {item["dataset_item_id"]: item for item in (baseline_snapshot.items if baseline_snapshot else [])}
        candidate_items = {item["dataset_item_id"]: item for item in candidate_snapshot.items}
        all_case_ids = sorted(set(baseline_items) | set(candidate_items))
        candidate_dataset_identity = _dataset_identity(candidate_snapshot.manifest)
        baseline_dataset_identity = _dataset_identity(baseline_snapshot.manifest) if baseline_snapshot else None
        dataset_identity_error = None
        if baseline_snapshot:
            if candidate_dataset_identity is None or baseline_dataset_identity is None:
                dataset_identity_error = "DATASET_IDENTITY_UNKNOWN"
            elif candidate_dataset_identity != baseline_dataset_identity:
                dataset_identity_error = "DATASET_IDENTITY_MISMATCH"
        # Issue #86: a formal comparison requires identical Measurement, Quality
        # Policy and Aggregation/Comparison contracts on both sides.
        comparability = assess_comparability(
            baseline_snapshot.manifest if baseline_snapshot else None,
            candidate_snapshot.manifest,
        )
        evaluator_specs = candidate_snapshot.manifest.get("evaluators", [])
        baseline_evaluators = baseline_snapshot.manifest.get("evaluators", []) if baseline_snapshot else []
        diffs: list[dict[str, Any]] = []
        comparable_baseline: list[dict[str, Any]] = []
        comparable_candidate: list[dict[str, Any]] = []
        classification_counts: Counter[str] = Counter()
        for case_id in all_case_ids:
            base_case = baseline_items.get(case_id)
            candidate_case = candidate_items.get(case_id)
            if baseline_snapshot is None:
                diff = compare_case_results(None, candidate_case, evaluator_specs=evaluator_specs, baseline_evaluators=baseline_evaluators)
                diff["reason"] = "BASELINE_NOT_BOUND"
            else:
                diff = compare_case_results(
                    base_case,
                    candidate_case,
                    evaluator_specs=evaluator_specs,
                    baseline_evaluators=baseline_evaluators,
                )
                if dataset_identity_error:
                    diff.update(classification="NOT_COMPARABLE", reason=dataset_identity_error)
            diff["baseline_output_ref"] = base_case.get("output_ref") if base_case else None
            diff["candidate_output_ref"] = candidate_case.get("output_ref") if candidate_case else None
            diff["baseline_experiment_url"] = baseline_launch.langfuse_experiment_url if baseline_launch else None
            diff["candidate_experiment_url"] = candidate_launch.langfuse_experiment_url
            classification_counts[diff["classification"]] += 1
            diff["basis"] = (
                "FORMAL"
                if comparability.comparable and diff["classification"] != "NOT_COMPARABLE"
                else "DIAGNOSTIC_ONLY"
            )
            if diff["classification"] != "NOT_COMPARABLE" and base_case and candidate_case:
                comparable_baseline.append(base_case)
                comparable_candidate.append(candidate_case)
            diffs.append(diff)

        if classification:
            diffs = [item for item in diffs if item["classification"] == classification.upper()]
        page = diffs[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < len(diffs) else None

        comparable_count = sum(value for key, value in classification_counts.items() if key != "NOT_COMPARABLE")
        summary = {
            "candidate": candidate_snapshot.summary,
            "baseline": baseline_snapshot.summary if baseline_snapshot else None,
            "comparable_case_count": comparable_count,
            "classification_counts": dict(classification_counts),
        }
        if baseline_snapshot:
            baseline_common = aggregate_run(comparable_baseline, baseline_evaluators)
            candidate_common = aggregate_run(comparable_candidate, evaluator_specs)
            summary["comparable_cohort"] = {
                "baseline": baseline_common,
                "candidate": candidate_common,
            }
            summary["cost_comparison"] = compare_costs(
                baseline_common, candidate_common, len(comparable_baseline)
            )
            baseline_pass = baseline_common.get("pass_rate")
            candidate_pass = candidate_common.get("pass_rate")
            summary["pass_rate_delta"] = (
                candidate_pass - baseline_pass
                if isinstance(candidate_pass, (int, float)) and isinstance(baseline_pass, (int, float))
                else None
            )
            baseline_core = baseline_common.get("score_means", {})
            candidate_core = candidate_common.get("score_means", {})
            summary["score_mean_deltas"] = {
                key: candidate_core[key] - baseline_core[key]
                for key in set(baseline_core) & set(candidate_core)
                if isinstance(candidate_core[key], (int, float)) and isinstance(baseline_core[key], (int, float))
            }
        else:
            summary["comparable_cohort"] = None
            summary["cost_comparison"] = compare_costs(
                None, candidate_snapshot.summary, int(candidate_snapshot.summary.get("total_cases", 0))
            )
            summary["pass_rate_delta"] = None
            summary["score_mean_deltas"] = {}

        formal = _formal_verdict(
            comparability=comparability,
            classification_counts=classification_counts,
            required_cases=len(all_case_ids),
            baseline_snapshot=baseline_snapshot,
            candidate_snapshot=candidate_snapshot,
            baseline_bound=baseline_snapshot is not None,
        )
        return ComparisonResponse(
            launch_id=launch_id,
            candidate_snapshot_id=candidate_snapshot.id,
            baseline_snapshot_id=baseline_snapshot.id if baseline_snapshot else None,
            baseline_binding_revision=comparison_manifest.get("baseline_binding_revision"),
            versions={"candidate": _versions(candidate_snapshot.manifest), "baseline": _versions(baseline_snapshot.manifest) if baseline_snapshot else None},
            summary=summary,
            classification_counts=dict(classification_counts),
            comparability=ComparisonComparability(
                comparable=comparability.comparable,
                reason_codes=list(comparability.reason_codes),
                provenance=comparability.provenance,
                dimensions=[
                    ComparisonContractDimension(**dim.to_payload()) for dim in comparability.dimensions
                ],
                suggestions=list(comparability.suggestions),
            ),
            formal=formal,
            diagnostic=ComparisonDiagnostic(
                note="仅供诊断，不作为正式发布比较。",
                comparable_cases=sum(
                    value for key, value in classification_counts.items() if key != "NOT_COMPARABLE"
                ),
                classification_counts=dict(classification_counts),
            ),
            items=page,
            next_cursor=next_cursor,
        )


@router.get(
    "/{launch_id}/comparison/case",
    response_model=ComparisonCaseOutputResponse,
    summary="Read frozen baseline and candidate outputs for one comparison case",
)
def get_comparison_case(
    launch_id: str,
    snapshot_id: str = Query(..., min_length=1),
    dataset_item_id: str = Query(..., min_length=1),
) -> ComparisonCaseOutputResponse:
    with _db_manager().get_session() as session:
        candidate_launch, candidate_snapshot = _get_snapshot(session, launch_id, snapshot_id)
        candidate_case = next(
            (item for item in candidate_snapshot.items if item.get("dataset_item_id") == dataset_item_id),
            None,
        )

        comparison_manifest = candidate_snapshot.manifest.get("comparison", {})
        baseline_id = comparison_manifest.get("baseline_snapshot_id")
        baseline_snapshot = session.get(RunResultSnapshotRecord, baseline_id) if baseline_id else None
        if baseline_id and baseline_snapshot is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Frozen Baseline result snapshot is unavailable")
        baseline_launch = session.get(ExperimentLaunchRecord, baseline_snapshot.launch_id) if baseline_snapshot else None
        baseline_case = next(
            (item for item in (baseline_snapshot.items if baseline_snapshot else [])
             if item.get("dataset_item_id") == dataset_item_id),
            None,
        )
        if candidate_case is None and baseline_case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comparison case not found")

        if baseline_snapshot is None:
            classification, reason = "NOT_COMPARABLE", "BASELINE_NOT_BOUND"
        else:
            baseline_identity = _dataset_identity(baseline_snapshot.manifest)
            candidate_identity = _dataset_identity(candidate_snapshot.manifest)
            if baseline_identity is None or candidate_identity is None:
                classification, reason = "NOT_COMPARABLE", "DATASET_IDENTITY_UNKNOWN"
            elif baseline_identity != candidate_identity:
                classification, reason = "NOT_COMPARABLE", "DATASET_IDENTITY_MISMATCH"
            else:
                diff = compare_case_results(
                    baseline_case,
                    candidate_case,
                    evaluator_specs=candidate_snapshot.manifest.get("evaluators", []),
                    baseline_evaluators=baseline_snapshot.manifest.get("evaluators", []),
                )
                classification, reason = diff["classification"], diff.get("reason")

        baseline_output = fetch_observation_output(
            baseline_case.get("output_ref") if baseline_case else None,
            scores=baseline_case.get("scores") if baseline_case else {},
            trace_url=baseline_case.get("trace_url") if baseline_case else (baseline_launch.langfuse_experiment_url if baseline_launch else None),
        )
        candidate_output = fetch_observation_output(
            candidate_case.get("output_ref") if candidate_case else None,
            scores=candidate_case.get("scores") if candidate_case else {},
            trace_url=(candidate_case.get("trace_url") if candidate_case else None)
            or candidate_launch.langfuse_experiment_url,
        )
        return ComparisonCaseOutputResponse(
            launch_id=launch_id,
            candidate_snapshot_id=candidate_snapshot.id,
            baseline_snapshot_id=baseline_snapshot.id if baseline_snapshot else None,
            dataset_item_id=dataset_item_id,
            classification=classification,
            reason=reason,
            baseline=baseline_output,
            candidate=candidate_output,
        )
