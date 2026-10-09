import { test, expect, type Page } from "@playwright/test";

// Issue #45: the Launch header shows a live all-cases quality ratio. A Launch
// that executed 6/6 successfully but passed only 2/6 quality must not read as
// one ambiguous "pass rate". This is a browser UI regression over mocked API
// responses; it is not a real Runner -> Agent -> Langfuse end-to-end run.
const launchId = "launch-quality-pass-rate-045";

const launch = {
  id: launchId,
  name: "quality-wording-launch",
  status: "COMPLETED",
  quality_conclusion: "FAIL",
  dataset_name: "banking-regression",
  dataset_version: "2026-09-20T00:00:00Z",
  agent_id: "banking-agent",
  agent_version: "v2",
  manifest: {
    schema_version: "1.0",
    dataset: { dataset_name: "banking-regression", dataset_version: "2026-09-20T00:00:00Z", items_count: 6 },
    agent: { id: "banking-agent", version: "v2" },
    evaluators: [{ id: "intent_match", version: "1.0.0", scope: "item" }],
  },
  langfuse_sync_status: "SYNCED",
  langfuse_experiment_url: "https://langfuse.example/project/demo/experiments/exp-45",
  created_at: "2026-09-20T00:00:00Z",
  started_at: "2026-09-20T00:00:01Z",
  completed_at: "2026-09-20T00:00:06Z",
  progress: {
    total: 6,
    pending: 0,
    queued: 0,
    running: 0,
    retry_wait: 0,
    succeeded: 6,
    failed: 0,
    timed_out: 0,
    cancelled: 0,
    completed: 6,
    percentage: 100,
    attempts: 6,
    retries: 0,
  },
  allowed_actions: [],
};

const items = Array.from({ length: 6 }, (_, index) => ({
  id: `item-exec-${index}`,
  launch_id: launchId,
  dataset_item_id: `case-${index}`,
  execution_status: "SUCCEEDED",
  eval_status: "SUCCEEDED",
  quality_conclusion: index < 2 ? "PASS" : "FAIL",
  scores: { intent_match: index < 2 ? 1 : 0 },
  attempt_count: 1,
  final_attempt_http_status: 200,
  final_attempt_latency_ms: 120,
  started_at: "2026-09-20T00:00:01Z",
}));

// A frozen result snapshot plus a comparable cohort, so the Baseline comparison
// area renders for real instead of degrading. Needed because the header metric
// and the cohort metric deliberately use different denominators.
const snapshotId = "snapshot-quality-045";

const snapshotRevision = {
  snapshot_id: snapshotId,
  revision: 1,
  is_latest: true,
  created_at: "2026-09-20T00:00:06Z",
  evidence_state: "COMPLETE",
  quality_pass_count: 2,
  quality_fail_count: 4,
  quality_unknown_count: 0,
};

const snapshotDetail = {
  launch_id: launchId,
  snapshot_id: snapshotId,
  revision: 1,
  is_latest: true,
  created_at: "2026-09-20T00:00:06Z",
  evidence_state: "COMPLETE",
  quality_pass_count: 2,
  quality_fail_count: 4,
  quality_unknown_count: 0,
  releasable: true,
  items: items.map((it) => ({
    ...it,
    scores: it.scores,
  })),
};

async function mockQualityApi(page: Page) {
  await page.route("**/api/v1/system/info", (route) =>
    route.fulfill({
      json: { service: "argus-eval-runner", version: "0.2.0", build_id: "issue-45-e2e", environment: "test" },
    })
  );
  await page.route("**/api/v1/agents**", (route) => route.fulfill({ json: [] }));

  // One dispatcher for the whole launches prefix. Playwright matches routes in
  // reverse registration order, so a narrower route registered after this one
  // would win; keeping every branch here removes that ordering hazard.
  await page.route("**/api/v1/experiment-launches**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;

    if (pathname.endsWith("/result-snapshots")) {
      await route.fulfill({
        json: {
          revisions: [snapshotRevision],
          latest_snapshot_id: snapshotId,
          latest_revision: 1,
        },
      });
      return;
    }

    if (pathname.includes("/result-snapshots/")) {
      await route.fulfill({ json: snapshotDetail });
      return;
    }

    // The result-snapshot endpoints are out of scope here. Fail them explicitly
    // so the comparison panel degrades on its own and the assertions stay
    // scoped to the header metric.
    if (pathname.endsWith("/summary") || pathname.includes("/comparison")) {
      await route.fulfill({ status: 503, json: { detail: "snapshot unavailable in this test" } });
      return;
    }

    if (pathname === "/api/v1/experiment-launches") {
      await route.fulfill({ json: [launch] });
      return;
    }
    if (pathname.endsWith("/items")) {
      await route.fulfill({ json: items });
      return;
    }
    await route.fulfill({ json: launch });
  });
}

