"""Unit tests for the public Langfuse Dataset Run link derivation (Issue #44)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services" / "eval-runner")]

from app.db_models import ExperimentItemExecutionRecord as Item  # noqa: E402
from app.db_models import ExperimentLaunchRecord as Launch  # noqa: E402
from app.db_models import LangfuseSyncTaskRecord as Task  # noqa: E402
from app.langfuse_links import (  # noqa: E402
    DATASET_ID_CONFLICT,
    DATASET_UNRESOLVED,
    DEFERRED,
    NO_REMOTE_RUN,
    NOT_FOUND,
    PROJECT_UNRESOLVED,
    RUN_ID_CONFLICT,
    STALE,
    UNAVAILABLE,
    UPDATED,
    LangfuseLinkResolver,
    build_dataset_run_url,
    is_synthetic_dataset_id,
    select_current_run_evidence,
)
from fake_langfuse import FakeLangfuseSDK, remote_run  # noqa: E402
from link_helpers import make_service, seed_terminal_launch  # noqa: E402

DASHBOARD = "https://cloud.langfuse.example.com"


# ---------------------------------------------------------------------------
# Pure URL builder
# ---------------------------------------------------------------------------
def test_build_url_uses_dataset_id_not_name():
    url = build_dataset_run_url(DASHBOARD, "proj-1", "ds-real-id", "run-1")
    assert url == f"{DASHBOARD}/project/proj-1/datasets/ds-real-id/runs/run-1"
    assert "banking-agent-regression" not in url


def test_build_url_preserves_path_prefix_and_strips_trailing_slash():
    assert build_dataset_run_url(
        "https://obs.example.com/langfuse/", "p", "d", "r"
    ) == "https://obs.example.com/langfuse/project/p/datasets/d/runs/r"


def test_build_url_percent_encodes_each_id():
    url = build_dataset_run_url(DASHBOARD, "proj/../etc", "ds+id", "run=x")
    assert url == f"{DASHBOARD}/project/proj%2F..%2Fetc/datasets/ds%2Bid/runs/run%3Dx"


def test_build_url_rejects_whitespace_and_backslash_ids():
    assert build_dataset_run_url(DASHBOARD, "p", "ds id", "r") is None
    assert build_dataset_run_url(DASHBOARD, "p", "ds", "r\\n") is None
    assert build_dataset_run_url(DASHBOARD, "p", "ds", "r\\\\n") is None
    assert build_dataset_run_url(DASHBOARD, " p", "d", "r") is None


def test_build_url_rejects_internal_host_config_and_bad_ids():
    # Internal API host is a valid URL shape, so the builder accepts it only when
    # explicitly passed; the resolver is what refuses to use it as a UI base.
    assert build_dataset_run_url("http://langfuse-web:3000", "p", "d", "r") is not None
    assert build_dataset_run_url(DASHBOARD, None, "d", "r") is None
    assert build_dataset_run_url(DASHBOARD, "p", "ds", None) is None
    assert build_dataset_run_url(DASHBOARD, ".", "d", "r") is None
    assert build_dataset_run_url(DASHBOARD, "..", "d", "r") is None
    assert build_dataset_run_url(DASHBOARD, "p\nx", "d", "r") is None
    assert build_dataset_run_url("not a url", "p", "d", "r") is None


def test_build_url_rejects_overlong_result():
    long_id = "d" * 1100
    assert build_dataset_run_url(DASHBOARD, "p", long_id, "r") is None


def test_synthetic_dataset_id_detection():
    assert is_synthetic_dataset_id("lf-banking", "banking")
    assert not is_synthetic_dataset_id("real-id", "banking")


# ---------------------------------------------------------------------------
# Current-generation evidence selection
# ---------------------------------------------------------------------------
def _item(item_id, gen):
    return SimpleNamespace(id=item_id, dispatch_generation=gen)


def _task(item_id, gen, run_id, status="SYNCED", run_name="run-name", source="langfuse"):
    payload = {"_dataset_source": source}
    if run_id is not None:
        payload["_dataset_run_id"] = run_id
    return SimpleNamespace(
        id=f"task-{item_id}-{gen}",
        item_id=item_id,
        dispatch_generation=gen,
        status=status,
        dataset_run_name=run_name,
        scores_payload=payload,
    )


def test_evidence_requires_matching_generation():
    items = [_item("i1", 2)]
    tasks = [_task("i1", 1, "old-run")]
    ev = select_current_run_evidence(items, tasks)
    assert ev.run_id is None
    assert ev.has_tasks is False


def test_evidence_conflict_reports_both_ids():
    items = [_item("i1", 1), _item("i2", 1)]
    tasks = [_task("i1", 1, "run-a"), _task("i2", 1, "run-b")]
    ev = select_current_run_evidence(items, tasks)
    assert ev.has_conflict
    assert ev.run_id is None
    assert set(ev.conflicting_run_ids) == {"run-a", "run-b"}


def test_evidence_single_run_id_is_selected():
    items = [_item("i1", 1), _item("i2", 1)]
    tasks = [_task("i1", 1, "run-a"), _task("i2", 1, None)]
    ev = select_current_run_evidence(items, tasks)
    assert ev.run_id == "run-a"
    assert not ev.has_conflict


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
def test_resolver_requires_exactly_one_project():
    zero = LangfuseLinkResolver(FakeLangfuseSDK(project_ids=[]), DASHBOARD)
    res = zero.resolve(run_id="r", dataset_name="d", run_names=["n"], manifest_dataset_id="ds")
    assert not res.ok and res.reason == PROJECT_UNRESOLVED

    two = LangfuseLinkResolver(FakeLangfuseSDK(project_ids=["p1", "p2"]), DASHBOARD)
    res = two.resolve(run_id="r", dataset_name="d", run_names=["n"], manifest_dataset_id="ds")
    assert not res.ok and res.reason == PROJECT_UNRESOLVED


def test_resolver_uses_public_projects_endpoint_with_timeout():
    lf = FakeLangfuseSDK()
    resolver = LangfuseLinkResolver(lf, DASHBOARD)
    res = resolver.resolve(run_id="r1", dataset_name="d", run_names=["n"], manifest_dataset_id="ds")
    assert res.ok
    assert res.project_id == "proj-real"
    opts = lf.projects.calls[0]["request_options"]
    assert opts["timeout_in_seconds"] == 2
    assert opts["max_retries"] == 0
    # private helper must never be used
    assert not hasattr(lf.projects, "_get_project_id")


def test_resolver_caches_project_per_client_and_invalidates_on_client_change():
    first = FakeLangfuseSDK()
    holder = {"client": first}
    resolver = LangfuseLinkResolver(lambda: holder["client"], DASHBOARD)
    resolver.resolve(run_id="r", dataset_name="d", run_names=["n"], manifest_dataset_id="ds")
    resolver.resolve(run_id="r", dataset_name="d", run_names=["n"], manifest_dataset_id="ds")
    assert len(first.projects.calls) == 1  # cached

    second = FakeLangfuseSDK()
    holder["client"] = second
    resolver.resolve(run_id="r", dataset_name="d", run_names=["n"], manifest_dataset_id="ds")
    assert len(second.projects.calls) == 1  # a new client instance re-resolves identity


def test_resolver_never_caches_failures():
    lf = FakeLangfuseSDK(project_ids=[])
    resolver = LangfuseLinkResolver(lf, DASHBOARD)
    resolver.resolve(run_id="r", dataset_name="d", run_names=["n"], manifest_dataset_id="ds")
    resolver.resolve(run_id="r", dataset_name="d", run_names=["n"], manifest_dataset_id="ds")
    assert len(lf.projects.calls) == 2  # failure is not cached


def test_resolver_prefers_frozen_dataset_id_without_remote_call():
    lf = FakeLangfuseSDK()
    resolver = LangfuseLinkResolver(lf, DASHBOARD)
    res = resolver.resolve(
        run_id="r1", dataset_name="banking", run_names=["n"], manifest_dataset_id="ds-real"
    )
    assert res.ok and res.dataset_id == "ds-real"
    assert lf.datasets.calls == []  # no name lookup needed


def test_resolver_falls_back_to_top_level_dataset_id():
    lf = FakeLangfuseSDK()
    resolver = LangfuseLinkResolver(lf, DASHBOARD)
    res = resolver.resolve(
        run_id="r1", dataset_name="banking", run_names=["n"], launch_dataset_id="ds-top"
    )
    assert res.ok and res.dataset_id == "ds-top"
    assert lf.datasets.calls == []


def test_resolver_detects_dataset_id_conflict():
    lf = FakeLangfuseSDK()
    resolver = LangfuseLinkResolver(lf, DASHBOARD)
    res = resolver.resolve(
        run_id="r1",
        dataset_name="banking",
        run_names=["n"],
        manifest_dataset_id="ds-a",
        launch_dataset_id="ds-b",
    )
    assert not res.ok and res.reason == DATASET_ID_CONFLICT


def test_resolver_resolves_synthetic_id_through_supported_apis_only():
    """A synthetic dataset id must resolve without the withdrawn v3 read path.

    ``datasets.get_run`` answers 410 ``LEGACY_API_UNAVAILABLE_FOR_NEW_ORGANIZATION``
    for Organizations created after 2026-09-16, so a resolver that depends on it can
    never recover the promised historical backfill in a current deployment.
    """
    lf = FakeLangfuseSDK(dataset_ids={"banking": "ds-real"}, experiment_datasets={"r1": "ds-real"})
    resolver = LangfuseLinkResolver(lf, DASHBOARD)
    res = resolver.resolve(
        run_id="r1", dataset_name="banking", run_names=["run-name"], manifest_dataset_id="lf-banking"
    )
    assert res.ok and res.dataset_id == "ds-real"
    assert lf.datasets.legacy_get_run_calls == []
    assert [name for name, _ in lf.datasets.calls] == ["banking"]
    assert lf.experiments.calls[0]["id"] == "r1"


def test_resolver_falls_back_to_dataset_name_when_run_is_not_exposed():
    """Runs written through the async ingestion API are not always readable back.

    ``experiments.list`` then answers an empty page, which must not be treated as a
    mismatch: the Key-scoped Dataset identity is still authoritative.
    """
    lf = FakeLangfuseSDK(dataset_ids={"banking": "ds-real"})
    resolver = LangfuseLinkResolver(lf, DASHBOARD)
    res = resolver.resolve(
        run_id="r1", dataset_name="banking", run_names=["run-name"], manifest_dataset_id="lf-banking"
    )
    assert res.ok and res.dataset_id == "ds-real"
    assert lf.datasets.legacy_get_run_calls == []
    assert lf.experiments.calls[0]["id"] == "r1"


def test_resolver_ignores_a_working_legacy_dataset_run_endpoint():
    """即使旧接口仍然可用，解析器也必须走受支持的 API。

    ``datasets.get_run`` 在新 Organization 上返回 410，依赖它的实现在旧部署上仍然
    "能跑"，因此只有断言它从未被调用，才能保证同一份代码在新旧环境上语义一致。
    """
    lf = FakeLangfuseSDK(
        runs={("banking", "run-name"): remote_run("ds-legacy", "r1")},
        dataset_ids={"banking": "ds-real"},
    )
    resolver = LangfuseLinkResolver(lf, DASHBOARD)
    res = resolver.resolve(
        run_id="r1", dataset_name="banking", run_names=["run-name"], manifest_dataset_id="lf-banking"
    )
    assert res.ok and res.dataset_id == "ds-real"
    assert lf.datasets.legacy_get_run_calls == []


def test_resolver_rejects_dataset_disagreeing_with_the_run():
    lf = FakeLangfuseSDK(dataset_ids={"banking": "ds-real"}, experiment_datasets={"r1": "ds-other"})
    resolver = LangfuseLinkResolver(lf, DASHBOARD)
    res = resolver.resolve(
        run_id="r1", dataset_name="banking", run_names=["run-name"], manifest_dataset_id="lf-banking"
    )
    assert not res.ok and res.reason == DATASET_ID_CONFLICT


def test_resolver_reports_unresolved_when_no_supported_path_answers():
    lf = FakeLangfuseSDK()
    resolver = LangfuseLinkResolver(lf, DASHBOARD)
    res = resolver.resolve(
        run_id="r1", dataset_name="banking", run_names=["run-name"], manifest_dataset_id="lf-banking"
    )
    assert not res.ok and res.reason == DATASET_UNRESOLVED


def test_resolver_no_dashboard_short_circuits_without_remote_calls():
    lf = FakeLangfuseSDK()
    resolver = LangfuseLinkResolver(lf, None)
    res = resolver.resolve(run_id="r1", dataset_name="d", run_names=["n"], manifest_dataset_id="ds")
    assert not res.ok and res.reason == "DASHBOARD_UNCONFIGURED"
    assert lf.projects.calls == [] and lf.datasets.calls == []


# ---------------------------------------------------------------------------
# Link service
# ---------------------------------------------------------------------------
def test_service_backfills_historical_synced_launch(setup_runtime):
    lid, _ = seed_terminal_launch(setup_runtime)
    lf = FakeLangfuseSDK(dataset_ids={"banking-agent-regression": "ds-real"})
    service = make_service(setup_runtime[0], lf, DASHBOARD)

    result = service.ensure_launch_link(lid)
    assert result.status == UPDATED
    with setup_runtime[0].get_session() as s:
        launch = s.get(Launch, lid)
        assert launch.langfuse_experiment_id == "r1"
        assert launch.langfuse_experiment_url == f"{DASHBOARD}/project/proj-real/datasets/ds-real/runs/r1"


def test_service_uses_frozen_manifest_dataset_id_without_run_lookup(setup_runtime):
    lid, _ = seed_terminal_launch(setup_runtime, dataset_id="ds-frozen")
    lf = FakeLangfuseSDK()
    service = make_service(setup_runtime[0], lf, DASHBOARD)

    result = service.ensure_launch_link(lid)
    assert result.status == UPDATED
    assert result.url.endswith("/project/proj-real/datasets/ds-frozen/runs/r1")
    assert lf.datasets.calls == []


def test_service_is_idempotent(setup_runtime):
    lid, _ = seed_terminal_launch(setup_runtime)
    lf = FakeLangfuseSDK()
    service = make_service(setup_runtime[0], lf, DASHBOARD)

    assert service.ensure_launch_link(lid).status == UPDATED
    project_calls = len(lf.projects.calls)
    assert service.ensure_launch_link(lid).status == "UNCHANGED"
    assert len(lf.projects.calls) == project_calls  # no extra remote work


def test_service_defers_non_terminal_launch(setup_runtime):
    lid, _ = seed_terminal_launch(setup_runtime, launch_status="RUNNING")
    service = make_service(setup_runtime[0], FakeLangfuseSDK(), DASHBOARD)
    assert service.ensure_launch_link(lid).status == DEFERRED


def test_service_missing_launch_returns_not_found(setup_runtime):
    service = make_service(setup_runtime[0], FakeLangfuseSDK(), DASHBOARD)
    assert service.ensure_launch_link("does-not-exist").status == NOT_FOUND


def test_service_reports_unavailable_without_writing(setup_runtime):
    lid, _ = seed_terminal_launch(setup_runtime)
    lf = FakeLangfuseSDK(project_ids=[])
    service = make_service(setup_runtime[0], lf, DASHBOARD)

    result = service.ensure_launch_link(lid)
    assert result.status == UNAVAILABLE
    assert result.reason == PROJECT_UNRESOLVED
    with setup_runtime[0].get_session() as s:
        launch = s.get(Launch, lid)
        assert launch.langfuse_experiment_url is None
        assert launch.langfuse_sync_status == "SYNCED"
        assert launch.langfuse_sync_error is None


def test_service_without_dashboard_does_not_call_remote(setup_runtime):
    lid, _ = seed_terminal_launch(setup_runtime)
    lf = FakeLangfuseSDK()
    service = make_service(setup_runtime[0], lf, None)

    result = service.ensure_launch_link(lid)
    assert result.status == UNAVAILABLE
    assert result.reason == "DASHBOARD_UNCONFIGURED"
    assert lf.projects.calls == [] and lf.datasets.calls == []


def test_service_seed_launch_without_run_evidence_creates_no_link(setup_runtime):
    lid, _ = seed_terminal_launch(
        setup_runtime, dataset_source="seed", store_run_id=None, task_run_id=None, sync_status="NOT_APPLICABLE"
    )
    lf = FakeLangfuseSDK()
    service = make_service(setup_runtime[0], lf, DASHBOARD)

    result = service.ensure_launch_link(lid)
    assert result.status == NO_REMOTE_RUN
    with setup_runtime[0].get_session() as s:
        assert s.get(Launch, lid).langfuse_experiment_url is None
    assert lf.projects.calls == []


def test_service_refuses_stale_launch_run_id_when_current_generation_has_none(setup_runtime):
    # Item re-dispatched: the current generation has no task yet, so the residual
    # launch run id must not be used to build a link.
    lid, _ = seed_terminal_launch(
        setup_runtime,
        store_run_id="old-run",
        task_run_id="old-run",
        task_generation=1,
        item_generation=2,
    )
    lf = FakeLangfuseSDK()
    service = make_service(setup_runtime[0], lf, DASHBOARD)

    result = service.ensure_launch_link(lid)
    assert result.status == NO_REMOTE_RUN
    with setup_runtime[0].get_session() as s:
        assert s.get(Launch, lid).langfuse_experiment_url is None


def test_service_refuses_conflicting_run_evidence(setup_runtime):
    lid, _ = seed_terminal_launch(
        setup_runtime,
        item_count=2,
        store_run_id=None,
        task_run_id="run-a",
        extra_tasks=[
            {"item_index": 1, "generation": 1, "payload": {"_dataset_run_id": "run-b"}, "run_name": "review"},
        ],
    )
    service = make_service(setup_runtime[0], FakeLangfuseSDK(), DASHBOARD)
    result = service.ensure_launch_link(lid)
    assert result.status == NO_REMOTE_RUN
    assert result.reason == "RUN_ID_CONFLICT"


def test_service_uses_launch_run_id_when_no_outbox_exists(setup_runtime):
    lid, _ = seed_terminal_launch(
        setup_runtime, dataset_id=None, store_run_id="legacy-run", task_status=None
    )
    lf = FakeLangfuseSDK(dataset_ids={"banking-agent-regression": "ds-legacy"})
    service = make_service(setup_runtime[0], lf, DASHBOARD)

    result = service.ensure_launch_link(lid)
    assert result.status == UPDATED
    assert result.url.endswith("/datasets/ds-legacy/runs/legacy-run")


def test_service_returns_stale_when_item_generation_changes_during_resolution(setup_runtime):
    db = setup_runtime[0]
    lid, item_id = seed_terminal_launch(setup_runtime)
    lf = FakeLangfuseSDK()
    service = make_service(db, lf, DASHBOARD)

    original_resolve = service.resolver.resolve

    def _resolve_then_mutate(**kwargs):
        # Simulate a re-dispatch that lands while the remote lookup is in flight.
        with db.get_session() as s:
            s.get(Item, item_id).dispatch_generation = 2
        return original_resolve(**kwargs)

    service.resolver.resolve = _resolve_then_mutate  # type: ignore[method-assign]
    result = service.ensure_launch_link(lid)
    assert result.status == STALE
    with db.get_session() as s:
        assert s.get(Launch, lid).langfuse_experiment_url is None


def test_service_stale_when_stored_run_id_changed_during_resolution(setup_runtime):
    db = setup_runtime[0]
    lid, _ = seed_terminal_launch(setup_runtime)
    service = make_service(db, FakeLangfuseSDK(), DASHBOARD)
    original_resolve = service.resolver.resolve

    def _resolve_then_mutate(**kwargs):
        with db.get_session() as s:
            s.get(Launch, lid).langfuse_experiment_id = "some-other-run"
        return original_resolve(**kwargs)

    service.resolver.resolve = _resolve_then_mutate  # type: ignore[method-assign]
    result = service.ensure_launch_link(lid)
    assert result.status == STALE
    with db.get_session() as s:
        assert s.get(Launch, lid).langfuse_experiment_url is None


def test_service_never_holds_transaction_during_remote_calls(setup_runtime):
    """The resolver must run with no live session: a session opened by the service during
    the remote call would mean IO inside a transaction/row lock."""
    db = setup_runtime[0]
    lid, _ = seed_terminal_launch(setup_runtime)
    service = make_service(db, FakeLangfuseSDK(), DASHBOARD)
    observed: dict[str, object] = {}

    original_resolve = service.resolver.resolve

    def _spy(**kwargs):
        from app.db import DatabaseManager

        original_get_session = DatabaseManager.get_session
        observed["sessions"] = []

        def _tracking_get_session(self):  # type: ignore[no-untyped-def]
            observed["sessions"].append(1)
            return original_get_session(self)

        DatabaseManager.get_session = _tracking_get_session  # type: ignore[method-assign]
        try:
            return original_resolve(**kwargs)
        finally:
            DatabaseManager.get_session = original_get_session  # type: ignore[method-assign]

    service.resolver.resolve = _spy  # type: ignore[method-assign]
    result = service.ensure_launch_link(lid)
    assert result.status == UPDATED
    assert observed["sessions"] == []


def test_service_runs_concurrently_without_double_write(setup_runtime):
    db = setup_runtime[0]
    lid, _ = seed_terminal_launch(setup_runtime)
    services = [make_service(db, FakeLangfuseSDK(), DASHBOARD) for _ in range(2)]
    results: list[str] = []
    barrier = threading.Barrier(2)

    def _run(service):
        barrier.wait(timeout=5)
        results.append(service.ensure_launch_link(lid).status)

    threads = [threading.Thread(target=_run, args=(svc,)) for svc in services]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == ["UNCHANGED", "UPDATED"]
    with db.get_session() as s:
        url = s.get(Launch, lid).langfuse_experiment_url
        assert url == f"{DASHBOARD}/project/proj-real/datasets/ds-real/runs/r1"


def test_service_does_not_modify_frozen_manifest(setup_runtime):
    db = setup_runtime[0]
    lid, _ = seed_terminal_launch(setup_runtime)
    with db.get_session() as s:
        before = dict(s.get(Launch, lid).manifest)
    service = make_service(db, FakeLangfuseSDK(), DASHBOARD)
    service.ensure_launch_link(lid)
    with db.get_session() as s:
        after = s.get(Launch, lid).manifest
        assert after == before
        assert after["dataset"]["dataset_id"] == "ds-real"
        assert "snapshot_digest" in after["dataset"] or after["dataset"].get("items") == [
            {"id": "0", "input": {}}
        ]


def test_seed_source_with_real_run_still_gets_a_link(setup_runtime):
    """A seed dataset is not a blanket link switch: a real remote run must still be linked."""
    lid, _ = seed_terminal_launch(
        setup_runtime,
        dataset_source="seed",
        dataset_id="seed-banking-agent-regression",
        store_run_id="r1",
        task_run_id="r1",
        sync_status="SYNCED",
    )
    lf = FakeLangfuseSDK(dataset_ids={"banking-agent-regression": "ds-real"})
    service = make_service(setup_runtime[0], lf, DASHBOARD)

    result = service.ensure_launch_link(lid)
    assert result.status == UPDATED
    assert result.url == f"{DASHBOARD}/project/proj-real/datasets/ds-real/runs/r1"


# ---------------------------------------------------------------------------
# Run evidence must be settled before a link is published or trusted
# ---------------------------------------------------------------------------
def test_service_defers_link_until_every_current_task_has_settled(setup_runtime):
    """当前代次仍有未落定任务时不得落链。

    旧实现在第一个任务 SYNCED 后立即为 run-a 生成链接。此时第二个任务还没上报任何
    Run，"没有冲突"只是证据尚未到齐，而不是证据一致。若它随后上报 run-b，
    ``aggregate_launch_sync_status`` 会因冲突抛错却不会清空已写入的 URL，而
    ``ensure_launch_link`` 又因"已存 URL 是安全的"直接短路返回 UNCHANGED，launch
    于是永久暴露 run-a。
    """
    db = setup_runtime[0]
    lid, _ = seed_terminal_launch(
        setup_runtime,
        store_run_id=None,
        task_run_id="run-a",
        item_count=2,
        extra_tasks=[{"item_index": 1, "payload": {}, "status": "PENDING"}],
    )
    lf = FakeLangfuseSDK()
    service = make_service(db, lf, DASHBOARD)

    # 1. 证据尚未完整：一个 Run 已被看到，但当前代次还没有全部落定
    assert service.ensure_launch_link(lid).status == DEFERRED
    with db.get_session() as s:
        assert s.get(Launch, lid).langfuse_experiment_url is None
    assert lf.projects.calls == []  # a launch that may still move costs no remote work

    # 2. 第二个任务落定为另一个 Run：launch 必须停止暴露第一个 Run
    with db.get_session() as s:
        second = s.query(Task).filter(Task.launch_id == lid, Task.dataset_item_id == "1").one()
        second.status = "SYNCED"
        second.scores_payload = {"_dataset_run_id": "run-b"}

    result = service.ensure_launch_link(lid)
    assert result.status == NO_REMOTE_RUN
    assert result.reason == RUN_ID_CONFLICT
    with db.get_session() as s:
        assert s.get(Launch, lid).langfuse_experiment_url is None


def test_service_retracts_stored_link_when_current_runs_conflict(setup_runtime):
    """已落链的 URL 必须先与当前代次证据核对，再谈幂等短路。"""
    db = setup_runtime[0]
    stored = f"{DASHBOARD}/project/proj-real/datasets/ds-real/runs/run-a"
    lid, _ = seed_terminal_launch(
        setup_runtime,
        store_url=stored,
        store_run_id="run-a",
        task_run_id="run-a",
        item_count=2,
        extra_tasks=[
            {"item_index": 1, "payload": {"_dataset_run_id": "run-b"}, "status": "SYNCED"},
        ],
    )
    lf = FakeLangfuseSDK()
    service = make_service(db, lf, DASHBOARD)

    result = service.ensure_launch_link(lid)
    assert result.status == RUN_ID_CONFLICT
    with db.get_session() as s:
        launch = s.get(Launch, lid)
        assert launch.langfuse_experiment_url is None
        # 只清链接：同步事实与已聚合的 Run ID 保持不变，冲突仍可审计、可重算
        assert launch.langfuse_experiment_id == "run-a"
        assert launch.langfuse_sync_status == "SYNCED"
        assert launch.langfuse_sync_error is None
    assert lf.projects.calls == []


def test_service_keeps_stored_link_when_evidence_agrees(setup_runtime):
    """证据一致时仍然是幂等短路，且不产生任何远端调用。"""
    db = setup_runtime[0]
    stored = f"{DASHBOARD}/project/proj-real/datasets/ds-real/runs/r1"
    lid, _ = seed_terminal_launch(setup_runtime, store_url=stored, store_run_id="r1", task_run_id="r1")
    lf = FakeLangfuseSDK()
    service = make_service(db, lf, DASHBOARD)

    result = service.ensure_launch_link(lid)
    assert result.status == "UNCHANGED"
    with db.get_session() as s:
        assert s.get(Launch, lid).langfuse_experiment_url == stored
    assert lf.projects.calls == [] and lf.datasets.calls == []
