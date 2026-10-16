#!/usr/bin/env python3
"""One-shot ops backfill: re-link historical Langfuse DatasetRunItems (Issue 198 S6.2).

Background: 2026-10-13 起 compose 云栈 `ARGUS_DATASET_SOURCE` 默认曾为 `seed`
（4591d28, S4.0），版本化 Launch 冻结 `dataset.source=seed` 后跳过了
`dataset_run_items.create`，导致云端 Langfuse run 有 URL 但没有任何 run item
（详情打开 404）。本脚本按显式传入的 Launch ID，从 Runner HTTP API 读取
item ↔ trace 归属，并以与 worker 相同的调用路径补绑 run items。

硬边界：
- Langfuse 云端凭据在运行时从 `.env.cloud` 加载；脚本不打印、不持久化任何凭据值。
- 只处理显式传入的 Launch ID，无通配符、无自动发现兜底。
- 重复绑定（HTTP 409 / duplicate）按 Skip 记录，不算失败。
- 真实云端写操作必须先获得用户显式批准该命令。

用法（真实执行前先 --dry-run）：
    python scripts/backfill_langfuse_run_items.py \
        --base-url http://localhost:8000 \
        --launch-id <uuid> [--launch-id <uuid> ...] \
        [--env-file .env.cloud] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVAL_RUNNER = ROOT / "services" / "eval-runner"
sys.path.insert(0, str(EVAL_RUNNER))

_LANGFUSE_PREFIX = "LANGFUSE_"
_REQUIRED_LANGFUSE_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
_DUPLICATE_MARKERS = ("duplicate", "conflict", "already exists", "unique constraint")


def _read_env_file(path: Path) -> dict[str, str]:
    """解析 `KEY=VALUE` 行；只保留 LANGFUSE_ 前缀，值仅在进程内存中流转。"""
    raw: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key.startswith(_LANGFUSE_PREFIX):
            raw[key] = value
    return raw


def load_langfuse_env(env_path: Path) -> dict[str, str]:
    """加载 Langfuse 凭据；缺失必需键时退出并列出缺失键名（不回显值）。"""
    credentials = _read_env_file(env_path)
    missing = [name for name in _REQUIRED_LANGFUSE_KEYS if not credentials.get(name)]
    if missing:
        raise SystemExit(
            f"Missing required Langfuse credentials in {env_path}: {', '.join(missing)} "
            "(values are never printed)."
        )
    return credentials


def _is_duplicate_error(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 409:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _DUPLICATE_MARKERS)


def _frozen_dataset_version(manifest_dataset: dict[str, Any]) -> str:
    """worker 侧等价：显式冻结版本必填；可解析的 datetime 归一化为 ISO 字符串。"""
    from app.dataset import parse_dataset_version

    raw_version = manifest_dataset.get("dataset_version") or manifest_dataset.get("version")
    raw_text = str(raw_version or "").strip()
    if not raw_text:
        raise ValueError("Frozen launch manifest is missing an explicit dataset version; refusing backfill.")
    try:
        parsed = parse_dataset_version(raw_text)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed.isoformat()
    if raw_text.lower() == "latest":
        raise ValueError(f"Refusing backfill with non-frozen dataset version '{raw_text}'.")
    return raw_text


def build_run_items(launch: dict[str, Any], items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 Runner API 返回的 launch/item 归属组装成与 worker 相同的 `dataset_run_items.create` 载荷。

    Skips 在返回值中显式记录原因；载荷本身与 `langfuse_sync.py` 的 worker 路径一致。
    """
    manifest = launch.get("manifest") or {}
    manifest_dataset = manifest.get("dataset") or {}
    if str(manifest_dataset.get("source", "")).lower() != "langfuse":
        raise ValueError(
            f"Launch {launch.get('id')} manifest dataset source is "
            f"'{manifest_dataset.get('source')}', not 'langfuse'; skip this launch explicitly instead."
        )
    run_name = manifest.get("name") or f"argus-{str(launch.get('id') or '')[:12]}"
    dataset_version = _frozen_dataset_version(manifest_dataset)

    payloads: list[dict[str, Any]] = []
    skips: list[dict[str, Any]] = []
    for item in items:
        item_id = str(item.get("id") or "")
        if str(item.get("execution_status") or "").lower() != "succeeded":
            skips.append({"item_execution_id": item_id, "dataset_item_id": item.get("dataset_item_id"), "reason": "EXECUTION_NOT_SUCCEEDED"})
            continue
        trace_id = item.get("trace_id")
        if not trace_id:
            skips.append({"item_execution_id": item_id, "dataset_item_id": item.get("dataset_item_id"), "reason": "MISSING_TRACE"})
            continue
        payloads.append({
            "run_name": run_name,
            "dataset_item_id": str(item.get("dataset_item_id") or ""),
            "trace_id": str(trace_id),
            "observation_id": item.get("observation_id"),
            "dataset_version": dataset_version,
        })
    return payloads, skips


