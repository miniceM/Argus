import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CasesTraceTab } from "../tabs/CasesTraceTab";
import { api } from "../../../api/client";
import { trajectoryFixture } from "./trajectoryFixtures";

vi.mock("../../../api/client", () => ({ api: { GET: vi.fn(), POST: vi.fn() } }));

const path = "/api/v1/experiment-launches/{launch_id}/trajectory";
const item = { id: "item-1", launch_id: "launch-1", dataset_item_id: "case-1", execution_status: "succeeded",
  eval_status: "succeeded", quality_conclusion: "fail", attempt_count: 1, dispatch_generation: 1 };
let client: QueryClient;
const view = (snapshotId: string | null = "snapshot-1", expanded = true) => <QueryClientProvider client={client}>
  <CasesTraceTab launchId="launch-1" snapshotId={snapshotId} items={[item as any]} expandedCaseId={expanded ? "case-1" : undefined} />
</QueryClientProvider>;

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(api.GET).mockReset();
  vi.mocked(api.GET).mockImplementation((url: any, options: any) => Promise.resolve(url === path
    ? { data: trajectoryFixture(options.params.query.snapshot_id) }
    : { data: { launch_id: "launch-1", candidate_snapshot_id: options?.params?.query?.snapshot_id, dataset_item_id: "case-1",
      baseline: { output_status: "NO_REFERENCE" }, candidate: { output_status: "NO_REFERENCE" } } }) as any);
});

describe("固定 Snapshot 轨迹诊断", () => {
  it("只在展开 Case 且有 Snapshot 时读取", async () => {
    const mounted = render(view("snapshot-1", false));
    expect(api.GET).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "展开" }));
    await screen.findByRole("heading", { name: "固定轨迹诊断" });
    expect(api.GET).toHaveBeenCalledWith(path, expect.objectContaining({ params: {
      path: { launch_id: "launch-1" }, query: { snapshot_id: "snapshot-1", dataset_item_id: "case-1" },
    } }));
    mounted.unmount();
    vi.mocked(api.GET).mockClear();
    render(view(null));
    expect(await screen.findByText("尚无固定 Snapshot，轨迹诊断待冻结后可用。" )).toBeVisible();
    expect(api.GET).not.toHaveBeenCalled();
  });

  it("区分测量与质量三态，并展示已证违规和降级原因", async () => {
    render(view());
    const fail = await screen.findByRole("region", { name: "轨迹规则 trajectory.forbid_tool" });
    expect(within(fail).getByText("违规 FAIL")).toBeVisible();
    expect(within(fail).getByText("测量成功")).toBeVisible();
    expect(within(fail).getByText("false")).toBeVisible();
    const unknown = screen.getByRole("region", { name: "轨迹规则 trajectory.tool_order" });
    expect(within(unknown).getByText("证据不足 UNKNOWN")).toBeVisible();
    expect(within(unknown).queryByText("通过 PASS")).not.toBeInTheDocument();
    expect(screen.getByText(/答案规则仍按冻结策略评测/)).toBeVisible();
    expect(screen.getByRole("link", { name: /在线观测内容/ })).toHaveAttribute("href", "https://langfuse.example/trace/trace-1");
  });

  it("规则定位 Step、Step 反向定位规则，参数不使用浮点舍入值", async () => {
    render(view());
    const locate = await screen.findByRole("button", { name: "定位 trajectory.forbid_tool 的证据 Step exec-a" });
    fireEvent.click(locate);
    const step = screen.getByTestId("trajectory-step-exec-a");
    await waitFor(() => expect(step).toHaveFocus());
    fireEvent.click(within(step).getByText("获准参数 JSON"));
    expect(within(step).getByText('{"amount":0.12345678901234567890123456789,"n":9007199254740993}')).toBeVisible();
    expect(within(step).queryByText(/9007199254740992/)).not.toBeInTheDocument();
    fireEvent.click(within(step).getByRole("button", { name: "返回规则 trajectory.forbid_tool" }));
    await waitFor(() => expect(screen.getByRole("region", { name: "轨迹规则 trajectory.forbid_tool" })).toHaveFocus());
    expect(screen.getByText(/同层不宣称时间先后/)).toBeVisible();
    expect(screen.getByTestId("trajectory-step-exec-b")).toHaveTextContent("BLOCKED");
  });

  it("拒绝外部身份响应，不能污染当前成功缓存", async () => {
    vi.mocked(api.GET).mockResolvedValue({ data: { ...trajectoryFixture(), snapshot_id: "foreign" } } as any);
    render(view());
    expect(await screen.findByText(/轨迹响应归属与当前 Snapshot Case 不符/)).toBeVisible();
    expect(client.getQueryData(["launches", "trajectory", "launch-1", "snapshot-1", "case-1"])).toBeUndefined();
    expect(screen.queryByText("crm-snapshot-1")).not.toBeInTheDocument();
  });

  it("反复激活同一规则与 Step 仍会重新聚焦目标", async () => {
    render(view());
    const locate = await screen.findByRole("button", { name: "定位 trajectory.forbid_tool 的证据 Step exec-a" });
    const step = screen.getByTestId("trajectory-step-exec-a");
    const rule = screen.getByRole("region", { name: "轨迹规则 trajectory.forbid_tool" });
    const back = within(step).getByRole("button", { name: "返回规则 trajectory.forbid_tool" });
    for (let round = 0; round < 2; round++) {
      locate.focus();
      fireEvent.click(locate);
      await waitFor(() => expect(step).toHaveFocus());
      back.focus();
      fireEvent.click(back);
      await waitFor(() => expect(rule).toHaveFocus());
    }
  });

  it.each(["quality", "rule_fields", "step_fields", "null_rule", "null_step"])("残缺响应 %s 收敛到契约错误且不能进入成功缓存", async damage => {
    const response: any = trajectoryFixture();
    if (damage === "quality") delete response.quality_conclusion;
    if (damage === "rule_fields") delete response.rule_results[0].evidence_step_ids;
    if (damage === "step_fields") delete response.trajectory.steps[0].argument_visibility;
    if (damage === "null_rule") response.rule_results[0] = null;
    if (damage === "null_step") response.trajectory.steps[0] = null;
    vi.mocked(api.GET).mockResolvedValue({ data: response } as any);
    render(view());
    expect(await screen.findByText(/轨迹响应格式不完整/)).toBeVisible();
    expect(client.getQueryData(["launches", "trajectory", "launch-1", "snapshot-1", "case-1"])).toBeUndefined();
    expect(screen.queryByText("crm-snapshot-1")).not.toBeInTheDocument();
  });

  it("切换 Snapshot 后迟到的旧响应不会出现在当前 Case", async () => {
    let resolveOld: (value: any) => void = () => {};
    vi.mocked(api.GET).mockImplementation((url: any, options: any) => url === path
      ? options.params.query.snapshot_id === "snapshot-1" ? new Promise((resolve) => { resolveOld = resolve; }) as any
        : Promise.resolve({ data: trajectoryFixture("snapshot-2") }) as any
      : Promise.resolve({ data: null }) as any);
    const mounted = render(view());
    await waitFor(() => expect(api.GET).toHaveBeenCalledWith(path, expect.anything()));
    mounted.rerender(view("snapshot-2"));
    expect(await screen.findByText("crm-snapshot-2")).toBeVisible();
    resolveOld({ data: trajectoryFixture("snapshot-1") });
    await waitFor(() => expect(client.getQueryData(["launches", "trajectory", "launch-1", "snapshot-1", "case-1"])).toBeDefined());
    expect(screen.queryByText("crm-snapshot-1")).not.toBeInTheDocument();
  });
});
