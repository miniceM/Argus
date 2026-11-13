"""只读发现 Langfuse 可用托管 Score 候选；不创建/修改远端配置，不替代执行后采纳校验。"""

from __future__ import annotations

import copy
import math
import time
from typing import Any

from .langfuse_scores_client import ScoresV3Client

CANDIDATES_CACHE_TTL_SECONDS = 30.0
ALLOWED_SCORE_TYPES = frozenset({"boolean", "numeric", "categorical", "text"})
TARGET_OBSERVATION_NAME = "remote-agent-http"

_CANDIDATES_CACHE: dict[tuple[Any, ...], tuple[float, list[dict[str, Any]]]] = {}


def clear_candidates_cache() -> None:
    _CANDIDATES_CACHE.clear()


def _column_filter_matches(filters: Any, column: str, expected: str) -> bool:
    if isinstance(filters, dict):
        val = filters.get(column)
        if isinstance(val, str):
            return val == expected
        if isinstance(val, list):
            return expected in [str(v) for v in val if isinstance(v, str)]
        return False
    if not isinstance(filters, list):
        return False
    matched_column = [f for f in filters if isinstance(f, dict) and f.get("column") == column]
    if not matched_column:
        return False
    for cond in matched_column:
        op = str(cond.get("operator") or "any of").strip().lower()
        val = cond.get("value")
        values = [str(v) for v in val if isinstance(v, str)] if isinstance(val, list) else ([val] if isinstance(val, str) else [])
        if op not in ("any of", "in", "=", "==", "equals", "is") or expected not in values:
            return False
    return True


def _normalize_data_type(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    return normalized if normalized in ALLOWED_SCORE_TYPES else None


def _latest_verified_score(
    scores: list[dict[str, Any]],
    *,
    rule_id: str,
    evaluator_id: str,
    environment: str,
) -> tuple[str | None, str | None, bool]:
    candidates: list[dict[str, Any]] = []
    for row in scores:
        if not isinstance(row, dict):
            continue
        if row.get("source") != "EVAL" or row.get("environment") != environment:
            continue
        meta = row.get("metadata")
        if not isinstance(meta, dict):
            continue
        if meta.get("evaluation_rule_id") != rule_id:
            continue
        meta_eval_id = meta.get("evaluator_id")
        if meta_eval_id is not None and meta_eval_id != evaluator_id:
            continue
        name = row.get("name")
        dt = _normalize_data_type(row.get("dataType"))
        if isinstance(name, str) and name.strip() and name == name.strip() and dt is not None:
            candidates.append(row)
    if not candidates:
        return None, None, False
    candidates.sort(key=lambda r: str(r.get("timestamp") or r.get("createdAt") or ""), reverse=True)
    chosen = candidates[0]
    return str(chosen["name"]).strip(), _normalize_data_type(chosen.get("dataType")), True


def discover_managed_score_candidates(
    client: ScoresV3Client,
    *,
    environment: str,
    cache_key: tuple[Any, ...] | None = None,
    ttl_seconds: float = CANDIDATES_CACHE_TTL_SECONDS,
) -> list[dict[str, Any]]:
    now = time.monotonic()
    if cache_key is not None:
        cached = _CANDIDATES_CACHE.get(cache_key)
        if cached is not None and (now - cached[0]) < ttl_seconds:
            return copy.deepcopy(cached[1])

    rules = client.evaluation_rules()
    evaluators = client.evaluators()
    scores = client.eval_scores(environment)

    evaluators_by_id: dict[str, dict[str, Any]] = {
        str(ev["id"]): ev
        for ev in evaluators
        if isinstance(ev, dict) and isinstance(ev.get("id"), str) and ev["id"].strip()
    }

    results: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            continue
        if rule.get("enabled") is not True:
            continue
        sampling = rule.get("sampling")
        if isinstance(sampling, bool) or not isinstance(sampling, (int, float)) or not math.isfinite(sampling) or sampling <= 0:
            continue
        filters = rule.get("filter")
        if not _column_filter_matches(filters, "name", TARGET_OBSERVATION_NAME):
            continue
        if not _column_filter_matches(filters, "environment", environment):
            continue

        assignments = rule.get("evaluatorAssignments")
        if not isinstance(assignments, list) or not assignments:
            continue

        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            evaluator_id = assignment.get("evaluatorId")
            if not isinstance(evaluator_id, str) or not evaluator_id.strip():
                continue
            ev = evaluators_by_id.get(evaluator_id)
            if ev is not None:
                if ev.get("status") not in (None, "active") or ev.get("pausedAt") is not None:
                    continue
                evaluator_name = str(ev.get("name") or evaluator_id)
                evaluator_type = str(ev.get("type") or "unknown")
                output_def = ev.get("outputDefinition")
                declared_dt = _normalize_data_type(output_def.get("dataType")) if isinstance(output_def, dict) else None
            else:
                evaluator_name = evaluator_id
                evaluator_type = "unknown"
                declared_dt = None

            score_name, verified_dt, verified = _latest_verified_score(
                scores,
                rule_id=rule_id,
                evaluator_id=evaluator_id,
                environment=environment,
            )
            results.append(
                {
                    "rule_id": rule_id,
                    "evaluator_id": evaluator_id,
                    "evaluator_name": evaluator_name,
                    "evaluator_type": evaluator_type,
                    "data_type": verified_dt if verified else declared_dt,
                    "score_name": score_name if verified else None,
                    "verified": verified,
                    "enabled": True,
                    "sampling": float(sampling),
                }
            )

    if cache_key is not None:
        _CANDIDATES_CACHE[cache_key] = (now, copy.deepcopy(results))
    return results
