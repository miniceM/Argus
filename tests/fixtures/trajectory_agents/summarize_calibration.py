"""验证并压缩来源校准元数据；不读取原始业务输入、输出或完整 Trace。"""

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path


def distribution(values):
    ordered = sorted(round(value, 3) if type(value) is float else value for value in values)
    return {"min": ordered[0], "median": statistics.median(ordered),
            "p95": ordered[math.ceil(len(ordered) * .95) - 1], "max": ordered[-1]}


def summarize(source):
    raw = json.loads(source.read_text())
    samples = [case for case in raw["cases"] if case.get("normalization", {}).get("calibration_sample")
               and not case["scenario"].endswith("-admission")]
    expected = {profile + ":" + scenario for profile in ("native_otel", "pydantic_ai")
                for scenario in ("serial", "blocked", "parallel", "late_all")} | {"http_only:serial"}
    if {case["normalization"]["qualification_group"] for case in samples} != expected:
        raise ValueError("CALIBRATION_GROUPS_INCOMPLETE")
    groups = {}
    for name in sorted(expected):
        cases = [case for case in samples if case["normalization"]["qualification_group"] == name]
        normalized = [case["normalization"] for case in cases]
        if (len(cases) != 10 or len({case["identity"]["attempt_id"] for case in cases}) != 10
                or not all(row["original_deadline_preserved"] and row["artifact_roundtrip"]
                           and row["parent_closed"] and row["acquisition_state"] == "READY" for row in normalized)):
            raise ValueError("CALIBRATION_SAMPLES_INVALID")
        identities = {tuple(row[key] for key in (
            "trajectory_contract_digest", "reader_artifact_digest", "api_document_digest")) for row in normalized}
        if len(identities) != 1 or not any(case["scenario"] == "calibration-" + name.split(":")[1] + "-admission"
                and case["profile"] == name.split(":")[0]
                and case.get("normalization", {}).get("qualified_factory_read") for case in raw["cases"]):
            raise ValueError("CALIBRATION_ADMISSION_UNPROVEN")
        fields = {}
        for field in ("id", "traceId", "parentObservationId", "startTime", "endTime", "type", "name", "input", "output"):
            types = Counter()
            for case in cases:
                types.update(case["fields"][field]["types"])
            fields[field] = {key: sum(case["fields"][field][key] for case in cases)
                            for key in ("total", "absent", "null")}
            fields[field]["types"] = dict(types)
            if fields[field]["absent"]:
                raise ValueError("CALIBRATION_FIELDS_INCOMPLETE")
        groups[name] = {"samples": 10, "approved_identity": dict(zip((
            "trajectory_contract_digest", "reader_artifact_digest", "api_document_digest"), next(iter(identities)), strict=True)),
            "profile_version": normalized[0]["source_profile_version"], "factory_read_and_close": True,
            "real_capacity_boundary_samples": sum(row.get("real_capacity_boundaries", False) for row in normalized),
            "fields": fields, "completeness": dict(Counter(row["completeness"] for row in normalized)),
            "response_to_ready_ms": distribution([row["response_to_ready_ms"] for row in normalized]),
            "capacity": {key: distribution([row[key] for row in normalized]) for key in (
                "source_bytes", "acquisition_source_bytes", "artifact_bytes", "assembled_steps",
                "graph_max_depth", "cursor_hops", "causal_edges", "call_pairs")}}
    return {key: raw[key] for key in ("captured_at", "server", "runner_sdk", "agent_packages", "api")} | {
        "raw_metadata_report_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "samples": len(samples), "scope": "isolated-banking-fixture-only", "groups": groups}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = summarize(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # 一组一行，完整逐样本元数据保留在测试 artifact；不复制业务原文。
    groups = result.pop("groups")
    header = json.dumps(result, ensure_ascii=False, indent=2)[:-2]
    args.output.write_text(header + ',\n  "groups": {\n' + ",\n".join(
        "    " + json.dumps(name) + ": " + json.dumps(value, ensure_ascii=False)
        for name, value in groups.items()) + "\n  }\n}\n")
    print("Summarized calibrated real samples:", result["samples"])


if __name__ == "__main__":
    main()
