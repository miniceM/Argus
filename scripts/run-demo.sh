#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

./scripts/bootstrap.sh >/tmp/agent-eval-bootstrap.json
cat /tmp/agent-eval-bootstrap.json

echo
echo "== Run baseline Agent v1 =="
curl -fsS -X POST http://localhost:18080/experiments/run \
  -H 'content-type: application/json' \
  -d '{"agent_id":"banking-agent","agent_version":"v1","dataset_name":"banking-agent-regression","experiment_name":"banking-agent-v1","max_concurrency":4}' \
  | tee /tmp/agent-eval-v1.json | python -m json.tool

echo
echo "== Run candidate Agent v2 =="
curl -fsS -X POST http://localhost:18080/experiments/run \
  -H 'content-type: application/json' \
  -d '{"agent_id":"banking-agent","agent_version":"v2","dataset_name":"banking-agent-regression","experiment_name":"banking-agent-v2","max_concurrency":4}' \
  | tee /tmp/agent-eval-v2.json | python -m json.tool

echo
echo "== Expected PoC outcome =="
echo "v1: 2/6 overall_pass (about 33.3%); v2: 6/6 overall_pass (100%)."
echo "Open Langfuse: http://localhost:3000"
echo "Login: admin@example.com / Poc-Admin-2026!  (PoC only)"
echo "Compare the two runs under Datasets/Experiments."
