import { test, expect, type Page } from "@playwright/test";
import {
  itemEvaluatorFixture,
  type EvaluatorFixture,
} from "./fixtures/evaluators";

/**
 * Issue #83 acceptance — the loop a reviewer must be able to complete:
 *
 *   1. confirm a quality policy per metric on the create page,
 *   2. see that exact policy submitted and frozen,
 *   3. read PASS / FAIL / UNKNOWN separately, and
 *   4. open one case and see which rule decided it, and why.
 *
 * The point of #83 is that "不通过" and "证据不足" are never the same thing:
 * a decided pass rate of 100% must still show the UNKNOWN count.
 */

const AGENT_ID = "banking-agent";

const intentMatch = itemEvaluatorFixture("intent_match", "意图匹配评测器", {
  default_selected: true,
  versions: [
    {
      version: "1.0.0",
      result_type: "numeric",
      scope: "item",
      threshold: 0.8,
      direction: "higher_is_better",
      critical: false,
      input_contract: { type: "object" },
      output_contract: { type: "number" },
      param_schema: { type: "object", properties: {} },
      implementation_ref: "builtin:intent_match@1.0.0",
      executor_type: "builtin_python",
      content_digest: "sha256:intent-v1",
      release_eligible: true,
      eligibility_reasons: [],
      eligibility_messages: [],
    },
  ],
});

// A lower-is-better metric proves the policy is not hard-coded to ">=".
const callCost = itemEvaluatorFixture("call_cost", "调用成本评测器", {
  default_selected: true,
  versions: [
    {
      version: "1.0.0",
      result_type: "numeric",
      scope: "item",
      threshold: 0.2,
      direction: "lower_is_better",
      critical: false,
      input_contract: { type: "object" },
      output_contract: { type: "number" },
      param_schema: { type: "object", properties: {} },
      implementation_ref: "builtin:call_cost@1.0.0",
      executor_type: "builtin_python",
      content_digest: "sha256:call-cost-v1",
      release_eligible: true,
      eligibility_reasons: [],
      eligibility_messages: [],
    },
  ],
});

const piiSafe = itemEvaluatorFixture("pii_safe", "隐私合规评测器", {
  default_selected: true,
  result_type: "boolean",
  versions: [
    {
      version: "1.0.0",
      result_type: "boolean",
      scope: "item",
      threshold: 1,
      direction: "higher_is_better",
      critical: false,
      input_contract: { type: "object" },
      output_contract: { type: "boolean" },
      param_schema: { type: "object", properties: {} },
      implementation_ref: "builtin:pii_safe@1.0.0",
      executor_type: "builtin_python",
      content_digest: "sha256:pii-safe-v1",
      release_eligible: true,
      eligibility_reasons: [],
      eligibility_messages: [],
    },
  ],
});

// Exactly the three metrics this spec configures. The legacy composite metric
// is deliberately absent: #83 removes it from the create-Launch flow.
const catalog = (): EvaluatorFixture[] => [intentMatch, callCost, piiSafe];

async function mockCreatePage(page: Page, created: Record<string, unknown>[]) {
  await page.route("**/api/v1/agents", async (route) => {
    await route.fulfill({
      json: [{ id: AGENT_ID, name: "Banking Agent", version_count: 1, latest_version: "v1" }],
    });
  });
  await page.route("**/api/v1/agent-versions**", async (route) => {
    await route.fulfill({
      json: [{ id: "ver-1", agent_id: AGENT_ID, version: "v1", is_active: true }],
    });
  });
  await page.route("**/api/v1/evaluators", async (route) => {
    await route.fulfill({ json: catalog() });
  });
  await page.route("**/api/v1/experiment-launches", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }
    created.push(route.request().postDataJSON());
    await route.fulfill({
      status: 201,
      json: {
        id: "launch-83",
        name: "issue-83",
        status: "PENDING",
        quality_conclusion: "unknown",
        dataset_name: "banking-agent-regression",
        agent_id: AGENT_ID,
        agent_version: "v1",
        agent_version_id: "ver-1",
        manifest: {},
        langfuse_sync_status: "PENDING",
        created_at: new Date().toISOString(),
      },
    });
  });
}

