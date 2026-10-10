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
        classificationCounts={{ regression: 1 }}
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

  it("only reports a flat pass-rate delta when both rates are present and equal", () => {
    renderKpis({
      comparable_cohort: {
        baseline: { pass_rate: 0.8 },
        candidate: { pass_rate: 0.8 },
      },
    });

    expect(screen.getByText("较基线持平 (0.0 pp)")).toBeInTheDocument();
  });

  it("does not call cost equal without a comparable cost delta", () => {
    renderKpis({
      candidate: { cost_per_case: 0.01 },
      cost_comparison: { status: "NOT_COMPARABLE", delta: null },
    });

    expect(screen.getByText("暂无可比较的成本对比")).toBeInTheDocument();
    expect(screen.queryByText("单例成本与基线持平")).not.toBeInTheDocument();
    expect(screen.queryByText("Token 消耗与基线相当")).not.toBeInTheDocument();
  });

  it("only reports equal cost for an explicit comparable zero delta", () => {
    renderKpis({
      comparable_cohort: {
        baseline: { cost_per_case: 0.01 },
        candidate: { cost_per_case: 0.01 },
      },
      cost_comparison: { status: "COMPARABLE", delta: 0 },
    });

    expect(screen.getByText("单例成本与基线持平")).toBeInTheDocument();
  });
});
