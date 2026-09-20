#!/usr/bin/env bash
set -euo pipefail

LAYER_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
IMAGE_NAME=${LANGFUSE_I18N_IMAGE:-argus/langfuse-i18n:4.38.0}

command -v docker >/dev/null || {
  echo "Docker is required to build the Langfuse i18n image" >&2
  exit 1
}

read -r REPOSITORY TAG COMMIT < <(
  python3 - "$LAYER_ROOT/upstream/manifest.json" <<'PY'
import json, sys
m=json.load(open(sys.argv[1]))
print(m["repository"], m["tag"], m["commit"])
PY
)

"$LAYER_ROOT/scripts/check-i18n-coverage.py"
docker build \
  --file "$LAYER_ROOT/Dockerfile" \
  --build-arg "LANGFUSE_REPOSITORY=$REPOSITORY" \
  --build-arg "LANGFUSE_TAG=$TAG" \
  --build-arg "LANGFUSE_COMMIT=$COMMIT" \
  --build-arg "NEXT_PUBLIC_BUILD_ID=argus-i18n-${COMMIT:0:12}" \
  --tag "$IMAGE_NAME" \
  "$LAYER_ROOT"

IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$IMAGE_NAME")
printf 'image=%s\nimage_id=%s\nupstream_commit=%s\n' "$IMAGE_NAME" "$IMAGE_ID" "$COMMIT"