def apply_backfill(lf: Any, payloads: list[dict[str, Any]], *, dry_run: bool) -> list[dict[str, Any]]:
    """逐条调用与 worker 相同的 `lf.api.dataset_run_items.create`。

    返回逐条 outcome：CREATED / SKIPPED_DUPLICATE / FAILED / DRY_RUN。
    """
    outcomes: list[dict[str, Any]] = []
    for payload in payloads:
        entry: dict[str, Any] = {"dataset_item_id": payload["dataset_item_id"]}
        if dry_run:
            entry["status"] = "DRY_RUN"
            outcomes.append(entry)
            continue
        try:
            lf.api.dataset_run_items.create(
                run_name=payload["run_name"],
                dataset_item_id=payload["dataset_item_id"],
                trace_id=payload["trace_id"],
                observation_id=payload.get("observation_id"),
                dataset_version=payload["dataset_version"],
            )
        except Exception as exc:  # noqa: BLE001 — 逐条独立归类，不让单条失败跳过整批
            if _is_duplicate_error(exc):
                entry["status"] = "SKIPPED_DUPLICATE"
            else:
                entry["status"] = "FAILED"
                entry["error"] = type(exc).__name__
        else:
            entry["status"] = "CREATED"
        outcomes.append(entry)
    return outcomes


def fetch_launch_and_items(launch_id: str, base_url: str, timeout_seconds: float = 30.0) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Runner HTTP API：launch 详情（含冻结 manifest）与 item ↔ trace 归属。"""
    base = base_url.rstrip("/")
    with urllib.request.urlopen(f"{base}/experiment-launches/{launch_id}", timeout=timeout_seconds) as resp:
        launch = resp.read()
    import json

    launch_detail = json.loads(launch.decode("utf-8"))
    with urllib.request.urlopen(f"{base}/experiment-launches/{launch_id}/items?limit=500", timeout=timeout_seconds) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    items = payload if isinstance(payload, list) else payload.get("data") or []
    return launch_detail, items


def backfill_launches(
    launch_ids: list[str],
    base_url: str,
    *,
    env_path: Path,
    dry_run: bool,
    lf: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """对显式传入的 Launch ID 依次补绑；返回每个 launch 的 outcome 汇总。"""
    if lf is None:
        credentials = load_langfuse_env(env_path)
        import os

        for key, value in credentials.items():
            os.environ[key] = value
        from langfuse import get_client

        lf = get_client()
        if lf is None:
            raise SystemExit("Langfuse client could not be initialized; check credentials in env file.")

    report: dict[str, dict[str, Any]] = {}
    for launch_id in launch_ids:
        launch, items = fetch_launch_and_items(launch_id, base_url)
        payloads, skips = build_run_items(launch, items)
        outcomes = apply_backfill(lf, payloads, dry_run=dry_run)
        report[launch_id] = {"payloads": outcomes, "skips": skips}
        created = sum(1 for o in outcomes if o["status"] == "CREATED")
        duplicated = sum(1 for o in outcomes if o["status"] == "SKIPPED_DUPLICATE")
        failed = sum(1 for o in outcomes if o["status"] == "FAILED")
        print(
            f"launch={launch_id} created={created} duplicated={duplicated} failed={failed} "
            f"skips={len(skips)} dry_run={dry_run}"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-link historical Langfuse DatasetRunItems for explicit launch IDs.")
    parser.add_argument("--base-url", required=True, help="Eval Runner HTTP base URL (e.g. http://localhost:8000)")
    parser.add_argument("--launch-id", action="append", required=True, help="Explicit launch id (repeatable; no wildcards)")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.cloud", help="Env file providing LANGFUSE_* credentials")
    parser.add_argument("--dry-run", action="store_true", help="Assemble payloads without touching the cloud")
    args = parser.parse_args(argv)

    report = backfill_launches(
        args.launch_id,
        args.base_url,
        env_path=args.env_file,
        dry_run=args.dry_run,
    )
    failed = sum(
        outcome["status"] == "FAILED"
        for results in report.values()
        for outcome in results["payloads"]
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