const frozenPolicy = {
  policy_id: "custom",
  version: "1.0",
  schema_version: "1.0",
  description: "Issue #83 验收策略",
  policy_digest: "sha256:" + "d".repeat(64),
  unknown_handling: "unknown_not_releasable",
  rules: [
    { evaluator_id: "intent_match", operator: ">=", threshold: 0.8, result_type: "numeric", required: true, critical: false },
    { evaluator_id: "call_cost", operator: "<=", threshold: 0.2, result_type: "numeric", required: true, critical: false },
    { evaluator_id: "pii_safe", operator: "==", expected_value: true, result_type: "boolean", required: true, critical: true },
  ],
};

const baseLaunch = {
  id: "launch-83",
  name: "issue-83 launch",
  status: "COMPLETED",
  quality_conclusion: "unknown",
  dataset_name: "banking-regression",
  dataset_version: "2026-09-20T00:00:00Z",
  agent_id: AGENT_ID,
  agent_version: "v2",
  langfuse_sync_status: "SYNCED",
  created_at: "2026-09-20T00:00:00Z",
  started_at: "2026-09-20T00:00:01Z",
  completed_at: "2026-09-20T00:00:12Z",
};

const baseItem = {
  id: "item-83-pass",
  launch_id: "launch-83",
  dataset_item_id: "case-pass",
  execution_status: "succeeded",
  eval_status: "succeeded",
  quality_conclusion: "pass",
  execution_error: null,
  eval_error: null,
  trace_id: "trace-83",
  observation_id: null,
  langfuse_trace_url: null,
  final_attempt_id: "att-1",
  scores: { intent_match: 1 },
  evaluation_results: [],
  quality_evaluation: {
    conclusion: "pass",
    policy_id: "custom",
    policy_version: "1.0",
    policy_digest: "sha256:" + "d".repeat(64),
    decided_by: "QUALITY_POLICY",
    releasable: true,
    unknown_reasons: [],
    rules: [
      {
        evaluator_id: "intent_match",
        result_type: "numeric",
        required: true,
        critical: false,
        operator: ">=",
        expected: 0.8,
        observed_value: 1,
        observed_status: "succeeded",
        conclusion: "pass",
        reason_code: null,
        explanation: "实测 1 满足 >= 0.8。",
      },
    ],
  },
  attempt_count: 1,
  final_attempt_http_status: 200,
  final_attempt_latency_ms: 20,
  started_at: "2026-09-20T00:00:02Z",
  completed_at: "2026-09-20T00:00:03Z",
};

const unknownItem = {
  ...baseItem,
  id: "item-83-unknown",
  dataset_item_id: "case-unknown",
  eval_status: "failed",
  quality_conclusion: "unknown",
  scores: {},
  quality_evaluation: {
    conclusion: "unknown",
    policy_id: "custom",
    policy_version: "1.0",
    policy_digest: "sha256:" + "d".repeat(64),
    decided_by: "QUALITY_POLICY",
    releasable: false,
    unknown_reasons: ["必要指标 'pii_safe' 证据不足（评测执行失败），无法判定质量结论。"],
    rules: [
      {
        evaluator_id: "intent_match",
        result_type: "numeric",
        required: true,
        critical: false,
        operator: ">=",
        expected: 0.8,
        observed_value: 1,
        observed_status: "succeeded",
        conclusion: "pass",
        reason_code: null,
        explanation: "实测 1 满足 >= 0.8。",
      },
      {
        evaluator_id: "pii_safe",
        result_type: "boolean",
        required: true,
        critical: true,
        operator: "==",
        expected: true,
        observed_value: null,
        observed_status: "failed",
        conclusion: "unknown",
        reason_code: "EVALUATOR_TIMEOUT",
        explanation: "必要指标 'pii_safe' 证据不足（评测执行失败），无法判定质量结论。",
      },
    ],
  },
};

