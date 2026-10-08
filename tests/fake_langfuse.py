"""Explicit fake Langfuse SDK doubles for link/identity tests.

Every identity is a plain string on a ``SimpleNamespace`` so no auto-created mock
attribute can silently masquerade as a real project/dataset/run id.

Only *supported* read paths are modelled as usable: ``datasets.get`` (v2) and
``experiments.list`` (v4). ``datasets.get_run`` still exists purely so tests can
assert the resolver never depends on it — Langfuse withdrew the whole v3 read
family for Organizations created after 2026-09-16 (HTTP 410).
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
    def __init__(
        self,
        runs: dict[tuple[str, str], SimpleNamespace] | None = None,
        *,
        dataset_ids: dict[str, str] | None = None,
        raise_error: Exception | None = None,
        get_error: Exception | None = None,
    ):
        self.runs = runs or {}
        self.dataset_ids = dict(dataset_ids or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.legacy_get_run_calls: list[tuple[str, str]] = []
        self.raise_error = raise_error
        self.get_error = get_error

    def get(self, dataset_name: str, **kwargs: Any) -> Any:
        """``GET /api/v2/datasets/{name}`` — the supported Dataset identity read."""
        self.calls.append((dataset_name, kwargs))
        if self.get_error is not None:
            raise self.get_error
        dataset_id = self.dataset_ids.get(dataset_name)
        if dataset_id is None:
            raise LookupError(f"no such dataset: {dataset_name}")
        return SimpleNamespace(id=dataset_id, name=dataset_name)

    def get_run(self, dataset_name: str, run_name: str, **kwargs: Any) -> Any:
        """Withdrawn v3 read path. Kept only to prove the resolver never calls it."""
        self.legacy_get_run_calls.append((dataset_name, run_name))
        if self.raise_error is not None:
            raise self.raise_error
        try:
            return self.runs[(dataset_name, run_name)]
        except KeyError as exc:  # pragma: no cover - explicit failure for wrong lookups
            raise LookupError(f"no such dataset run: {dataset_name}/{run_name}") from exc


class FakeExperiments:
    """``GET /api/public/experiments`` — a Dataset Run is an Experiment in v4."""

    def __init__(self, dataset_ids: dict[str, str] | None = None):
        self.dataset_ids = dict(dataset_ids or {})
        self.calls: list[dict[str, Any]] = []
        self.raise_error: Exception | None = None

    def list(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raise_error is not None:
            raise self.raise_error
        run_id = kwargs.get("id")
        if run_id is None:
            return SimpleNamespace(data=[])
        dataset_id = self.dataset_ids.get(str(run_id))
        if dataset_id is None:
            # Runs written through the asynchronous ingestion API are not always
            # exposed by the v4 read layer; that is "not exposed", not "unknown id".
            return SimpleNamespace(data=[])
        return SimpleNamespace(
            data=[SimpleNamespace(id=run_id, name=f"run-{run_id}", dataset_id=dataset_id)]
        )


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
        dataset_ids: dict[str, str] | None = None,
        experiment_datasets: dict[str, str] | None = None,
    ):
        self.projects = FakeProjects(project_ids)
        self.datasets = FakeDatasets(runs, dataset_ids=dataset_ids)
        self.experiments = FakeExperiments(experiment_datasets)
        self.dataset_run_items = FakeDatasetRunItems(dataset_run_id)
        self.scores = FakeScores()
        self.api = SimpleNamespace(
            projects=self.projects,
            datasets=self.datasets,
            experiments=self.experiments,
            dataset_run_items=self.dataset_run_items,
            scores=self.scores,
        )
        self.flushed = 0

    def flush(self) -> None:
        self.flushed += 1


def remote_run(dataset_id: str, run_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=run_id, dataset_id=dataset_id)
