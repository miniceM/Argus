import { beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AgentsList } from "../../features/agents/AgentsList";
import { AgentDetail } from "../../features/agents/AgentDetail";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
    DELETE: vi.fn(),
  },
}));

const agent = {
  id: "issue-23-agent",
  name: "Issue 23 Agent",
  description: "",
  owner: "",
  status: "ACTIVE",
  version_count: 1,
  latest_version: "v1",
  launch_count: 5,
  active_launch_count: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const renderWithProviders = (ui: React.ReactNode, initialEntry = "/agents") => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>{ui}</MemoryRouter>
    </QueryClientProvider>
  );
};

describe("Agent active launch count terminology", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("labels the active launch count as active evaluations on the agents list", async () => {
    (api.GET as any).mockResolvedValue({ data: [agent] });

    renderWithProviders(<AgentsList />);

    expect(await screen.findByText("1 条活跃评测")).toBeInTheDocument();
    expect(screen.queryByText("1 运行中")).not.toBeInTheDocument();
  });

  it("uses the same active evaluation label on the agent detail page", async () => {
    (api.GET as any).mockImplementation(async (path: string) => {
      if (path === "/api/v1/agents") return { data: agent };
      if (path === "/api/v1/agent-versions") return { data: [] };
      throw new Error(`Unexpected API path: ${path}`);
    });

    renderWithProviders(
      <Routes>
        <Route path="/agents/:agentId" element={<AgentDetail />} />
      </Routes>,
      "/agents/issue-23-agent"
    );

    expect(await screen.findByText("1 条活跃评测")).toBeInTheDocument();
    expect(screen.queryByText("1 运行中")).not.toBeInTheDocument();
  });

  it("hides the active evaluation badge when the count is zero", async () => {
    (api.GET as any).mockResolvedValue({ data: [{ ...agent, active_launch_count: 0 }] });

    renderWithProviders(<AgentsList />);

    expect(await screen.findByText("Issue 23 Agent")).toBeInTheDocument();
    expect(screen.queryByText(/活跃评测/)).not.toBeInTheDocument();
  });

  it("also hides the active evaluation badge on agent details when the count is zero", async () => {
    (api.GET as any).mockImplementation(async (path: string) => {
      if (path === "/api/v1/agents") return { data: { ...agent, active_launch_count: 0 } };
      if (path === "/api/v1/agent-versions") return { data: [] };
      throw new Error(`Unexpected API path: ${path}`);
    });

    renderWithProviders(
      <Routes>
        <Route path="/agents/:agentId" element={<AgentDetail />} />
      </Routes>,
      "/agents/issue-23-agent"
    );

    expect(await screen.findByText("Issue 23 Agent")).toBeInTheDocument();
    expect(screen.queryByText(/活跃评测/)).not.toBeInTheDocument();
  });
});
