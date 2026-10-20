#!/usr/bin/env python3
"""#217 真实联调：显式 --execute 后创建隔离 Rule、运行一次 HTTP Agent，观察自动异步收敛。

与 #216 的区别：Agent 成功后**不调用任何手动 collect**，只轮询 Item 状态，
证明 Runner 的 Reconciler/Worker 自动完成 pending → recovered 收敛，且
attempt_count 保持 1（评分重试永不调用业务 Agent）。

凭据仅从环境读取；不创建 Score，不修改既有 Rule。退出时暂停自有 Rule。
Argus URL 必须指向当前源码（含 #217）、独立数据库且 tracing environment
与 --environment 一致的 Runner，且后台循环必须运行（非 test 模式）。
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
    parser.add_argument("--argus-url", default="http://127.0.0.1:29681")
    parser.add_argument("--environment", required=True)
    parser.add_argument("--agent-endpoint", required=True)
    parser.add_argument("--evidence-dir", type=Path, default=HERE / "evidence/live-async-convergence")
    parser.add_argument("--converge-timeout", type=int, default=1200)
    args = parser.parse_args()
    if not args.execute:
        print("DRY RUN：会创建两条隔离 Rule，调用一次真实 Agent，观察自动收敛；不会创建 Score 或修改既有 Rule。")
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
    prefix = "issue217-" + datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz")
    lf = httpx.Client(
        base_url=base,
        auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]),
        timeout=60,
        follow_redirects=False,
    )
    argus = httpx.Client(base_url=args.argus_url, timeout=180)

    def request(client, method, path, body=None):
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
            current = request(lf, "GET", "/api/public/v2/evaluators/" + evaluator["id"])
            save("evaluator-" + evaluator["type"], current)
            body = {
                "name": prefix + "-" + evaluator["type"],
                "enabled": True,
                "sampling": 1,
                "filter": [
                    {"type": "stringOptions", "column": "environment", "operator": "any of",
                     "value": [args.environment]},
                    {"type": "stringOptions", "column": "name", "operator": "any of",
                     "value": ["remote-agent-http"]},
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
        request(argus, "POST", "/api/v1/agents",
                {"id": prefix, "name": prefix, "description": "Issue217 live async convergence"})
        version = json.loads((PRIOR / "agent-version.json").read_text())["request"]
        version.update(agent_id=prefix, endpoint=args.agent_endpoint, environment=args.environment, max_retries=0)
        save("agent-version",
             {"request": version, "response": request(argus, "POST", "/api/v1/agent-versions", version)})
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
                    {"evaluator_id": e["name"], "result_type": "boolean", "operator": "==",
                     "expected_value": True, "required": True}
                    for e in expectations
                ]
            },
        }
        launch = request(argus, "POST", "/api/v1/experiment-launches", body)
        save("launch", {"request": body, "response": launch})
        path = "/api/v1/experiment-launches/" + launch["id"]
        save("run", request(argus, "POST", path + "/run"))
        t0 = time.monotonic()
        deadline = t0 + 300
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
        # 执行成功但评分未齐：必须已进入评分等待态。
        assert item["eval_status"] == "pending", item
        assert item["quality_conclusion"] == "unknown", item
        assert item["evaluation_status"] == "evaluating", item
        timeline = [
            {"at": datetime.now(UTC).isoformat(), "elapsed_s": round(time.monotonic() - t0, 1),
             "execution_status": item["execution_status"], "eval_status": item["eval_status"],
             "quality_conclusion": item["quality_conclusion"],
             "evaluation_status": item["evaluation_status"],
             "evaluation_generation": item.get("evaluation_generation"),
             "attempt_count": item.get("attempt_count")}
        ]
        # 关键：此后只做 GET 轮询，不调用任何 collect/重评 API，等待自动收敛。
        deadline = time.monotonic() + args.converge_timeout
        final = None
        last_sig = None
        while time.monotonic() < deadline:
            time.sleep(15)
            rows = request(argus, "GET", path + "/items")
            row = rows[0]
            sig = (row["execution_status"], row["eval_status"], row["quality_conclusion"],
                   row["evaluation_status"], row.get("evaluation_generation"), row.get("attempt_count"))
            if sig != last_sig:
                last_sig = sig
                timeline.append(
                    {"at": datetime.now(UTC).isoformat(), "elapsed_s": round(time.monotonic() - t0, 1),
                     "execution_status": row["execution_status"], "eval_status": row["eval_status"],
                     "quality_conclusion": row["quality_conclusion"],
                     "evaluation_status": row["evaluation_status"],
                     "evaluation_generation": row.get("evaluation_generation"),
                     "attempt_count": row.get("attempt_count")}
                )
                save("timeline", timeline)
            if row["evaluation_status"] == "recovered":
                final = row
                break
        save("timeline", timeline)
        assert final is not None, "Managed scores did not auto-converge in time; no synthetic fallback"
        # 评分重试永不调用业务 Agent：全程恰好一次 HTTP 执行。
        assert final["attempt_count"] == 1, final
        assert final["execution_status"] == "succeeded", final
        assert final["evaluation_status"] == "recovered", final
        assert final["eval_status"] == "succeeded", final
        # 质量结论记实测值（真实 Judge 未必满足 ==True，不锁定期望）。
        diagnostic = request(argus, "GET", path + "/items/" + final["id"] + "/managed-scores")
        save("diagnostics-final", diagnostic)
        assert len(diagnostic["results"]) == 2, diagnostic
        save("items-after", request(argus, "GET", path + "/items"))
        save(
            "receipt",
            {
                "status": "PASS",
                "mode": "automatic-convergence-without-manual-collect",
                "environment": args.environment,
                "launch_id": launch["id"],
                "item_id": final["id"],
                "trace_id": final["trace_id"],
                "observation_id": final["observation_id"],
                "rule_ids": rules,
                "score_ids": [r["evidence"]["score"]["id"] for r in diagnostic["results"]],
                "effective_result_count": len(diagnostic["results"]),
                "http_attempts": final["attempt_count"],
                "score_write_requests": 0,
                "manual_collect_calls": 0,
                "eval_status": final["eval_status"],
                "quality_conclusion": final["quality_conclusion"],
                "at": datetime.now(UTC).isoformat(),
                "recovery_sha256": hashlib.sha256(
                    (ROOT / "services/eval-runner/app/managed_score_recovery.py").read_bytes()
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
