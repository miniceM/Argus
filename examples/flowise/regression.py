#!/usr/bin/env python3
"""隔离回归现有 Demo 和可选真实 #198；不会调用或重复执行 Flowise。"""

import argparse
import json
import time
import urllib.parse
import uuid
from pathlib import Path

from e2e import RUNTIME, Clients, expect, launch_body, load_env, request, safe_write, wait_launch


def run(env, real_llm=False):
    client = Clients(env)
    namespace = "i213-reg-" + uuid.uuid4().hex[:10]
    out = RUNTIME / namespace
    dataset = namespace + "-banking"
    source = json.loads((Path(__file__).resolve().parents[2] / "data/dataset.json").read_text())
    client.lf("/v2/datasets", {"name": dataset})
    ids = set()
    for case in source["items"]:
        item = client.lf(
            "/dataset-items",
            {
                "datasetName": dataset,
                "input": case["input"],
                "expectedOutput": case["expected_output"],
                "metadata": case["metadata"],
            },
        )
        ids.add(item["id"])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        visible = client.lf("/dataset-items?" + urllib.parse.urlencode({"datasetName": dataset, "limit": 100}))
        if {i["id"] for i in visible["data"]} == ids:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Regression dataset not fully visible")
    client.argus("/agents", {"id": namespace, "name": "Issue213 existing-agent regression"})
    for version in ["v1", "v2"] + (["real"] if real_llm else []):
        body = {
            "agent_id": namespace,
            "version": version,
            "protocol": "HTTP_JSON",
            "method": "POST",
            "endpoint": "http://"
            + ("real-llm-agent" if version == "real" else "demo-agent-" + version)
            + ":8080/invoke",
            "request_mapping": {"messages": "input.messages", "customer_id": "input.customer_id"},
            "timeout_seconds": 120,
            "max_retries": 0,
            "max_concurrency": 1,
            "is_idempotent": False,
        }
        if version == "real":
            body["usage_cost_mapping"] = {
                "input_tokens_path": "usage.input_tokens",
                "output_tokens_path": "usage.output_tokens",
                "total_tokens_path": "usage.total_tokens",
                "source": "provider_reported",
                "measurement_scope": "agent_invocation_total",
            }
        client.argus("/agent-versions", body)
        if version != "real":
            status, result = request(
                client.argus_base,
                "/experiments/run",
                {
                    "agent_id": namespace,
                    "agent_version": version,
                    "dataset_name": dataset,
                    "experiment_name": namespace + "-" + version,
                    "max_concurrency": 1,
                },
            )
            result = expect(status, result)
            safe_write(out / (version + ".json"), result)
            cases = result["result"]["items"]
            assert len(cases) == 6 and all(c["error"] is None for c in cases)
            assert sum(c["scores"]["overall_pass"] == 1 for c in cases) == (2 if version == "v1" else 6)
            print(version, "result stored:", out / (version + ".json"), flush=True)
        else:
            launch = client.argus("/experiment-launches", launch_body(namespace, version, dataset, namespace + "-real"))
            lid = launch["id"]
            safe_write(out / "real-created.json", launch)
            client.argus("/experiment-launches/" + lid + "/run", {})
            final = wait_launch(client, lid, time.monotonic() + 600)
            items = client.argus("/experiment-launches/" + lid + "/items")
            summary = client.argus("/experiment-launches/" + lid + "/summary")
            attempts = {i["id"]: client.argus("/experiment-item-executions/" + i["id"] + "/attempts") for i in items}
            safe_write(out / "real.json", {"launch": final, "items": items, "summary": summary, "attempts": attempts})
            assert len(items) == 6 and all(i["execution_status"] == "succeeded" for i in items)
            assert all(len(a) == 1 for a in attempts.values())
            print("real model regression stored:", out / "real.json", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--real-llm", action="store_true", help="执行六次真实模型请求，可能计费")
    args = parser.parse_args()
    run(load_env(args.env_file), real_llm=args.real_llm)
