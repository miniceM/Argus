from __future__ import annotations

from fastapi import APIRouter

from .evaluators import default_evaluator_registry
from .models import EvaluatorResponse

router = APIRouter(prefix="/api/v1", tags=["Evaluators"])


@router.get(
    "/evaluators",
    response_model=list[EvaluatorResponse],
    summary="List all registered Evaluators and their specifications",
)
def list_evaluators() -> list[EvaluatorResponse]:
    specs = default_evaluator_registry.list_specs()
    return [EvaluatorResponse.model_validate(s) for s in specs]
