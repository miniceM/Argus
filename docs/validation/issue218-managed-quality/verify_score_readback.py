#!/usr/bin/env python3
"""独立只读回查真实 Langfuse Score，与三份 Frozen Snapshot 逐项核对。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/eval-runner"))


def main():
    from app.langfuse_managed_scores import _score_evidence
    from app.langfuse_scores_client import ScoresV3Client

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--credential-container", default="argus-flowise-e2e-eval-runner-1")
    args = parser.parse_args()
    raw = json.loads(subprocess.check_output(["docker", "inspect", args.credential_container]))[0]
    env = dict(x.split("=", 1) for x in raw["Config"]["Env"] if "=" in x)
    proof = []
    with ScoresV3Client(
        *(env[k] for k in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"))
    ) as client:
        for label in ("pass", "fail", "missing"):
            frozen = json.loads((args.evidence_dir / (label + "-snapshot.json")).read_text())
            contract = frozen["manifest"]["managed_evaluation"]
            for item in frozen["items"]:
                for expected in contract["scores"]:
                    time.sleep(2.2)
                    # 不加 source 筛选：API 来源的重复回写也必须被发现。
                    rows = client._pages(
                        "/api/public/v3/scores",
                        {
                            "traceId": item["trace_id"],
                            "observationId": item["observation_id"],
                            "name": expected["name"],
                            "fields": "details,subject",
                        },
                    )
                    rows = [r for r in rows if r.get("name") == expected["name"]]
                    adopted = [r for r in item["evaluation_results"] if r["evaluator_id"] == expected["name"]]
                    if label == "missing" and expected["name"].endswith("-code"):
                        assert rows == adopted == []
                    else:
                        assert len(rows) == len(adopted) == 1
                        assert _score_evidence(rows[0]) == adopted[0]["evidence"]["score"]
                        assert rows[0]["source"] == "EVAL"
                        assert rows[0]["value"] is (label == "pass")
                    proof.append(
                        dict(
                            scenario=label,
                            snapshot_id=frozen["snapshot_id"],
                            score_name=expected["name"],
                            rows=[_score_evidence(r) for r in rows],
                            matches_frozen_adoption=True,
                        )
                    )
        result = dict(
            status="PASS",
            method="independent read-only ScoresV3Client GET",
            checks=proof,
            requests=client.requests,
            score_write_requests=0,
        )
        (args.evidence_dir / "independent-score-readback.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
    print("PASS: 5 real native EVAL Scores and 1 required Score absence match frozen evidence")


if __name__ == "__main__":
    main()
