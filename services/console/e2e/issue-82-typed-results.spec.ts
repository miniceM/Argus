import { test, expect, type Page } from "@playwright/test";

/**
 * Issue #82 acceptance: one case must let the reviewer read a real numeric 0,
 * a boolean false, a categorical value, free text and an Evaluator failure —
 * each with its own status and reason, and none of them collapsed into a zero.
 */

const typedResults = [
  {
    evaluator_id: "intent_match",
    evaluator_version: "1.0.0",
    result_type: "numeric",
    status: "succeeded",
    value: 0,
    normalized_value: 0,
    comment: "expected=refund; actual=other",
    duration_ms: 1.4,
    provenance: { binding_id: "bind_numeric", contract_status: "FROZEN_VERIFIED" },
  },
  {
    evaluator_id: "answer_present",
    evaluator_version: "1.0.0",
    result_type: "boolean",
    status: "succeeded",
    value: false,
    normalized_value: 0,
    comment: "未返回非空回答",
    duration_ms: 0.8,
    provenance: { binding_id: "bind_bool", contract_status: "FROZEN_VERIFIED" },
  },
  {
    evaluator_id: "resolution_bucket",
    evaluator_version: "1.0.0",
    result_type: "categorical",
    status: "succeeded",
    value: "review",
    normalized_value: 1,
    comment: "resolution_bucket=review",
    duration_ms: 0.9,
    provenance: { binding_id: "bind_cat", contract_status: "FROZEN_VERIFIED" },
  },
  {
    evaluator_id: "answer_excerpt",
    evaluator_version: "1.0.0",
    result_type: "text",
    status: "succeeded",
    value: "意图=other；升级=True；工具=无",
    normalized_value: null,
    comment: "意图=other；升级=True；工具=无",
    duration_ms: 0.6,
    provenance: { binding_id: "bind_text", contract_status: "FROZEN_VERIFIED" },
  },
  {
    evaluator_id: "raising_evaluator",
    evaluator_version: "1.0.0",
    result_type: "numeric",
    status: "failed",
    value: null,
    normalized_value: null,
    error_code: "EVALUATION_FAILED",
    error_message: "确定性测试 Provider：模拟评测执行异常",
    duration_ms: 0.4,
    provenance: { binding_id: "bind_fail", contract_status: "FROZEN_VERIFIED" },
  },
];

const baseLaunch = {
  id: "launch-issue-82",
  name: "typed results launch",
  status: "COMPLETED",
  quality_conclusion: "unknown",
  dataset_name: "banking-regression",
  dataset_version: "2026-09-20T00:00:00Z",
  agent_id: "banking-agent",
  agent_version: "v2",
  langfuse_sync_status: "SYNCED",
  langfuse_experiment_url: null,
  created_at: "2026-09-20T00:00:00Z",
  started_at: "2026-09-20T00:00:01Z",
  completed_at: "2026-09-20T00:00:10Z",
};

const baseItem = {
  id: "item-issue-82",
  launch_id: "launch-issue-82",
  dataset_item_id: "case-typed",
  execution_status: "succeeded",
  eval_status: "failed",
  quality_conclusion: "unknown",
  execution_error: null,
  eval_error: null,
  trace_id: "trace-typed",
  observation_id: null,
  langfuse_trace_url: "https://cloud.langfuse.com/trace/trace-typed",
  final_attempt_id: "att-1",
  scores: { intent_match: 0 },
  evaluation_results: typedResults,
  attempt_count: 1,
  final_attempt_http_status: 200,
  final_attempt_latency_ms: 20,
  started_at: "2026-09-20T00:00:02Z",
  completed_at: "2026-09-20T00:00:03Z",
};

const binding = {
  id: "intent_match",
  version: "1.0.0",
  scope: "item",
  threshold: 1,
  binding_id: "bind_numeric",
  binding_digest: "sha256:" + "a".repeat(64),
  binding_schema_version: "1.2",
  definition_digest: "sha256:" + "b".repeat(64),
  content_digest: "b".repeat(64),
  implementation_ref: "builtin:intent_match@1.0.0",
  executor_type: "builtin_python",
  contract_status: "FROZEN_VERIFIED",
  verification_status: "RECORDED",
  implementation_artifact: {
    kind: "python_source",
    locator: "app.evaluators:intent_match",
    digest: "sha256:" + "c".repeat(64),
    runtime: "cpython",
  },
  runner: { runner_version: "0.1.0", build_id: "issue-82-e2e" },
  result_type: "numeric",
};

