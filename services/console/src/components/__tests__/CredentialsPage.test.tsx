import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CredentialsPage } from "../../features/credentials/CredentialsPage";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({ api: { GET: vi.fn(), POST: vi.fn(), DELETE: vi.fn() } }));

describe("Credentials 管理", () => {
  it("creates a credential with authorization and clears both secret inputs", async () => {
    vi.mocked(api.GET).mockResolvedValue({ data: [], response: new Response() } as any);
    vi.mocked(api.POST).mockResolvedValue({ data: { id: "cred-1", name: "生产 Token", provider: "managed", environment: "production", enabled: true, version: 1 }, response: new Response() } as any);
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><CredentialsPage /></QueryClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: "创建凭据" }));
    fireEvent.change(screen.getByLabelText("凭据名称"), { target: { value: "生产 Token" } });
    fireEvent.change(screen.getByLabelText("Agent Token（仅输入一次）"), { target: { value: "example-agent-token" } });
    fireEvent.change(screen.getByLabelText("管理授权 Token"), { target: { value: "example-admin-token" } });
    fireEvent.submit(screen.getByLabelText("创建凭据表单"));
    await waitFor(() => expect(api.POST).toHaveBeenCalledWith("/api/v1/credentials", expect.objectContaining({
      headers: { Authorization: "Bearer example-admin-token" },
      body: expect.objectContaining({ secret: "example-agent-token", provider: "managed" }),
    })));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByDisplayValue("example-agent-token")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("example-admin-token")).not.toBeInTheDocument();
  });

  it("displays precise error message when server returns 503 master key unavailable", async () => {
    vi.mocked(api.GET).mockResolvedValue({ data: [], response: new Response() } as any);
    vi.mocked(api.POST).mockResolvedValue({
      error: { detail: "secret manager not initialized" },
      response: new Response(null, { status: 503, headers: { "X-Argus-Error-Code": "MANAGED_SECRET_UNAVAILABLE" } }),
    } as any);
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><CredentialsPage /></QueryClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: "创建凭据" }));
    fireEvent.change(screen.getByLabelText("凭据名称"), { target: { value: "生产 Token" } });
    fireEvent.change(screen.getByLabelText("Agent Token（仅输入一次）"), { target: { value: "example-agent-token" } });
    fireEvent.change(screen.getByLabelText("管理授权 Token"), { target: { value: "example-admin-token" } });
    fireEvent.submit(screen.getByLabelText("创建凭据表单"));
    expect(await screen.findByRole("alert")).toHaveTextContent("当前服务环境未挂载或未启用 Managed Secret 主密钥");
  });
});

it("names each Credential action and its Modal target even for duplicate names", async () => {
  const ids = ["00000000-0000-4000-8000-aaaaaaaaaaaa", "00000000-0000-4000-8000-bbbbbbbbbbbb"];
  vi.mocked(api.GET).mockResolvedValue({ data: ids.map(id => ({ id, name: "同名凭据", provider: "managed", environment: "production", enabled: true, version: 1 })), response: new Response() } as any);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><CredentialsPage /></QueryClientProvider>);
  for (const suffix of ["aaaaaaaa", "bbbbbbbb"]) {
    for (const action of ["查看使用关系", "轮换", "停用", "删除"]) {
      expect(await screen.findByRole("button", { name: `${action} 同名凭据 (${suffix})` })).toBeVisible();
    }
  }
  fireEvent.click(screen.getByRole("button", { name: "轮换 同名凭据 (bbbbbbbb)" }));
  expect(screen.getByRole("dialog", { name: "轮换凭据 · 同名凭据 (bbbbbbbb)" })).toBeVisible();
});

it("creates a Managed Credential without asking users to configure a Provider", () => {
  vi.mocked(api.GET).mockResolvedValue({ data: [], response: new Response() } as any);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><CredentialsPage /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "创建凭据" }));
  expect(screen.queryByRole("combobox", { name: "存储方式" })).not.toBeInTheDocument();
  expect(screen.queryByText("Vault（需管理员启用）")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Agent Token（仅输入一次）")).toBeVisible();
});

