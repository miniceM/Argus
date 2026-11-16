#!/usr/bin/env bash
set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${ARGUS_PYTHON:-python3}" "$SCRIPT_DIR/deploy.py" upgrade "$@"
