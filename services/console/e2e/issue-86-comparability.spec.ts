import { test, expect, type Page } from "@playwright/test";

/**
 * Issue #86 acceptance — the promise a reviewer must be able to verify:
 *
 *   1. identical contracts give a formal Regression,
 *   2. tightening only the quality policy says "cannot compare formally" and
 *      names the changed dimension, instead of showing an Agent regression,
 *   3. a legacy Baseline reports an unknown contract rather than guessing it,
 *   4. incomplete required evidence withholds the verdict instead of claiming
 *      "no regression",
 *   5. the case table stays readable and is labelled diagnostic-only.
 */

const LAUNCH_ID = "launch-86";

type Dimension = {
  dimension: string;
  status: string;
  baseline_digest: string | null;
  candidate_digest: string | null;
  baseline_version: string | null;
  candidate_version: string | null;
};

const dimensions = (over: Record<string, string> = {}): Dimension[] => [
  {
    dimension: "MEASUREMENT",
    status: over.MEASUREMENT ?? "MATCH",
    baseline_digest: "sha256:aaaa1111bbbb2222",
    candidate_digest: "sha256:aaaa1111bbbb2222",
    baseline_version: "binding-1.2",
    candidate_version: "binding-1.2",
  },
  {
    dimension: "QUALITY_POLICY",
    status: over.QUALITY_POLICY ?? "MATCH",
    baseline_digest: "sha256:cccc3333dddd4444",
    candidate_digest: "sha256:eeee5555ffff6666",
    baseline_version: "default-all-required@1.0",
    candidate_version: "default-all-required@1.1",
  },
  {
    dimension: "AGGREGATION_COMPARISON",
    status: over.AGGREGATION_COMPARISON ?? "MATCH",
    baseline_digest: "sha256:9999aaaa8888bbbb",
    candidate_digest: "sha256:9999aaaa8888bbbb",
    baseline_version: "comparison-v2",
    candidate_version: "comparison-v2",
  },
];

const summary = () => ({
  launch_id: LAUNCH_ID,
  snapshot_id: "snap-1",
  revision: 1,
  created_at: "2026-09-30T00:00:00Z",
  manifest_digest: "manifest-digest",
  source_result_digest: "source-digest",
  evidence_state: "COMPLETE",
  evidence_reasons: [],
  versions: { agent: { id: "banking-agent", version: "2.4.0" }, dataset: {}, evaluators: [], runner: {} },
  summary: {
    pass_rate: 0.8,
    evaluation_coverage: 1,
    total_cases: 4,
    evaluated_cases: 4,
    critical_failure_count: 0,
    execution_error_count: 0,
    execution_error_rate: 0,
    evaluator_error_count: 0,
    p95_latency_ms: 30,
    cost_per_case: null,
  },
  langfuse_score_sync_status: "SYNCED",
});

const launch = {
  id: LAUNCH_ID,
  name: "issue-86 launch",
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
  manifest: { schema_version: "1.2", dataset: { items_count: 4 } },
  allowed_actions: [],
  progress: { total: 4, completed: 4, allowed_actions: [] },
};

const metrics = (over: Record<string, unknown> = {}) => ({
  pass_rate: 1,
  evaluation_coverage: 1,
  total_cases: 4,
  evaluated_cases: 4,
  critical_failure_count: 0,
  execution_error_count: 0,
  execution_error_rate: 0,
  evaluator_error_count: 0,
  p95_latency_ms: 30,
  cost_per_case: null,
  total_cost: null,
  cost_currency: null,
  cost_case_count: 0,
  cost_coverage: 0,
  cost_unavailable_reason: "COST_NOT_RECORDED",
  score_means: { correctness: 0.9 },
  ...over,
});

