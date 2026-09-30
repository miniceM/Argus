from __future__ import annotations

from fastapi import APIRouter

from .evaluators import default_evaluator_registry
from .models import EvaluatorResponse

router = APIRouter(prefix="/api/v1", tags=["Evaluators"])


@router.get(
    "/evaluators",
    response_model=list[EvaluatorResponse],
    summary="List registered Evaluators with their immutable versions and release eligibility",
)
def list_evaluators() -> list[EvaluatorResponse]:
    """Return the Evaluator catalog.

    Each entry carries the stable definition identity plus every immutable
    version the caller may select from. The user must confirm an exact version;
    there is deliberately no ``latest`` alias (Issue #80).
    """
    catalog = default_evaluator_registry.list_versions()
    return [EvaluatorResponse.model_validate(entry) for entry in catalog]