const failItem = {
  ...baseItem,
  id: "item-83-fail",
  dataset_item_id: "case-fail",
  quality_conclusion: "fail",
  scores: { intent_match: 0.3 },
  quality_evaluation: {
    conclusion: "fail",
    policy_id: "custom",
    policy_version: "1.0",
    policy_digest: "sha256:" + "d".repeat(64),
    decided_by: "QUALITY_POLICY",
    releasable: true,
    unknown_reasons: [],
    rules: [
      {
        evaluator_id: "intent_match",
        result_type: "numeric",
        required: true,
        critical: false,
        operator: ">=",
        expected: 0.8,
        observed_value: 0.3,
        observed_status: "succeeded",
        conclusion: "fail",
        reason_code: "QUALITY_RULE_VIOLATED",
        explanation: "必要指标 'intent_match' 违反规则：实测 0.3 不满足 >= 0.8。",
      },
    ],
  },
};

async function mockDetail(page: Page, items: unknown[]) {
  await page.route("**/api/v1/system/info", (route) =>
    route.fulfill({
      json: { service: "argus-eval-runner", version: "0.2.0", build_id: "issue-83", environment: "test" },
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
      await route.fulfill({ json: { launch_id: "launch-83", versions: {} } });
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
          dataset: { dataset_name: "banking-regression", items_count: items.length, snapshot_digest: "sha256:ds83" },
          agent: { id: AGENT_ID, version: "v2", endpoint: "http://127.0.0.1:18082/invoke", spec_digest: "sha256:spec83" },
          evaluators: [],
          quality_policy: frozenPolicy,
          measurement_digest: "sha256:" + "e".repeat(64),
          execution_policy: { max_concurrency: 2, timeout_seconds: 30, max_retries: 1 },
          runner: { runner_version: "0.1.0", mapping_engine_version: "sha256-mapping-engine-v1" },
        },
      },
    });
  });
}