const comparison = (over: Record<string, any> = {}) => ({
  launch_id: LAUNCH_ID,
  candidate_snapshot_id: "snap-1",
  baseline_snapshot_id: "baseline-snap",
  baseline_binding_revision: 2,
  versions: { candidate: summary().versions, baseline: summary().versions },
  summary: {
    comparable_case_count: 4,
    baseline: metrics(),
    candidate: metrics({ pass_rate: 0.75 }),
    comparable_cohort: {
      baseline: metrics(),
      candidate: metrics({ pass_rate: 0.75, score_means: { correctness: 0.8 } }),
    },
    cost_comparison: {
      status: "NOT_COMPARABLE",
      reason: "COST_NOT_RECORDED",
      cohort: "quality_comparable_cases",
      case_count: 4,
      currency: null,
      baseline_cost_per_case: null,
      candidate_cost_per_case: null,
      delta: null,
      baseline_coverage: 0,
      candidate_coverage: 0,
    },
  },
  classification_counts: { REGRESSION: 1, IMPROVEMENT: 0, UNCHANGED: 3, NOT_COMPARABLE: 0 },
  comparability: {
    comparable: true,
    reason_codes: [],
    provenance: "FROZEN",
    dimensions: dimensions(),
    suggestions: [],
  },
  formal: {
    available: true,
    verdict: "REGRESSION",
    reason: "CASE_REGRESSION",
    required_cases: 4,
    comparable_cases: 4,
    coverage: 1,
    withheld_reasons: [],
  },
  diagnostic: {
    note: "仅供诊断，不作为正式发布比较。",
    comparable_cases: 4,
    classification_counts: { REGRESSION: 1, IMPROVEMENT: 0, UNCHANGED: 3, NOT_COMPARABLE: 0 },
  },
  items: [
    {
      dataset_item_id: "case-1",
      classification: "REGRESSION",
      reason: "QUALITY_CONCLUSION_CHANGED",
      basis: "FORMAL",
      baseline_scores: { correctness: 0.9 },
      candidate_scores: { correctness: 0.8 },
      score_deltas: { correctness: -0.1 },
    },
  ],
  next_cursor: null,
  ...over,
});

async function mockApi(page: Page, body: Record<string, any>) {
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/summary**`, async (route) => {
    await route.fulfill({ json: summary() });
  });
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/comparison**`, async (route) => {
    await route.fulfill({ json: body });
  });
  await page.route("**/api/v1/agents/*/baselines**", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      json: {
        agent_id: "banking-agent",
        environment: "production",
        result_snapshot_id: "baseline-snap",
        revision: 2,
        result_revision: 1,
        result_evidence_state: "COMPLETE",
        updated_by: "alice",
        updated_at: "2026-09-29T00:00:00Z",
        launch_id: "baseline-launch",
        agent_version: "2.3.0",
        dataset_name: "golden",
        summary: {},
      },
    });
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
            source_result_digest: "source-digest",
            manifest_digest: "manifest-digest",
            evidence_state: "COMPLETE",
            evidence_reasons: [],
            total_cases: 4,
            quality_pass_count: 3,
            quality_fail_count: 1,
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

