"""将真实 API 元数据报告压缩成可提交摘要；不读取/输出业务原文。"""

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path


def summarize(source):
    raw = json.loads(source.read_text())
    profiles = {}
    for profile in sorted({case["profile"] for case in raw["cases"]}):
        cases = [case for case in raw["cases"] if case["profile"] == profile]
        delays = sorted(case["visibility"]["elapsed_ms"] for case in cases)
        fields = {}
        for key in cases[0]["fields"]:
            types = Counter()
            for case in cases:
                types.update(case["fields"][key]["types"])
            fields[key] = {name: sum(case["fields"][key][name] for case in cases)
                           for name in ("total", "absent", "null", "empty_string", "empty_object", "empty_array", "encoded_empty_object")}
            fields[key]["types"] = dict(types)
        metadata = {}
        for key in sorted({key for case in cases for key in case["metadata_fields"]}):
            types = Counter()
            for case in cases:
                types.update(case["metadata_fields"].get(key, {}).get("types", {}))
            metadata[key] = {"present": sum(case["metadata_fields"].get(key, {}).get("present", 0) for case in cases),
                             "types": dict(types)}
        profiles[profile] = {"cases": len(cases), "observations": sum(len(case["edges"]) for case in cases),
                             "fields": fields, "metadata": metadata,
                             "visibility_ms": {"min": delays[0], "median": statistics.median(delays),
                                               "p95": delays[math.ceil(len(delays) * .95) - 1], "max": delays[-1]}}
    return {key: raw[key] for key in ("captured_at", "server", "runner_sdk", "agent_packages", "api", "poll_interval_ms")} | {
        "raw_metadata_report_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "profiles": profiles,
        "cases": [{key: case[key] for key in ("profile", "scenario", "identity", "visibility", "pagination", "causal_sources", "dependency_edges")}
                  for case in raw["cases"]],
        "representative_lineages": [{key: case[key] for key in ("profile", "scenario", "identity", "edges", "span_classes")}
            for case in raw["cases"] if case["scenario"] in {"sync", "retry-failed", "retry-success", "approved", "approval-zero-escalate"}
            or (case["scenario"] == "serial" and case["profile"] == "http_only")],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = summarize(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # 各 case/profile 独立一行；完整矩阵留在 CI artifact，避免生成数据挤占 PR 审查。
    sections = []
    for key, value in result.items():
        heading = json.dumps(key) + ": "
        if isinstance(value, list):
            body = "[\n" + ",\n".join("    " + json.dumps(row, ensure_ascii=False) for row in value) + "\n  ]"
        elif key == "profiles":
            body = "{\n" + ",\n".join("    " + json.dumps(name) + ": " + json.dumps(profile, ensure_ascii=False)
                                      for name, profile in value.items()) + "\n  }"
        else:
            body = json.dumps(value, ensure_ascii=False)
        sections.append("  " + heading + body)
    args.output.write_text("{\n" + ",\n".join(sections) + "\n}\n")
    print("Summarized real observation metadata:", sum(value["cases"] for value in result["profiles"].values()), "cases")


if __name__ == "__main__":
    main()
