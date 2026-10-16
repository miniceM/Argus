import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { LaunchKpiOverview } from "../LaunchKpiOverview";

describe("LaunchKpiOverview keyboard navigation & accessibility (K1)", () => {
  it("enables keyboard navigation on Quality Pass Rate and Regression KPI cards", () => {
    const handleNavigate = vi.fn();
    render(
      <LaunchKpiOverview
        totalItems={10}
        passedItems={8}
        decisionCounts={{ pass: 8, fail: 2, unknown: 0 }}
        decidedPassRate="80.0%"
        decisionCoverage={1.0}
        classificationCounts={{ REGRESSION: 1 }}
        onNavigateTab={handleNavigate}
      />
    );

    const qualityCard = screen.getByTestId("quality-pass-rate");
    expect(qualityCard).toHaveAttribute("role", "button");
    expect(qualityCard).toHaveAttribute("tabindex", "0");

    // Enter key activates tab navigation
    fireEvent.keyDown(qualityCard, { key: "Enter" });
    expect(handleNavigate).toHaveBeenCalledWith("cases");

    // Space key activates tab navigation
    handleNavigate.mockClear();
    fireEvent.keyDown(qualityCard, { key: " " });
    expect(handleNavigate).toHaveBeenCalledWith("cases");

    // Regression card is also keyboard operable
    const regressionCard = screen.getByRole("button", { name: /正式回归用例数/ });
    expect(regressionCard).toHaveAttribute("tabindex", "0");

    handleNavigate.mockClear();
    fireEvent.keyDown(regressionCard, { key: "Enter" });
    expect(handleNavigate).toHaveBeenCalledWith("compare");
  });
});

describe("LaunchKpiOverview comparison evidence", () => {
  const renderKpis = (comparisonSummary?: any) =>
    render(
      <LaunchKpiOverview
        totalItems={2}
        passedItems={2}
        decisionCounts={{ pass: 2, fail: 0, unknown: 0 }}
        decidedPassRate="100.0%"
        decisionCoverage={1}
        comparisonSummary={comparisonSummary}
      />,
    );

  it("does not call pass rate unchanged without both baseline and candidate evidence", () => {
    renderKpis({
      candidate: { pass_rate: 1 },
      comparable_cohort: null,
    });

    expect(screen.getByText("暂无基线对比")).toBeInTheDocument();
    expect(screen.queryByText("与基线持平")).not.toBeInTheDocument();
    expect(screen.queryByText(/较基线持平/)).not.toBeInTheDocument();
  });

  it("does not claim normal latency when no candidate P95 measurement is available", () => {
    renderKpis(undefined);

    expect(screen.getByText("暂无时延数据")).toBeInTheDocument();
    expect(screen.queryByText("耗时正常")).not.toBeInTheDocument();
  });

  it("reports a P95 comparison only when valid candidate measurements exist", () => {
    renderKpis({
      comparable_cohort: {
        baseline: { p95_latency_ms: 120 },
        candidate: { p95_latency_ms: 120 },
      },
    });

    expect(screen.getByText("与基线持平")).toBeInTheDocument();
  });

  it("does not fall back to full-run P95 when comparable candidate evidence is missing", () => {
    renderKpis({
      candidate: { p95_latency_ms: 230 },
      baseline: { p95_latency_ms: 120 },
      comparable_cohort: {
        baseline: { p95_latency_ms: 100 },
        candidate: { p95_latency_ms: null },
      },
    });

    expect(screen.getByText("暂无时延数据")).toBeInTheDocument();
    expect(screen.queryByText("230")).not.toBeInTheDocument();
    expect(screen.queryByText("较基线增加 130ms")).not.toBeInTheDocument();
  });

  it("does not compare comparable candidate P95 with full-run baseline P95", () => {
    renderKpis({
      candidate: { p95_latency_ms: 230 },
      baseline: { p95_latency_ms: 120 },
      comparable_cohort: {
        baseline: { p95_latency_ms: null },
        candidate: { p95_latency_ms: 210 },
      },
    });

    expect(screen.getByText("210")).toBeInTheDocument();
    expect(screen.getByText("单版本测量")).toBeInTheDocument();
    expect(screen.queryByText("较基线增加 90ms")).not.toBeInTheDocument();
  });

  it("only reports a flat pass-rate delta when both rates are present and equal", () => {
    renderKpis({
      comparable_cohort: {
        baseline: { pass_rate: 0.8 },
        candidate: { pass_rate: 0.8 },
      },
    });

    expect(screen.getByText("较基线持平 (0.0 pp)")).toBeInTheDocument();
  });

  it("does not fabricate token usage when only legacy cost evidence exists (S6.5)", () => {
    renderKpis({
      candidate: { cost_per_case: 0.01 },
      cost_comparison: { status: "NOT_COMPARABLE", delta: null },
    });

    expect(screen.getByTestId("usage-tokens-per-case")).toHaveTextContent("—");
    expect(screen.getByText("未采集到 token 消耗数据")).toBeInTheDocument();
    expect(screen.queryByText(/\$|USD/)).not.toBeInTheDocument();
  });

  it("keeps token evidence independent of legacy cost comparison (S6.5)", () => {
    renderKpis({
      comparable_cohort: {
        baseline: { cost_per_case: 0.01 },
        candidate: { cost_per_case: 0.01 },
      },
      cost_comparison: { status: "COMPARABLE", delta: 0 },
    });

    expect(screen.getByTestId("usage-tokens-per-case")).toHaveTextContent("—");
    expect(screen.getByText("未采集到 token 消耗数据")).toBeInTheDocument();
  });
});

