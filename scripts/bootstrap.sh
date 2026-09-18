#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
"$ROOT/scripts/wait-http.sh" http://localhost:3000/api/public/ready 300
"$ROOT/scripts/wait-http.sh" http://localhost:18080/health 120
curl -fsS -X POST http://localhost:18080/admin/bootstrap | python -m json.tool
