#!/usr/bin/env python3
"""Export OpenAPI specification from FastAPI application without requiring active DB or Langfuse connections."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

# Force test DB mode so application loads without external dependencies
os.environ["ARGUS_DB_MODE"] = "test"
os.environ["ARGUS_AUTO_IMPORT_YAML"] = "false"

from app.main import app  # noqa: E402


def export():
    openapi_schema = app.openapi()
    docs_dir = ROOT / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    out_file = docs_dir / "openapi.json"
    out_file.write_text(json.dumps(openapi_schema, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OpenAPI specification exported to: {out_file}")


if __name__ == "__main__":
    export()