async function mockDetail(page: Page, items: unknown[] = [baseItem]) {
  await page.route("**/api/v1/system/info", (route) =>
    route.fulfill({
      json: { service: "argus-eval-runner", version: "0.2.0", build_id: "issue-82", environment: "test" },
    }),
  );
  await page.route("**/api/v1/experiment-launches**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/api/v1/experiment-launches") {
      await route.fulfill({ json: [baseLaunch] });
      return;
    }
    if (pathname.endsWith("/items")) {
      await route.fulfill({ json: items });
      return;
    }
    if (pathname.includes("/attempts")) {
      await route.fulfill({ json: [] });
      return;
    }
    if (pathname.endsWith("/summary")) {
      // Minimal safe summary: no snapshot_id keeps the comparison/baseline
      // queries dormant, and `versions.agent` stays undefined so the baseline
      // read is disabled. The run summary is #83's scope, not this case view.
      await route.fulfill({ json: { launch_id: "launch-issue-82", versions: {} } });
      return;
    }
    if (pathname.endsWith("/comparison")) {
      await route.fulfill({ json: {} });
      return;
    }
    await route.fulfill({
      json: {
        ...baseLaunch,
        manifest: {
          schema_version: "1.2",
          dataset: { dataset_name: "banking-regression", items_count: items.length, snapshot_digest: "sha256:ds82" },
          agent: { id: "banking-agent", version: "v2", endpoint: "http://127.0.0.1:18082/invoke", spec_digest: "sha256:spec82" },
          evaluators: [binding],
          execution_policy: { max_concurrency: 2, timeout_seconds: 30, max_retries: 1 },
          runner: { runner_version: "0.1.0", mapping_engine_version: "sha256-mapping-engine-v1" },
        },
      },
    });
  });
}

test.describe("Issue #82: typed evaluation results", () => {
  test("reads 0, false, category, text and a failure in one case without collapsing to zero", async ({ page }) => {
    await mockDetail(page);
    await page.goto("/launches/launch-issue-82");

    // The item table shows each typed result with its own type.
    const numericCell = page.getByTestId("typed-result-intent_match").first();
    await expect(numericCell).toBeVisible();
    await expect(numericCell).toHaveAttribute("data-result-type", "numeric");
    // A real zero is displayed as 0.
    await expect(numericCell).toContainText("0");
    await expect(numericCell).toContainText("数值");

    const boolCell = page.getByTestId("typed-result-answer_present").first();
    await expect(boolCell).toHaveAttribute("data-result-type", "boolean");
    await expect(boolCell).toContainText("false");

    const catCell = page.getByTestId("typed-result-resolution_bucket").first();
    await expect(catCell).toHaveAttribute("data-result-type", "categorical");
    await expect(catCell).toContainText("review");

    const textCell = page.getByTestId("typed-result-answer_excerpt").first();
    await expect(textCell).toHaveAttribute("data-result-type", "text");
    await expect(textCell).toContainText("意图=other");

    // The failure shows a status badge and an em dash, never a 0.
    const failCell = page.getByTestId("typed-result-raising_evaluator").first();
    await expect(failCell).toHaveAttribute("data-result-status", "failed");
    await expect(failCell).toContainText("评测失败");
    await expect(failCell).toContainText("—");

    // Open the case drawer for the full, auditable detail.
    await page.getByRole("button", { name: "明细" }).first().click();
    await expect(page.getByTestId("typed-value-intent_match")).toHaveText("0");
    await expect(page.getByTestId("typed-value-answer_present")).toHaveText("false");
    await expect(page.getByTestId("typed-value-resolution_bucket")).toHaveText("review");
    await expect(page.getByTestId("typed-value-answer_excerpt")).toContainText("意图=other");
    // The failure explains itself instead of being scored as zero.
    await expect(page.getByTestId("typed-reason-raising_evaluator")).toContainText("模拟评测执行异常");
    // The Langfuse trace link is available for cross-checking.
    await expect(page.getByRole("link", { name: "在 Langfuse 中查看 Trace" })).toBeVisible();
  });

  test("a legacy numeric-only item still renders without typed evidence", async ({ page }) => {
    await mockDetail(page, [
      { ...baseItem, id: "item-legacy", evaluation_results: [], scores: { intent_match: 1 } },
    ]);
    await page.goto("/launches/launch-issue-82");

    const legacy = page.getByTestId("typed-result-intent_match").first();
    await expect(legacy).toBeVisible();
    await expect(legacy).toContainText("1");
    await expect(legacy).toHaveAttribute("data-result-type", "numeric");
  });
});
