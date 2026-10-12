import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "../../api/client";
import { CreateVersionDialog } from "../../features/agents/CreateVersionDialog";

vi.mock("../../api/client", () => ({
  api: {
    POST: vi.fn(),
    GET: vi.fn().mockResolvedValue({ data: [] }),
  },
}));

describe("CreateVersionDialog Help Indicators (Issue #18)", () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  const renderDialog = () => {
    return render(
      <QueryClientProvider client={queryClient}>
        <CreateVersionDialog agentId="banking-agent" isOpen={true} onClose={vi.fn()} />
      </QueryClientProvider>
    );
  };

  it("renders all 6 required field help buttons on the form", () => {
    renderDialog();

    expect(screen.getByRole("button", { name: "查看「版本号 (Tag)」说明" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看「运行环境 (Environment)」说明" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看「远程调用端点 (Endpoint)」说明" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看「凭据引用 (SecretRef)」说明" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看「产物标识 (ArtifactRef)」说明" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看「请求映射关系 (Request Mapping)」说明" })).toBeInTheDocument();
  });

  it("opens popover with correct meaning and example when clicking help button", () => {
    renderDialog();

    const versionHelpBtn = screen.getByRole("button", { name: "查看「版本号 (Tag)」说明" });
    fireEvent.click(versionHelpBtn);

    // Verify content in popover
    expect(screen.getByText(/永久冻结不可修改/)).toBeInTheDocument();
    expect(screen.getByText(/1\.0\.0 或 v2/)).toBeInTheDocument();

    // Click secret help button
    const secretHelpBtn = screen.getByRole("button", { name: "查看「凭据引用 (SecretRef)」说明" });
    fireEvent.click(secretHelpBtn);

    expect(screen.getByText(/严禁直接填入明文 Secret/)).toBeInTheDocument();
    expect(screen.getByText("生产 Agent Token（Credential 名称）")).toBeInTheDocument();
    expect(screen.getByText(/仅供本地开发\/PoC/)).toBeInTheDocument();
  });
});

describe("CreateVersionDialog field-level validation", () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  const renderDialog = () =>
    render(
      <QueryClientProvider client={queryClient}>
        <CreateVersionDialog
          agentId="banking-agent"
          isOpen={true}
          onClose={vi.fn()}
        />
      </QueryClientProvider>,
    );

  // The submit button lives in the Modal footer and reaches the form through
  // the `form` attribute, which jsdom does not resolve on click. Drive the
  // form directly so this exercises the validation rather than the wiring;
  // the button-to-form association is covered end-to-end.
  const submit = () =>
    fireEvent.submit(
      document.getElementById("create-version-form") as HTMLFormElement,
    );

  it("marks the offending control invalid instead of only showing a banner", () => {
    renderDialog();

    const version = screen.getByRole("textbox", { name: /版本号/ });
    expect(version).not.toHaveAttribute("aria-invalid");

    submit();

    // The banner names the field; the control itself has to carry the state,
    // or a screen reader announces "请填写版本号与远程 HTTP 端点" and moves on
    // with no indication of which input to go to.
    expect(version).toHaveAttribute("aria-invalid", "true");
    expect(version).toHaveAccessibleDescription(/版本号为必填项/);
  });

  it("sends focus to the first control that needs fixing", () => {
    renderDialog();
    submit();

    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: /版本号/ }));
  });

  it("clears the field error as soon as the user types", () => {
    renderDialog();
    submit();
    expect(screen.getByRole("textbox", { name: /版本号/ })).toHaveAttribute("aria-invalid", "true");

    fireEvent.change(screen.getByRole("textbox", { name: /版本号/ }), {
      target: { value: "1.0.0" },
    });

    expect(screen.getByRole("textbox", { name: /版本号/ })).not.toHaveAttribute("aria-invalid");
    expect(screen.queryByText(/版本号为必填项/)).not.toBeInTheDocument();
  });

  it("leaves a valid control unmarked", () => {
    renderDialog();
    submit();

    // The endpoint is pre-filled, so only the version is at fault.
    expect(screen.getByRole("textbox", { name: /远程调用端点/ })).not.toHaveAttribute(
      "aria-invalid",
    );
  });
});

