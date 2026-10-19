#!/usr/bin/env python3
"""Read-only Issue #214 evidence probe, not a Score Collector or gate input.

Reads live public APIs using an explicit receipt. It never creates scores,
executes evaluators, changes rules, updates Argus, or treats missing data as 0.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class EvidenceError(ValueError):
    """Evidence is missing, ambiguous, or does not match the receipt."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def parse_io(value: Any) -> Any:
    """Observations v2 returns raw strings; never request deprecated parseIoAsJson=true."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def verify_observation(rows: list[dict], receipt: dict) -> dict:
    require(all(isinstance(row, dict) for row in rows), "Malformed observation row")
    # Langfuse v4 may return a TOOL and an empty replay SPAN with the same IDs.
    candidates = [row for row in rows if row.get("id") == receipt["observation_id"]
                  and row.get("traceId") == receipt["trace_id"] and row.get("type") == "TOOL"
                  and row.get("name") == "remote-agent-http"]
    require(len(candidates) == 1, "Expected exactly one business TOOL, not a replay SPAN")
    row = candidates[0]
    require(row.get("environment") == receipt["environment"], "Observation environment mismatch")
    for field in ("input", "output"):
        require(row.get(field) is not None, f"Missing observation {field}")
        require(json_digest(parse_io(row[field])) == receipt[f"{field}_digest"], f"Native observation {field} mismatch")
    return row


def verify_scores(rows: list[dict], receipt: dict) -> list[dict]:
    require(all(isinstance(row, dict) and isinstance(row.get("metadata"), dict) for row in rows),
            "Missing score provenance metadata")
    expected = receipt["evaluators"]
    require(len(expected) == 2 and {e["type"] for e in expected} == {"code", "llm_as_judge"}
            and len({e["id"] for e in expected}) == 2, "Require distinct LLM judge and Code evaluator identities")
    require(len(rows) == 2 and len({s.get("id") for s in rows}) == 2, "Missing or ambiguous managed scores")
    verified = []
    for evaluator in expected:
        matches = [s for s in rows if s.get("metadata", {}).get("evaluator_id") == evaluator["id"]]
        require(len(matches) == 1, "Missing or duplicate evaluator score; do not choose latest")
        score = matches[0]
        require(score.get("source") == "EVAL", "Manual API/annotation scores are not managed proof")
        require(score.get("name") == evaluator["score_name"], "Score name mismatch")
        require(score.get("environment") == receipt["environment"], "Score environment mismatch")
        require(score.get("dataType") == "BOOLEAN" and type(score.get("value")) is bool
                and score["value"] is True, "Expected a genuine BOOLEAN true smoke-test score")
        require(score.get("subject") == {"kind": "observation", "id": receipt["observation_id"],
                                        "traceId": receipt["trace_id"]}, "Score subject mismatch")
        metadata = score.get("metadata", {})
        for key, value in {"evaluator_version_id": evaluator["version_id"],
                           "evaluation_rule_id": evaluator["rule_id"],
                           "target_trace_id": receipt["trace_id"],
                           "target_observation_id": receipt["observation_id"]}.items():
            require(metadata.get(key) == value, f"Managed score {key} mismatch")
        require(bool(metadata.get("job_execution_id")), "Missing Langfuse job execution provenance")
        if evaluator["type"] == "code":
            require(bool(metadata.get("dispatcher_name")) and metadata.get("code_eval_runtime") == "PYTHON",
                    "Missing remote Python runtime provenance")
            if receipt.get("code_io_hashes"):
                for field in ("input", "output"):
                    require(metadata.get(f"{field}_sha256") == receipt[f"{field}_digest"],
                            f"Remote Code evaluator {field} does not match actual Agent IO")
        verified.append(score)
    return verified


def verify_experiment_item(rows: list[dict], receipt: dict) -> dict:
    require(all(isinstance(row, dict) for row in rows), "Malformed Experiment Item row")
    expected = {"id": receipt["observation_id"], "traceId": receipt["trace_id"],
                "experimentId": receipt["experiment_id"], "experimentDatasetId": receipt["dataset_id"],
                "experimentItemId": receipt["dataset_item_id"]}
    matches = [row for row in rows if all(row.get(k) == v for k, v in expected.items())]
    require(len(matches) == 1, "Missing or ambiguous Experiment / Dataset Item / root Observation join")
    return matches[0]


def read_pages(fetch, path: str, params: dict, *, max_pages: int = 100) -> list:
    params = dict(params)
    rows, seen = [], set()
    for _ in range(max_pages):
        page = fetch(path, params)
        require(isinstance(page, dict) and isinstance(page.get("data"), list), "Malformed API data, not an empty page")
        rows.extend(page["data"])
        meta = page.get("meta")
        require(isinstance(meta, dict), "Missing API pagination metadata")
        cursor = meta.get("cursor")
        if cursor is None:
            return rows
        require(isinstance(cursor, str) and bool(cursor) and cursor not in seen, "Invalid or repeated API cursor")
        seen.add(cursor)
        params["cursor"] = cursor
    raise EvidenceError("API pagination exceeded bounded probe limit")


class ReadOnlyLangfuse:
    REQUEST_INTERVAL_SECONDS = 2.1  # Stay below the observed Cloud 30 requests/minute limit.
    ALLOWED_PATHS = frozenset({"/api/public/v3/scores", "/api/public/v2/observations",
                               "/api/public/experiment-items", "/api/public/experiments"})

    def __init__(self, base_url: str, public_key: str, secret_key: str, evidence_dir: Path | None = None):
        parsed = urllib.parse.urlparse(base_url)
        require(parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password
                and not parsed.path.strip("/") and not parsed.query and not parsed.fragment,
                "Use a credential-free HTTPS Langfuse origin")
        self.base_url = base_url.rstrip("/")
        self.authorization = "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._sensitive_values = tuple(v for v in (public_key, secret_key) if v)
        self.evidence_dir = evidence_dir
        self.requests: list[dict] = []
        self._last_request_at: float | None = None

    def get(self, path: str, params: dict) -> dict:
        require(path in self.ALLOWED_PATHS, "Probe only permits allowlisted read endpoints")
        query = urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(self.base_url + path + "?" + query,
                                     headers={"Authorization": self.authorization}, method="GET")
        for attempt in range(3):
            if self._last_request_at is not None:
                time.sleep(max(0, self.REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last_request_at)))
            self._last_request_at = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=45) as response:
                    result = json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                self.requests.append({"method": "GET", "path": path, "query": params.copy(), "status": exc.code})
                if exc.code == 429 and attempt < 2:
                    try:
                        detail = json.loads(exc.read())
                        delay = float(detail["details"]["retryAfterSeconds"])
                    except (ValueError, KeyError, TypeError):
                        delay = 30.0
                    require(0 <= delay <= 60, "Rate-limit wait exceeds bounded probe limit")
                    time.sleep(delay + 1)
                    continue
                raise EvidenceError(f"Langfuse read failed: HTTP {exc.code} on {path}") from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                raise EvidenceError(f"Langfuse read unavailable or invalid JSON on {path}") from None
        self.requests.append({"method": "GET", "path": path, "query": params.copy()})
        if self.evidence_dir:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            destination = self.evidence_dir / f"read-{len(self.requests):03d}.json"
            serialized = json.dumps({"request": self.requests[-1], "response": result}, ensure_ascii=False, indent=2)
            for secret in self._sensitive_values:
                serialized = serialized.replace(secret, "[redacted]")
            destination.write_text(serialized + "\n")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path, help="Explicit observation, IO digest, evaluator/rule and Experiment identities")
    parser.add_argument("--evidence-dir", type=Path, required=True, help="Save fresh raw read responses and audit report here")
    parser.add_argument("--page-size", type=int, default=100, choices=range(1, 101), metavar="1..100")
    args = parser.parse_args()
    client = ReadOnlyLangfuse(*(os.environ[k] for k in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")), args.evidence_dir)
    receipt = json.loads(args.receipt.read_text())
    checks = []
    for run in receipt["runs"]:
        observations = read_pages(client.get, "/api/public/v2/observations", {
            "traceId": run["trace_id"], "fields": "core,basic,io,metadata,trace_context",
            "limit": args.page_size})
        verify_observation(observations, run)
        scores = read_pages(client.get, "/api/public/v3/scores", {
            "traceId": run["trace_id"], "observationId": run["observation_id"], "source": "EVAL",
            "fields": "subject,details", "limit": args.page_size})
        verified = verify_scores(scores, run)
        items = read_pages(client.get, "/api/public/experiment-items", {
            "experimentId": run["experiment_id"], "fromStartTime": run["from_start_time"], "limit": args.page_size})
        verify_experiment_item(items, run)
        checks.append({"launch_id": run["launch_id"], "trace_id": run["trace_id"], "score_ids": [s["id"] for s in verified]})
    report = {"status": "PASS", "read_only": True, "score_write_requests": 0, "checks": checks, "requests": client.requests}
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    (args.evidence_dir / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("status", "read_only", "score_write_requests", "checks")}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvidenceError, KeyError, OSError, ValueError) as exc:
        # Do not print environment values, headers, or traceback with locals.
        print(f"FAIL: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