test.describe("Issue #86 comparison comparability", () => {
  test("identical contracts produce a formal Regression", async ({ page }) => {
    await mockApi(page, comparison());
    await page.goto(`/launches/${LAUNCH_ID}`);

    const verdict = page.getByTestId("comparison-formal-verdict");
    await expect(verdict).toContainText("正式比较");
    await expect(verdict).toContainText("Regression");
    await expect(page.getByTestId("comparison-comparability-banner")).toHaveCount(0);
  });

  test("a tightened policy is explained instead of reported as an Agent regression", async ({ page }) => {
    await mockApi(
      page,
      comparison({
        comparability: {
          comparable: false,
          reason_codes: ["QUALITY_POLICY_CHANGED"],
          provenance: "FROZEN",
          dimensions: dimensions({ QUALITY_POLICY: "CHANGED" }),
          suggestions: ["使用相同质量策略（阈值、operator、critical、UNKNOWN 处置）重新评测后再比较。"],
        },
        formal: {
          available: false,
          verdict: null,
          reason: null,
          required_cases: 4,
          comparable_cases: 4,
          coverage: 1,
          withheld_reasons: ["QUALITY_POLICY_CHANGED"],
        },
        items: [
          {
            dataset_item_id: "case-1",
            classification: "REGRESSION",
            reason: "QUALITY_CONCLUSION_CHANGED",
            basis: "DIAGNOSTIC_ONLY",
            baseline_scores: { correctness: 0.9 },
            candidate_scores: { correctness: 0.8 },
            score_deltas: { correctness: -0.1 },
          },
        ],
      }),
    );
    await page.goto(`/launches/${LAUNCH_ID}`);

    const banner = page.getByTestId("comparison-comparability-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("判定规则不同，无法正式比较");
    await expect(page.getByTestId("comparability-reason-QUALITY_POLICY_CHANGED")).toBeVisible();
    // The changed dimension names both sides, and the unchanged one stays "一致".
    await expect(page.getByTestId("comparability-dimension-QUALITY_POLICY")).toContainText("已变化");
    await expect(page.getByTestId("comparability-dimension-QUALITY_POLICY")).toContainText("default-all-required@1.1");
    await expect(page.getByTestId("comparability-dimension-MEASUREMENT")).toContainText("一致");
    await expect(page.getByTestId("comparability-suggestion-0")).toContainText("相同质量策略");
    // No formal verdict, and the table is explicitly diagnostic.
    await expect(page.getByTestId("comparison-formal-verdict")).toContainText("无法给出正式结论");
    await expect(page.getByTestId("comparison-diagnostic-label")).toContainText("仅供诊断");
    await expect(page.getByTestId("comparison-item-basis-DIAGNOSTIC_ONLY")).toContainText("仅诊断");
  });

  test("a legacy Baseline reports an unknown contract rather than guessing it", async ({ page }) => {
    const legacyDimensions: Dimension[] = dimensions({ AGGREGATION_COMPARISON: "UNKNOWN" });
    legacyDimensions[2] = {
      ...legacyDimensions[2],
      baseline_digest: null,
      baseline_version: "comparison-v1",
    };
    await mockApi(
      page,
      comparison({
        comparability: {
          comparable: false,
          reason_codes: ["CONTRACT_PROVENANCE_UNKNOWN"],
          provenance: "LEGACY_PARTIAL",
          dimensions: legacyDimensions,
          suggestions: [],
        },
        formal: {
          available: false,
          verdict: null,
          reason: null,
          required_cases: 4,
          comparable_cases: 4,
          coverage: 1,
          withheld_reasons: ["CONTRACT_PROVENANCE_UNKNOWN"],
        },
        items: [],
      }),
    );
    await page.goto(`/launches/${LAUNCH_ID}`);

    await expect(page.getByTestId("comparability-reason-CONTRACT_PROVENANCE_UNKNOWN")).toBeVisible();
    await expect(page.getByTestId("comparability-dimension-AGGREGATION_COMPARISON")).toContainText("证据缺失");
    await expect(page.getByTestId("comparability-dimension-MEASUREMENT")).toContainText("一致");
    await expect(page.getByTestId("comparison-formal-verdict")).toContainText("无法给出正式结论");
  });

  test("incomplete required evidence is shown as insufficient, not as no regression", async ({ page }) => {
    await mockApi(
      page,
      comparison({
        comparability: {
          comparable: true,
          reason_codes: [],
          provenance: "FROZEN",
          dimensions: dimensions(),
          suggestions: [],
        },
        formal: {
          available: false,
          verdict: null,
          reason: null,
          required_cases: 4,
          comparable_cases: 3,
          coverage: 0.75,
          withheld_reasons: ["COVERAGE_INCOMPLETE"],
        },
        classification_counts: { REGRESSION: 0, IMPROVEMENT: 0, UNCHANGED: 3, NOT_COMPARABLE: 1 },
        items: [],
      }),
    );
    await page.goto(`/launches/${LAUNCH_ID}`);

    const verdict = page.getByTestId("comparison-formal-verdict");
    await expect(verdict).toContainText("无法给出正式结论");
    await expect(page.getByTestId("formal-withheld-COVERAGE_INCOMPLETE")).toContainText("证据不足");
    await expect(page.getByTestId("comparison-diagnostic-label")).toContainText("仅供诊断");
  });
});
