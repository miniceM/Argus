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
});
