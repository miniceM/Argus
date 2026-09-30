"""Explicit fake Langfuse SDK doubles for link/identity tests.

Every identity is a plain string on a ``SimpleNamespace`` so no auto-created mock
attribute can silently masquerade as a real project/dataset/run id.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

REQUEST_OPTIONS_KEY = "request_options"


class FakeProjects:
    def __init__(self, project_ids: tuple[str, ...] | list[str] = ("proj-real",)):
        self.project_ids = list(project_ids)
        self.calls: list[dict[str, Any]] = []
        self.raise_error: Exception | None = None

    def get(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raise_error is not None:
            raise self.raise_error
        return SimpleNamespace(data=[SimpleNamespace(id=pid) for pid in self.project_ids])


class FakeDatasets:
    def __init__(self, runs: dict[tuple[str, str], SimpleNamespace] | None = None):
        self.runs = runs or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.raise_error: Exception | None = None

    def get_run(self, dataset_name: str, run_name: str, **kwargs: Any) -> Any:
        self.calls.append((dataset_name, run_name, kwargs))
        if self.raise_error is not None:
            raise self.raise_error
        try:
            return self.runs[(dataset_name, run_name)]
        except KeyError as exc:  # pragma: no cover - explicit failure for wrong lookups
            raise LookupError(f"no such dataset run: {dataset_name}/{run_name}") from exc


class FakeDatasetRunItems:
    def __init__(self, dataset_run_id: str | None = "run-real"):
        self.dataset_run_id = dataset_run_id
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(dataset_run_id=self.dataset_run_id)


class FakeScores:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return SimpleNamespace(id=kwargs.get("id"))


class FakeLangfuseSDK:
    """Minimal Langfuse client double exposing only the public API surface we use."""

    def __init__(
        self,
        *,
        project_ids: tuple[str, ...] | list[str] = ("proj-real",),
        runs: dict[tuple[str, str], SimpleNamespace] | None = None,
        dataset_run_id: str | None = "run-real",
    ):
        self.projects = FakeProjects(project_ids)
        self.datasets = FakeDatasets(runs)
        self.dataset_run_items = FakeDatasetRunItems(dataset_run_id)
        self.scores = FakeScores()
        self.api = SimpleNamespace(
            projects=self.projects,
            datasets=self.datasets,
            dataset_run_items=self.dataset_run_items,
            scores=self.scores,
        )
        self.flushed = 0

    def flush(self) -> None:
        self.flushed += 1


def remote_run(dataset_id: str, run_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=run_id, dataset_id=dataset_id)
