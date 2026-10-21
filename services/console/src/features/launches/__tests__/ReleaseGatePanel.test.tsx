import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { api } from "../../../api/client";
import { ReleaseGatePanel } from "../ReleaseGatePanel";
vi.mock("../../../api/client", () => ({ api: { GET: vi.fn(), POST: vi.fn() } }));
const gate = { id: "gate-1", candidate_launch_id: "launch", candidate_snapshot_id: "S1", candidate_revision: 1,
  decision: "UNKNOWN", releasable: false, reason_codes: ["CANDIDATE_DIAGNOSTIC"], rules: [], policy: { name: "policy", version: "1" }, policy_digest: "sha256:policy" };
function show(snapshotId: string | null = "S1", url = "/launches/launch?snapshot_id=S1") {
  return render(<MemoryRouter initialEntries={[url]}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ReleaseGatePanel launchId="launch" snapshotId={snapshotId} />
  </QueryClientProvider></MemoryRouter>);
}
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it("向既有 Gate API 提交固定 Snapshot，不把 UNKNOWN 当发布通过，保存可回看的链接", async () => {
  vi.mocked(api.POST).mockResolvedValue({ data: gate } as never);
  vi.mocked(api.GET).mockResolvedValue({ data: gate } as never);
  show();
  fireEvent.change(screen.getByLabelText("ReleasePolicy 名称"), { target: { value: "policy" } });
  fireEvent.change(screen.getByLabelText("ReleasePolicy 版本"), { target: { value: "1" } });
  fireEvent.click(screen.getByRole("button", { name: "计算并保存 Gate" }));
  await screen.findByText("UNKNOWN · 不可发布");
  expect(api.POST).toHaveBeenCalledWith("/api/v1/release-gates/evaluate", { body: { policy_name: "policy", policy_version: "1", candidate_launch_id: "launch", candidate_snapshot_id: "S1" } });
  expect(screen.getByRole("link", { name: "固定 Gate 报告" })).toHaveAttribute("href", "/launches/launch?snapshot_id=S1&tab=compare&gate_id=gate-1");
  expect(screen.getByText(/原因：CANDIDATE_DIAGNOSTIC/)).toBeVisible();
});
it("拒绝其他 Snapshot 的 Gate，不能把 S2 PASS 显示在 S1", async () => {
  vi.mocked(api.GET).mockResolvedValue({ data: { ...gate, candidate_snapshot_id: "S2", decision: "PASS", releasable: true } } as never);
  show("S1", "/launches/launch?snapshot_id=S1&gate_id=foreign");
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Gate 与当前 Launch / Snapshot 不一致"));
  expect(screen.queryByText("PASS · 可发布")).not.toBeInTheDocument();
});
it("未冻结时禁用计算，不触发业务调用或生成假 Gate", () => {
  show(null);
  expect(screen.getByRole("button", { name: "计算并保存 Gate" })).toBeDisabled();
  expect(screen.getByText(/等待 Frozen Snapshot/)).toBeVisible();
  expect(api.POST).not.toHaveBeenCalled();
});

it("畸形 releasable 不能变成可发布 PASS", async () => {
  vi.mocked(api.GET).mockResolvedValue({ data: { ...gate, decision: "PASS", releasable: "false" } } as never);
  show("S1", "/launches/launch?snapshot_id=S1&gate_id=gate-1");
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Gate 响应格式无效"));
  expect(screen.queryByText("PASS · 可发布")).not.toBeInTheDocument();
});
