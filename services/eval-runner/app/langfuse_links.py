"""Public Langfuse Dataset Run link derivation.

The browser-facing Dataset Run link is a *derived display value*. It must never
change execution results, sync status, outbox state, or frozen manifests, and it
must never be produced from guessed identities.

This module owns four responsibilities:

1. A pure URL builder (:func:`build_dataset_run_url`).
2. Current-generation run evidence selection shared with sync-status aggregation.
3. :class:`LangfuseLinkResolver` - public-SDK identity lookups with success-only caching.
4. :class:`LangfuseLaunchLinkService` - short read, transaction-free resolve, short verified write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from sqlalchemy import select, update

from .config import validate_langfuse_dashboard_url
from .db import DatabaseManager
from .db_models import (
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    LangfuseSyncTaskRecord,
)
from .state_machine import TERMINAL_LAUNCH_STATUSES

logger = logging.getLogger("argus.langfuse_links")

MAX_URL_LENGTH = 1024
PROJECT_CACHE_TTL_SECONDS = 300.0
DATASET_CACHE_TTL_SECONDS = 300.0
DATASET_CACHE_MAX_ENTRIES = 256
REMOTE_READ_TIMEOUT_SECONDS = 2.0

# Internal reason codes. They are only used for logs and internal results and must
# never be written into `langfuse_sync_error` (that would fake a sync failure).
DASHBOARD_UNCONFIGURED = "DASHBOARD_UNCONFIGURED"
CLIENT_UNAVAILABLE = "CLIENT_UNAVAILABLE"
PROJECT_UNRESOLVED = "PROJECT_UNRESOLVED"
DATASET_UNRESOLVED = "DATASET_UNRESOLVED"
DATASET_ID_CONFLICT = "DATASET_ID_CONFLICT"
RUN_ID_CONFLICT = "RUN_ID_CONFLICT"
NO_REMOTE_RUN = "NO_REMOTE_RUN"
STALE = "STALE"
URL_TOO_LONG = "URL_TOO_LONG"
NOT_FOUND = "NOT_FOUND"
DEFERRED = "DEFERRED"
UNCHANGED = "UNCHANGED"
UPDATED = "UPDATED"
UNAVAILABLE = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def is_real_remote_id(value: Any) -> bool:
    """True only for a plausible real remote identifier string."""
    if not isinstance(value, str):
        return False
    if value != value.strip() or not value:
        return False
    if value in (".", ".."):
        return False
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() or ch == "\\" for ch in value)


def is_synthetic_dataset_id(value: Any, dataset_name: Any) -> bool:
    """True for locally synthesized dataset identifiers.

    ``DatasetResolver`` falls back to ``lf-{dataset_name}`` when Langfuse returns no
    id, and to ``seed-{dataset_name}`` for the local seed source. Neither is a real
    remote identity, so neither may be used to build a page URL.
    """
    if not isinstance(value, str) or not isinstance(dataset_name, str):
        return False
    candidate = value.strip()
    name = dataset_name.strip()
    return candidate in (f"lf-{name}", f"seed-{name}")


def is_safe_browser_url(value: Any) -> bool:
    """True for absolute browser-safe http(s) URLs without credentials."""
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() or ch == "\\" for ch in value):
        return False
    try:
        parsed = validate_langfuse_dashboard_url(value)
    except Exception:  # pragma: no cover - validator is defensive already
        return False
    if parsed is None:
        # validate_langfuse_dashboard_url rejects query/fragment, which a stored
        # run link may legitimately carry; re-check the base rules only.
        from urllib.parse import urlsplit

        try:
            parts = urlsplit(value)
            _ = parts.port
        except ValueError:
            return False
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.netloc
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.netloc.endswith(":")
        ):
            return False
    return True


def build_dataset_run_url(
    dashboard_base: str | None,
    project_id: str | None,
    dataset_id: str | None,
    run_id: str | None,
) -> str | None:
    """Build the canonical Langfuse Dataset Run page URL.

    Returns ``None`` when any identity is missing/implausible, when the dashboard
    base is not a valid browser UI address, or when the result would exceed the
    persisted column width. Never performs network or database access.
    """
    if not is_real_remote_id(project_id) or not is_real_remote_id(dataset_id) or not is_real_remote_id(run_id):
        return None

    base = validate_langfuse_dashboard_url(dashboard_base) if isinstance(dashboard_base, str) else None
    if base is None:
        return None

    # `base.rstrip("/")` keeps path prefixes such as `/langfuse`; an absolute-path
    # urljoin() would drop them.
    url = (
        f"{base.rstrip('/')}/project/{quote(project_id, safe='')}"
        f"/datasets/{quote(dataset_id, safe='')}/runs/{quote(run_id, safe='')}"
    )
    if len(url) > MAX_URL_LENGTH:
        return None
    return url


def _clean_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate or None


def _extract_field(payload: Any, *names: str) -> Any:
    if payload is None:
        return None
    for name in names:
        if isinstance(payload, dict):
            if name in payload:
                return payload[name]
        else:
            value = getattr(payload, name, None)
            if value is not None:
                return value
    return None


def dataset_fingerprint(manifest_dataset: Any) -> str | None:
    """Stable fingerprint of the frozen dataset snapshot (change detection only)."""
    if manifest_dataset is None:
        return None
    try:
        payload = json.dumps(manifest_dataset, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Current-generation run evidence
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CurrentTaskRunEvidence:
    item_id: str
    dispatch_generation: int
    task_id: str
    task_status: str
    dataset_run_name: str
    run_id: str | None
    dataset_source: str | None


@dataclass(frozen=True)
class CurrentRunEvidence:
    run_id: str | None
    conflicting_run_ids: tuple[str, ...]
    run_names: tuple[str, ...]
    generations: tuple[tuple[str, int], ...]
    current_task_evidence: tuple[CurrentTaskRunEvidence, ...]

    @property
    def has_tasks(self) -> bool:
        return bool(self.current_task_evidence)

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicting_run_ids)

    def signature(self) -> tuple[Any, ...]:
        return (
            self.run_id,
            self.conflicting_run_ids,
            self.run_names,
            self.generations,
            tuple((e.task_id, e.task_status, e.run_id) for e in self.current_task_evidence),
        )


def _task_run_id(task: Any) -> str | None:
    payload = getattr(task, "scores_payload", None) or {}
    if not isinstance(payload, dict):
        return None
    return _clean_id(payload.get("_dataset_run_id"))


def select_current_run_evidence(
    items: Iterable[Any],
    tasks: Iterable[Any],
) -> CurrentRunEvidence:
    """Select run evidence strictly from tasks matching each item's current generation.

    Never falls back to the newest task, the highest generation, or the first
    non-empty run id.
    """
    task_list = list(tasks)
    tasks_map: dict[tuple[str, int], Any] = {}
    for task in task_list:
        key = (str(getattr(task, "item_id", "")), int(getattr(task, "dispatch_generation", 0) or 0))
        tasks_map.setdefault(key, task)

    evidence: list[CurrentTaskRunEvidence] = []
    for item in items:
        item_id = str(getattr(item, "id", ""))
        generation = int(getattr(item, "dispatch_generation", 0) or 0)
        task = tasks_map.get((item_id, generation))
        if task is None:
            continue
        payload = getattr(task, "scores_payload", None) or {}
        source = payload.get("_dataset_source") if isinstance(payload, dict) else None
        evidence.append(
            CurrentTaskRunEvidence(
                item_id=item_id,
                dispatch_generation=generation,
                task_id=str(getattr(task, "id", "")),
                task_status=str(getattr(task, "status", "") or ""),
                dataset_run_name=str(getattr(task, "dataset_run_name", "") or ""),
                run_id=_task_run_id(task),
                dataset_source=str(source) if source is not None else None,
            )
        )

    run_ids = sorted({e.run_id for e in evidence if e.run_id})
    run_names = tuple(dict.fromkeys(e.dataset_run_name for e in evidence if e.dataset_run_name))
    generations = tuple((e.item_id, e.dispatch_generation) for e in evidence)

    return CurrentRunEvidence(
        run_id=run_ids[0] if len(run_ids) == 1 else None,
        conflicting_run_ids=tuple(run_ids) if len(run_ids) > 1 else (),
        run_names=run_names,
        generations=generations,
        current_task_evidence=tuple(evidence),
    )


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LinkResolution:
    ok: bool
    url: str | None = None
    reason: str | None = None
    project_id: str | None = None
    dataset_id: str | None = None


_REQUEST_OPTIONS_KEY = "request_options"


def _read_timeout_options() -> dict[str, Any]:
    return {"timeout_in_seconds": REMOTE_READ_TIMEOUT_SECONDS, "max_retries": 0}


class LangfuseLinkResolver:
    """Resolves Project/Dataset identity through public Langfuse SDK endpoints only.

    Never calls private helpers (``_get_project_id`` and friends) and never guesses
    a project. Successful lookups are cached per client instance; failures are not
    cached, and no network IO happens while holding the cache lock.
    """

    def __init__(
        self,
        client_provider: Any | Callable[[], Any] | None = None,
        dashboard_base: str | None = None,
        *,
        project_cache_ttl: float = PROJECT_CACHE_TTL_SECONDS,
        dataset_cache_ttl: float = DATASET_CACHE_TTL_SECONDS,
        dataset_cache_max: int = DATASET_CACHE_MAX_ENTRIES,
    ):
        self._client_provider = client_provider
        self.dashboard_base = (
            validate_langfuse_dashboard_url(dashboard_base) if isinstance(dashboard_base, str) else None
        )
        self._project_cache_ttl = float(project_cache_ttl)
        self._dataset_cache_ttl = float(dataset_cache_ttl)
        self._dataset_cache_max = int(dataset_cache_max)
        self._lock = threading.Lock()
        self._project_cache: tuple[int, str, float] | None = None
        self._dataset_cache: OrderedDict[tuple[int, str, str, str], tuple[str, float]] = OrderedDict()

    # -- client -----------------------------------------------------------
    def _client(self) -> Any | None:
        provider = self._client_provider
        if provider is None:
            return None
        try:
            client = provider() if callable(provider) and not hasattr(provider, "api") else provider
        except Exception as exc:
            logger.warning("Langfuse link resolver could not obtain client: %s", exc)
            return None
        if client is None or not hasattr(client, "api"):
            return None
        return client

    # -- project ----------------------------------------------------------
    def resolve_project_id(self, client: Any) -> tuple[str | None, str | None]:
        now = time.monotonic()
        with self._lock:
            cached = self._project_cache
            if cached is not None and cached[0] == id(client) and cached[2] > now:
                return cached[1], None
            self._project_cache = None

        projects_api = getattr(getattr(client, "api", None), "projects", None)
        getter = getattr(projects_api, "get", None)
        if not callable(getter):
            return None, PROJECT_UNRESOLVED

        try:
            response = getter(**{_REQUEST_OPTIONS_KEY: _read_timeout_options()})
        except Exception as exc:
            logger.warning("Langfuse project lookup failed: %s", exc)
            return None, PROJECT_UNRESOLVED

        data = _extract_field(response, "data")
        if not isinstance(data, (list, tuple)) or len(data) != 1:
            logger.warning("Langfuse project lookup returned %s projects; refusing to guess", len(data) if isinstance(data, (list, tuple)) else "no")
            return None, PROJECT_UNRESOLVED

        project_id = _clean_id(_extract_field(data[0], "id"))
        if not is_real_remote_id(project_id):
            return None, PROJECT_UNRESOLVED

        with self._lock:
            self._project_cache = (id(client), project_id, time.monotonic() + self._project_cache_ttl)
        return project_id, None

    # -- dataset ----------------------------------------------------------
    def resolve_dataset_id(
        self,
        client: Any,
        *,
        dataset_name: str | None,
        run_names: Sequence[str | None],
        run_id: str,
        manifest_dataset_id: str | None = None,
        launch_dataset_id: str | None = None,
        observed_dataset_id: str | None = None,
    ) -> tuple[str | None, str | None]:
        frozen = _clean_id(manifest_dataset_id)
        top_level = _clean_id(launch_dataset_id)
        if frozen and top_level and frozen != top_level:
            return None, DATASET_ID_CONFLICT

        # The dataset object just fetched from the SDK is the most direct identity
        # evidence; the frozen manifest and the top-level column are fallbacks.
        known = _clean_id(observed_dataset_id) or frozen or top_level
        if known and not is_synthetic_dataset_id(known, dataset_name):
            return known, None
        if known and is_synthetic_dataset_id(known, dataset_name) and not dataset_name:
            return None, DATASET_UNRESOLVED

        if not dataset_name:
            return None, DATASET_UNRESOLVED

        datasets_api = getattr(getattr(client, "api", None), "datasets", None)
        getter = getattr(datasets_api, "get_run", None)
        if not callable(getter):
            return None, DATASET_UNRESOLVED

        for candidate_run_name in [n for n in run_names if n]:
            cached = self._cached_dataset(client, dataset_name, candidate_run_name, run_id)
            if cached is not None:
                return cached, None

            try:
                response = getter(
                    dataset_name,
                    candidate_run_name,
                    **{_REQUEST_OPTIONS_KEY: _read_timeout_options()},
                )
            except Exception as exc:
                logger.warning(
                    "Langfuse dataset run lookup failed for dataset=%s run=%s: %s",
                    dataset_name,
                    candidate_run_name,
                    exc,
                )
                continue

            remote_run_id = _clean_id(_extract_field(response, "id", "run_id", "dataset_run_id"))
            remote_dataset_id = _clean_id(_extract_field(response, "dataset_id"))
            if not is_real_remote_id(remote_dataset_id):
                continue
            # Never trust a name lookup that resolves to a different run.
            if remote_run_id != run_id:
                logger.warning(
                    "Langfuse dataset run lookup mismatch for dataset=%s run_name=%s (expected run %s)",
                    dataset_name,
                    candidate_run_name,
                    run_id,
                )
                continue

            self._cache_dataset(client, dataset_name, candidate_run_name, run_id, remote_dataset_id)
            return remote_dataset_id, None

        return None, DATASET_UNRESOLVED

    def _cached_dataset(self, client: Any, dataset_name: str, run_name: str, run_id: str) -> str | None:
        now = time.monotonic()
        key = (id(client), dataset_name, run_name, run_id)
        with self._lock:
            entry = self._dataset_cache.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at <= now:
                self._dataset_cache.pop(key, None)
                return None
            self._dataset_cache.move_to_end(key)
            return value

    def _cache_dataset(self, client: Any, dataset_name: str, run_name: str, run_id: str, dataset_id: str) -> None:
        key = (id(client), dataset_name, run_name, run_id)
        with self._lock:
            self._dataset_cache[key] = (dataset_id, time.monotonic() + self._dataset_cache_ttl)
            self._dataset_cache.move_to_end(key)
            while len(self._dataset_cache) > self._dataset_cache_max:
                self._dataset_cache.popitem(last=False)

    def invalidate_client(self, client: Any) -> None:
        """Drop cached identities when the SDK client instance is replaced."""
        with self._lock:
            if self._project_cache is not None and self._project_cache[0] == id(client):
                self._project_cache = None
            for key in [k for k in self._dataset_cache if k[0] == id(client)]:
                self._dataset_cache.pop(key, None)

    # -- entry point ------------------------------------------------------
    def resolve(
        self,
        *,
        run_id: str | None,
        dataset_name: str | None = None,
        run_names: Sequence[str | None] = (),
        manifest_dataset_id: str | None = None,
        launch_dataset_id: str | None = None,
        observed_dataset_id: str | None = None,
    ) -> LinkResolution:
        if not self.dashboard_base:
            return LinkResolution(False, reason=DASHBOARD_UNCONFIGURED)
        if not is_real_remote_id(run_id):
            return LinkResolution(False, reason=NO_REMOTE_RUN)

        client = self._client()
        if client is None:
            return LinkResolution(False, reason=CLIENT_UNAVAILABLE)

        project_id, project_error = self.resolve_project_id(client)
        if project_error or not project_id:
            return LinkResolution(False, reason=project_error or PROJECT_UNRESOLVED)

        dataset_id, dataset_error = self.resolve_dataset_id(
            client,
            dataset_name=dataset_name,
            run_names=list(run_names),
            run_id=run_id,
            manifest_dataset_id=manifest_dataset_id,
            launch_dataset_id=launch_dataset_id,
            observed_dataset_id=observed_dataset_id,
        )
        if dataset_error or not dataset_id:
            return LinkResolution(False, reason=dataset_error or DATASET_UNRESOLVED, project_id=project_id)

        url = build_dataset_run_url(self.dashboard_base, project_id, dataset_id, run_id)
        if url is None:
            return LinkResolution(
                False, reason=URL_TOO_LONG, project_id=project_id, dataset_id=dataset_id
            )
        return LinkResolution(True, url=url, project_id=project_id, dataset_id=dataset_id)


def resolve_dataset_run_link(
    client: Any,
    dashboard_base: str | None,
    *,
    run_id: str | None,
    dataset_name: str | None = None,
    run_names: Sequence[str | None] = (),
    manifest_dataset_id: str | None = None,
    launch_dataset_id: str | None = None,
    observed_dataset_id: str | None = None,
) -> LinkResolution:
    """One-shot helper for the synchronous execution path."""
    resolver = LangfuseLinkResolver(client, dashboard_base)
    return resolver.resolve(
        run_id=run_id,
        dataset_name=dataset_name,
        run_names=run_names,
        manifest_dataset_id=manifest_dataset_id,
        launch_dataset_id=launch_dataset_id,
        observed_dataset_id=observed_dataset_id,
    )


# ---------------------------------------------------------------------------
# Link service
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LinkResult:
    status: str
    url: str | None = None
    run_id: str | None = None
    reason: str | None = None

    @property
    def updated(self) -> bool:
        return self.status == UPDATED


@dataclass(frozen=True)
class LaunchLinkSnapshot:
    launch_id: str
    launch_name: str
    launch_status: str
    langfuse_sync_status: str
    stored_run_id: str | None
    stored_url: str | None
    dataset_name: str
    launch_dataset_id: str | None
    manifest_dataset_id: str | None
    manifest_dataset_source: str | None
    dataset_fingerprint: str | None
    generations: tuple[tuple[str, int], ...]
    run_id: str | None
    run_names: tuple[str, ...]
    conflicting_run_ids: tuple[str, ...]
    has_current_tasks: bool
    has_any_tasks: bool


class LangfuseLaunchLinkService:
    """Backfills ``langfuse_experiment_url`` for launches that already have real run evidence."""

    def __init__(self, db_mgr: DatabaseManager, resolver: LangfuseLinkResolver):
        self.db_mgr = db_mgr
        self.resolver = resolver

    # -- phase A ----------------------------------------------------------
    def read_snapshot(self, launch_id: str) -> LaunchLinkSnapshot | None:
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        with self.db_mgr.get_session() as session:
            stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == launch_id)
            if is_pg:
                stmt = stmt.with_for_update()
            launch = session.scalars(stmt).first()
            if launch is None:
                return None

            items = session.scalars(
                select(ExperimentItemExecutionRecord).where(
                    ExperimentItemExecutionRecord.launch_id == launch_id
                )
            ).all()
            tasks = session.scalars(
                select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.launch_id == launch_id)
            ).all()

            evidence = select_current_run_evidence(items, tasks)
            manifest_dataset = (launch.manifest or {}).get("dataset") if isinstance(launch.manifest, dict) else None
            manifest_dataset_id = _clean_id(manifest_dataset.get("dataset_id")) if isinstance(manifest_dataset, dict) else None
            manifest_source = (
                _clean_id(manifest_dataset.get("source")) if isinstance(manifest_dataset, dict) else None
            )

            return LaunchLinkSnapshot(
                launch_id=launch.id,
                launch_name=launch.name,
                launch_status=launch.status,
                langfuse_sync_status=launch.langfuse_sync_status,
                stored_run_id=_clean_id(launch.langfuse_experiment_id),
                stored_url=_clean_id(launch.langfuse_experiment_url),
                dataset_name=launch.dataset_name,
                launch_dataset_id=_clean_id(launch.dataset_id),
                manifest_dataset_id=manifest_dataset_id,
                manifest_dataset_source=manifest_source,
                dataset_fingerprint=dataset_fingerprint(manifest_dataset),
                generations=evidence.generations,
                run_id=evidence.run_id,
                run_names=evidence.run_names,
                conflicting_run_ids=evidence.conflicting_run_ids,
                has_current_tasks=evidence.has_tasks,
                has_any_tasks=len(tasks) > 0,
            )

    def choose_run_id(self, snapshot: LaunchLinkSnapshot) -> tuple[str | None, str | None]:
        if snapshot.conflicting_run_ids:
            return None, RUN_ID_CONFLICT
        if snapshot.has_current_tasks:
            # Current generation is authoritative; never fall back to a stale launch value.
            if snapshot.run_id:
                return snapshot.run_id, None
            return None, NO_REMOTE_RUN
        if snapshot.has_any_tasks:
            # Items were re-dispatched but their current tasks are not materialized yet.
            return None, NO_REMOTE_RUN
        if snapshot.stored_run_id and is_real_remote_id(snapshot.stored_run_id):
            return snapshot.stored_run_id, None
        return None, NO_REMOTE_RUN

    # -- phase C ----------------------------------------------------------
    def _facts_match(self, snapshot: LaunchLinkSnapshot, fresh: LaunchLinkSnapshot, run_id: str) -> bool:
        if fresh.launch_status not in TERMINAL_LAUNCH_STATUSES:
            return False
        if fresh.stored_url:
            return False
        if fresh.dataset_fingerprint != snapshot.dataset_fingerprint:
            return False
        if fresh.dataset_name != snapshot.dataset_name:
            return False
        if fresh.generations != snapshot.generations:
            return False
        if fresh.conflicting_run_ids:
            return False
        if fresh.has_current_tasks and fresh.run_id != run_id:
            return False
        if snapshot.stored_run_id and fresh.stored_run_id != snapshot.stored_run_id:
            return False
        return True

    def ensure_launch_link(self, launch_id: str) -> LinkResult:
        snapshot = self.read_snapshot(launch_id)
        if snapshot is None:
            return LinkResult(NOT_FOUND, reason=NOT_FOUND)
        if snapshot.stored_url and is_safe_browser_url(snapshot.stored_url):
            return LinkResult(UNCHANGED, url=snapshot.stored_url, run_id=snapshot.stored_run_id)
        if snapshot.launch_status not in TERMINAL_LAUNCH_STATUSES:
            return LinkResult(DEFERRED, reason=DEFERRED, run_id=snapshot.stored_run_id)

        run_id, evidence_error = self.choose_run_id(snapshot)
        if not run_id:
            return LinkResult(NO_REMOTE_RUN, reason=evidence_error or NO_REMOTE_RUN)

        # Phase B: no DB session, no transaction, no row lock while doing remote IO.
        resolution = self.resolver.resolve(
            run_id=run_id,
            dataset_name=snapshot.dataset_name,
            run_names=(*snapshot.run_names, snapshot.launch_name),
            manifest_dataset_id=snapshot.manifest_dataset_id,
            launch_dataset_id=snapshot.launch_dataset_id,
        )
        if not resolution.ok or not resolution.url:
            logger.info(
                "Langfuse link backfill unavailable launch=%s run=%s reason=%s",
                launch_id,
                run_id,
                resolution.reason,
            )
            return LinkResult(UNAVAILABLE, reason=resolution.reason, run_id=run_id)

        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        with self.db_mgr.get_session() as session:
            stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == launch_id)
            if is_pg:
                stmt = stmt.with_for_update()
            launch = session.scalars(stmt).first()
            if launch is None:
                return LinkResult(NOT_FOUND, reason=NOT_FOUND)

            items = session.scalars(
                select(ExperimentItemExecutionRecord).where(
                    ExperimentItemExecutionRecord.launch_id == launch_id
                )
            ).all()
            tasks = session.scalars(
                select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.launch_id == launch_id)
            ).all()
            evidence = select_current_run_evidence(items, tasks)
            manifest_dataset = (launch.manifest or {}).get("dataset") if isinstance(launch.manifest, dict) else None
            fresh = LaunchLinkSnapshot(
                launch_id=launch.id,
                launch_name=launch.name,
                launch_status=launch.status,
                langfuse_sync_status=launch.langfuse_sync_status,
                stored_run_id=_clean_id(launch.langfuse_experiment_id),
                stored_url=_clean_id(launch.langfuse_experiment_url),
                dataset_name=launch.dataset_name,
                launch_dataset_id=_clean_id(launch.dataset_id),
                manifest_dataset_id=(
                    _clean_id(manifest_dataset.get("dataset_id")) if isinstance(manifest_dataset, dict) else None
                ),
                manifest_dataset_source=(
                    _clean_id(manifest_dataset.get("source")) if isinstance(manifest_dataset, dict) else None
                ),
                dataset_fingerprint=dataset_fingerprint(manifest_dataset),
                generations=evidence.generations,
                run_id=evidence.run_id,
                run_names=evidence.run_names,
                conflicting_run_ids=evidence.conflicting_run_ids,
                has_current_tasks=evidence.has_tasks,
                has_any_tasks=len(tasks) > 0,
            )

            if fresh.stored_url and is_safe_browser_url(fresh.stored_url):
                return LinkResult(UNCHANGED, url=fresh.stored_url, run_id=fresh.stored_run_id)
            if not self._facts_match(snapshot, fresh, run_id):
                logger.info("Langfuse link backfill stale launch=%s run=%s", launch_id, run_id)
                return LinkResult(STALE, reason=STALE, run_id=run_id)

            # Only run metadata is written: never execution/sync state, outbox, traces or scores.
            # The conditional update is the SQLite-safe CAS equivalent of the row lock used above.
            write_result = session.execute(
                update(ExperimentLaunchRecord)
                .where(
                    ExperimentLaunchRecord.id == launch_id,
                    ExperimentLaunchRecord.langfuse_experiment_url.is_(None),
                )
                .values(langfuse_experiment_id=run_id, langfuse_experiment_url=resolution.url)
            )
            session.commit()
            if write_result.rowcount == 0:
                # A concurrent compensator won the race; converge instead of overwriting.
                with self.db_mgr.get_session() as verify:
                    current = verify.get(ExperimentLaunchRecord, launch_id)
                    current_url = _clean_id(current.langfuse_experiment_url) if current else None
                if current_url and is_safe_browser_url(current_url):
                    return LinkResult(UNCHANGED, url=current_url, run_id=run_id)
                return LinkResult(STALE, reason=STALE, run_id=run_id)
            return LinkResult(UPDATED, url=resolution.url, run_id=run_id)