it("creates Basic credentials as protected fields with explicit target origins", async () => {
  vi.mocked(api.GET).mockResolvedValue({ data: [], response: new Response() } as any);
  vi.mocked(api.POST).mockClear().mockResolvedValue({ data: {}, response: new Response() } as any);
  render(<QueryClientProvider client={new QueryClient()}><CredentialsPage /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "创建凭据" }));
  fireEvent.change(screen.getByLabelText("凭据类型"), { target: { value: "basic_auth" } });
  fireEvent.change(screen.getByLabelText("凭据名称"), { target: { value: "Java Basic" } });
  fireEvent.change(screen.getByLabelText("允许的目标 Origin（每行一个）"), { target: { value: "https://agent.example" } });
  fireEvent.change(screen.getByLabelText("Basic 用户名"), { target: { value: "test-user" } });
  fireEvent.change(screen.getByLabelText("Basic 密码"), { target: { value: "test-password" } });
  fireEvent.change(screen.getByLabelText("管理授权 Token"), { target: { value: "admin" } });
  fireEvent.submit(screen.getByLabelText("创建凭据表单"));
  await waitFor(() => expect(api.POST).toHaveBeenCalledWith("/api/v1/credentials", expect.objectContaining({ body: expect.objectContaining({
    type: "basic_auth", allowed_origins: ["https://agent.example"], secret: { username: "test-user", password: "test-password" },
  }) })));
  await waitFor(() => expect(screen.queryByDisplayValue("test-password")).not.toBeInTheDocument());
});

it("defaults to the service environment without overwriting explicit input on late response", async () => {
  let resolveInfo!: (value: any) => void;
  vi.mocked(api.GET).mockImplementation(((url: string) => url === "/api/v1/system/info"
    ? new Promise(resolve => { resolveInfo = resolve; })
    : Promise.resolve({ data: [], response: new Response() })) as any);
  render(<QueryClientProvider client={new QueryClient()}><CredentialsPage /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "创建凭据" }));
  fireEvent.change(screen.getByLabelText("环境"), { target: { value: "explicit-env" } });
  resolveInfo({ data: { environment: "argus-kunpeng-test" }, response: new Response() });
  await waitFor(() => expect(api.GET).toHaveBeenCalledWith("/api/v1/system/info"));
  await waitFor(() => expect(screen.getByLabelText("环境")).toHaveValue("explicit-env"));
  expect(screen.getByLabelText("环境")).toHaveValue("explicit-env");
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  fireEvent.click(screen.getByRole("button", { name: "创建凭据" }));
  await waitFor(() => expect(screen.getByLabelText("环境")).toHaveValue("argus-kunpeng-test"));
});

it.each([
  ["CREDENTIAL_ADMIN_UNAVAILABLE", "未配置凭据管理授权文件"],
  ["MANAGED_SECRET_UNAVAILABLE", "Managed Secret 主密钥"],
  ["", "服务暂不可用，无法确认主密钥状态"],
])("uses only safe error code %s for 503 diagnostics", async (code, message) => {
  vi.mocked(api.GET).mockResolvedValue({ data: [], response: new Response() } as any);
  vi.mocked(api.POST).mockResolvedValue({ error: { detail: "never-echo-secret" },
    response: new Response(null, { status: 503, headers: { "X-Argus-Error-Code": code } }) } as any);
  render(<QueryClientProvider client={new QueryClient()}><CredentialsPage /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "创建凭据" }));
  fireEvent.submit(screen.getByLabelText("创建凭据表单"));
  expect(await screen.findByRole("alert")).toHaveTextContent(message);
  expect(screen.queryByText(/never-echo-secret/)).not.toBeInTheDocument();
});
