from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, status

from .baselines import BaselineConflictError, get_baseline, set_baseline
from .models import BaselineCreateRequest, BaselineResponse

router = APIRouter(prefix="/api/v1/agents/{agent_id}/baselines", tags=["Baselines"])


def _services():
    from .main import db_manager

    return db_manager


def _response(db_manager, binding, snapshot) -> BaselineResponse:
    from .db_models import ExperimentLaunchRecord

    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Baseline result snapshot not found")
    with db_manager.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, snapshot.launch_id)
        if not launch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Baseline Launch not found")
        return BaselineResponse(
            agent_id=binding.agent_id,
            environment=binding.environment,
            result_snapshot_id=snapshot.id,
            revision=binding.revision,
            # Issue #85: report the frozen result revision explicitly next to
            # the binding revision, so a success response never implies "latest".
            result_revision=snapshot.revision,
            result_evidence_state=(snapshot.evidence_state or "COMPLETE").upper(),
            updated_by=binding.updated_by,
            updated_at=binding.updated_at,
            launch_id=launch.id,
            agent_version=launch.agent_version,
            dataset_name=launch.dataset_name,
            dataset_version=launch.dataset_version,
            summary=snapshot.summary,
        )


@router.get("", response_model=BaselineResponse, summary="Get the current Agent/environment Baseline")
def get_agent_baseline(
    agent_id: str,
    environment: str = Query(default="production"),
) -> BaselineResponse:
    try:
        db_manager = _services()
        baseline = get_baseline(db_manager, agent_id, environment)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not baseline:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No Baseline is bound for this Agent/environment")
    return _response(db_manager, *baseline)


@router.post("", response_model=BaselineResponse, status_code=status.HTTP_200_OK, summary="Bind a completed result as the current Baseline")
def put_agent_baseline(
    agent_id: str,
    payload: BaselineCreateRequest,
    x_user: str | None = Header(default=None, alias="X-User"),
) -> BaselineResponse:
    try:
        db_manager = _services()
        binding = set_baseline(
            db_manager,
            agent_id=agent_id,
            environment=payload.environment,
            result_snapshot_id=payload.result_snapshot_id,
            expected_revision=payload.expected_revision,
            updated_by=x_user,
        )
        from .db_models import RunResultSnapshotRecord

        with db_manager.get_session() as session:
            snapshot = session.get(RunResultSnapshotRecord, binding.result_snapshot_id)
        return _response(db_manager, binding, snapshot)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BaselineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
