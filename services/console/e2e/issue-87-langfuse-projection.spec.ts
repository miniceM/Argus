import { test, expect, type Page } from "@playwright/test";

/**
 * Issue #87 acceptance — the promise a reviewer must be able to verify:
 *
 *   1. while Langfuse is unreachable the Argus quality conclusion and the
 *      frozen revision stay exactly as they were, and the sync failure is shown
 *      as a separate, explained fact,
 *   2. a synced Item/Trace scope never implies the Run Score scope is synced,
 *   3. after the connection recovers the sync state turns healthy and the
 *      quality conclusion has not moved.
 */

const LAUNCH_ID = "launch-87";

const summary = (langfuseSync: Record<string, unknown>) => ({
  launch_id: LAUNCH_ID,
  snapshot_id: "snap-1",
  revision: 1,
  created_at: "2026-09-30T00:00:00Z",
  manifest_digest: "manifest-digest",
  source_result_digest: "aaaaaaaabbbbbbbbcccc",
  evidence_state: "COMPLETE",
  evidence_reasons: [],
  versions: { agent: { id: "banking-agent", version: "2.4.0" }, dataset: {}, evaluators: [], runner: {} },
  summary: {
    pass_rate: 1,
    evaluation_coverage: 1,
    total_cases: 2,
    evaluated_cases: 2,
    critical_failure_count: 0,
    execution_error_count: 0,
    execution_error_rate: 0,
    evaluator_error_count: 0,
    p95_latency_ms: 30,
    cost_per_case: null,
  },
  langfuse_score_sync_status: "SYNCED",
  langfuse_sync: langfuseSync,
});

const scope = (over: Record<string, unknown> = {}) => ({
  status: "SYNCED",
  reason: null,
  task_count: 2,
  failed_count: 0,
  pending_count: 0,
  ...over,
});

const launch = {
  id: LAUNCH_ID,
  name: "issue-87 launch",
  status: "COMPLETED",
  quality_conclusion: "pass",
  dataset_name: "golden",
  dataset_version: "v1",
  agent_id: "banking-agent",
  agent_version: "2.4.0",
  langfuse_sync_status: "SYNCED",
  created_at: "2026-09-30T00:00:00Z",
  started_at: "2026-09-30T00:00:01Z",
  completed_at: "2026-09-30T00:00:12Z",
  manifest: { schema_version: "1.2", dataset: { items_count: 2 } },
  allowed_actions: [],
  progress: { total: 2, completed: 2, allowed_actions: [] },
};

async function mockApi(page: Page, langfuseSync: Record<string, unknown>) {
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/summary**`, async (route) => {
    await route.fulfill({ json: summary(langfuseSync) });
  });
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/comparison**`, async (route) => {
    await route.fulfill({
      json: {
        launch_id: LAUNCH_ID,
        candidate_snapshot_id: "snap-1",
        baseline_snapshot_id: "baseline-snap",
        baseline_binding_revision: 1,
        versions: { candidate: summary(langfuseSync).versions, baseline: summary(langfuseSync).versions },
        summary: { comparable_case_count: 2, baseline: {}, candidate: {}, comparable_cohort: null },
        classification_counts: {},
        items: [],
        next_cursor: null,
      },
    });
  });
  await page.route("**/api/v1/agents/*/baselines**", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({ status: 404, json: { detail: "not found" } });
  });
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/items**`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/result-snapshots**`, async (route) => {
    await route.fulfill({
      json: {
        launch_id: LAUNCH_ID,
        latest_snapshot_id: "snap-1",
        latest_revision: 1,
        revisions: [
          {
            snapshot_id: "snap-1",
            revision: 1,
            created_at: "2026-09-30T00:00:00Z",
            source_result_digest: "aaaaaaaabbbbbbbbcccc",
            manifest_digest: "manifest-digest",
            evidence_state: "COMPLETE",
            evidence_reasons: [],
            total_cases: 2,
            quality_pass_count: 2,
            quality_fail_count: 0,
            quality_unknown_count: 0,
            is_latest: true,
          },
        ],
      },
    });
  });
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}`, async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({ json: launch });
  });
}

const recovered = {
  overall: "SYNCED",
  item_trace: scope(),
  run_score: scope({ task_count: 1 }),
};

const itemSyncedRunScoreFailed = {
  overall: "FAILED",
  item_trace: scope(),
  run_score: scope({ status: "FAILED", reason: "Run score publish failed", task_count: 1, failed_count: 1 }),
};

test.describe("Issue #87 Langfuse projection and sync state", () => {
  test("a run-score failure is shown without hiding the synced item scope", async ({ page }) => {
    await mockApi(page, itemSyncedRunScoreFailed);
    await page.goto(`/launches/${LAUNCH_ID}`);

    const panel = page.getByTestId("langfuse-sync-panel");
    await expect(panel).toBeVisible();
    // Not "everything synced", and the failing scope is named.
    await expect(panel.getByTestId("langfuse-sync-overall")).toContainText("同步失败");
    await expect(panel.getByTestId("langfuse-sync-overall")).not.toContainText("全部已同步");
    await expect(panel.getByTestId("langfuse-sync-item-trace")).toContainText("已同步");
    await expect(panel.getByTestId("langfuse-sync-run-score")).toContainText("同步失败");
    await expect(panel.getByTestId("langfuse-sync-run-score")).toContainText("Run score publish failed");
  });

  test("an outage never changes the frozen quality conclusion or the revision", async ({ page }) => {
    await mockApi(page, {
      overall: "RETRY_EXHAUSTED",
      item_trace: scope({ status: "RETRY_EXHAUSTED", reason: "Langfuse unreachable", failed_count: 2 }),
      run_score: scope({ status: "RETRY_EXHAUSTED", reason: "Langfuse unreachable", failed_count: 1 }),
    });
    await page.goto(`/launches/${LAUNCH_ID}`);

    // The sync scope is broken...
    await expect(page.getByTestId("langfuse-sync-overall")).toContainText("重试已耗尽");
    // ...while the Argus-side evidence is untouched and still says PASS.
    await expect(page.getByTestId("summary-evidence-state")).toContainText("完整");
    await expect(page.getByTestId("result-snapshot-panel")).toContainText("PASS");
    await expect(page.getByTestId("langfuse-sync-disclaimer")).toContainText("不会改变");
  });

  test("after recovery the sync state is healthy and the conclusion has not moved", async ({ page }) => {
    await mockApi(page, recovered);
    await page.goto(`/launches/${LAUNCH_ID}`);

    const panel = page.getByTestId("langfuse-sync-panel");
    await expect(panel.getByTestId("langfuse-sync-overall")).toContainText("全部已同步");
    await expect(panel.getByTestId("langfuse-sync-item-trace")).toContainText("已同步");
    await expect(panel.getByTestId("langfuse-sync-run-score")).toContainText("已同步");
    // The same revision and the same quality conclusion as during the outage.
    await expect(page.getByTestId("result-snapshot-panel")).toContainText("PASS");
    await expect(page.getByTestId("summary-evidence-state")).toContainText("完整");
  });
});
