#!/usr/bin/env bash
set -euo pipefail

LAYER_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SOURCE_DIR=$($LAYER_ROOT/scripts/prepare-upstream.sh)
cleanup() { rm -rf "$SOURCE_DIR"; }
trap cleanup EXIT

"$LAYER_ROOT/scripts/apply-patches.sh" "$SOURCE_DIR"

command -v node >/dev/null || {
  echo "Node.js 24 is required for full i18n validation" >&2
  exit 1
}
command -v corepack >/dev/null || {
  echo "Corepack is required for the locked pnpm toolchain" >&2
  exit 1
}

NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
test "$NODE_MAJOR" = "24" || {
  echo "Node.js 24 is required, got $(node --version)" >&2
  exit 1
}

PNPM_BIN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/argus-pnpm.XXXXXX")
cat > "$PNPM_BIN_DIR/pnpm" <<'SH'
#!/usr/bin/env bash
exec corepack pnpm "$@"
SH
chmod +x "$PNPM_BIN_DIR/pnpm"
export PATH="$PNPM_BIN_DIR:$PATH"

cd "$SOURCE_DIR"
corepack pnpm install --frozen-lockfile
node "$LAYER_ROOT/scripts/check-icu.mjs" "$SOURCE_DIR"
node "$LAYER_ROOT/scripts/check-hardcoded-ui.mjs" \
  "$SOURCE_DIR" "$LAYER_ROOT/hardcoded-ui-baseline.json"

DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
NEXTAUTH_URL=http://localhost:3000 \
SALT=argus-i18n-validation \
CLICKHOUSE_URL=http://localhost:8123 \
CLICKHOUSE_USER=clickhouse \
CLICKHOUSE_PASSWORD=clickhouse \
  pnpm --dir web exec vitest run --project client \
    src/features/i18n/config.clienttest.ts \
    src/features/i18n/LanguageSwitcher.clienttest.tsx

pnpm exec turbo run typecheck --filter=web...
pnpm --dir web exec eslint \
  src/pages/_app.tsx src/pages/_document.tsx src/features/i18n \
  src/components/nav/AppSidebar/AppSidebar.tsx \
  src/components/nav/nav-main.tsx \
  src/components/layouts/routes.tsx \
  src/components/layouts/page-header.tsx \
  src/components/layouts/breadcrumb.tsx \
  src/components/layouts/page-tabs.tsx \
  src/components/table/data-table.tsx \
  src/components/table/simple-data-table.tsx \
  --max-warnings 0

echo "Langfuse i18n validation: PASS"
