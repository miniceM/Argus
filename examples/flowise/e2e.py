#!/usr/bin/env python3
"""#213 可选付费联调：仅配置原生 Flowise、调用既有 Argus API；不参与生产执行路径。"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import http.cookiejar
import json
import os
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / ".runtime"


def load_env(path: Path) -> dict[str, str]:
    """只读取简单 dotenv；不执行 shell、不支持命令替换。"""
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        # JSON 配置原样保留；普通值用 shlex 正确移除引号/注释。
        values[key.strip()] = (
            value.strip() if value.strip().startswith("{") else (shlex.split(value, comments=True) or [""])[0]
        )
    values.update(os.environ)
    return values


def safe_write(path: Path, data: object):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def request(base: str, path: str, body=None, *, method=None, headers=None, opener=None):
    """HTTP 失败不暴露请求头、模型 key、完整上游错误。"""
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=180) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except ValueError:
            return exc.code, {"error": "Non-JSON HTTP error"}


def expect(status, data, allowed=(200, 201, 202)):
    if status not in allowed:
        raise RuntimeError(f"API request rejected: HTTP {status}; inspect service logs locally (response omitted)")
    return data


def materialize_cases(namespace: str):
    items = copy.deepcopy(json.loads((HERE / "dataset.json").read_text())["items"])
    for item in items:
        question = (
            item.pop("question")
            .replace("{marker}", "secret-marker-" + namespace)
            .replace("{case_id}", namespace + "-tools")
        )
        item["input"] = {
            "question": question,
            "streaming": False,
            "overrideConfig": {"sessionId": namespace + "-" + item["id"]},
        }
    return items


def version_body(agent_id, version, flow_id, scenario, image):
    return {
        "agent_id": agent_id,
        "version": version,
        "endpoint": f"http://flowise:3000/api/v1/prediction/{flow_id}",
        "protocol": "HTTP_JSON",
        "method": "POST",
        "request_mapping": {
            "question": "input.question",
            "streaming": "input.streaming",
            "overrideConfig": "input.overrideConfig",
        },
        "credential_ref": "env://FLOWISE_INVALID_TOKEN" if scenario == "auth" else "env://FLOWISE_API_TOKEN",
        "timeout_seconds": 1 if scenario == "timeout" else 120,
        "max_retries": 0,
        "max_concurrency": 1,
        "is_idempotent": False,
        "artifact_ref": image,
        "environment": "issue213",
    }


def launch_body(agent_id, version, dataset, name):
    return {
        "name": name,
        "idempotency_key": name,
        "agent_id": agent_id,
        "agent_version": version,
        "dataset_name": dataset,
        "environment": "issue213",
        "max_concurrency": 1,
        "evaluator_selections": [{"id": "answer_present", "version": "1.0.0"}],
        "quality_policy": {
            "rules": [
                {
                    "evaluator_id": "answer_present",
                    "operator": "==",
                    "expected_value": True,
                    "result_type": "boolean",
                    "required": True,
                }
            ]
        },
    }


class Clients:
    def __init__(self, env):
        self.env = env
        self.flow_base = env.get("FLOWISE_URL", "http://127.0.0.1:" + env.get("FLOWISE_PORT", "19091"))
        self.argus_base = env.get("ARGUS_URL", "http://127.0.0.1:" + env.get("ARGUS_PORT", "19090"))
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def flow(self, path, body=None, method=None):
        return expect(
            *request(
                self.flow_base,
                "/api/v1" + path,
                body,
                method=method,
                headers={"x-request-from": "internal"},
                opener=self.opener,
            )
        )

    def login(self):
        self.flow("/auth/login", {"email": self.env["FLOWISE_EMAIL"], "password": self.env["FLOWISE_PASSWORD"]})

    def argus(self, path, body=None):
        return expect(*request(self.argus_base, "/api/v1" + path, body))

    def lf(self, path, body=None):
        auth = base64.b64encode(
            (self.env["LANGFUSE_PUBLIC_KEY"] + ":" + self.env["LANGFUSE_SECRET_KEY"]).encode()
        ).decode()
        return expect(
            *request(
                self.env["LANGFUSE_BASE_URL"], "/api/public" + path, body, headers={"Authorization": "Basic " + auth}
            )
        )


def setup(env, *, register_owner=False):
    """只用于本地专用实例。成功状态文件已存在时不重复创建远端资源。"""
    if (RUNTIME / "resources.json").exists():
        raise RuntimeError("Local resources already exist; use run, or choose a fresh checkout/project")
    client = Clients(env)
    if register_owner:
        client.flow(
            "/account/register",
            {"user": {"name": "Issue213 Local", "email": env["FLOWISE_EMAIL"], "credential": env["FLOWISE_PASSWORD"]}},
        )
    client.login()
    credential = client.flow(
        "/credentials",
        {
            "name": "issue213-model",
            "credentialName": "openAIApi",
            "plainDataObj": {"openAIApiKey": env["MODEL_API_KEY"]},
        },
    )
    keys = client.flow("/apikey", {"keyName": "issue213-prediction", "permissions": ["chatflows:execute"]})
    key = next(k for k in keys if k["keyName"] == "issue213-prediction")
    flow_ids = {}
    configs = {}
    for scenario in ["success", "unreachable"]:
        graph = json.loads((HERE / "agentflow.json").read_text())
        node = next(n for n in graph["nodes"] if n["data"]["name"] == "agentAgentflow")
        config = node["data"]["inputs"]["agentModelConfig"]
        config.update(
            FLOWISE_CREDENTIAL_ID=credential["id"],
            modelName=env["MODEL_NAME"],
            basepath=env["MODEL_BASE_URL"] if scenario == "success" else "http://127.0.0.1:9/v1",
            baseOptions=json.dumps(json.loads(env.get("MODEL_EXTRA_HEADERS", "{}"))),
        )
        flow = client.flow(
            "/chatflows",
            {
                "name": "Issue213 " + scenario,
                "flowData": json.dumps(graph),
                "type": "AGENTFLOW",
                "apikeyid": key["id"],
                "deployed": True,
                "isPublic": False,
            },
        )
        flow_ids[scenario] = flow["id"]
        # 不在证据中保存密钥，只保存原生配置及 Credential 引用。
        configs[scenario] = graph
    resources = {
        "flow_ids": flow_ids,
        "flow_configs": configs,
        "flowise_key_id": key["id"],
        "image": "flowiseai/flowise@sha256:cf880b17b1290fbaebebc76c33954bde67b3775aff95feec72a1d060cc81e43d",
    }
    safe_write(RUNTIME / "resources.json", resources)
    lines = {k: env[k] for k in ["LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]}
    lines.update(FLOWISE_API_TOKEN=key["apiKey"], FLOWISE_INVALID_TOKEN="invalid-issue213-token")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    secret = RUNTIME / "runner.env"
    # JSON quoting avoids dotenv interpolation; reject newline-bearing secrets.
    if any("\n" in v or "\r" in v for v in lines.values()):
        raise ValueError("Multiline environment value is not supported")
    secret.touch(mode=0o600)
    secret.chmod(0o600)
    secret.write_text("".join(k + "=" + v + "\n" for k, v in lines.items()))
    print("Native Flowise configuration ready. Credentials saved only to ignored .runtime/runner.env")


def wait_launch(client, lid, deadline):
    while time.monotonic() < deadline:
        launch = client.argus("/experiment-launches/" + lid)
        if launch["status"] in {"COMPLETED", "SUCCEEDED", "PARTIAL_FAILED", "FAILED", "CANCELLED"}:
            return launch
        time.sleep(1)
    raise RuntimeError("Bounded Launch wait expired; inspect existing Launch, do not blindly run again")


def validate_attempts(items, attempts, scenario):
    """禁止以 evaluation failed / quality fail 冒充执行故障。"""
    for item in items:
        records = attempts[item["id"]]
        if len(records) != 1:
            raise AssertionError("Exactly one Argus HTTP attempt required")
        if scenario == "success":
            assert item["execution_status"] == "succeeded" and records[0]["http_status"] == 200
        else:
            assert item["execution_status"] == "failed", "Failure injection unexpectedly succeeded"
            if scenario == "auth":
                # Flowise 3.1.2 的 Prediction 把 Unauthorized 包装成 500；
                # 必须另核对 Flowise 日志和无原生模型 execution，不能称为 401。
                assert records[0]["http_status"] in (401, 403, 500)
            if scenario == "timeout":
                assert records[0]["http_status"] is None
                assert records[0]["error_type"] == "AMBIGUOUS_OUTCOME"
                assert records[0]["request_phase"] == "MAY_HAVE_BEEN_SENT"
                assert records[0]["latency_ms"] >= 900
            if scenario == "unreachable":
                assert records[0]["http_status"] == 500


def validate_native_evidence(launches, executions, receipts, namespace):
    """只核验 HTTP/原生工具证据，不把本地质量 fail 改成 pass。"""
    records = {r["scenario"]: r for r in launches}
    success = records["success"]
    outputs = {key: case["candidate"]["output"] for key, case in success["case_outputs"].items()}
    assert set(outputs) == {"plain", "tools", "structured"}
    for name, output in outputs.items():
        assert isinstance(output, dict) and "text" in output and "answer" not in output
        assert output["sessionId"] == namespace + "-" + name
        native = [e for e in executions if e["id"] == output["executionId"]]
        assert len(native) == 1 and native[0]["state"] == "FINISHED"
    assert outputs["plain"]["text"].strip() == "READY"
    assert json.loads(outputs["structured"]["text"]) == {"status": "ok", "value": 42}
    tool_output = outputs["tools"]
    node = next(n["data"] for n in tool_output["agentFlowExecutedData"] if n["nodeId"].startswith("agentAgentflow"))
    assert node["input"]["agentEnableMemory"] is False
    assert "secret-marker-" + namespace not in json.dumps(node["input"]["messages"])
    assert node["output"]["usageMetadata"]["total_tokens"] > 0
    tools = node["output"]["usedTools"]
    lookup = [t for t in tools if t["tool"] == "evidence_lookup"]
    calculator = [t for t in tools if t["tool"] == "calculator"]
    assert len(lookup) == len(calculator) == 1
    assert calculator[0]["toolOutput"] == "1266"
    receipt = json.loads(lookup[0]["toolOutput"])
    assert receipt in receipts and receipt["case_id"] == namespace + "-tools"
    assert receipt["receipt"] in tool_output["text"]
    expected_sessions = Counter({namespace + "-" + name: 1 for name in ["plain", "tools", "structured", "timeout"]})
    # 模型不可达使用另一个 flow；复用 timeout 逻辑 Item，共两条原生执行记录。
    expected_sessions[namespace + "-timeout"] += 1
    assert Counter(e["sessionId"] for e in executions) == expected_sessions
    assert not any(e["sessionId"] == namespace + "-auth" for e in executions)
    assert len(receipts) == 1
    return {
        "native_execution_count": len(executions),
        "tool_request_count": len(receipts),
        "session_isolation": "PASS",
        "native_text_preserved": "PASS",
        "model_usage_present": "PASS",
        "note": "Auth 500 requires independent Unauthorized server log; timeout is AMBIGUOUS_OUTCOME, not a cancelled model task",
    }


def run(env, resume=None):
    client = Clients(env)
    client.login()
    resources = json.loads((RUNTIME / "resources.json").read_text())
    info = client.argus("/system/info")
    if info["build_id"] != env["ARGUS_BUILD_ID"]:
        raise RuntimeError("Runner SHA mismatch")
    namespace = resume or "i213-" + uuid.uuid4().hex[:12]
    if not namespace.startswith("i213-") or not all(c.isalnum() or c == "-" for c in namespace):
        raise ValueError("Invalid run namespace")
    out = RUNTIME / namespace
    cases = materialize_cases(namespace)
    safe_write(out / "cases.json", cases)
    safe_write(out / "runtime.json", {"runner": info, "model": env["MODEL_NAME"], "resources": resources})
    agent_id = namespace
    if not resume:
        client.argus("/agents", {"id": agent_id, "name": "Flowise HTTP E2E " + namespace})
    launches = []
    for scenario in ["success", "auth", "timeout", "unreachable"]:
        selected = [c for c in cases if c["scenario"] == ("timeout" if scenario == "unreachable" else scenario)]
        dataset = namespace + "-" + scenario
        created_path = out / (scenario + "-created.json")
        if created_path.exists():
            # 恢复只读收集既有 Launch，不再次调用 Agent。
            created = json.loads(created_path.read_text())
            launch = created["launch"]
            lid = launch["id"]
            dataset_ids = {i["id"]: i["metadata"]["case_id"] for i in launch["manifest"]["dataset"]["items"]}
        else:
            client.lf("/v2/datasets", {"name": dataset, "description": "Isolated Issue213 " + scenario})
            dataset_ids = {}
            for case in selected:
                item = client.lf(
                    "/dataset-items",
                    {
                        "datasetName": dataset,
                        "input": case["input"],
                        "expectedOutput": case["expected_output"],
                        "metadata": {"issue": 213, "case_id": case["id"], "scenario": scenario},
                    },
                )
                dataset_ids[item["id"]] = case["id"]
            # Langfuse 写后读有延迟；在冻结 Manifest 前核对全部 Item 可见。
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                visible = client.lf("/dataset-items?" + urllib.parse.urlencode({"datasetName": dataset, "limit": 100}))
                if {i["id"] for i in visible.get("data", [])} == set(dataset_ids):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Dataset items not fully visible; no Launch was submitted")
            flow_id = resources["flow_ids"]["unreachable" if scenario == "unreachable" else "success"]
            version = client.argus(
                "/agent-versions", version_body(agent_id, scenario, flow_id, scenario, resources["image"])
            )
            launch = client.argus(
                "/experiment-launches", launch_body(agent_id, scenario, dataset, namespace + "-" + scenario)
            )
            lid = launch["id"]
            safe_write(out / (scenario + "-created.json"), {"version": version, "launch": launch})
            client.argus("/experiment-launches/" + lid + "/run", {})
        launch = wait_launch(client, lid, time.monotonic() + 240)
        items = client.argus("/experiment-launches/" + lid + "/items")
        attempts = {i["id"]: client.argus("/experiment-item-executions/" + i["id"] + "/attempts") for i in items}
        summary = client.argus("/experiment-launches/" + lid + "/summary")
        outputs = {}
        for item in items:
            query = urllib.parse.urlencode(
                {"snapshot_id": summary["snapshot_id"], "dataset_item_id": item["dataset_item_id"]}
            )
            output_deadline = time.monotonic() + 60
            while True:
                case_output = client.argus("/experiment-launches/" + lid + "/comparison/case?" + query)
                if case_output["candidate"]["output_status"] == "AVAILABLE" or time.monotonic() >= output_deadline:
                    break
                time.sleep(1)
            outputs[dataset_ids[item["dataset_item_id"]]] = case_output
        record = {
            "scenario": scenario,
            "launch": launch,
            "items": items,
            "attempts": attempts,
            "summary": summary,
            "case_outputs": outputs,
            "dataset_ids": dataset_ids,
        }
        safe_write(out / (scenario + ".json"), record)
        assert len(items) == len(selected), "Frozen Dataset omitted an Item"
        validate_attempts(items, attempts, scenario)
        launches.append(record)
    # 原生运行记录与独立工具请求记录均不是 Argus/LLM 自报判断。
    executions = []
    for flow_id in resources["flow_ids"].values():
        page = 1
        while True:
            batch = client.flow(
                "/executions?" + urllib.parse.urlencode({"agentflowId": flow_id, "page": page, "limit": 100})
            )
            executions.extend(e for e in batch["data"] if e["sessionId"].startswith(namespace))
            if page * 100 >= batch["total"]:
                break
            page += 1
    receipts = expect(*request(env.get("TOOL_URL", "http://127.0.0.1:" + env.get("TOOL_PORT", "19094")), "/receipts"))
    safe_write(out / "flowise-executions.json", executions)
    safe_write(out / "tool-receipts.json", [r for r in receipts if r["case_id"].startswith(namespace)])
    safe_write(
        out / "launch-index.json",
        {
            "namespace": namespace,
            "cases_count": len(cases),
            "unique_case_ids": [c["id"] for c in cases],
            "launches": [{"scenario": x["scenario"], "id": x["launch"]["id"]} for x in launches],
            "at": dt.datetime.now(dt.UTC).isoformat(),
        },
    )
    verdict = validate_native_evidence(
        launches, executions, [r for r in receipts if r["case_id"].startswith(namespace)], namespace
    )
    safe_write(out / "native-verdict.json", verdict)
    print("HTTP execution matrix collected:", out)
    print("Inspect raw outputs/session isolation and external tool evidence before claiming acceptance.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "run"])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--resume", help="Collect an existing namespace without repeating submitted Launches")
    parser.add_argument(
        "--register-owner", action="store_true", help="Only for a newly-created dedicated local Flowise"
    )
    args = parser.parse_args()
    env = load_env(args.env_file)
    if args.action == "setup":
        setup(env, register_owner=args.register_owner)
    else:
        run(env, resume=args.resume)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, urllib.error.URLError, AssertionError) as error:
        # 不 dump 含 credential 的 Request、环境或响应。
        raise SystemExit(str(error) or "Acceptance assertion failed; inspect local evidence") from None
