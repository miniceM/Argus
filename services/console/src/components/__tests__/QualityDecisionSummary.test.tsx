/**
 * Issue #83 — a quality conclusion must be explainable.
 *
 * These tests pin the user-visible contract: PASS / FAIL / UNKNOWN are counted
 * separately, a high decided rate can never hide UNKNOWN cases, and every
 * frozen rule is shown with its own reason.
 */
import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { LaunchDetail } from "../../features/launches/LaunchDetail";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: { GET: vi.fn(), POST: vi.fn() },
}));

const policy = {
  policy_id: "default-all-required",
  version: "1.0",
  schema_version: "1.0",
  description: "默认策略：全部诊断指标均为必要规则。",
  policy_digest: "sha256:abcdef0123456789",
  rules: [
    { evaluator_id: "intent_match", operator: ">=", threshold: 0.8, result_type: "numeric", required: true, critical: false },
    { evaluator_id: "latency", operator: "<=", threshold: 0.2, result_type: "numeric", required: true, critical: false },
    { evaluator_id: "tone", operator: "==", expected_value: "polite", result_type: "categorical", required: true, critical: false },
    { evaluator_id: "transcript", operator: null, result_type: "text", required: false, critical: false },
  ],
};

const launch = (overrides: Record<string, unknown> = {}) => ({
  id: "launch-83",
  name: "Quality Policy Launch",
  status: "COMPLETED",
  quality_conclusion: "unknown",
  dataset_name: "banking-regression",
  agent_id: "banking-agent",
  agent_version: "v2",
  langfuse_sync_status: "SYNCED",
  created_at: "2026-09-20T00:00:00Z",
  started_at: "2026-09-20T00:00:01Z",
  completed_at: "2026-09-20T00:00:05Z",
  manifest: {
    schema_version: "1.2",
    dataset: { items_count: 4 },
    quality_policy: policy,
    measurement_digest: "sha256:0123456789abcdef",
  },
  progress: {
    total: 4,
    pending: 0,
    queued: 0,
    running: 0,
    retry_wait: 0,
    succeeded: 4,
    failed: 0,
    timed_out: 0,
    cancelled: 0,
    completed: 4,
    percentage: 100,
    attempts: 4,
    retries: 0,
    allowed_actions: [],
  },
  ...overrides,
});

const item = (index: number, spec: Record<string, unknown>) => ({
  id: `item-${index}`,
  launch_id: "launch-83",
  dataset_item_id: `case-${index}`,
  execution_status: "SUCCEEDED",
  eval_status: "SUCCEEDED",
  quality_conclusion: "PASS",
  scores: {},
  attempt_count: 1,
  started_at: "2026-09-20T00:00:01Z",
  ...spec,
});

const renderDetail = (
  launchPayload: Record<string, unknown>,
  items: Array<Record<string, unknown>>,
  initialPath?: string,
) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  (api.GET as any).mockImplementation((path: string) => {
    if (path.includes("/items")) return Promise.resolve({ data: items });
    if (path.includes("/summary") || path.includes("/comparison") || path.includes("/baselines")) {
      return Promise.resolve({ data: null });
    }
    if (path.includes("/api/v1/experiment-launches")) return Promise.resolve({ data: launchPayload });
    return Promise.resolve({ data: null });
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialPath || `/launches/${launchPayload.id}`]}>
        <Routes>
          <Route path="/launches/:launchId" element={<LaunchDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
};

const unknownEvaluation = {
  conclusion: "unknown",
  policy_id: "default-all-required",
  policy_version: "1.0",
  policy_digest: "sha256:abcdef0123456789",
  decided_by: "QUALITY_POLICY",
  releasable: false,
  unknown_reasons: ["必要指标 'intent_match' 证据不足（评测执行失败），无法判定质量结论。"],
  rules: [
    {
      evaluator_id: "intent_match",
      result_type: "numeric",
      required: true,
      critical: false,
      operator: ">=",
      expected: 0.8,
      observed_value: null,
      observed_status: "failed",
      conclusion: "unknown",
      reason_code: "EVALUATOR_TIMEOUT",
      explanation: "必要指标 'intent_match' 证据不足（评测执行失败），无法判定质量结论。",
    },
    {
      evaluator_id: "latency",
      result_type: "numeric",
      required: true,
      critical: false,
      operator: "<=",
      expected: 0.2,
      observed_value: 0.1,
      observed_status: "succeeded",
      conclusion: "pass",
      reason_code: null,
      explanation: "实测 0.1 满足 <= 0.2。",
    },
  ],
};

const failEvaluation = {
  conclusion: "fail",
  policy_id: "default-all-required",
  policy_version: "1.0",
  policy_digest: "sha256:abcdef0123456789",
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
      observed_value: 0.42,
      observed_status: "succeeded",
      conclusion: "fail",
      reason_code: "QUALITY_RULE_VIOLATED",
      explanation: "必要指标 'intent_match' 违反规则：实测 0.42 不满足 >= 0.8。",
    },
  ],
};

