import { describe, it, expect } from "vitest";
import {
  getLangfuseLinkView,
  isSafeLangfuseUrl,
} from "../../features/launches/langfuseLink";
import {
  LINK_BACKFILL_WINDOW_MS,
  backfillKeyFor,
  createBackfillTracker,
  evaluateLaunchPolling,
} from "../../features/launches/useLaunchPolling";

const linkedUrl = "https://cloud.example.com/project/p1/datasets/d1/runs/r1";

const syncedNoLink = {
  id: "launch-1",
  status: "COMPLETED",
  langfuse_sync_status: "SYNCED",
  langfuse_experiment_id: "r1",
  langfuse_experiment_url: null,
};

describe("isSafeLangfuseUrl", () => {
  it("accepts absolute http(s) URLs including query and hash", () => {
    expect(isSafeLangfuseUrl("http://localhost:3000/project/p/datasets/d/runs/r")).toBe(true);
    expect(isSafeLangfuseUrl("https://obs.example.com/langfuse/project/p/datasets/d/runs/r")).toBe(true);
    expect(isSafeLangfuseUrl(`${linkedUrl}?tab=items#trace`)).toBe(true);
  });

  it("rejects unsafe or non-absolute values", () => {
    expect(isSafeLangfuseUrl("javascript:alert(1)")).toBe(false);
    expect(isSafeLangfuseUrl("data:text/html,<script>")).toBe(false);
    expect(isSafeLangfuseUrl("//evil.example.com/x")).toBe(false);
    expect(isSafeLangfuseUrl("/project/p/datasets/d/runs/r")).toBe(false);
    expect(isSafeLangfuseUrl("https://user:pass@obs.example.com/x")).toBe(false);
    expect(isSafeLangfuseUrl(" https://obs.example.com/x")).toBe(false);
    expect(isSafeLangfuseUrl("https://obs.example.com/x\n")).toBe(false);
    expect(isSafeLangfuseUrl("https://obs.example.com\\x")).toBe(false);
    expect(isSafeLangfuseUrl("https://obs.example.com/x")).toBe(true);
    expect(isSafeLangfuseUrl("")).toBe(false);
    expect(isSafeLangfuseUrl(null)).toBe(false);
    expect(isSafeLangfuseUrl(undefined)).toBe(false);
  });
});

describe("getLangfuseLinkView", () => {
  it("prefers a safe URL over every other state", () => {
    const view = getLangfuseLinkView({
      id: "l1",
      status: "COMPLETED",
      langfuse_sync_status: "FAILED",
      langfuse_experiment_url: linkedUrl,
    });
    expect(view.kind).toBe("link");
    if (view.kind === "link") {
      expect(view.href).toBe(linkedUrl);
      expect(view.detailLabel).toBe("在 Langfuse 中查看");
    }
  });

  it("reports an invalid stored URL instead of rendering it", () => {
    const view = getLangfuseLinkView({
      id: "l1",
      status: "COMPLETED",
      langfuse_sync_status: "SYNCED",
      langfuse_experiment_id: "r1",
      langfuse_experiment_url: "javascript:alert(1)",
    });
    expect(view).toEqual({ kind: "reason", label: "Langfuse 地址无效", title: "Langfuse 地址无效" });
  });

  it("shows a real link for a seed dataset that has a remote run", () => {
    const view = getLangfuseLinkView({
      id: "l1",
      status: "COMPLETED",
      langfuse_sync_status: "SYNCED",
      langfuse_experiment_id: "r1",
      langfuse_experiment_url: linkedUrl,
      manifest: { dataset: { source: "seed" } },
    });
    expect(view.kind).toBe("link");
  });

  it("does not create a link for a seed dataset without a run", () => {
    const view = getLangfuseLinkView({
      id: "l1",
      status: "COMPLETED",
      langfuse_sync_status: "SYNCED",
      manifest: { dataset: { source: "seed" } },
    });
    expect(view.kind).toBe("reason");
    if (view.kind === "reason") expect(view.label).toBe("未创建 Langfuse Run");
  });
});