test.describe("Issue #83: independent quality policy", () => {
  test("confirms a per-metric policy on the create page and submits it verbatim", async ({ page }) => {
    const created: Record<string, unknown>[] = [];
    await mockCreatePage(page, created);
    await page.goto("/launches/new");

    // One rule per selected metric, each seeded from its own frozen identity.
    await expect(page.getByTestId("quality-policy-editor")).toBeVisible();
    await expect(page.getByTestId("quality-rule-intent_match")).toBeVisible();
    await expect(page.getByLabel("intent_match 判定运算符")).toHaveValue(">=");
    await expect(page.getByLabel("intent_match 阈值")).toHaveValue("0.8");
    // lower_is_better is honoured instead of being forced to ">=".
    await expect(page.getByLabel("call_cost 判定运算符")).toHaveValue("<=");
    await expect(page.getByLabel("call_cost 阈值")).toHaveValue("0.2");
    // A boolean metric needs an explicit typed match before it can decide.
    await expect(page.getByLabel("pii_safe 判定运算符")).toHaveValue("==");

    // An incomplete policy blocks the request and says why.
    const submit = page.getByRole("button", { name: /创建评测任务/ });
    await expect(submit).toBeDisabled();
    await expect(page.getByTestId("quality-rule-pii_safe")).toContainText("布尔规则必须显式选择期望取值");

    // Confirm the rule, mark it critical, then tighten one threshold.
    await page.getByLabel("pii_safe 期望取值").selectOption("true");
    await page.getByLabel("pii_safe 关键规则").check();
    await page.getByLabel("intent_match 阈值").fill("0.9");
    await expect(submit).toBeEnabled();

    await submit.click();
    await expect.poll(() => created.length).toBe(1);

    const body = created[0] as {
      evaluator_selections: Array<{ id: string; version: string }>;
      quality_policy: { rules: Array<Record<string, unknown>> };
    };
    // The exact metric versions are pinned.
    expect(body.evaluator_selections.map((entry) => entry.id).sort()).toEqual([
      "call_cost",
      "intent_match",
      "pii_safe",
    ]);
    // And the confirmed rules travel with them, with no composite metric.
    const byId = Object.fromEntries(body.quality_policy.rules.map((rule) => [rule.evaluator_id, rule]));
    expect(byId.intent_match).toMatchObject({ operator: ">=", threshold: 0.9, required: true, result_type: "numeric" });
    expect(byId.call_cost).toMatchObject({ operator: "<=", threshold: 0.2, required: true });
    expect(byId.pii_safe).toMatchObject({ operator: "==", expected_value: true, critical: true, result_type: "boolean" });
    expect(body.quality_policy.rules.map((rule) => rule.evaluator_id)).not.toContain("overall_pass");
  });

  test("shows the frozen policy and separates PASS / FAIL / UNKNOWN on the detail page", async ({ page }) => {
    await mockDetail(page, [baseItem, failItem, unknownItem]);
    await page.goto("/launches/launch-83");

    // The frozen policy is visible with its identity, not just a JSON blob.
    const frozen = page.getByTestId("frozen-quality-policy");
    await expect(frozen).toBeVisible();
    await expect(frozen).toContainText("custom@1.0");
    await expect(page.getByTestId("frozen-policy-digest")).toContainText("sha256:");
    await expect(frozen).toContainText(">= 0.8");
    await expect(frozen).toContainText("<= 0.2");
    await expect(frozen).toContainText("== true");

    // The three conclusions are counted separately.
    await expect(page.getByTestId("quality-count-pass")).toContainText("PASS 1");
    await expect(page.getByTestId("quality-count-fail")).toContainText("FAIL 1");
    await expect(page.getByTestId("quality-count-unknown")).toContainText("UNKNOWN 1");
    await expect(page.getByTestId("decided-pass-rate")).toContainText("50.0%");
    await expect(page.getByTestId("decision-coverage")).toContainText("66.7%");
    // The all-cases ratio keeps its own, wider denominator.
    await expect(page.getByTestId("quality-all-cases-ratio")).toContainText("1 / 3");
    await expect(page.getByTestId("quality-all-cases-ratio")).toContainText("含 UNKNOWN");
  });

  test("explains an UNKNOWN case as insufficient evidence rather than a rule violation", async ({ page }) => {
    await mockDetail(page, [unknownItem]);
    await page.goto("/launches/launch-83");

    // The row itself says 证据不足, so it cannot be misread as 不通过.
    await expect(page.getByTestId("quality-summary-case-unknown")).toContainText("证据不足");
    await expect(page.getByTestId("quality-summary-case-unknown")).toContainText("pii_safe");

    await page.getByRole("button", { name: "明细" }).first().click();
    const panel = page.getByTestId("quality-decision-panel");
    await expect(panel).toBeVisible();

    // The rule that actually failed is named, with its expected/observed values.
    const unknownRule = page.getByTestId("quality-rule-result-pii_safe");
    await expect(unknownRule).toHaveAttribute("data-conclusion", "unknown");
    await expect(unknownRule).toContainText("条件 == true");
    await expect(unknownRule).toContainText("原因：评测错误 EVALUATOR_TIMEOUT");
    await expect(unknownRule).toContainText("证据不足");

    // A rule that held is still shown, so UNKNOWN is never rendered as FAIL.
    await expect(page.getByTestId("quality-rule-result-intent_match")).toHaveAttribute("data-conclusion", "pass");
    await expect(panel).toContainText("证据不足，不可用于发布门禁");
  });

  test("names the violated rule for a FAIL case", async ({ page }) => {
    await mockDetail(page, [failItem]);
    await page.goto("/launches/launch-83");

    await expect(page.getByTestId("quality-summary-case-fail")).toContainText("违反规则");
    await page.getByRole("button", { name: "明细" }).first().click();

    const violated = page.getByTestId("quality-rule-result-intent_match");
    await expect(violated).toHaveAttribute("data-conclusion", "fail");
    await expect(violated).toContainText("原因：违反判定规则");
    await expect(violated).toContainText("实测 0.3");
    await expect(page.getByTestId("quality-decision-panel")).not.toContainText("证据不足，不可用于发布门禁");
  });
});