it("submits the same normalized environment used by Credential selection", async () => {
  vi.mocked(api.GET).mockResolvedValue({ data: [{ id: "cred-1", name: "生产凭据", environment: "production", provider: "managed", enabled: true, version: 1 }], response: new Response() } as any);
  vi.mocked(api.POST).mockResolvedValue({ data: {}, response: new Response() } as any);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><CreateVersionDialog agentId="banking-agent" isOpen onClose={vi.fn()} /></QueryClientProvider>);
  fireEvent.change(screen.getByRole("textbox", { name: /版本号/ }), { target: { value: "secured-v1" } });
  fireEvent.change(screen.getByRole("textbox", { name: /^运行环境$/ }), { target: { value: " Production " } });
  await screen.findByRole("option", { name: "生产凭据 · managed · cred-1" });
  fireEvent.change(screen.getByRole("combobox", { name: /选择凭据/ }), { target: { value: "cred-1" } });
  fireEvent.change(screen.getByLabelText("凭据绑定管理授权 Token"), { target: { value: "example-binding-authorization" } });
  fireEvent.submit(document.getElementById("create-version-form")!);
  await waitFor(() => expect(api.POST).toHaveBeenCalledWith("/api/v1/agent-versions", expect.objectContaining({ body: expect.objectContaining({ credential_id: "cred-1", environment: "production" }) })));
});

it("distinguishes same-name Credentials before binding a specific ID", async () => {
  const ids = ["00000000-0000-4000-8000-aaaaaaaaaaaa", "00000000-0000-4000-8000-bbbbbbbbbbbb"];
  vi.mocked(api.GET).mockResolvedValue({ data: ids.map(id => ({ id, name: "同名凭据", environment: "production", provider: "managed", enabled: true, version: 1 })), response: new Response() } as any);
  vi.mocked(api.POST).mockResolvedValue({ data: {}, response: new Response() } as any);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><CreateVersionDialog agentId="banking-agent" isOpen onClose={vi.fn()} /></QueryClientProvider>);
  fireEvent.change(screen.getByRole("textbox", { name: /^运行环境$/ }), { target: { value: "production" } });
  const first = await screen.findByRole("option", { name: /同名凭据.*aaaaaaaa/ });
  const second = screen.getByRole("option", { name: /同名凭据.*bbbbbbbb/ });
  expect(first).toHaveAttribute("value", ids[0]);
  expect(second).toHaveAttribute("value", ids[1]);
  fireEvent.change(screen.getByRole("textbox", { name: /版本号/ }), { target: { value: "specific-credential" } });
  fireEvent.change(screen.getByRole("combobox", { name: /选择凭据/ }), { target: { value: ids[1] } });
  fireEvent.change(screen.getByLabelText("凭据绑定管理授权 Token"), { target: { value: "example-binding-authorization" } });
  fireEvent.submit(document.getElementById("create-version-form")!);
  await waitFor(() => expect(api.POST).toHaveBeenCalledWith("/api/v1/agent-versions", expect.objectContaining({ body: expect.objectContaining({ credential_id: ids[1] }) })));
});

it("requires binding authorization and clears it after a successful version creation", async () => {
  vi.mocked(api.POST).mockClear();
  vi.mocked(api.GET).mockResolvedValue({ data: [{ id: "cred-1", name: "生产凭据", environment: "production", provider: "managed", enabled: true, version: 1 }], response: new Response() } as any);
  vi.mocked(api.POST).mockResolvedValue({ data: {}, response: new Response() } as any);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><CreateVersionDialog agentId="banking-agent" isOpen onClose={vi.fn()} /></QueryClientProvider>);
  fireEvent.change(screen.getByRole("textbox", { name: /版本号/ }), { target: { value: "authorized-v1" } });
  fireEvent.change(screen.getByRole("textbox", { name: /^运行环境$/ }), { target: { value: "production" } });
  await screen.findByRole("option", { name: /生产凭据/ });
  fireEvent.change(screen.getByRole("combobox", { name: /选择凭据/ }), { target: { value: "cred-1" } });
  fireEvent.submit(document.getElementById("create-version-form")!);
  expect(api.POST).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("凭据绑定管理授权 Token"), { target: { value: "example-binding-authorization" } });
  fireEvent.submit(document.getElementById("create-version-form")!);
  await waitFor(() => expect(api.POST).toHaveBeenCalledWith("/api/v1/agent-versions", expect.objectContaining({ headers: { Authorization: "Bearer example-binding-authorization" } })));
  await waitFor(() => expect(screen.queryByDisplayValue("example-binding-authorization")).not.toBeInTheDocument());
});