const runSummary = {
  launch_id: launchId,
  snapshot_id: snapshotId,
  revision: 2,
  created_at: "2026-09-20T00:00:06Z",
  manifest_digest: "sha256:manifest-045",
  versions: {
    agent: { id: "banking-agent", version: "v2" },
    dataset: { name: "banking-regression", version: "2026-09-20T00:00:00Z" },
    evaluators: [{ id: "intent_match", version: "1.0.0" }],
    runner: { runner_version: "0.2.0", build_id: "issue-45-e2e" },
    environment: "production",
  },
  summary: {
    total_cases: 6,
    evaluated_cases: 6,
    passed_cases: 2,
    pass_rate: 2 / 6,
    evaluation_coverage: 1,
    execution_error_count: 0,
    execution_error_rate: 0,
    evaluator_error_count: 0,
    critical_failure_count: 0,
    p95_latency_ms: 120,
    cost_per_case: null,
    score_means: { intent_match: 0.33 },
  },
  langfuse_score_sync_status: "SYNCED",
};

const cohortMetrics = (passRate: number) => ({
  total_cases: 5,
  evaluated_cases: 5,
  passed_cases: Math.round(passRate * 5),
  pass_rate: passRate,
  evaluation_coverage: 1,
  execution_error_count: 0,
  execution_error_rate: 0,
  evaluator_error_count: 0,
  critical_failure_count: 0,
  p95_latency_ms: 120,
  cost_per_case: null,
  score_means: { intent_match: passRate },
});

const comparisonPage = {
  launch_id: launchId,
  candidate_snapshot_id: snapshotId,
  baseline_snapshot_id: "snapshot-baseline-044",
  baseline_binding_revision: 1,
  versions: {
    candidate: runSummary.versions,
    baseline: { ...runSummary.versions, agent: { id: "banking-agent", version: "v1" } },
  },
  // classification_counts is a top-level field of the comparison response, not
  // part of summary; the panel reads comparison.classification_counts.REGRESSION.
  classification_counts: { REGRESSION: 2, IMPROVEMENT: 1, UNCHANGED: 2, NOT_COMPARABLE: 0 },
  summary: {
    comparable_case_count: 5,
    baseline: cohortMetrics(1),
    candidate: cohortMetrics(0.4),
    comparable_cohort: {
      baseline: cohortMetrics(1),
      candidate: cohortMetrics(0.4),
    },
  },
  items: [
    {
      dataset_item_id: "case-0",
      classification: "REGRESSION",
      reason: "SCORE_CHANGED",
      baseline_scores: { intent_match: 1 },
      candidate_scores: { intent_match: 0 },
      score_deltas: { intent_match: -1 },
    },
  ],
  next_cursor: null,
};

