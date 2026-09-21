import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ItemTable } from "../../features/launches/ItemTable";

const queryClient = new QueryClient();

const mockItems = [
  {
    id: "item-1",
    launch_id: "launch-123",
    dataset_item_id: "case-001",
    execution_status: "succeeded",
    eval_status: "succeeded",
    quality_conclusion: "pass",
    execution_error: null,
    eval_error: null,
    trace_id: null,
    observation_id: null,
    final_attempt_id: "att-1",
    scores: { intent_match: 1.0 },
    attempt_count: 1,
    final_attempt_http_status: 200,
    final_attempt_latency_ms: 50,
    started_at: "2026-09-20T00:00:00Z",
    completed_at: "2026-09-20T00:00:01Z",
  },
  {
    id: "item-2",
    launch_id: "launch-123",
    dataset_item_id: "case-002",
    execution_status: "succeeded",
    eval_status: "succeeded",
    quality_conclusion: "fail",
    execution_error: null,
    eval_error: null,
    trace_id: null,
    observation_id: null,
    final_attempt_id: "att-2",
    scores: { intent_match: 0.0 },
    attempt_count: 1,
    final_attempt_http_status: 200,
    final_attempt_latency_ms: 60,
    started_at: "2026-09-20T00:00:00Z",
    completed_at: "2026-09-20T00:00:01Z",
  },
];

describe("ItemTable Case-Insensitive Matching and Filtering", () => {
  it("correctly counts and filters items with lowercase backend quality conclusions", () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ItemTable items={mockItems as any} />
      </QueryClientProvider>
    );

    // Verify filter buttons count correctly
    expect(screen.getByText("全部 (2)")).toBeInTheDocument();
    expect(screen.getByText("质量通过 (1)")).toBeInTheDocument();
    expect(screen.getByText("未通过 (1)")).toBeInTheDocument();

    // Both items visible initially
    expect(screen.getByText("case-001")).toBeInTheDocument();
    expect(screen.getByText("case-002")).toBeInTheDocument();

    // Click "质量通过 (1)"
    fireEvent.click(screen.getByText("质量通过 (1)"));
    expect(screen.getByText("case-001")).toBeInTheDocument();
    expect(screen.queryByText("case-002")).not.toBeInTheDocument();

    // Click "未通过 (1)"
    fireEvent.click(screen.getByText("未通过 (1)"));
    expect(screen.queryByText("case-001")).not.toBeInTheDocument();
    expect(screen.getByText("case-002")).toBeInTheDocument();
  });
});
