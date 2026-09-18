#!/usr/bin/env bash
set -euo pipefail

URL=${1:?usage: wait-http.sh <url> [timeout-seconds]}
TIMEOUT=${2:-180}
START=$(date +%s)

until curl -fsS "$URL" >/dev/null 2>&1; do
  NOW=$(date +%s)
  if (( NOW - START >= TIMEOUT )); then
    echo "timeout waiting for $URL" >&2
    exit 1
  fi
  sleep 2
done

echo "ready: $URL"
