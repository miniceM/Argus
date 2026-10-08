"""CI 使用的评测入口：固定版本、等待终态、捕获 Snapshot，再请求 Gate。"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

TERMINAL = {"COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLED"}
ACTIVE = {"PENDING", "QUEUED", "RUNNING", "CANCELLING"}


class CLIError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _same_dataset_version(actual, requested):
    if not isinstance(actual, str):
        return False
    if actual == requested:
        return True
    try:
        left = datetime.fromisoformat(actual.replace("Z", "+00:00"))
        right = datetime.fromisoformat(requested.replace("Z", "+00:00"))
        return left.tzinfo is not None and right.tzinfo is not None and left == right
    except ValueError:
        return False


def _assert_candidate(launch, requested):
    """验证创建/恢复/轮询响应的完整版本身份，避免错误缓存放行其他候选。"""
    manifest = launch.get("manifest") or {}
    agent, dataset = manifest.get("agent", {}), manifest.get("dataset", {})
    bindings = [(binding.get("id"), binding.get("version")) for binding in manifest.get("evaluators", [])]
    expected = [(binding["id"], binding["version"]) for binding in requested["evaluator_selections"]]
    if (launch.get("agent_id") != requested["agent_id"] or agent.get("agent_id") != requested["agent_id"]
            or launch.get("agent_version") != requested["agent_version"] or agent.get("version") != requested["agent_version"]
            or launch.get("dataset_name") != requested["dataset_name"] or dataset.get("dataset_name") != requested["dataset_name"]
            or not _same_dataset_version(launch.get("dataset_version"), requested["dataset_version"])
            or not _same_dataset_version(dataset.get("dataset_version"), requested["dataset_version"])
            or sorted(bindings) != sorted(expected)
            or manifest.get("comparison", {}).get("environment") != requested["environment"]
            or ("baseline_snapshot_id" in requested and manifest.get("comparison", {}).get("baseline_snapshot_id") != requested["baseline_snapshot_id"])):
        raise CLIError("CANDIDATE_IDENTITY_MISMATCH")


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # 不回显参数值；将所有参数错误纳入 JSON 证据与退出码契约。
        raise CLIError("INVALID_ARGUMENTS")


def _parser():
    parser = SafeArgumentParser(prog="argus", description="Argus 评测与发布门禁")
    evaluation = parser.add_subparsers(dest="group", required=True).add_parser("eval")
    commands = evaluation.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-url", default=os.getenv("ARGUS_API_URL", "http://localhost:18080"))
    common.add_argument("--timeout", type=float, default=300, help="总等待预算（秒），默认 300")
    common.add_argument("--report", type=Path, help="同时保存 JSON 门禁证据")
    for name in ("run", "wait", "result"):
        command = commands.add_parser(name, parents=[common])
        if name == "result":
            command.add_argument("--gate-id", required=True)
            continue
        command.add_argument("--policy", required=True)
        command.add_argument("--policy-version", required=True)
        command.add_argument("--poll-interval", type=float, default=1)
        if name == "wait":
            command.add_argument("--launch-id", required=True)
            continue
        command.add_argument("--agent", required=True)
        command.add_argument("--agent-version", required=True)
        command.add_argument("--dataset", required=True)
        command.add_argument("--dataset-version", required=True)
        command.add_argument("--evaluator", action="append", required=True, help="固定 id@version，可重复")
        command.add_argument("--environment", help="省略时使用策略 environment")
        command.add_argument("--baseline-snapshot")
        command.add_argument("--idempotency-key")
        command.add_argument("--wait", action="store_true", help="等待完成（run 默认即等待）")
    return parser


def _exact(value):
    if not isinstance(value, str) or not value or value != value.strip() or value.lower() in {"latest", "dev", "main", "head"}:
        raise CLIError("INVALID_VERSION")
    return value


def _validate(args):
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise CLIError("INVALID_TIMEOUT")
    url = urlsplit(args.api_url)
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(char.isspace() or char == "\\" for char in args.api_url)):
        raise CLIError("INVALID_API_URL")
    if os.getenv("ARGUS_API_TOKEN") and url.scheme != "https":
        try:
            loopback = ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            loopback = url.hostname == "localhost"
        if not loopback:
            raise CLIError("INSECURE_AUTH_TRANSPORT")
    if args.command == "result":
        return []
    _exact(args.policy_version)
    if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
        raise CLIError("INVALID_POLL_INTERVAL")
    selections = []
    if args.command == "run":
        if args.idempotency_key is not None and len(args.idempotency_key) > 128:
            raise CLIError("INVALID_IDEMPOTENCY_KEY")
        _exact(args.agent_version)
        _exact(args.dataset_version)
        for entry in args.evaluator:
            parts = entry.rsplit("@", 1)
            if len(parts) != 2 or not parts[0].strip():
                raise CLIError("INVALID_VERSION")
            selections.append({"id": parts[0], "version": _exact(parts[1])})
        if len({selection["id"] for selection in selections}) != len(selections):
            raise CLIError("DUPLICATE_EVALUATOR")
    return selections


def _gate_exit(gate):
    if not all(isinstance(gate.get(key), str) and gate[key] for key in ("id", "candidate_launch_id", "candidate_snapshot_id")):
        raise CLIError("INVALID_RESPONSE")
    if gate.get("decision") == "PASS" and gate.get("releasable") is True:
        return 0
    if gate.get("decision") == "FAIL" and gate.get("releasable") is False:
        return 1
    if gate.get("decision") == "UNKNOWN" and gate.get("releasable") is False:
        return 2
    raise CLIError("INVALID_RESPONSE")


def main(argv=None, *, client=None, stdout=None, stderr=None, monotonic=time.monotonic, sleep=time.sleep):
    arguments = list(sys.argv[1:] if argv is None else argv)
    preliminary = SafeArgumentParser(add_help=False)
    preliminary.add_argument("--report", type=Path)
    report = None
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    launch_id = None
    idempotency_key = None
    owned = client is None
    result, code = {}, 2
    try:
        report = preliminary.parse_known_args(arguments)[0].report
        args = _parser().parse_args(arguments)
        selections = _validate(args)
        deadline = monotonic() + args.timeout
        if owned:
            token = os.getenv("ARGUS_API_TOKEN")
            client = httpx.Client(headers={"Authorization": f"Bearer {token}"} if token else {}, follow_redirects=False)

        def request(method, path, **kwargs):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CLIError("TIMEOUT")
            # 不重试写请求；网络结果不明时用户可用同一幂等键或已返回的 Launch ID 恢复。
            response = client.request(method, args.api_url.rstrip("/") + path, timeout=min(10, remaining), **kwargs)
            if monotonic() >= deadline:
                raise CLIError("TIMEOUT")
            if not 200 <= response.status_code < 300:
                raise CLIError(f"HTTP_{response.status_code}")
            try:
                data = response.json()
            except ValueError:
                raise CLIError("INVALID_RESPONSE") from None
            if not isinstance(data, dict):
                raise CLIError("INVALID_RESPONSE")
            return data

        if args.command == "result":
            result = request("GET", f"/api/v1/release-gates/{quote(args.gate_id, safe='')}")
            if result.get("id") != args.gate_id:
                raise CLIError("INVALID_RESPONSE")
        else:
            policy = request("GET", "/api/v1/release-policies", params={"name": args.policy, "version": args.policy_version})
            if policy.get("name") != args.policy or policy.get("version") != args.policy_version:
                raise CLIError("INVALID_RESPONSE")
            if args.command == "run":
                if policy.get("agent_id") != args.agent or (args.environment and policy.get("environment") != args.environment):
                    raise CLIError("POLICY_SCOPE_MISMATCH")
                if not policy.get("environment"):
                    raise CLIError("INVALID_RESPONSE")
                payload = {
                    "agent_id": args.agent, "agent_version": args.agent_version,
                    "dataset_name": args.dataset, "dataset_version": args.dataset_version,
                    "environment": policy["environment"], "evaluator_selections": selections,
                }
                if args.baseline_snapshot:
                    payload["baseline_snapshot_id"] = args.baseline_snapshot
                idempotency_key = args.idempotency_key or str(uuid.uuid4())
                launch = request("POST", "/api/v1/experiment-launches", json=payload,
                                 headers={"Idempotency-Key": idempotency_key})
                launch_id = launch.get("id")
                if not isinstance(launch_id, str) or not launch_id:
                    raise CLIError("INVALID_RESPONSE")
                _assert_candidate(launch, payload)
                if launch.get("status") == "PENDING":
                    path = f"/api/v1/experiment-launches/{quote(launch_id, safe='')}"
                    try:
                        request("POST", path + "/run")
                    except CLIError as exc:
                        if exc.code != "HTTP_409":
                            raise
                        recovered = request("GET", path)
                        if recovered.get("id") != launch_id:
                            raise CLIError("INVALID_RESPONSE") from None
                        _assert_candidate(recovered, payload)
                        if recovered.get("status") not in (ACTIVE | TERMINAL) - {"PENDING"}:
                            raise exc
            else:
                launch_id = args.launch_id
            path = f"/api/v1/experiment-launches/{quote(launch_id, safe='')}"
            while True:
                launch = request("GET", path)
                if launch.get("id") != launch_id:
                    raise CLIError("INVALID_RESPONSE")
                if args.command == "run":
                    _assert_candidate(launch, payload)
                environment = launch.get("manifest", {}).get("comparison", {}).get("environment", "production")
                if launch.get("agent_id") != policy.get("agent_id") or environment != policy.get("environment"):
                    raise CLIError("POLICY_SCOPE_MISMATCH")
                status = launch.get("status")
                if status in TERMINAL:
                    evaluating = (launch.get("progress") or {}).get("evaluating")
                    if not isinstance(evaluating, int) or isinstance(evaluating, bool) or evaluating < 0:
                        raise CLIError("INVALID_RESPONSE")
                    if evaluating == 0:
                        break
                elif status not in ACTIVE:
                    raise CLIError("UNKNOWN_LAUNCH_STATUS")
                sleep(min(args.poll_interval, max(0, deadline - monotonic())))
            summary = request("POST", path + "/result-snapshots")
            snapshot_id = summary.get("snapshot_id")
            if summary.get("launch_id") != launch_id or not isinstance(snapshot_id, str) or not snapshot_id:
                raise CLIError("INVALID_RESPONSE")
            result = request("POST", "/api/v1/release-gates/evaluate", json={
                "policy_name": args.policy, "policy_version": args.policy_version,
                "candidate_launch_id": launch_id, "candidate_snapshot_id": snapshot_id,
            })
            if (result.get("candidate_launch_id") != launch_id or result.get("candidate_snapshot_id") != snapshot_id
                    or result.get("policy", {}).get("name") != args.policy
                    or result.get("policy", {}).get("version") != args.policy_version):
                raise CLIError("INVALID_RESPONSE")
        code = _gate_exit(result)
    except (CLIError, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        error = exc.code if isinstance(exc, CLIError) else "CLIENT_ERROR"
        result = {"decision": "UNKNOWN", "releasable": False, "error_code": error,
                  "launch_id": launch_id, "idempotency_key": idempotency_key}
        print(f"Argus 发布被阻断：{error}", file=stderr)
    finally:
        if owned and client is not None:
            client.close()
    if report:
        try:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            code = 2
            result = {**result, "decision": "UNKNOWN", "releasable": False, "error_code": "REPORT_WRITE_FAILED"}
            print("Argus 发布被阻断：REPORT_WRITE_FAILED", file=stderr)
    print(json.dumps(result, ensure_ascii=False), file=stdout)
    return code
