"""Historical Langfuse link compensation (Issue #44)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services" / "eval-runner")]

from app.db_models import ExperimentLaunchRecord as Launch  # noqa: E402
from app.db_models import LangfuseSyncTaskRecord as Task  # noqa: E402
from app.langfuse_sync import LangfuseOutboxSyncer  # noqa: E402
from fake_langfuse import FakeLangfuseSDK  # noqa: E402
from link_helpers import make_service, seed_terminal_launch  # noqa: E402

DASHBOARD = "https://cloud.langfuse.example.com"
DATASET = "banking-agent-regression"
EXPECTED_URL = f"{DASHBOARD}/project/proj-real/datasets/ds-real/runs/r1"


def _launch_row(db, lid):
    with db.get_session() as s:
        rec = s.get(Launch, lid)
        return SimpleNamespace(
            status=rec.status,
            sync_status=rec.langfuse_sync_status,
            sync_error=rec.langfuse_sync_error,
            run_id=rec.langfuse_experiment_id,
            url=rec.langfuse_experiment_url,
        )


def _task_rows(db, lid):
    with db.get_session() as s:
        rows = s.query(Task).filter(Task.launch_id == lid).all()
        return [(r.status, r.attempts, (r.scores_payload or {}).get("_dataset_run_id")) for r in rows]


# ---------------------------------------------------------------------------
# Historical compensation
# ---------------------------------------------------------------------------
def test_history_backfill_without_rerun(setup_runtime):
    db, reconciler = setup_runtime[0], setup_runtime[5]
    lid, _ = seed_terminal_launch(setup_runtime)
    tasks_before = _task_rows(db, lid)

    lf = FakeLangfuseSDK(dataset_ids={DATASET: "ds-real"})
    service = make_service(db, lf, DASHBOARD)

    assert reconciler.reconcile_langfuse_links(service) == 1
    row = _launch_row(db, lid)
    assert row.url == EXPECTED_URL
    assert row.run_id == "r1"
    # no re-run, no resend, no sync fact changes
    assert row.sync_status == "SYNCED" and row.sync_error is None
    assert _task_rows(db, lid) == tasks_before
    assert lf.scores.created == []
    assert lf.dataset_run_items.calls == []


def test_history_backfill_recovers_run_id_from_task_only(setup_runtime):
    db, reconciler = setup_runtime[0], setup_runtime[5]
    lid, _ = seed_terminal_launch(setup_runtime, store_run_id=None, task_run_id="r1")

    lf = FakeLangfuseSDK(dataset_ids={DATASET: "ds-real"})
    service = make_service(db, lf, DASHBOARD)

    assert reconciler.reconcile_langfuse_links(service) == 1
    row = _launch_row(db, lid)
    assert row.url == EXPECTED_URL
    assert row.run_id == "r1"


def test_run_id_only_launch_is_rebuilt_after_a_dashboard_is_configured(setup_runtime):
    """没有 dashboard 时只保留 Run ID，配置之后补偿器必须能无重跑补齐链接。

    这正是同步路径放弃 SDK 回退链接的前提：URL 留空，launch 才会继续留在补链
    候选集合里。
    """
    db, reconciler = setup_runtime[0], setup_runtime[5]
    lid, _ = seed_terminal_launch(
        setup_runtime, dataset_id=None, store_run_id="r1", task_status=None
    )
    assert _launch_row(db, lid).url is None
    assert lid in reconciler._link_candidate_ids(None, 50)

    service = make_service(db, FakeLangfuseSDK(dataset_ids={DATASET: "ds-real"}), DASHBOARD)
    assert reconciler.reconcile_langfuse_links(service) == 1
    assert _launch_row(db, lid).url == EXPECTED_URL


def test_history_backfill_is_idempotent(setup_runtime):
    db, reconciler = setup_runtime[0], setup_runtime[5]
    lid, _ = seed_terminal_launch(setup_runtime)
    service = make_service(db, FakeLangfuseSDK(), DASHBOARD)

    assert reconciler.reconcile_langfuse_links(service) == 1
    assert reconciler.reconcile_langfuse_links(service) == 0  # already linked
    assert _launch_row(db, lid).url == EXPECTED_URL


def test_candidate_scan_skips_ineligible_launches(setup_runtime):
    db, reconciler = setup_runtime[0], setup_runtime[5]
    terminal, _ = seed_terminal_launch(setup_runtime)
    running, _ = seed_terminal_launch(setup_runtime, launch_status="RUNNING")
    linked, _ = seed_terminal_launch(setup_runtime, store_url=EXPECTED_URL)
    no_evidence, _ = seed_terminal_launch(
        setup_runtime, store_run_id=None, task_run_id=None, sync_status="NOT_APPLICABLE"
    )
    service = make_service(db, FakeLangfuseSDK(), DASHBOARD)

    candidates = reconciler._link_candidate_ids(None, 50)
    assert terminal in candidates
    assert running not in candidates
    assert linked not in candidates

    # A current-generation SYNCED task without run evidence is scanned cheaply, but the
    # link service refuses it and nothing is written.
    assert service.ensure_launch_link(no_evidence).status == "NO_REMOTE_RUN"
    assert _launch_row(db, no_evidence).url is None
    assert _launch_row(db, no_evidence).run_id is None


def test_failure_triggers_backoff_and_is_isolated(setup_runtime):
    db, reconciler = setup_runtime[0], setup_runtime[5]
    good, _ = seed_terminal_launch(setup_runtime)
    bad, _ = seed_terminal_launch(setup_runtime)

    class FlakyService:
        def __init__(self, inner):
            self.inner = inner
            self.calls: list[str] = []

        def ensure_launch_link(self, launch_id):
            self.calls.append(launch_id)
            if launch_id == bad:
                raise RuntimeError("remote boom")
            return self.inner.ensure_launch_link(launch_id)

    flaky = FlakyService(make_service(db, FakeLangfuseSDK(), DASHBOARD))
    assert reconciler.reconcile_langfuse_links(flaky) == 1
    assert _launch_row(db, good).url == EXPECTED_URL
    assert _launch_row(db, bad).url is None

    # the failing launch is backed off, so a second pass only retries the healthy one
    first_round = list(flaky.calls)
    assert flaky.calls.count(bad) == 1
    assert reconciler.reconcile_langfuse_links(flaky) == 0
    assert flaky.calls.count(bad) == 1  # still inside the backoff window
    assert set(flaky.calls) - set(first_round) == set()


def test_budget_is_enforced_even_when_no_launch_is_updated(setup_runtime):
    """所有候选都失败时预算同样必须生效。

    旧实现只在 ``updated > 0`` 时检查 deadline，因此"全部不可用"的一批候选会
    顺序做完所有远端查询（每次自带 2 秒超时），远超声明的 10 秒预算。
    """
    import time as time_module

    reconciler = setup_runtime[5]
    for _ in range(6):
        seed_terminal_launch(setup_runtime)

    sleep_per_call = 0.05

    class UnavailableService:
        def __init__(self):
            self.calls: list[str] = []

        def ensure_launch_link(self, launch_id):
            self.calls.append(launch_id)
            time_module.sleep(sleep_per_call)
            return SimpleNamespace(status="UNAVAILABLE", run_id=None)

    service = UnavailableService()
    assert reconciler.reconcile_langfuse_links(service, budget_seconds=0.12) == 0
    # 必须提前放弃，而不是把整批候选的远端查询都跑完
    assert len(service.calls) < 6
    assert len(service.calls) > 0


def test_budget_exhaustion_keeps_cursor_on_last_visited_candidate(setup_runtime):
    """预算耗尽时游标只能推进到真正访问过的候选。

    旧实现无条件把 ``_link_cursor`` 推到 ``candidate_ids[-1]``：一批 6 个候选只访问了
    前 3 个就超时，剩下 3 个仍被当作"已扫描"，必须等整个键空间回绕才会重试；若游标
    之后持续有新 Launch 插入，它们会被永久饿死。
    """
    import time as time_module

    reconciler = setup_runtime[5]
    lids = [seed_terminal_launch(setup_runtime)[0] for _ in range(6)]

    class SlowService:
        def __init__(self):
            self.calls: list[str] = []

        def ensure_launch_link(self, launch_id):
            self.calls.append(launch_id)
            time_module.sleep(0.05)
            return SimpleNamespace(status="UNAVAILABLE", run_id=None)

    # 候选按主键排序，UUID 的字典序与创建顺序无关
    ordered = sorted(lids)

    service = SlowService()
    assert reconciler.reconcile_langfuse_links(service, batch_size=6, budget_seconds=0.13) == 0

    visited = list(service.calls)
    assert 0 < len(visited) < len(lids)
    assert reconciler._link_cursor == visited[-1]
    assert reconciler._link_cursor != ordered[-1]

    # 未访问的尾部必须立刻成为下一轮的候选，而不是等键空间回绕
    remaining = reconciler._link_candidate_ids(reconciler._link_cursor, len(lids))
    assert remaining == [lid for lid in ordered if lid > reconciler._link_cursor]
    assert remaining, "budget exhaustion must not skip the unvisited tail"


def test_backoff_eviction_bounds_both_dictionaries(setup_runtime):
    """退避表淘汰时必须同步淘汰延迟计数，否则字典无界增长。"""
    _db, reconciler = setup_runtime[0], setup_runtime[5]
    cap = reconciler._LINK_BACKOFF_MAX_ENTRIES

    for i in range(cap + 50):
        reconciler._link_note_failure(f"launch-{i:05d}", "UNAVAILABLE:None", 0.0)

    assert len(reconciler._link_backoff) == cap
    assert len(reconciler._link_backoff_delays) == cap
    oldest = {f"launch-{i:05d}" for i in range(50)}
    assert oldest.isdisjoint(reconciler._link_backoff_delays)


def test_backoff_signature_change_resets_delay(setup_runtime):
    db, reconciler = setup_runtime[0], setup_runtime[5]
    lid, _ = seed_terminal_launch(setup_runtime)
    service = make_service(db, FakeLangfuseSDK(project_ids=[]), DASHBOARD)

    assert reconciler.reconcile_langfuse_links(service) == 0
    first_deadline = reconciler._link_backoff[lid][0]
    assert reconciler._link_backoff[lid][1].startswith("UNAVAILABLE")

    service_ok = make_service(db, FakeLangfuseSDK(), DASHBOARD)
    assert reconciler.reconcile_langfuse_links(service_ok, ) == 0  # still backed off
    # a changed evidence signature is retried immediately
    reconciler._link_backoff[lid] = (0.0, reconciler._link_backoff[lid][1])
    assert reconciler.reconcile_langfuse_links(service_ok) == 1
    assert lid not in reconciler._link_backoff
    assert first_deadline > 0


def test_keyset_cursor_visits_every_candidate(setup_runtime):
    db, reconciler = setup_runtime[0], setup_runtime[5]
    lids = [seed_terminal_launch(setup_runtime)[0] for _ in range(5)]
    service = make_service(db, FakeLangfuseSDK(), DASHBOARD)

    seen: list[str] = []
    for _ in range(6):
        reconciler.reconcile_langfuse_links(service, batch_size=2)
        seen.extend(lids)
    assert all(_launch_row(db, lid).url == EXPECTED_URL for lid in lids)


def test_cursor_wraps_instead_of_starving_tail(setup_runtime):
    db, reconciler = setup_runtime[0], setup_runtime[5]
    lids = [seed_terminal_launch(setup_runtime)[0] for _ in range(3)]
    service = make_service(db, FakeLangfuseSDK(), DASHBOARD)

    reconciler._link_cursor = "zzzz-zzzz"  # exhausted pass wraps to the beginning
    first = reconciler.reconcile_langfuse_links(service, batch_size=2)
    second = reconciler.reconcile_langfuse_links(service, batch_size=2)
    assert first == 2 and second == 1
    assert all(_launch_row(db, lid).url == EXPECTED_URL for lid in lids)


def test_two_reconciler_instances_converge(setup_runtime):
    db = setup_runtime[0]
    lid, _ = seed_terminal_launch(setup_runtime)
    services = [make_service(db, FakeLangfuseSDK(), DASHBOARD) for _ in range(2)]
    results: list[int] = []
    barrier = threading.Barrier(2)

    def _run(reconciler, service):
        barrier.wait(timeout=5)
        results.append(reconciler.reconcile_langfuse_links(service))

    threads = [
        threading.Thread(target=_run, args=(setup_runtime[5], svc)) for svc in services
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sum(results) == 1
    assert _launch_row(db, lid).url == EXPECTED_URL


def test_reconcile_without_service_is_noop(setup_runtime):
    assert setup_runtime[5].reconcile_langfuse_links(None) == 0


# ---------------------------------------------------------------------------
# Async outbox path
# ---------------------------------------------------------------------------
def test_outbox_success_persists_run_id_and_link(setup_runtime):
    db = setup_runtime[0]
    lid, _ = seed_terminal_launch(
        setup_runtime,
        store_run_id=None,
        task_run_id=None,
        sync_status="SYNCING",
        task_status="PENDING",
        task_run_name="review",
    )
    lf = FakeLangfuseSDK(dataset_run_id="r1", dataset_ids={DATASET: "ds-real"})
    service = make_service(db, lf, DASHBOARD)
    syncer = LangfuseOutboxSyncer(db, lf, link_service=service)

    assert syncer.process_batch() == 1
    row = _launch_row(db, lid)
    assert row.run_id == "r1"
    assert row.url == EXPECTED_URL
    assert row.sync_status == "SYNCED"


def test_outbox_link_failure_does_not_fail_task(setup_runtime):
    db = setup_runtime[0]
    lid, _ = seed_terminal_launch(
        setup_runtime,
        store_run_id=None,
        task_run_id=None,
        sync_status="SYNCING",
        task_status="PENDING",
    )
    lf = FakeLangfuseSDK(dataset_run_id="r1", dataset_ids={DATASET: "ds-real"})

    class BrokenLinkService:
        def ensure_launch_link(self, launch_id):
            raise RuntimeError("link resolution exploded")

    syncer = LangfuseOutboxSyncer(db, lf, link_service=BrokenLinkService())
    assert syncer.process_batch() == 1  # task still succeeded
    row = _launch_row(db, lid)
    assert row.run_id == "r1"
    assert row.url is None
    assert row.sync_status == "SYNCED"
    assert row.sync_error is None


def test_outbox_without_link_service_keeps_legacy_behaviour(setup_runtime):
    db = setup_runtime[0]
    lid, _ = seed_terminal_launch(
        setup_runtime,
        store_run_id=None,
        task_run_id=None,
        sync_status="SYNCING",
        task_status="PENDING",
    )
    lf = FakeLangfuseSDK(dataset_run_id="r1")
    assert LangfuseOutboxSyncer(db, lf).process_batch() == 1
    row = _launch_row(db, lid)
    assert row.run_id == "r1" and row.url is None


def test_new_run_id_clears_stale_link(setup_runtime):
    db = setup_runtime[0]
    lid, _ = seed_terminal_launch(
        setup_runtime,
        store_run_id="r1",
        store_url=EXPECTED_URL,
        task_run_id="r2",
        task_run_name="review",
        task_status="PENDING",
    )
    lf = FakeLangfuseSDK(dataset_run_id="r2")
    assert LangfuseOutboxSyncer(db, lf).process_batch() == 1
    row = _launch_row(db, lid)
    assert row.run_id == "r2"
    assert row.url is None  # old link no longer matches the current run
