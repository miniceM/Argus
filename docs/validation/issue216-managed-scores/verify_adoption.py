#!/usr/bin/env python3
"""在现有 Managed Launch 上重复收集并验收；不重新调用 Agent、不修改远端 Rule。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--argus-url", default="http://127.0.0.1:29680")
    parser.add_argument("--launch-id", required=True)
    parser.add_argument("--item-id", required=True)
    parser.add_argument("--evidence-dir", type=Path, default=HERE / "evidence/live-adoption")
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    audit = []

    def save(name, value):
        (args.evidence_dir / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    with httpx.Client(base_url=args.argus_url, timeout=180) as api:

        def request(method, path):
            response = api.request(method, path)
            audit.append(
                {"method": method, "path": path, "status": response.status_code, "at": datetime.now(UTC).isoformat()}
            )
            save("adoption-api-audit", audit)
            assert response.status_code == 200, (path, response.status_code)
            return response.json()

        path = "/api/v1/experiment-launches/" + args.launch_id
        launch = request("GET", path)
        assert launch["manifest"]["evaluation_source"] == "langfuse_managed"
        items_before = request("GET", path + "/items")
        before = next(i for i in items_before if i["id"] == args.item_id)
        assert before["execution_status"] == "succeeded"
        assert before["attempt_count"] == 1
        managed = path + "/items/" + args.item_id + "/managed-scores"
        first = request("POST", managed + "/collect")
        save("collection-first-fixed-client", first)
        assert all(d["decision"] in ("ADOPTED", "ALREADY_ADOPTED") for d in first["decisions"]), first
        diagnostic = request("GET", managed)
        assert len(diagnostic["results"]) == len(launch["manifest"]["managed_evaluation"]["scores"]) == 2
        save("diagnostics-first", diagnostic)
        second = request("POST", managed + "/collect")
        save("collection-repeat", second)
        assert all(d["decision"] == "ALREADY_ADOPTED" for d in second["decisions"]), second
        after = request("GET", managed)
        assert after["results"] == diagnostic["results"]
        assert after["quality_conclusion"] == "unknown" and after["releasable"] is False
        assert len(after["evidence"]) == len(diagnostic["evidence"])
        save("diagnostics-repeat", after)
        items_after = request("GET", path + "/items")
        save("items-after", items_after)
        item = next(i for i in items_after if i["id"] == args.item_id)
        assert item["attempt_count"] == before["attempt_count"] == 1
        assert item["trace_id"] == before["trace_id"] and item["observation_id"] == before["observation_id"]
        assert item["quality_conclusion"] == "unknown" and item["scores"] == {}
        receipt = {
            "status": "PASS",
            "launch_id": args.launch_id,
            "item_id": args.item_id,
            "trace_id": item["trace_id"],
            "observation_id": item["observation_id"],
            "score_ids": [r["evidence"]["score"]["id"] for r in after["results"]],
            "values": [r["value"] for r in after["results"]],
            "external_sources": [r["evidence"]["score"]["source"] for r in after["results"]],
            "effective_result_count": len(after["results"]),
            "http_attempts": 1,
            "score_write_requests": 0,
            "quality_conclusion": "unknown",
            "releasable": False,
            "at": datetime.now(UTC).isoformat(),
            "implementation_ref": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "collector_sha256": hashlib.sha256(
                (ROOT / "services/eval-runner/app/langfuse_managed_scores.py").read_bytes()
            ).hexdigest(),
            "client_sha256": hashlib.sha256(
                (ROOT / "services/eval-runner/app/langfuse_scores_client.py").read_bytes()
            ).hexdigest(),
        }
        save("receipt", receipt)
        print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
