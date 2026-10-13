"""Write a bounded, non-secret Cloud E2E coverage summary to GITHUB_STEP_SUMMARY."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from scripts.ci.cloud_e2e_policy import REQUIRED_CI_CHECKS


def _display(value: Any, fallback: str = "unknown") -> str:
    text = str(value if value is not None else fallback)
    return text.replace("`", "'").replace("\r", " ").replace("\n", " ")[:300]


def render_summary(data: dict[str, Any]) -> str:
    lines = [
        "## Langfuse Cloud E2E coverage",
        "",
        f"- Cloud state: `{_display(data.get('cloud_state'))}`",
        f"- Policy state: `{_display(data.get('policy_state', 'awaiting trusted policy'))}`",
        f"- Reason: {_display(data.get('reason'))}",
        f"- Event/ref: `{_display(data.get('event'))}` / `{_display(data.get('ref'))}`",
        f"- PR: `{_display(data.get('pr_number', 'not applicable'))}`",
        f"- Head/base/tested SHA: `{_display(data.get('head_sha'))}` / `{_display(data.get('base_sha', 'not applicable'))}` / `{_display(data.get('tested_sha'))}`",
        f"- Risk: `{_display(data.get('risk', 'unclassified by trusted policy'))}`",
        "- Regular CI:",
    ]
    results = data.get("checks", {})
    if not isinstance(results, dict):
        results = {}
    lines.extend(f"  - {name}: `{_display(results.get(name, 'not run'))}`" for name in REQUIRED_CI_CHECKS)
    if data.get("evidence_url"):
        lines.append(f"- Evidence: {_display(data['evidence_url'])}")
    return "\n".join(lines) + "\n"


def main() -> int:
    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    event = json.loads(event_path.read_text(encoding="utf-8"))
    pull_request = event.get("pull_request") if isinstance(event, dict) else None
    pull_request = pull_request if isinstance(pull_request, dict) else {}
    checks_payload = json.loads(os.environ.get("CI_CHECKS_JSON", "{}"))
    check_names = {
        "quality": "Code Quality",
        "unit-tests": "Full Python Tests",
        "docker-build": "Docker / Compose Validation",
        "console": "Console Quality & E2E",
    }
    check_results = {
        check_names.get(key, str(job.get("name", key))): str(job.get("result", "not run"))
        for key, job in checks_payload.items()
        if isinstance(job, dict)
    } if isinstance(checks_payload, dict) else {}
    data = {
        "cloud_state": os.environ.get("CLOUD_STATE", "unknown"),
        "policy_state": os.environ.get("POLICY_STATE", "awaiting trusted policy"),
        "reason": os.environ.get("CLOUD_REASON", ""),
        "event": os.environ.get("GITHUB_EVENT_NAME", ""),
        "ref": os.environ.get("GITHUB_REF", ""),
        "pr_number": pull_request.get("number"),
        "head_sha": (pull_request.get("head") or {}).get("sha") or os.environ.get("GITHUB_SHA"),
        "base_sha": (pull_request.get("base") or {}).get("sha"),
        "tested_sha": os.environ.get("GITHUB_SHA", ""),
        "risk": os.environ.get("RISK_STATE", "unclassified by trusted policy"),
        "checks": check_results,
        "evidence_url": f"{os.environ.get('GITHUB_SERVER_URL', '')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}",
    }
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        Path(summary_path).write_text(render_summary(data), encoding="utf-8")
    print(f"Cloud E2E coverage: {data['cloud_state']}; policy: {data['policy_state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
