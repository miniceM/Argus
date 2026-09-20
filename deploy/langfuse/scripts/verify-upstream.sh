#!/usr/bin/env bash
set -euo pipefail

LAYER_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SOURCE_DIR=${1:?usage: verify-upstream.sh <langfuse-source>}

read -r EXPECTED_REPOSITORY EXPECTED_TAG EXPECTED_COMMIT < <(
  python3 - "$LAYER_ROOT/upstream/manifest.json" <<'PY'
import json, sys
m=json.load(open(sys.argv[1]))
print(m["repository"], m["tag"], m["commit"])
PY
)

test -d "$SOURCE_DIR/.git" || {
  echo "not a git checkout: $SOURCE_DIR" >&2
  exit 1
}

ACTUAL_COMMIT=$(git -C "$SOURCE_DIR" rev-parse HEAD)
test "$ACTUAL_COMMIT" = "$EXPECTED_COMMIT" || {
  echo "wrong Langfuse commit: expected $EXPECTED_COMMIT ($EXPECTED_TAG), got $ACTUAL_COMMIT" >&2
  exit 1
}

ACTUAL_REPOSITORY=$(git -C "$SOURCE_DIR" remote get-url origin)
case "$ACTUAL_REPOSITORY" in
  "$EXPECTED_REPOSITORY"|"${EXPECTED_REPOSITORY%.git}") ;;
  *)
    echo "wrong Langfuse repository: expected $EXPECTED_REPOSITORY, got $ACTUAL_REPOSITORY" >&2
    exit 1
    ;;
esac

test -z "$(git -C "$SOURCE_DIR" status --porcelain)" || {
  echo "Langfuse source must be a clean checkout before patching" >&2
  git -C "$SOURCE_DIR" status --short >&2
  exit 1
}

echo "upstream verification: OK ($EXPECTED_TAG $EXPECTED_COMMIT)"
