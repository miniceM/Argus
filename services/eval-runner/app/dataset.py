from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from .config import find_path, settings


def _get_langfuse_client():
    from langfuse import get_client
    return get_client()


class DatasetResolver:
    """Resolves and snapshots datasets from either Langfuse (production) or local seed (testing/offline).

    Policy:
    - Source is controlled via ARGUS_DATASET_SOURCE environment variable ('langfuse' or 'seed').
    - When source='langfuse', failures MUST fail fast and NEVER silently fall back to local seed.
    - When source='seed', requested dataset_name MUST match the seed file's dataset name.
    - Frozen snapshot contains full provenance, items count, items list, and a deterministic SHA-256 snapshot_digest.
    """

    def __init__(self, source: str | None = None):
        if source:
            self.source = source.lower()
        else:
            env_source = os.getenv("ARGUS_DATASET_SOURCE")
            if env_source:
                self.source = env_source.lower()
            elif os.getenv("ARGUS_DB_MODE", "").lower() == "test":
                self.source = "seed"
            else:
                self.source = "langfuse"

    def resolve(self, dataset_name: str, dataset_version: str | None = None) -> dict[str, Any]:
        if self.source == "langfuse":
            return self._resolve_from_langfuse(dataset_name, dataset_version)
        elif self.source == "seed":
            return self._resolve_from_seed(dataset_name, dataset_version)
        else:
            raise ValueError(f"Unknown dataset source: '{self.source}'. Must be 'langfuse' or 'seed'.")

    def _resolve_from_langfuse(self, dataset_name: str, dataset_version: str | None) -> dict[str, Any]:
        try:
            lf = _get_langfuse_client()
            dataset = lf.get_dataset(dataset_name)
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch dataset '{dataset_name}' from Langfuse: {exc}") from exc

        raw_items = getattr(dataset, "items", []) or []
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            items.append({
                "id": str(getattr(raw, "id", "")),
                "input": getattr(raw, "input", None),
                "expected_output": getattr(raw, "expected_output", None),
                "metadata": getattr(raw, "metadata", None),
            })

        items.sort(key=lambda x: str(x["id"]))
        canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
        snapshot_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        dataset_id = str(getattr(dataset, "id", "")) or f"lf-{dataset_name}"
        effective_version = dataset_version or str(getattr(dataset, "version", "latest"))

        return {
            "source": "langfuse",
            "dataset_name": dataset_name,
            "dataset_id": dataset_id,
            "dataset_version": effective_version,
            "snapshot_digest": snapshot_digest,
            "items_count": len(items),
            "items": items,
        }

    def _resolve_from_seed(self, dataset_name: str, dataset_version: str | None) -> dict[str, Any]:
        seed_path = find_path(settings.dataset_seed_path, "data", "dataset.json")
        if not seed_path.exists():
            raise FileNotFoundError(f"Dataset seed file not found: {seed_path}")

        seed_data = json.loads(seed_path.read_text(encoding="utf-8"))
        name_in_seed = seed_data.get("name") or seed_data.get("dataset_name")

        if name_in_seed != dataset_name:
            raise ValueError(
                f"Dataset '{dataset_name}' not found in seed file (found: '{name_in_seed}'). "
                "Seed mode strictly checks dataset name."
            )

        raw_items = seed_data.get("items", [])
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            items.append({
                "id": str(raw["id"]),
                "input": raw.get("input"),
                "expected_output": raw.get("expected_output"),
                "metadata": raw.get("metadata"),
            })

        items.sort(key=lambda x: str(x["id"]))
        canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
        snapshot_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        dataset_id = seed_data.get("id") or f"seed-{dataset_name}"
        effective_version = dataset_version or seed_data.get("version") or "seed-v1"

        return {
            "source": "seed",
            "dataset_name": dataset_name,
            "dataset_id": dataset_id,
            "dataset_version": effective_version,
            "snapshot_digest": snapshot_digest,
            "items_count": len(items),
            "items": items,
        }
