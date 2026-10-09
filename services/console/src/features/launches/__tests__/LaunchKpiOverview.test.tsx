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
