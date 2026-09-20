#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

ARTIFACT_DIR="${ARTIFACT_DIR:-$ROOT/artifacts/e2e}"
mkdir -p "$ARTIFACT_DIR"

COMPOSE=(docker compose --env-file .env.cloud -f docker-compose.cloud.yml)
STATUS=0

cleanup() {
  STATUS=$?
  if [ "$STATUS" -ne 0 ]; then
    "${COMPOSE[@]}" ps >"$ARTIFACT_DIR/docker-compose-ps.txt" 2>&1 || true
    "${COMPOSE[@]}" logs --no-color >"$ARTIFACT_DIR/docker-compose.log" 2>&1 || true
  fi
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  exit "$STATUS"
}
trap cleanup EXIT

: "${LANGFUSE_BASE_URL:?LANGFUSE_BASE_URL is required}"
: "${LANGFUSE_PUBLIC_KEY:?LANGFUSE_PUBLIC_KEY is required}"
: "${LANGFUSE_SECRET_KEY:?LANGFUSE_SECRET_KEY is required}"

SUFFIX="${E2E_RUN_SUFFIX:-local-$(date +%s)}"
V1_NAME="argus-ci-v1-${SUFFIX}"
V2_NAME="argus-ci-v2-${SUFFIX}"

echo "== Start Demo Agents and Eval Runner =="
"${COMPOSE[@]}" up -d --build

"$ROOT/scripts/wait-http.sh" http://localhost:18080/health 180

echo "== Bootstrap Dataset =="
curl -fsS -X POST http://localhost:18080/admin/bootstrap   | tee "$ARTIFACT_DIR/bootstrap.json"   | python3 -m json.tool

echo "== Run baseline Agent v1 =="
curl -fsS -X POST http://localhost:18080/experiments/run   -H 'content-type: application/json'   -d "$(python3 - "$V1_NAME" <<'PY'
import json
import sys
print(json.dumps({
    "agent_id": "banking-agent",
    "agent_version": "v1",
    "dataset_name": "banking-agent-regression",
    "experiment_name": sys.argv[1],
    "max_concurrency": 4,
}))
PY
)"   | tee "$ARTIFACT_DIR/v1.json"   | python3 -m json.tool

echo "== Run candidate Agent v2 =="
curl -fsS -X POST http://localhost:18080/experiments/run   -H 'content-type: application/json'   -d "$(python3 - "$V2_NAME" <<'PY'
import json
import sys
print(json.dumps({
    "agent_id": "banking-agent",
    "agent_version": "v2",
    "dataset_name": "banking-agent-regression",
    "experiment_name": sys.argv[1],
    "max_concurrency": 4,
}))
PY
)"   | tee "$ARTIFACT_DIR/v2.json"   | python3 -m json.tool

echo "== Assert end-to-end results =="
python3 - "$ARTIFACT_DIR/bootstrap.json" "$ARTIFACT_DIR/v1.json" "$ARTIFACT_DIR/v2.json" <<'PY'
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

bootstrap = json.loads(Path(sys.argv[1]).read_text())
v1 = json.loads(Path(sys.argv[2]).read_text())
v2 = json.loads(Path(sys.argv[3]).read_text())

assert bootstrap["items_upserted"] == 6, bootstrap

def assert_run(payload: dict, expected_passed: int, expected_rate: float) -> None:
    result = payload["result"]
    items = result.get("items") or []
    assert len(items) == 6, f"expected 6 item results, got {len(items)}"

    passed = 0
    for item in items:
        score = (item.get("scores") or {}).get("overall_pass")
        assert score is not None, f"missing overall_pass score: {item}"
        passed += int(float(score) == 1.0)

    run_rate = (result.get("run_scores") or {}).get("overall_pass_rate")
    assert run_rate is not None, f"missing overall_pass_rate: {result}"
    assert passed == expected_passed, f"expected {expected_passed}/6 passed, got {passed}/6"
    assert math.isclose(float(run_rate), expected_rate, abs_tol=1e-9), (
        f"expected run rate {expected_rate}, got {run_rate}"
    )

    dataset_run_url = payload.get("dataset_run_url")
    assert dataset_run_url, "Langfuse dataset_run_url must be present"

assert_run(v1, 2, 2 / 6)
assert_run(v2, 6, 1.0)

print("Langfuse Cloud E2E: PASS")
print("v1 = 2/6; v2 = 6/6")
print(f"v1 run: {v1.get('dataset_run_url')}")
print(f"v2 run: {v2.get('dataset_run_url')}")
PY
