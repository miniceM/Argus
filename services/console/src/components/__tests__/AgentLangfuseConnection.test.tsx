import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import type { components } from "../../api/schema";
import { api } from "../../api/client";
import { AgentLangfuseConnection } from "../../features/agents/AgentLangfuseConnection";

vi.mock("../../api/client", () => ({ api: { GET: vi.fn(), PUT: vi.fn() } }));
const binding = { base_url: "https://lf.example", project_id: "project-b", credential_id: "lf-credential" };
const agent: components["schemas"]["AgentResponse"] = {
  id: "team-b", name: "团队 B", status: "active", created_at: "2026-11-12T00:00:00Z", updated_at: "2026-11-12T00:00:00Z",
  version_count: 0, launch_count: 0, active_launch_count: 0,
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.GET).mockResolvedValue({ data: [{ id: "lf-credential", name: "团队 B", type: "basic_auth", enabled: true, environment: "staging" }] } as any);
  vi.mocked(api.PUT).mockResolvedValue({ data: { ...agent, langfuse_connection: binding } } as any);
});
function renderConnection(connection: typeof binding | null = null) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <AgentLangfuseConnection agent={{ ...agent, langfuse_connection: connection }} />
  </QueryClientProvider>);
}

it("在 Agent 配置一次项目，提交引用和授权并清除 Token", async () => {
  renderConnection();
  fireEvent.click(screen.getByRole("button", { name: "配置 Langfuse 项目" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "使用 Agent 独立 Langfuse 项目" }));
  fireEvent.change(screen.getByLabelText(/Langfuse Base URL/), { target: { value: binding.base_url } });
  fireEvent.change(screen.getByLabelText(/Langfuse Project ID/), { target: { value: binding.project_id } });
  await screen.findByRole("option", { name: /团队 B.*staging/ });
  fireEvent.change(screen.getByLabelText(/Langfuse 凭据/), { target: { value: binding.credential_id } });
  fireEvent.submit(document.getElementById("agent-langfuse-form")!);
  expect(api.PUT).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText(/项目连接管理授权 Token/), { target: { value: "example-admin" } });
  fireEvent.submit(document.getElementById("agent-langfuse-form")!);
  await waitFor(() => expect(api.PUT).toHaveBeenCalledWith("/api/v1/agents/langfuse-connection", expect.objectContaining({
    headers: { Authorization: "Bearer example-admin" }, params: { query: { id: "team-b" } }, body: { langfuse_connection: binding },
  })));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.queryByDisplayValue("example-admin")).not.toBeInTheDocument();
});

it("展示全部版本共用的项目，清除绑定也需要管理授权", async () => {
  renderConnection(binding);
  expect(screen.getByText(/全部版本共用/)).toBeInTheDocument();
  expect(screen.getByText(/Project：project-b/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "编辑 Langfuse 项目" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "使用 Agent 独立 Langfuse 项目" }));
  fireEvent.submit(document.getElementById("agent-langfuse-form")!);
  expect(api.PUT).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText(/项目连接管理授权 Token/), { target: { value: "example-admin" } });
  fireEvent.submit(document.getElementById("agent-langfuse-form")!);
  await waitFor(() => expect(api.PUT).toHaveBeenCalledWith("/api/v1/agents/langfuse-connection", expect.objectContaining({
    body: { langfuse_connection: null },
  })));
});

it("保存失败时保留表单并展示错误", async () => {
  vi.mocked(api.PUT).mockResolvedValue({ error: { detail: "Invalid project" } } as any);
  renderConnection(binding);
  fireEvent.click(screen.getByRole("button", { name: "编辑 Langfuse 项目" }));
  fireEvent.change(screen.getByLabelText(/项目连接管理授权 Token/), { target: { value: "example-admin" } });
  fireEvent.submit(document.getElementById("agent-langfuse-form")!);
  expect(await screen.findByRole("alert")).toHaveTextContent("Invalid project");
  expect(screen.getByRole("dialog")).toBeInTheDocument();
});