describe("launch link backfill polling", () => {
  it("does not backfill without a run id or outside SYNCED", () => {
    expect(backfillKeyFor({ id: "l1", status: "COMPLETED", langfuse_sync_status: "SYNCED" })).toBeNull();
    expect(
      backfillKeyFor({
        id: "l1",
        status: "COMPLETED",
        langfuse_sync_status: "FAILED",
        langfuse_experiment_id: "r1",
      })
    ).toBeNull();
    expect(
      backfillKeyFor({
        id: "l1",
        status: "COMPLETED",
        langfuse_sync_status: "NOT_APPLICABLE",
        langfuse_experiment_id: "r1",
      })
    ).toBeNull();
    expect(
      backfillKeyFor({
        id: "l1",
        status: "RUNNING",
        langfuse_sync_status: "SYNCED",
        langfuse_experiment_id: "r1",
      })
    ).toBeNull();
    expect(backfillKeyFor(syncedNoLink)).toBe("launch-1::r1");
  });

  it("polls while execution or sync is active", () => {
    const tracker = createBackfillTracker();
    expect(
      evaluateLaunchPolling(
        tracker,
        [{ id: "a", status: "RUNNING", langfuse_sync_status: "SYNCED" }],
        0
      ).shouldPoll
    ).toBe(true);
    expect(
      evaluateLaunchPolling(
        tracker,
        [{ ...syncedNoLink, id: "b", langfuse_sync_status: "SYNCING" }],
        0
      ).shouldPoll
    ).toBe(true);
  });

  it("keeps polling for SYNCING beyond the backfill window", () => {
    const tracker = createBackfillTracker();
    const syncing = { ...syncedNoLink, langfuse_sync_status: "SYNCING" };
    const later = Date.now() + 10 * LINK_BACKFILL_WINDOW_MS;
    expect(evaluateLaunchPolling(tracker, [syncing], later).shouldPoll).toBe(true);
  });

  it("stops a COMPLETED + SYNCING launch from polling", () => {
    const tracker = createBackfillTracker();
    const done = { ...syncedNoLink, langfuse_experiment_url: linkedUrl };
    expect(evaluateLaunchPolling(tracker, [done], Date.now()).shouldPoll).toBe(false);
  });

  it("opens a bounded window for SYNCED with a run id and no URL", () => {
    const tracker = createBackfillTracker();
    expect(evaluateLaunchPolling(tracker, [syncedNoLink], 1_000).shouldPoll).toBe(true);
    expect(
      evaluateLaunchPolling(tracker, [syncedNoLink], 1_000 + LINK_BACKFILL_WINDOW_MS - 1).shouldPoll
    ).toBe(true);
    expect(
      evaluateLaunchPolling(tracker, [syncedNoLink], 1_000 + LINK_BACKFILL_WINDOW_MS).shouldPoll
    ).toBe(false);
  });

  it("does not extend the window for new responses or object identities", () => {
    const tracker = createBackfillTracker();
    evaluateLaunchPolling(tracker, [syncedNoLink], 0);
    // fresh GET, brand new object, later dataUpdatedAt
    const refreshed = { ...syncedNoLink, updated_at: new Date().toISOString() };
    expect(
      evaluateLaunchPolling(tracker, [refreshed], LINK_BACKFILL_WINDOW_MS - 1).shouldPoll
    ).toBe(true);
    expect(evaluateLaunchPolling(tracker, [{ ...refreshed }], LINK_BACKFILL_WINDOW_MS).shouldPoll).toBe(
      false
    );
  });

  it("starts a new window when the run id changes", () => {
    const tracker = createBackfillTracker();
    evaluateLaunchPolling(tracker, [syncedNoLink], 0);
    expect(evaluateLaunchPolling(tracker, [syncedNoLink], LINK_BACKFILL_WINDOW_MS).shouldPoll).toBe(false);

    const rerun = { ...syncedNoLink, langfuse_experiment_id: "r2" };
    expect(evaluateLaunchPolling(tracker, [rerun], LINK_BACKFILL_WINDOW_MS + 1).shouldPoll).toBe(true);
  });

  it("re-arms after the launch becomes active again", () => {
    const tracker = createBackfillTracker();
    evaluateLaunchPolling(tracker, [syncedNoLink], 0);
    expect(evaluateLaunchPolling(tracker, [syncedNoLink], LINK_BACKFILL_WINDOW_MS).shouldPoll).toBe(false);

    evaluateLaunchPolling(tracker, [{ ...syncedNoLink, status: "RUNNING" }], LINK_BACKFILL_WINDOW_MS + 1);
    expect(
      evaluateLaunchPolling(tracker, [syncedNoLink], LINK_BACKFILL_WINDOW_MS + 2).shouldPoll
    ).toBe(true);
  });

  it("stops as soon as the URL appears and forgets the window", () => {
    const tracker = createBackfillTracker();
    evaluateLaunchPolling(tracker, [syncedNoLink], 0);
    expect(tracker.size).toBe(1);
    const linked = { ...syncedNoLink, langfuse_experiment_url: linkedUrl };
    expect(evaluateLaunchPolling(tracker, [linked], 10).shouldPoll).toBe(false);
    expect(tracker.size).toBe(0);
  });

  it("never backfills seed, failed or not-applicable launches", () => {
    const tracker = createBackfillTracker();
    const cases = [
      { ...syncedNoLink, langfuse_experiment_id: undefined, manifest: { dataset: { source: "seed" } } },
      { ...syncedNoLink, langfuse_sync_status: "FAILED" },
      { ...syncedNoLink, langfuse_sync_status: "NOT_APPLICABLE" },
    ];
    for (const launch of cases) {
      expect(evaluateLaunchPolling(tracker, [launch], 0).shouldPoll).toBe(false);
    }
  });

  it("drops windows for launches that leave the page so filtering can re-arm them", () => {
    const tracker = createBackfillTracker();
    evaluateLaunchPolling(tracker, [syncedNoLink], 0);
    expect(tracker.size).toBe(1);

    // The launch is filtered out: its window is released.
    const other = { ...syncedNoLink, id: "other-launch" };
    evaluateLaunchPolling(tracker, [other], 10);
    expect(tracker.size).toBe(1);
    expect([...tracker.keys()]).toEqual(["other-launch"]);

    // An unresolved query (loaded=false) must not release anything.
    evaluateLaunchPolling(tracker, [], 20, LINK_BACKFILL_WINDOW_MS, false);
    expect(tracker.size).toBe(1);

    // Re-entering the filtered launch opens a fresh window instead of a dead one.
    expect(evaluateLaunchPolling(tracker, [syncedNoLink], 30).shouldPoll).toBe(true);
  });

  it("releases expired windows when a filter loads zero results", () => {
    const tracker = createBackfillTracker();
    evaluateLaunchPolling(tracker, [syncedNoLink], 0);
    expect(tracker.size).toBe(1);

    // The filter matches nothing. That is a loaded answer, not an unresolved query,
    // so the window must be released instead of lingering.
    const afterWindow = LINK_BACKFILL_WINDOW_MS + 10;
    expect(evaluateLaunchPolling(tracker, [], afterWindow).shouldPoll).toBe(false);
    expect(tracker.size).toBe(0);

    // Resetting the filter must therefore open a fresh window rather than reuse the
    // expired deadline, otherwise the launch never polls for a late link again.
    expect(evaluateLaunchPolling(tracker, [syncedNoLink], afterWindow + 1).shouldPoll).toBe(true);
  });

  it("aggregates a list of launches", () => {
    const tracker = createBackfillTracker();
    const decision = evaluateLaunchPolling(
      tracker,
      [
        { id: "done", status: "COMPLETED", langfuse_sync_status: "SYNCED", langfuse_experiment_url: linkedUrl },
        { ...syncedNoLink, id: "waiting" },
      ],
      0
    );
    expect(decision.executionActive).toBe(false);
    expect(decision.backfilling).toBe(true);
    expect(decision.shouldPoll).toBe(true);
  });
});
