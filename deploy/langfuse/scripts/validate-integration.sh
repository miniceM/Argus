#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
LAYER_ROOT="$ROOT/deploy/langfuse"

required=(
  LANGFUSE_BASE_URL LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY
  LANGFUSE_ADMIN_EMAIL LANGFUSE_ADMIN_PASSWORD LANGFUSE_PROJECT_ID
  LANGFUSE_I18N_IMAGE_DIGEST LANGFUSE_I18N_BUILD_ID
)
for name in "${required[@]}"; do
  test -n "${!name:-}" || {
    echo "NOT_RUN: missing $name for the isolated self-hosted acceptance environment" >&2
    exit 2
  }
done

export E2E_RUN_SUFFIX=${E2E_RUN_SUFFIX:-i18n-$(date -u +%Y%m%dT%H%M%SZ)}
export LANGFUSE_DATASET_NAME=${LANGFUSE_DATASET_NAME:-banking-agent-regression}

if [[ ! "$LANGFUSE_I18N_IMAGE_DIGEST" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]]; then
  echo "NOT_RUN: LANGFUSE_I18N_IMAGE_DIGEST must be a full GHCR digest reference" >&2
  exit 2
fi

SOURCE_DIR=""
CLOUD_ENV_FILE=$(mktemp "$ROOT/.env.cloud.argus.XXXXXX")
cleanup() {
  [[ -n "$SOURCE_DIR" ]] && rm -rf "$SOURCE_DIR"
  rm -f "$CLOUD_ENV_FILE"
}
trap cleanup EXIT

umask 077
{
  printf 'LANGFUSE_BASE_URL=%s\n' "$LANGFUSE_BASE_URL"
  printf 'LANGFUSE_PUBLIC_KEY=%s\n' "$LANGFUSE_PUBLIC_KEY"
  printf 'LANGFUSE_SECRET_KEY=%s\n' "$LANGFUSE_SECRET_KEY"
} > "$CLOUD_ENV_FILE"

ARGUS_CLOUD_ENV_FILE="$CLOUD_ENV_FILE" bash "$ROOT/scripts/ci-e2e-cloud.sh"

SOURCE_DIR=$($LAYER_ROOT/scripts/prepare-upstream.sh)
"$LAYER_ROOT/scripts/apply-patches.sh" "$SOURCE_DIR"

cd "$SOURCE_DIR"
corepack pnpm install --frozen-lockfile
corepack pnpm --filter web exec playwright install --with-deps chromium
LANGFUSE_UPSTREAM_DIR="$SOURCE_DIR" \
I18N_EVIDENCE_DIR="$ROOT/artifacts/i18n-integration" \
node "$LAYER_ROOT/scripts/integration-ui.mjs"

identity_json=$(python3 "$LAYER_ROOT/scripts/verify-image-identity.py" \
  "$LANGFUSE_I18N_IMAGE_DIGEST" "$LANGFUSE_BASE_URL" "$LANGFUSE_I18N_BUILD_ID")

python3 - "$ROOT/artifacts/i18n-integration/report.json" "$identity_json" <<'PY'
import json, os, pathlib, sys
path=pathlib.Path(sys.argv[1]); path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({
  "status":"PASS",
  "imageDigest":os.environ["LANGFUSE_I18N_IMAGE_DIGEST"],
  "imageBuildId":os.environ["LANGFUSE_I18N_BUILD_ID"],
  "imageIdentity":json.loads(sys.argv[2]),
  "baseUrl":os.environ["LANGFUSE_BASE_URL"],
  "dataset":os.environ["LANGFUSE_DATASET_NAME"],
}, indent=2)+"\n")
PY

echo "Langfuse self-hosted integration: PASS"
