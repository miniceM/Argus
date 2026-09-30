import { test, expect, type Page } from "@playwright/test";

/**
 * Issue #85 acceptance — the promise a reviewer must be able to verify:
 *
 *   1. the report names the exact revision it is showing, never "latest",
 *   2. a shared link pins that revision and keeps returning it after a newer
 *      revision exists,
 *   3. the history is switchable, and switching writes the revision into the
 *      URL so the new view is itself shareable,
 *   4. an incomplete revision says so and is refused as a Baseline,
 *   5. the current Baseline shows both its result revision and its binding
 *      revision.
 */

const LAUNCH_ID = "launch-85";

const revision = (over: Record<string, unknown> = {}) => ({
  snapshot_id: "snap-2",
  revision: 2,
  created_at: "2026-09-30T00:00:00Z",
  source_result_digest: "aaaaaaaabbbbbbbbcccc",
  manifest_digest: "manifest-digest",
  evidence_state: "COMPLETE",
  evidence_reasons: [],
  total_cases: 6,
  quality_pass_count: 6,
  quality_fail_count: 0,
  quality_unknown_count: 0,
  is_latest: true,
  ...over,
});

const summary = (over: Record<string, unknown> = {}) => ({
  launch_id: LAUNCH_ID,
  snapshot_id: "snap-2",
  revision: 2,
  created_at: "2026-09-30T00:00:00Z",
  manifest_digest: "manifest-digest",
  source_result_digest: "aaaaaaaabbbbbbbbcccc",
  evidence_state: "COMPLETE",
  evidence_reasons: [],
  versions: { agent: { id: "banking-agent", version: "v2" }, dataset: {}, evaluators: [], runner: {} },
  summary: {
    pass_rate: 1,
    evaluation_coverage: 1,
    total_cases: 6,
    evaluated_cases: 6,
    critical_failure_count: 0,
    execution_error_count: 0,
    evaluator_error_count: 0,
    p95_latency_ms: 30,
    cost_per_case: null,
  },
  langfuse_score_sync_status: "SYNCED",
  ...over,
});

const baseline = {
  agent_id: "banking-agent",
  environment: "production",
  result_snapshot_id: "baseline-snap",
  revision: 3,
  result_revision: 1,
  result_evidence_state: "COMPLETE",
  updated_by: "alice",
  updated_at: "2026-09-29T00:00:00Z",
  launch_id: "baseline-launch",
  agent_version: "v2",
  dataset_name: "golden",
  summary: {},
};

const launch = {
  id: LAUNCH_ID,
  name: "issue-85 launch",
  status: "COMPLETED",
  quality_conclusion: "pass",
  dataset_name: "golden",
  dataset_version: "v1",
  agent_id: "banking-agent",
  agent_version: "v2",
  langfuse_sync_status: "SYNCED",
  created_at: "2026-09-30T00:00:00Z",
  started_at: "2026-09-30T00:00:01Z",
  completed_at: "2026-09-30T00:00:12Z",
  manifest: { schema_version: "1.2", dataset: { items_count: 6 } },
  allowed_actions: [],
  progress: { total: 6, completed: 6, allowed_actions: [] },
};

async function mockApi(
  page: Page,
  opts: { revisions?: Record<string, unknown>[]; summary?: Record<string, unknown> } = {},
) {
  const revisions = opts.revisions ?? [
    revision(),
    revision({
      snapshot_id: "snap-1",
      revision: 1,
      is_latest: false,
      source_result_digest: "11111111222222223333",
    }),
  ];

  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/result-snapshots`, async (route) => {
    await route.fulfill({
      json: {
        launch_id: LAUNCH_ID,
        latest_snapshot_id: revisions[0].snapshot_id,
        latest_revision: revisions[0].revision,
        revisions,
      },
    });
  });
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/result-snapshots/**`, async (route) => {
    const id = route.request().url().split("/").pop();
    const found = revisions.find((r) => r.snapshot_id === id) ?? revisions[0];
    await route.fulfill({
      json: {
        launch_id: LAUNCH_ID,
        snapshot_id: found.snapshot_id,
        revision: found.revision,
        created_at: found.created_at,
        source_result_digest: found.source_result_digest,
        manifest_digest: found.manifest_digest,
        evidence_state: found.evidence_state,
        evidence_reasons: found.evidence_reasons,
        releasable: found.evidence_state === "COMPLETE",
        versions: {},
        summary: {},
        items: [],
      },
    });
  });
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/summary**`, async (route) => {
    await route.fulfill({ json: summary(opts.summary ?? {}) });
  });
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/comparison**`, async (route) => {
    await route.fulfill({
      json: { items: [], next_cursor: null, classification_counts: {}, summary: {} },
    });
  });
  await page.route("**/api/v1/agents/*/baselines**", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({ json: baseline });
  });
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/items**`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}`, async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({ json: launch });
  });
}