test.describe("Issue #45: quality pass rate wording", () => {
  test.beforeEach(async ({ page }) => {
    await mockQualityApi(page);
  });

  test("separates 6/6 execution success from 2/6 quality pass and explains the denominator", async ({ page }) => {
    await page.goto(`/launches/${launchId}`);

    // Issue #83: the header now reports the three conclusions separately, so a
    // high decided rate can never hide the cases that had no verdict.
    const metric = page.getByTestId("quality-pass-rate");
    await expect(metric).toBeVisible();
    await expect(metric).toContainText("质量判定汇总 (Quality Decision Summary)");
    await expect(metric).toContainText("PASS 2");
    await expect(metric).toContainText("FAIL 4");
    await expect(metric).toContainText("UNKNOWN 0");
    await expect(metric).toContainText("已判定通过率：33.3%");

    // The all-cases ratio keeps the wider denominator and names it.
    const allCases = page.getByTestId("quality-all-cases-ratio");
    await expect(allCases).toContainText("2 / 6");
    await expect(allCases).toContainText("(33.3%)");
    await expect(allCases).toContainText("含 UNKNOWN");

    // The ambiguous legacy label is gone from the page.
    await expect(page.getByText("用例通过率 (Pass Rate)")).toHaveCount(0);

    // Execution status and quality conclusion stay independent.
    await expect(page.getByTestId("status-badge").first()).toHaveText("COMPLETED");
    await expect(page.getByTestId("quality-badge").first()).toHaveText("FAIL");

    // Execution progress still reports every case as successful.
    await expect(page.getByText("实时执行进度看板")).toBeVisible();
    await expect(page.locator('[data-card="pass"]')).toContainText("6");

    // The denominator rules are readable without hovering.
    const help = page.getByTestId("quality-pass-rate-help");
    await expect(help).toBeVisible();
    await expect(help).toContainText("PASS / FAIL / UNKNOWN");
    await expect(help).toContainText("UNKNOWN 表示证据不足");
    await expect(help).toContainText("分母只含有明确结论的用例");
    await expect(help).toContainText("分母包含全部用例");
    await expect(help).toContainText("不是执行成功率");
  });

  test("keeps the denominator help readable without sideways overflow on a narrow screen", async ({ page }) => {
    // The help text is an always-visible paragraph, not a tooltip. It must wrap
    // rather than force the detail page to scroll horizontally.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/launches/${launchId}`);

    await expect(page.getByTestId("quality-pass-rate")).toBeVisible();
    await expect(page.getByTestId("quality-pass-rate-help")).toBeVisible();

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow, "detail page must not scroll sideways at 390px").toBeLessThanOrEqual(0);
  });

  test("labels the comparable-cohort ratio in the browser and states its denominator", async ({ page }) => {
    // Serve a real comparison so the Baseline area renders. The cohort ratio is
    // 2/5 on a 5-case comparable cohort while the header shows 2/6 over all six
    // cases; the two labels and help texts must keep that distinction visible.
    await page.unroute("**/api/v1/experiment-launches**");
    await page.route("**/api/v1/agents/**/baselines**", (route) =>
      route.fulfill({ status: 404, json: { detail: "no baseline bound" } })
    );
    await page.route("**/api/v1/experiment-launches**", async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.endsWith("/result-snapshots")) {
        await route.fulfill({
          json: {
            revisions: [snapshotRevision],
            latest_snapshot_id: snapshotId,
            latest_revision: 1,
          },
        });
        return;
      }
      if (pathname.includes("/result-snapshots/")) {
        await route.fulfill({ json: snapshotDetail });
        return;
      }
      if (pathname.endsWith("/summary")) {
        await route.fulfill({ json: runSummary });
        return;
      }
      if (pathname.includes("/comparison")) {
        await route.fulfill({ json: comparisonPage });
        return;
      }
      if (pathname === "/api/v1/experiment-launches") {
        await route.fulfill({ json: [launch] });
        return;
      }
      if (pathname.endsWith("/items")) {
        await route.fulfill({ json: items });
        return;
      }
      await route.fulfill({ json: launch });
    });

    await page.goto(`/launches/${launchId}?snapshot_id=${snapshotId}`);

    const cohortTable = page.getByRole("table", { name: "Baseline 与 Candidate 聚合指标对比" });
    await expect(cohortTable).toBeVisible();
    await expect(cohortTable.getByText("质量通过率 (Quality Pass Rate)")).toBeVisible();
    // Candidate cohort ratio is 2/5 = 40%, distinct from the header's 2/6.
    await expect(cohortTable.getByText("40.0%")).toBeVisible();

    const help = page.getByTestId("comparable-quality-pass-rate-help");
    await expect(help).toBeVisible();
    await expect(help).toContainText("仅统计双方共同可比样本");
    await expect(help).toContainText("执行成功、评测成功且质量结论为 PASS 或 FAIL");
    await expect(help).toContainText("不可比或无有效质量结论的用例不参与该比例");

    // The header keeps its own, different denominator.
    await expect(page.getByTestId("quality-all-cases-ratio")).toContainText("(33.3%)");
  });
});
