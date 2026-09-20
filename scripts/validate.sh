#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

PYTHON_BIN="python"
if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi

echo "[1/7] Parse YAML/JSON"
"$PYTHON_BIN" - <<'PY'
import json, yaml
from pathlib import Path
yaml.safe_load(Path('docker-compose.yml').read_text())
yaml.safe_load(Path('config/agents.yaml').read_text())
seed=json.loads(Path('data/dataset.json').read_text())
assert len(seed['items']) == 6
assert len({x['id'] for x in seed['items']}) == 6
print('configuration parse: OK')
PY

echo "[2/7] Compile Python sources"
"$PYTHON_BIN" -m compileall -q services tests

echo "[3/7] Verify zero evaluation-SDK dependency in Demo Agent"
if grep -RinE '^[[:space:]]*(from|import)[[:space:]]+langfuse|Evaluation\(|run_experiment' services/demo-agent --include='*.py' || grep -in 'langfuse' services/demo-agent/requirements.txt; then
  echo "Demo Agent unexpectedly contains evaluation-platform dependencies" >&2
  exit 1
fi
if ! grep -q 'traceparent' services/demo-agent/app.py; then
  echo "Expected W3C traceparent demonstration marker missing" >&2
  exit 1
fi

echo "[4/7] Verify runner contains remote experiment + W3C propagation"
grep -q 'run_experiment' services/eval-runner/app/main.py
grep -q 'inject(headers)' services/eval-runner/app/main.py

echo "[5/7] Validate Langfuse i18n release resources"
./deploy/langfuse/scripts/check-i18n-coverage.py

echo "[6/7] Run local behavior tests"
"$PYTHON_BIN" -m pytest -q tests

echo "[7/7] Docker Compose validation"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose --env-file .env.poc config -q
  echo "docker compose config: OK"
else
  echo "SKIP: Docker/Compose is not installed in this validation environment."
  echo "Static YAML, Python compile and business behavior tests passed."
fi

echo "Validation complete."
