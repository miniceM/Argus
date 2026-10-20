#!/usr/bin/env python3
"""可选真实联调：显式 --execute 后创建隔离 Rule、运行一次 HTTP Agent，并重复采纳。

凭据仅从环境读取；不创建 Score/执行器，不修改既有 Rule。退出时暂停自有 Rule。
Argus URL 必须指向当前源码、独立数据库且 tracing environment 与 --environment 一致的 Runner。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PRIOR = ROOT / "docs/validation/issue214-managed-evaluation/evidence"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--argus-url", default="http://127.0.0.1:29680")
    parser.add_argument("--environment", default="issue216-20261020")
    parser.add_argument("--agent-endpoint", required=True)
    parser.add_argument("--evidence-dir", type=Path, default=HERE / "evidence/live-adoption")
    args = parser.parse_args()
    if not args.execute:
        print(
            "DRY RUN：会创建两条隔离 Rule，调用一次真实 Flowise/模型，收集真实 Score；不会创建 Score 或修改既有 Rule。"
        )
        return
    output = args.evidence_dir
    output.mkdir(parents=True, exist_ok=True)

    def save(name, data):
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            if os.environ.get(key):
                text = text.replace(os.environ[key], "[redacted]")
        (output / (name + ".json")).write_text(text + "\n")

    setup = json.loads((PRIOR / "setup.json").read_text())
    base = os.environ["LANGFUSE_BASE_URL"].rstrip("/")
    rules = []
    audit = []
    prefix = "issue216-" + datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz")
    lf = httpx.Client(
        base_url=base,
        auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]),
        timeout=60,
        follow_redirects=False,
    )
    argus = httpx.Client(base_url=args.argus_url, timeout=180)

    def request(client, method, path, body=None):
        # 白名单分离：外部只允许 Rule 配置；评分永远只通过 Argus 内部官方 GET 查询。
        if client is lf:
            assert path.startswith("/api/public/v2/evaluation-rules") or (
                method == "GET" and path.startswith("/api/public/v2/evaluators/")
            )
            time.sleep(2.2)
        response = client.request(method, path, json=body) if body is not None else client.request(method, path)
        audit.append(
            {
                "service": "langfuse" if client is lf else "argus",
                "method": method,
                "path": path,
                "status": response.status_code,
                "at": datetime.now(UTC).isoformat(),
            }
        )
        save("request-audit", audit)
        if response.status_code not in (200, 201, 202):
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code}; response body omitted")
        return response.json()

    try:
        expectations = []
        for evaluator in setup["evaluators"]:
            # 读取 current latest，只记录稳定 evaluator/rule 引用，不冻结其实现制品。
            current = request(lf, "GET", "/api/public/v2/evaluators/" + evaluator["id"])
            save("evaluator-" + evaluator["type"], current)
            body = {
                "name": prefix + "-" + evaluator["type"],
                "enabled": True,
                "sampling": 1,
                "filter": [
                    {
                        "type": "stringOptions",
                        "column": "environment",
                        "operator": "any of",
                        "value": [args.environment],
                    },
                    {"type": "stringOptions", "column": "name", "operator": "any of", "value": ["remote-agent-http"]},
                ],
                "evaluatorAssignments": [{"evaluatorId": evaluator["id"], "variableMapping": None}],
            }
            rule = request(lf, "POST", "/api/public/v2/evaluation-rules", body)
            rules.append(rule["id"])
            save("rule-" + evaluator["type"], {"request": body, "response": rule})
            expectations.append(
                {
                    "name": evaluator["score_name"],
                    "data_type": "boolean",
                    "required": True,
                    "rule_id": rule["id"],
                    "evaluator_id": evaluator["id"],
                }
            )
        request(
            argus,
            "POST",
            "/api/v1/agents",
            {"id": prefix, "name": prefix, "description": "Issue216 synthetic real acceptance"},
        )
        version = json.loads((PRIOR / "agent-version.json").read_text())["request"]
        version.update(agent_id=prefix, endpoint=args.agent_endpoint, environment=args.environment, max_retries=0)
        save(
            "agent-version", {"request": version, "response": request(argus, "POST", "/api/v1/agent-versions", version)}
        )
        project = json.loads((PRIOR / "fresh-api/read-005.json").read_text())["response"]["data"][0]["projectId"]
        body = {
            "agent_id": prefix,
            "agent_version": "v1",
            "dataset_name": setup["prefix"],
            "name": prefix,
            "environment": args.environment,
            "max_concurrency": 1,
            "idempotency_key": prefix,
            "evaluation_source": "langfuse_managed",
            "managed_evaluation": {
                "project_id": project,
                "environment": args.environment,
                "target": {"name": "remote-agent-http", "type": "TOOL"},
                "scores": expectations,
            },
            "quality_policy": {
                "rules": [
                    {
                        "evaluator_id": e["name"],
                        "result_type": "boolean",
                        "operator": "==",
                        "expected_value": True,
                        "required": True,
                    }
                    for e in expectations
                ]
            },
        }
        launch = request(argus, "POST", "/api/v1/experiment-launches", body)
        save("launch", {"request": body, "response": launch})
        path = "/api/v1/experiment-launches/" + launch["id"]
        save("run", request(argus, "POST", path + "/run"))
        deadline = time.monotonic() + 300
        item = None
        while time.monotonic() < deadline:
            rows = request(argus, "GET", path + "/items")
            if rows and rows[0]["execution_status"] == "succeeded":
                item = rows[0]
                break
            if rows and rows[0]["execution_status"] == "failed":
                save("failed-items", rows)
                raise RuntimeError("Agent execution failed; no retry")
            time.sleep(3)
        assert item is not None, "HTTP execution timed out; no retry"
        save("items-before", rows)
        managed = path + "/items/" + item["id"] + "/managed-scores"
        history = []
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            result = request(argus, "POST", managed + "/collect")
            history.append(result)
            save("collection-history", history)
            diagnostic = request(argus, "GET", managed)
            if len(diagnostic["results"]) == 2:
                break
            time.sleep(15)
        assert len(diagnostic["results"]) == 2, "Native managed Scores not available; no synthetic fallback"
        save("diagnostics-first", diagnostic)
        second = request(argus, "POST", managed + "/collect")
        save("collection-repeat", second)
        assert all(d["decision"] == "ALREADY_ADOPTED" for d in second["decisions"]), second
        after = request(argus, "GET", managed)
        assert after["results"] == diagnostic["results"]
        assert after["quality_conclusion"] == "unknown" and after["releasable"] is False
        save("diagnostics-repeat", after)
        final_items = request(argus, "GET", path + "/items")
        save("items-after", final_items)
        assert final_items[0]["attempt_count"] == 1
        save(
            "receipt",
            {
                "status": "PASS",
                "environment": args.environment,
                "launch_id": launch["id"],
                "item_id": item["id"],
                "trace_id": item["trace_id"],
                "observation_id": item["observation_id"],
                "rule_ids": rules,
                "score_ids": [r["evidence"]["score"]["id"] for r in after["results"]],
                "effective_result_count": len(after["results"]),
                "http_attempts": 1,
                "score_write_requests": 0,
                "quality_conclusion": "unknown",
                "releasable": False,
                "at": datetime.now(UTC).isoformat(),
                "collector_sha256": hashlib.sha256(
                    (ROOT / "services/eval-runner/app/langfuse_managed_scores.py").read_bytes()
                ).hexdigest(),
            },
        )
        print(json.dumps(json.loads((output / "receipt.json").read_text()), indent=2))
    finally:
        paused = []
        for rule_id in rules:
            try:
                rule = request(lf, "PATCH", "/api/public/v2/evaluation-rules/" + rule_id, {"enabled": False})
                paused.append({"id": rule_id, "enabled": rule.get("enabled")})
            except Exception:
                paused.append({"id": rule_id, "cleanup": "FAILED"})
        save("owned-rules-paused", paused)
        lf.close()
        argus.close()
        if not all(p.get("enabled") is False for p in paused):
            raise RuntimeError("Own Rule cleanup incomplete; inspect owned-rules-paused.json")


if __name__ == "__main__":
    main()