describe("LaunchKpiOverview regression count unknown-safe (issue 119 U2)", () => {
  const FORMAL_COMPARABLE = {
    comparability: { comparable: true, reason_codes: [] },
    formal: { available: true, verdict: "UNCHANGED" },
  };

  const renderRegression = (props: {
    classificationCounts?: Record<string, unknown> | null;
    comparability?: any;
    formal?: any;
  }) =>
    render(
      <LaunchKpiOverview
        totalItems={2}
        passedItems={2}
        decisionCounts={{ pass: 2, fail: 0, unknown: 0 }}
        decidedPassRate="100.0%"
        decisionCoverage={1}
        {...props}
      />,
    );

  const regressionValue = () => screen.getByTestId("regression-count-value");
  const regressionNote = () => screen.getByTestId("regression-count-note");

  it("shows a real zero as a formal no-regression result", () => {
    renderRegression({
      classificationCounts: { REGRESSION: 0 },
      ...FORMAL_COMPARABLE,
    });
    expect(regressionValue()).toHaveTextContent("0");
    expect(regressionNote()).toHaveTextContent("正式比较未发现回归");
  });

  it("does not turn a missing regression count into a green zero", () => {
    renderRegression({ classificationCounts: {}, ...FORMAL_COMPARABLE });
    expect(regressionValue()).not.toHaveTextContent("0");
    expect(regressionValue()).toHaveTextContent("不可用");
    expect(screen.queryByText(/未发现回归/)).not.toBeInTheDocument();
  });

  it("does not coerce a null regression count into zero", () => {
    renderRegression({ classificationCounts: { REGRESSION: null }, ...FORMAL_COMPARABLE });
    expect(regressionValue()).toHaveTextContent("不可用");
    expect(screen.queryByText(/未发现回归/)).not.toBeInTheDocument();
  });

  it("does not coerce a string regression count into a number", () => {
    renderRegression({ classificationCounts: { REGRESSION: "0" }, ...FORMAL_COMPARABLE });
    expect(regressionValue()).toHaveTextContent("不可用");
    expect(screen.queryByText(/未发现回归/)).not.toBeInTheDocument();
  });

  it("rejects negative, fractional and non-finite regression counts", () => {
    for (const bad of [-1, 0.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      const { unmount } = renderRegression({
        classificationCounts: { REGRESSION: bad } as any,
        ...FORMAL_COMPARABLE,
      });
      expect(regressionValue()).toHaveTextContent("不可用");
      unmount();
    }
  });

  it("keeps a valid positive regression count with its排查 entry", () => {
    renderRegression({ classificationCounts: { REGRESSION: 3 }, ...FORMAL_COMPARABLE });
    expect(regressionValue()).toHaveTextContent("3");
    expect(regressionNote()).toHaveTextContent("3 项性能退化");
  });

  it("does not display a formal count when comparability is false even if formal is available", () => {
    renderRegression({
      classificationCounts: { REGRESSION: 0 },
      comparability: { comparable: false, reason_codes: [] },
      formal: { available: true, verdict: "UNCHANGED" },
    });
    expect(regressionValue()).not.toHaveTextContent("0");
    expect(screen.queryByText(/未发现回归/)).not.toBeInTheDocument();
  });

  it("does not display a formal count when the formal verdict is unavailable", () => {
    renderRegression({
      classificationCounts: { REGRESSION: 0 },
      comparability: { comparable: true, reason_codes: [] },
      formal: { available: false, withheld_reasons: ["EVIDENCE_INCOMPLETE"] },
    });
    expect(regressionValue()).not.toHaveTextContent("0");
    expect(screen.queryByText(/未发现回归/)).not.toBeInTheDocument();
  });
});


describe("LaunchKpiOverview token consumption card (S6.5)", () => {
  const renderKpis = (comparisonSummary?: any) =>
    render(
      <LaunchKpiOverview
        totalItems={3}
        passedItems={3}
        decisionCounts={{ pass: 3, fail: 0, unknown: 0 }}
        decidedPassRate="100.0%"
        decisionCoverage={1}
        comparisonSummary={comparisonSummary}
      />,
    );

  it("renders token consumption from the frozen total-case denominator", () => {
    renderKpis({
      candidate: {
        usage_tokens_per_case: 91.6667,
        usage_input_tokens_per_case: 60,
        usage_output_tokens_per_case: 31.6667,
        usage_case_count: 3,
        usage_coverage: 1,
      },
    });

    expect(screen.getByText("单用例平均 token 消耗")).toBeInTheDocument();
    const main = screen.getByTestId("usage-tokens-per-case");
    expect(main).toHaveTextContent("91.7");
    expect(screen.getByTestId("usage-tokens-breakdown")).toHaveTextContent("输入 60 / 输出 31.7 tokens");
    expect(screen.queryByText(/\$|USD/)).not.toBeInTheDocument();
  });

  it("never renders a money amount even when legacy cost evidence exists", () => {
    renderKpis({
      candidate: {
        cost_per_case: 0.01,
        cost_currency: "USD",
      },
    });

    expect(screen.getByTestId("usage-tokens-per-case")).toHaveTextContent("—");
    expect(screen.getByText("未采集到 token 消耗数据")).toBeInTheDocument();
    expect(screen.queryByText(/\$|USD/)).not.toBeInTheDocument();
  });

  it("surfaces fail-closed reason when token evidence is incomplete", () => {
    renderKpis({
      candidate: {
        usage_case_count: 2,
        usage_coverage: 0.6667,
        usage_partial: true,
        usage_unavailable_reason: "INCOMPLETE_ATTEMPT_COST",
      },
    });

    expect(screen.getByTestId("usage-tokens-per-case")).toHaveTextContent("—");
    expect(screen.getByText("部分用例缺少 token 证据 (2/3)")).toBeInTheDocument();
  });

  it("keeps a baseline comparable cohort candidate without money formatting", () => {
    renderKpis({
      comparable_cohort: {
        baseline: { usage_tokens_per_case: 50 },
        candidate: {
          usage_tokens_per_case: 91.6667,
          usage_input_tokens_per_case: 60,
          usage_output_tokens_per_case: 31.6667,
        },
      },
      cost_comparison: { status: "COMPARABLE", delta: 0 },
    });

    expect(screen.getByTestId("usage-tokens-per-case")).toHaveTextContent("91.7");
    expect(screen.getByTestId("usage-tokens-breakdown")).toHaveTextContent("输入 60 / 输出 31.7 tokens");
    expect(screen.queryByText(/\$|USD/)).not.toBeInTheDocument();
  });

  it("prefers no usage value instead of full-run fallback inside a cohort", () => {
    renderKpis({
      candidate: { usage_tokens_per_case: 91.6667 },
      comparable_cohort: {
        baseline: { usage_tokens_per_case: null },
        candidate: { usage_tokens_per_case: null },
      },
    });

    expect(screen.getByTestId("usage-tokens-per-case")).toHaveTextContent("—");
  });
});
