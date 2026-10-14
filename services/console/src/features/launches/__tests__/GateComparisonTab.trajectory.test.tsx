import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { GateComparisonTab } from "../tabs/GateComparisonTab";

vi.mock("../launchComparisonQueries", () => ({
  comparisonPagesQueryOptions: () => ({ queryKey: ["disabled"], queryFn: async () => null, enabled: false }),
}));

type Summary = NonNullable<React.ComponentProps<typeof GateComparisonTab>["summary"]>;
const frozen = (over: Partial<Summary> = {}): Summary => ({
  launch_id: "launch", snapshot_id: "snapshot", revision: 2, created_at: "2026-10-14T00:00:00Z",
  manifest_digest: "digest", source_result_digest: "source", langfuse_score_sync_status: "SYNCED", versions: {}, evidence_state: "COMPLETE", evidence_reasons: [],
  summary: { decided_pass_rate: 1, decision_coverage: 1, quality_unknown_count: 0, critical_failure_count: 0,
    required_evidence_status: "COMPLETE", evidence_contract_version: "trajectory-1.0" },
  required_case_counts: { total: 6, passed: 6, failed: 0, unknown: 0 }, trajectory_coverage: 0,
  ...over,
});
function show(summary: Summary) {
  render(<QueryClientProvider client={new QueryClient()}>
    <GateComparisonTab launchId="launch" snapshotId="snapshot" environment="production"
      activeBaseline={null} summary={summary} candidateQualityConclusion="pass" />
  </QueryClientProvider>);
}

describe("冻结轨迹门禁观测", () => {
  it("分别展示已决覆盖与轨迹覆盖，Case 分母不按 evaluator 数量放大", () => {
    show(frozen());
    expect(screen.getByTestId("frozen-decided-pass-rate")).toHaveTextContent("100.0%");
    expect(screen.getByTestId("frozen-decision-coverage")).toHaveTextContent("100.0%");
    expect(screen.getByTestId("frozen-trajectory-coverage")).toHaveTextContent("0.0%");
    expect(screen.getByTestId("frozen-case-counts")).toHaveTextContent("6 / PASS 6 / FAIL 0 / UNKNOWN 0");
  });
  it("零分母或历史缺字段显示破折号，真实零值保留", () => {
    show(frozen({ summary: { decided_pass_rate: null, decision_coverage: null,
      quality_unknown_count: 2, critical_failure_count: 0 }, required_case_counts: null, trajectory_coverage: null }));
    expect(screen.getByTestId("frozen-decided-pass-rate")).toHaveTextContent("—");
    expect(screen.getByTestId("frozen-decision-coverage")).toHaveTextContent("—");
    expect(screen.getByTestId("frozen-trajectory-coverage")).toHaveTextContent("—");
    expect(screen.getByTestId("frozen-unknown-count")).toHaveTextContent("2");
    expect(screen.getByTestId("frozen-critical-count")).toHaveTextContent("0");
  });
  it("不展示另一个 Snapshot 的门禁观测", () => {
    show(frozen({ snapshot_id: "foreign" }));
    expect(screen.queryByTestId("frozen-gate-observations")).not.toBeInTheDocument();
  });
});
