#!/usr/bin/env bash

# Load only the test credentials needed by scripts/ci-e2e-cloud.sh. This is an
# env-file parser, not a shell source operation: values remain data even when
# they contain shell metacharacters.
load_cloud_environment_file() {
  local env_file="$1"
  local name value

  [[ -f "$env_file" ]] || return 0
  if [[ ! -r "$env_file" ]]; then
    echo "Cloud environment file is not readable" >&2
    return 1
  fi

  while IFS='=' read -r name value || [[ -n "$name" ]]; do
    case "$name" in
      LANGFUSE_BASE_URL|LANGFUSE_PUBLIC_KEY|LANGFUSE_SECRET_KEY)
        if [[ -z "${!name:-}" ]]; then
          printf -v "$name" '%s' "$value"
          export "$name"
        fi
        ;;
    esac
  done < "$env_file"
}
