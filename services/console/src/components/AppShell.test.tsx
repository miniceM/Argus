import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { api } from "../api/client";
import { queryKeys } from "../api/query-keys";
import { AppShell } from "./AppShell";

vi.mock("../api/client", () => ({ api: { GET: vi.fn() } }));

const systemInfo = (langfuse_dashboard_url?: string | null) => ({
  service: "argus-control-plane",
  version: "0.2.0",
  build_id: "test-build",
  environment: "test",
  ...(langfuse_dashboard_url !== undefined ? { langfuse_dashboard_url } : {}),
});

function renderAppShell(result: Promise<unknown>) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  vi.mocked(api.GET).mockReturnValueOnce(result as never);
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/agents"]}>
        <Routes>
          <Route path="/" element={<AppShell />}>
            <Route path="agents" element={<div>Agents page</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
}

const success = (data: unknown) => Promise.resolve({ data } as never);

describe("AppShell Langfuse Dashboard link", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => cleanup());

  it("uses the configured external URL verbatim and opens it safely", async () => {
    const url = "https://observability.example.com/langfuse/nested/";
    renderAppShell(success(systemInfo(url)));

    const link = await screen.findByRole("link", { name: /Langfuse Dashboard/ });
    expect(link).toHaveAttribute("href", url);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText("Agents page")).toBeInTheDocument();
  });

  it.each([null, undefined])("does not render a link when the URL is %s", async (url) => {
    renderAppShell(success(systemInfo(url)));

    expect(await screen.findByText("未配置 Langfuse Dashboard")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Langfuse Dashboard/ })).not.toBeInTheDocument();
    expect(screen.getByText("Agents page")).toBeInTheDocument();
  });

  it("shows a loading status without a clickable link", async () => {
    renderAppShell(new Promise(() => {}));

    expect(screen.getByText("正在加载 Langfuse Dashboard 地址")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Langfuse Dashboard/ })).not.toBeInTheDocument();
  });

  it("shows an unavailable state after a network failure", async () => {
    renderAppShell(Promise.reject(new Error("network unavailable")));

    expect(await screen.findByText("无法获取 Langfuse Dashboard 地址")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Langfuse Dashboard/ })).not.toBeInTheDocument();
    expect(screen.getByText("Agents page")).toBeInTheDocument();
  });

  it("treats an HTTP error response as a query failure", async () => {
    renderAppShell(Promise.resolve({ error: { detail: "server error" }, response: { status: 500 } }));

    expect(await screen.findByText("无法获取 Langfuse Dashboard 地址")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Langfuse Dashboard/ })).not.toBeInTheDocument();
  });

  it.each(["javascript:alert(1)", "//evil.example.com/path", "not a URL"])(
    "rejects unsafe or malformed API URLs (%s)",
    async (url) => {
      renderAppShell(success(systemInfo(url)));

      expect(await screen.findByText("Langfuse Dashboard 地址无效")).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /Langfuse Dashboard/ })).not.toBeInTheDocument();
    },
  );

  it("keeps a previously cached valid link during a failed background refresh", async () => {
    const url = "https://observability.example.com/langfuse/";
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const queryKey = queryKeys.system.info();
    queryClient.setQueryData(queryKey, systemInfo(url));
    vi.mocked(api.GET).mockImplementationOnce(async () => { throw new Error("refresh failed"); });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/agents"]}>
          <Routes>
            <Route path="/" element={<AppShell />}>
              <Route path="agents" element={<div>Agents page</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const link = await screen.findByRole("link", { name: /Langfuse Dashboard/ });
    expect(link).toHaveAttribute("href", url);
    expect(api.GET).not.toHaveBeenCalled();

    await queryClient.invalidateQueries({ queryKey });

    expect(api.GET).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      const queryState = queryClient.getQueryState(queryKey);
      expect(queryState?.fetchStatus).toBe("idle");
      expect(queryState?.error).toBeInstanceOf(Error);
    });
    expect(screen.getByRole("link", { name: /Langfuse Dashboard/ })).toHaveAttribute("href", url);
  });
});
