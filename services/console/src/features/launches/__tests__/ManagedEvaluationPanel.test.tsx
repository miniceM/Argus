import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ManagedEvaluationPanel, ManagedEvaluationNotice, isItemEvaluationActive } from "../ManagedEvaluationPanel";
import { api } from "../../../api/client";
import { createBackfillTracker, evaluateLaunchPolling } from "../useLaunchPolling";

vi.mock("../../../api/client", () => ({ api: { GET: vi.fn() } }));
const contract = { scores: [
  { name: "judge", data_type: "boolean", required: true, rule_id: "rule-judge" },
  { name: "code", data_type: "numeric", required: true, rule_id: "rule-code" },
] };
const manifest = { evaluation_source: "langfuse_managed", managed_evaluation: contract };
const result = { evaluator_id: "judge", result_type: "boolean", status: "succeeded", value: false,
  provenance: { executor_type: "langfuse_managed", contract_status: "MANAGED_SCORE_VERIFIED" },
  evidence: { score: { id: "score-real", name: "judge", source: "EVAL", dataType: "BOOLEAN", value: 0 },
    score_url: "https://langfuse.example/project/p/traces/t?observation=o", observed_at: "2026-10-21T00:00:00Z" } };
const base = { launchId: "launch", snapshotId: "snap", manifest,
  item: { id: "item", dataset_item_id: "case", execution_status: "succeeded", eval_status: "failed", quality_conclusion: "unknown", evaluation_results: [result] } };
function show(props: Partial<Parameters<typeof ManagedEvaluationPanel>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><ManagedEvaluationPanel {...base} {...props} /></QueryClientProvider>);
}
afterEach(() => { cleanup(); vi.clearAllMocks(); });
describe("Managed Evaluation 证据与状态边界", () => {
  it("冻结视图显示必选到齐情况、真实 false 和缺失原因；不查询实时诊断", () => {
    show();
    expect(screen.getByText("必要评分 1 / 2 已采纳")).toBeVisible();
    expect(screen.getByText("Agent 执行：succeeded")).toBeVisible();
    expect(screen.getByText("评分失败")).toBeVisible();
    expect(screen.getByText("质量结论：UNKNOWN")).toBeVisible();
    expect(screen.getByText("false")).toBeVisible();
    expect(screen.getByText(/SCORE_MISSING/)).toBeVisible();
    expect(screen.getByText(/source=EVAL；Score ID/)).toBeVisible();
    expect(screen.getByRole("link", { name: /查看 Score 所在 Observation/ })).toHaveAttribute("href", result.evidence.score_url);
    expect(screen.queryByRole("link", { name: /^Score$/ })).not.toBeInTheDocument();
    expect(api.GET).not.toHaveBeenCalled();
  });
  it("不把质量 FAIL 当评分失败；不伪造外部版本", () => {
    show({ item: { ...base.item, eval_status: "succeeded", quality_conclusion: "fail" } });
    expect(screen.getByText("评分完成")).toBeVisible();
    expect(screen.getByText("质量结论：FAIL")).toBeVisible();
    expect(screen.getByText(/latest/)).toBeVisible();
    expect(screen.getByText(/远程执行实现并未冻结/)).toBeVisible();
    expect(screen.queryByText(/@latest/)).not.toBeInTheDocument();
  });
  it("拒绝带凭据或危险协议的导航；不从 Score 名称推断归属", () => {
    show({ item: { ...base.item, evaluation_results: [{ ...result, provenance: null,
      evidence: { ...result.evidence, score_url: "https://user:secret@example.com/t" } }] } });
    expect(screen.getByText("必要评分 0 / 2 已采纳")).toBeVisible();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
  it("实时读取拒绝审计，故障显式提示且仍可重试；不采用诊断端点的 UNKNOWN 覆盖质量", async () => {
    vi.mocked(api.GET).mockResolvedValueOnce({ data: { launch_id: "launch", item_execution_id: "item", quality_conclusion: "unknown", results: [], evidence: [
      { id: "audit", score_name: "code", score_id: "foreign", decision: "REJECTED", reason: "SOURCE_MISMATCH", evidence: {} },
    ] } } as never);
    show({ snapshotId: null });
    await waitFor(() => expect(screen.getByText(/未采纳诊断/)).toBeVisible());
    fireEvent.click(screen.getByText(/未采纳诊断/));
    expect(screen.getByText(/SOURCE_MISMATCH/)).toBeVisible();
    expect(api.GET).toHaveBeenCalledWith("/api/v1/experiment-launches/{launch_id}/items/{item_id}/managed-scores", expect.anything());
  });
  it("Builtin 不请求 Managed API", () => {
    show({ manifest: {} });
    expect(screen.queryByText(/必要评分/)).not.toBeInTheDocument();
    expect(api.GET).not.toHaveBeenCalled();
  });
  it("Agent 已结束且同步完成，评分 pending 仍轮询 Launch", () => {
    expect(evaluateLaunchPolling(createBackfillTracker(), [{ id: "l", status: "COMPLETED", langfuse_sync_status: "SYNCED",
      langfuse_experiment_url: "https://langfuse.example/run", progress: { evaluating: 1 } }], 0).shouldPoll).toBe(true);
  });
});

it.each(["javascript:alert(1)", "https://user:secret@example.com/t"])("已核验结果也拒绝危险外链 %s，并避免把凭据 URL 渲染进证据", (url) => {
  show({ item: { ...base.item, evaluation_results: [{ ...result, evidence: { ...result.evidence, score_url: url } }] } });
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  expect(screen.getByText("必要评分 1 / 2 已采纳")).toBeVisible();
  expect(document.body.innerHTML).not.toContain(url);
});

it("Managed 契约缺失时显式提示，不假装 Builtin 或零必选已完成", () => {
  show({ manifest: { evaluation_source: "langfuse_managed" } });
  expect(screen.getByRole("alert")).toHaveTextContent("Managed 采纳契约缺失");
  expect(screen.queryByText(/必要评分 0 \/ 0/)).not.toBeInTheDocument();
  expect(api.GET).not.toHaveBeenCalled();
});

it("缺少可信 Manifest 不显示 Builtin 的执行断言", () => {
  render(<ManagedEvaluationNotice manifest={{}} />);
  expect(screen.getByText("评分来源未记录")).toBeVisible();
  expect(screen.queryByText(/Builtin 在 Argus 内执行/)).not.toBeInTheDocument();
});
it.each([{ scores: [null] }, { scores: [{}] }, { scores: [] }])("畸形托管 scores 不崩溃或伪造覆盖率 %j", ({ scores }) => {
  show({ manifest: { evaluation_source: "langfuse_managed", managed_evaluation: { scores } } });
  expect(screen.getByRole("alert")).toHaveTextContent("Managed 采纳契约缺失");
});

it("已取消或执行失败且未启动评分的 pending 占位不维持无限评分轮询", () => {
  for (const execution_status of ["cancelled", "failed", "timed_out"]) {
    expect(isItemEvaluationActive({ execution_status, eval_status: "pending", evaluation_status: "none" })).toBe(false);
  }
  expect(isItemEvaluationActive({ execution_status: "succeeded", eval_status: "pending" })).toBe(true);
});
