#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

# 方案 B：Langfuse 使用 Cloud，本地只跑 demo-agent + eval-runner。
# 与 run-demo.sh 的唯一区别：不等待本地 Langfuse Web（localhost:3000），
# 因为 Dataset/Experiment 直接写入 Cloud。

"$ROOT/scripts/wait-http.sh" http://localhost:18080/health 180

echo "== Bootstrap dataset into Langfuse Cloud =="
curl -fsS -X POST http://localhost:18080/admin/bootstrap | python3 -m json.tool

echo
echo "== Run baseline Agent v1 =="
curl -fsS -X POST http://localhost:18080/experiments/run \
  -H 'content-type: application/json' \
  -d '{"agent_id":"banking-agent","agent_version":"v1","dataset_name":"banking-agent-regression","experiment_name":"banking-agent-v1","max_concurrency":4}' \
  | tee /tmp/agent-eval-cloud-v1.json | python3 -m json.tool

echo
echo "== Run candidate Agent v2 =="
curl -fsS -X POST http://localhost:18080/experiments/run \
  -H 'content-type: application/json' \
  -d '{"agent_id":"banking-agent","agent_version":"v2","dataset_name":"banking-agent-regression","experiment_name":"banking-agent-v2","max_concurrency":4}' \
  | tee /tmp/agent-eval-cloud-v2.json | python3 -m json.tool

echo
echo "== Expected PoC outcome =="
echo "v1: 2/6 overall_pass (about 33.3%); v2: 6/6 overall_pass (100%)."
echo "打开你的 Langfuse Cloud 项目，在 Datasets / Experiments 下对比两次运行。"