describe("Issue #83 quality decision summary", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => cleanup());

  // The core of #83: a decided rate of 100% must still show the UNKNOWN count,
  // so "everything we could judge passed" can never read as "everything passed".
  it("shows PASS / FAIL / UNKNOWN side by side with the decided rate", async () => {
    renderDetail(launch(), [
      item(0, { quality_conclusion: "pass" }),
      item(1, { quality_conclusion: "pass" }),
      item(2, { quality_conclusion: "fail" }),
      item(3, { quality_conclusion: "unknown", quality_evaluation: unknownEvaluation }),
    ]);

    const summary = await screen.findByTestId("quality-pass-rate");
    expect(summary).toHaveTextContent("PASS 2");
    expect(summary).toHaveTextContent("FAIL 1");
    expect(summary).toHaveTextContent("UNKNOWN 1");
    expect(screen.getByTestId("decided-pass-rate")).toHaveTextContent("66.7%");
    expect(screen.getByTestId("decision-coverage")).toHaveTextContent("75.0%");
  });

  it("never shows a decided rate when nothing could be decided", async () => {
    renderDetail(launch({ quality_conclusion: "unknown" }), [
      item(0, { quality_conclusion: "unknown", quality_evaluation: unknownEvaluation }),
      item(1, { quality_conclusion: "unknown", quality_evaluation: unknownEvaluation }),
    ]);

    await screen.findByTestId("quality-pass-rate");
    expect(screen.getByTestId("quality-count-unknown")).toHaveTextContent("UNKNOWN 2");
    expect(screen.getByTestId("decided-pass-rate")).toHaveTextContent("—");
    expect(screen.getByTestId("decision-coverage")).toHaveTextContent("0.0%");
  });

  it("surfaces the frozen policy with its digest and each rule", async () => {
    renderDetail(launch(), [item(0, { quality_conclusion: "pass" })], `/launches/launch-83?tab=audit`);

    const panel = await screen.findByTestId("frozen-quality-policy");
    expect(panel).toHaveTextContent("default-all-required@1.0");
    expect(screen.getByTestId("frozen-policy-digest")).toHaveTextContent("sha256:abcdef0123456789");
    expect(screen.getByTestId("frozen-measurement-digest")).toHaveTextContent("sha256:0123456789abcdef");
    expect(panel).toHaveTextContent("intent_match");
    expect(panel).toHaveTextContent(">= 0.8");
    expect(panel).toHaveTextContent("<= 0.2");
    expect(panel).toHaveTextContent("== polite");
    // The text metric is listed as evidence, never as a deciding rule.
    expect(panel).toHaveTextContent("仅作为证据");
  });

  it("reports a pre-#83 launch as a historical contract instead of an empty policy", async () => {
    // A pre-#83 Manifest carries the old placeholder shape, not a rule list.
    const legacy = launch({
      manifest: {
        schema_version: "1.0",
        dataset: { items_count: 1 },
        quality_policy: { mode: "all_pass" },
      },
    });
    renderDetail(legacy, [item(0, { quality_conclusion: "pass" })], `/launches/launch-83?tab=audit`);

    expect(await screen.findByTestId("frozen-quality-policy-legacy")).toBeInTheDocument();
  });
});

describe("Issue #83 per-rule explanations in the item drawer", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => cleanup());

  it("explains an UNKNOWN case as insufficient evidence, not as a rule violation", async () => {
    renderDetail(
      launch(),
      [item(0, { quality_conclusion: "unknown", quality_evaluation: unknownEvaluation })],
      `/launches/launch-83?tab=cases`,
    );

    fireEvent.click(await screen.findByRole("button", { name: "明细" }));

    const panel = await screen.findByTestId("quality-decision-panel");
    expect(panel).toHaveTextContent("UNKNOWN");
    const failing = screen.getByTestId("quality-rule-result-intent_match");
    expect(failing).toHaveAttribute("data-conclusion", "unknown");
    expect(failing).toHaveTextContent("条件 >= 0.8");
    expect(failing).toHaveTextContent("原因：评测错误 EVALUATOR_TIMEOUT");
    expect(failing).toHaveTextContent("证据不足");
    // A rule that did hold is still shown, so UNKNOWN is not read as FAIL.
    expect(screen.getByTestId("quality-rule-result-latency")).toHaveAttribute("data-conclusion", "pass");
    expect(panel).toHaveTextContent("证据不足，不可用于发布门禁");
  });

  it("names the violated rule for a FAIL case", async () => {
    renderDetail(
      launch(),
      [item(0, { quality_conclusion: "fail", quality_evaluation: failEvaluation })],
      `/launches/launch-83?tab=cases`,
    );

    fireEvent.click(await screen.findByRole("button", { name: "明细" }));

    const panel = await screen.findByTestId("quality-decision-panel");
    const violated = screen.getByTestId("quality-rule-result-intent_match");
    expect(violated).toHaveAttribute("data-conclusion", "fail");
    expect(violated).toHaveTextContent("原因：违反判定规则");
    expect(violated).toHaveTextContent("实测 0.42");
    expect(panel).not.toHaveTextContent("证据不足，不可用于发布门禁");
  });

  it("summarises the cause in the item row so 证据不足 is visible without the drawer", async () => {
    renderDetail(
      launch(),
      [item(0, { quality_conclusion: "unknown", quality_evaluation: unknownEvaluation })],
      `/launches/launch-83?tab=cases`,
    );

    const summary = await screen.findByTestId("quality-summary-case-0");
    expect(summary).toHaveTextContent("证据不足");
    expect(summary).toHaveTextContent("intent_match");
  });

  it("offers 证据不足 as its own filter bucket", async () => {
    renderDetail(
      launch(),
      [
        item(0, { quality_conclusion: "pass" }),
        item(1, { quality_conclusion: "unknown", quality_evaluation: unknownEvaluation }),
      ],
      `/launches/launch-83?tab=cases`,
    );

    await screen.findByTestId("quality-pass-rate");
    const filter = screen.getByRole("button", { name: /证据不足 \(1\)/ });
    fireEvent.click(filter);

    await waitFor(() => {
      expect(screen.queryByTestId("quality-summary-case-0")).not.toBeInTheDocument();
      expect(screen.getByTestId("quality-summary-case-1")).toBeInTheDocument();
    });
  });
});