test.describe("Issue #85 fixed result revisions", () => {
  test("the report names its revision and offers a shareable pinned link", async ({ page }) => {
    await mockApi(page);
    await page.goto(`/launches/${LAUNCH_ID}`);

    const panel = page.getByTestId("result-snapshot-panel");
    await expect(panel).toBeVisible();
    await expect(page.getByTestId("snapshot-revision")).toHaveText("Revision 2");
    await expect(page.getByTestId("snapshot-latest-tag")).toBeVisible();
    // The share link pins a concrete snapshot id, never "latest".
    await expect(page.getByTestId("snapshot-share-url")).toContainText("snapshot_id=snap-2");
    await expect(page.getByTestId("snapshot-share-url")).not.toContainText("latest");
  });

  test("a shared link stays on its revision after a newer one exists", async ({ page }) => {
    await mockApi(page);
    await page.goto(`/launches/${LAUNCH_ID}?snapshot_id=snap-1`);

    await expect(page.getByTestId("snapshot-revision")).toHaveText("Revision 1");
    await expect(page.getByTestId("snapshot-newer-available")).toContainText("已有更新的 Revision 2");
    await expect(page.getByTestId("snapshot-share-url")).toContainText("snapshot_id=snap-1");
  });

  test("switching history writes the revision into the URL", async ({ page }) => {
    await mockApi(page);
    await page.goto(`/launches/${LAUNCH_ID}`);

    await expect(page.getByTestId("snapshot-revision")).toHaveText("Revision 2");
    await page.getByTestId("snapshot-revision-1").click();

    await expect(page).toHaveURL(/snapshot_id=snap-1/);
    await expect(page.getByTestId("snapshot-revision")).toHaveText("Revision 1");
  });

  test("an incomplete revision is labelled diagnostic and refused as a Baseline", async ({ page }) => {
    await mockApi(page, {
      revisions: [
        revision({
          evidence_state: "DIAGNOSTIC",
          evidence_reasons: ["1/6 个用例评测失败或未产出结果"],
          quality_unknown_count: 1,
        }),
      ],
      summary: {
        evidence_state: "DIAGNOSTIC",
        evidence_reasons: ["1/6 个用例评测失败或未产出结果"],
      },
    });
    await page.goto(`/launches/${LAUNCH_ID}`);

    await expect(page.getByTestId("snapshot-evidence-badge")).toHaveText("诊断快照");
    await expect(page.getByTestId("snapshot-evidence-reasons")).toContainText("评测失败");
    await expect(page.getByTestId("snapshot-evidence-help")).toContainText("不可作为正式 Baseline");
    await expect(page.getByTestId("snapshot-not-releasable")).toBeVisible();
    // The Baseline action is withheld and the reason is shown.
    await expect(page.getByTestId("summary-evidence-state")).toContainText("诊断");
    await expect(page.getByTestId("baseline-ineligible-hint")).toBeVisible();
    await expect(page.getByRole("button", { name: "设为当前环境 Baseline" })).toHaveCount(0);
  });

  test("the current Baseline shows both its result revision and its binding revision", async ({ page }) => {
    await mockApi(page);
    await page.goto(`/launches/${LAUNCH_ID}`);

    const line = page.getByTestId("baseline-revision-line");
    await expect(line).toContainText("结果修订 Revision 1");
    await expect(line).toContainText("绑定修订 3");
  });
});
