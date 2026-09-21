import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusBadge } from "../StatusBadge";
import { QualityBadge } from "../QualityBadge";

describe("Execution Status vs Quality Conclusion Semantic Decoupling", () => {
  it("StatusBadge correctly renders SUCCEEDED execution status", () => {
    const { container } = render(<StatusBadge status="SUCCEEDED" />);
    expect(screen.getByText("SUCCEEDED")).toBeInTheDocument();
    // Verify it doesn't mention "PASS" or "质量通过"
    expect(screen.queryByText("PASS")).not.toBeInTheDocument();
    expect(screen.queryByText("质量通过")).not.toBeInTheDocument();
    expect(container.firstChild).toHaveClass("text-emerald-700");
  });

  it("StatusBadge correctly renders FAILED execution status", () => {
    render(<StatusBadge status="FAILED" />);
    expect(screen.getByText("FAILED")).toBeInTheDocument();
    expect(screen.queryByText("FAIL")).not.toBeInTheDocument();
  });

  it("StatusBadge renders unknown statuses gracefully", () => {
    render(<StatusBadge status="CUSTOM_STATUS" />);
    expect(screen.getByText("CUSTOM_STATUS")).toBeInTheDocument();
  });

  it("StatusBadge correctly renders S2 lifecycle statuses", () => {
    const { rerender, container } = render(<StatusBadge status="QUEUED" />);
    expect(screen.getByText("QUEUED")).toBeInTheDocument();
    expect(container.firstChild).toHaveClass("text-indigo-700");

    rerender(<StatusBadge status="PARTIAL_FAILED" />);
    expect(screen.getByText("PARTIAL_FAILED")).toBeInTheDocument();
    expect(container.firstChild).toHaveClass("text-orange-700");

    rerender(<StatusBadge status="RETRY_WAIT" />);
    expect(screen.getByText("RETRY_WAIT")).toBeInTheDocument();
    expect(container.firstChild).toHaveClass("text-yellow-700");

    rerender(<StatusBadge status="CANCELLED" />);
    expect(screen.getByText("CANCELLED")).toBeInTheDocument();
    expect(container.firstChild).toHaveClass("text-gray-600");
  });

  it("QualityBadge correctly renders PASS quality conclusion", () => {
    const { container } = render(<QualityBadge quality="PASS" />);
    expect(screen.getByText("PASS")).toBeInTheDocument();
    // Verify it does not mention "SUCCEEDED"
    expect(screen.queryByText("SUCCEEDED")).not.toBeInTheDocument();
    expect(container.firstChild).toHaveClass("text-emerald-700");
  });

  it("QualityBadge correctly renders FAIL quality conclusion", () => {
    const { container } = render(<QualityBadge quality="FAIL" />);
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    expect(screen.queryByText("FAILED")).not.toBeInTheDocument();
    expect(container.firstChild).toHaveClass("text-rose-700");
  });

  it("QualityBadge correctly renders UNKNOWN quality conclusion", () => {
    render(<QualityBadge quality="UNKNOWN" />);
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
  });

  it("Guarantees that a SUCCEEDED execution does NOT imply PASS quality badge", () => {
    render(
      <div data-testid="dual-container">
        <StatusBadge status="SUCCEEDED" />
        <QualityBadge quality="FAIL" />
      </div>
    );
    // Even if execution SUCCEEDED, quality can independently be FAIL
    expect(screen.getByText("SUCCEEDED")).toBeInTheDocument();
    expect(screen.getByText("FAIL")).toBeInTheDocument();
  });
});
