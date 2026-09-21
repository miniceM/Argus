#!/usr/bin/env bash
set -euo pipefail

LAYER_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DESTINATION=${1:-}

read -r REPOSITORY TAG < <(
  python3 - "$LAYER_ROOT/upstream/manifest.json" <<'PY'
import json, sys
m=json.load(open(sys.argv[1]))
print(m["repository"], m["tag"])
PY
)

if [[ -z "$DESTINATION" ]]; then
  DESTINATION=$(mktemp -d "${TMPDIR:-/tmp}/argus-langfuse.XXXXXX")
  rmdir "$DESTINATION"
elif [[ -e "$DESTINATION" ]]; then
  echo "destination already exists: $DESTINATION" >&2
  exit 1
fi

git clone --depth 1 --branch "$TAG" --filter=blob:none "$REPOSITORY" "$DESTINATION"
"$LAYER_ROOT/scripts/verify-upstream.sh" "$DESTINATION" >&2
printf '%s\n' "$DESTINATION"
