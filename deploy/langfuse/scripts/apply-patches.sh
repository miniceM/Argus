#!/usr/bin/env bash
set -euo pipefail

LAYER_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SOURCE_DIR=${1:?usage: apply-patches.sh <clean-langfuse-source>}

"$LAYER_ROOT/scripts/check-i18n-coverage.py"
"$LAYER_ROOT/scripts/verify-upstream.sh" "$SOURCE_DIR"

while IFS= read -r relative_path; do
  patch="$LAYER_ROOT/$relative_path"
  echo "checking $relative_path"
  git -C "$SOURCE_DIR" apply --check "$patch" || {
    echo "patch conflict: $relative_path" >&2
    exit 1
  }
  git -C "$SOURCE_DIR" apply "$patch"
done < <(
  python3 - "$LAYER_ROOT/upstream/manifest.json" <<'PY'
import json, sys
for patch in json.load(open(sys.argv[1]))["patches"]:
    print(patch["path"])
PY
)

install -d "$SOURCE_DIR/web/messages"
install -m 0644 "$LAYER_ROOT/locales/en.json" "$SOURCE_DIR/web/messages/en.json"
install -m 0644 "$LAYER_ROOT/locales/zh-CN.json" "$SOURCE_DIR/web/messages/zh-CN.json"

python3 - "$LAYER_ROOT/upstream/manifest.json" "$SOURCE_DIR/web/public/i18n-build.json" <<'PY'
import json, sys
manifest=json.load(open(sys.argv[1]))
metadata={
    "upstream": {"tag": manifest["tag"], "commit": manifest["commit"]},
    "patches": manifest["patches"],
    "locales": manifest["locales"],
}
open(sys.argv[2], "w").write(json.dumps(metadata, indent=2)+"\n")
PY

"$LAYER_ROOT/scripts/check-i18n-coverage.py" --upstream "$SOURCE_DIR"
echo "patch application: OK"
