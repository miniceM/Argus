import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { LaunchesList, LAUNCH_STATUSES } from "../../features/launches/LaunchesList";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    GET: vi.fn(),
  },
}));

describe("LaunchesList High-Density Table & Information Hierarchy (Issue #26)", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.clearAllMocks();
  });

  const mockLaunches = [
    {
      id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      name: "run-fraud-regression",
      status: "RUNNING",
      quality_conclusion: "unknown",
      dataset_name: "financial-fraud-dataset-long-name",
      dataset_version: "2026-09-24T00:00:00Z",
      agent_id: "financial-fraud-detection-assistant",
      agent_version: "v2.1.0",
      agent_version_id: "ver-1",
      manifest: {},
      langfuse_sync_status: "PENDING",
      created_at: "2026-09-24T10:00:00Z",
      progress: {
        total: 20,
        completed: 15,
        percentage: 75.0,
      },
    },
    {
      id: "8a2df9aa-f370-4f52-8700-60b616782d49",
      name: "run-simple",
      status: "COMPLETED",
      quality_conclusion: "pass",
      dataset_name: "simple-eval",
      dataset_version: "v1.0",
      agent_id: "unregistered-agent",
      agent_version: "1.0.0",
      agent_version_id: "ver-2",
      manifest: {},
      langfuse_sync_status: "SYNCED",
      created_at: "2026-09-24T09:00:00Z",
    },
  ];

  const mockAgents = [
    {
      id: "financial-fraud-detection-assistant",
      name: "反欺诈风控助手",
      status: "active",
      version_count: 2,
    },
  ];

  it("renders table with fixed layout, min-w-[1080px], and simplified headers", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches") {
        return Promise.resolve({ data: mockLaunches });
      }
      if (path === "/api/v1/agents") {
        return Promise.resolve({ data: mockAgents });
      }
      return Promise.resolve({ data: null });
    });

    render(
describe("LaunchesList Status Filter Contract and Behavior (Issue #24)", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    vi.clearAllMocks();
    queryClient = new QueryClient({
      defaultOptions: {
        queries: {
          retry: false,
        },
      },
    });
    vi.mocked(api.GET).mockResolvedValue({
      data: [],
      error: undefined,
      response: new Response(),
    } as any);
  });

  const renderComponent = () => {
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <LaunchesList />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByRole("table")).toBeInTheDocument();
    });

    const table = screen.getByRole("table");
    expect(table).toHaveClass("table-fixed");
    expect(table).toHaveClass("min-w-[1080px]");

    // Simplified headers without long verbose titles
    expect(screen.getByRole("columnheader", { name: "Launch" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Agent" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Dataset" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "状态" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "质量" })).toBeInTheDocument();
  });

  it("displays truncated Launch ID, provides copy button with accessible label and clipboard action", async () => {
    const writeTextMock = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, {
      clipboard: {
        writeText: writeTextMock,
      },
    });

    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches") {
        return Promise.resolve({ data: mockLaunches });
      }
      if (path === "/api/v1/agents") {
        return Promise.resolve({ data: mockAgents });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <LaunchesList />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText("3fa85f64-5717-4562-b3fc-2c963f66afa6")).toBeInTheDocument();
    });

    const idLink = screen.getByText("3fa85f64-5717-4562-b3fc-2c963f66afa6");
    expect(idLink).toHaveAttribute("title", "3fa85f64-5717-4562-b3fc-2c963f66afa6");
    expect(idLink).toHaveClass("truncate");

    // Copy button with aria-label
    const copyBtn = screen.getByRole("button", {
      name: "复制 Launch ID 3fa85f64-5717-4562-b3fc-2c963f66afa6",
    });
    expect(copyBtn).toBeInTheDocument();
    expect(copyBtn).toHaveClass("size-7");

    // Click copy
    await waitFor(() => {
      fireEvent.click(copyBtn);
    });
    expect(writeTextMock).toHaveBeenCalledWith("3fa85f64-5717-4562-b3fc-2c963f66afa6");
  });

  it("prioritizes readable agent name in 2-line layout and falls back to agent_id when unmapped", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches") {
        return Promise.resolve({ data: mockLaunches });
      }
      if (path === "/api/v1/agents") {
        return Promise.resolve({ data: mockAgents });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <LaunchesList />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText("反欺诈风控助手")).toBeInTheDocument();
    });

    // 1st row has registered agent: displays name + version badge in line 1, agent_id in line 2
    expect(screen.getByText("反欺诈风控助手")).toBeInTheDocument();
    expect(screen.getByText("v2.1.0")).toBeInTheDocument();
    expect(screen.getByText("financial-fraud-detection-assistant")).toBeInTheDocument();

    // 2nd row has unregistered agent: falls back to agent_id as primary display
    expect(screen.getByText("unregistered-agent")).toBeInTheDocument();
    expect(screen.getByText("1.0.0")).toBeInTheDocument();
  });

  it("handles agent query failure gracefully as non-blocking enrichment", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches") {
        return Promise.resolve({ data: mockLaunches });
      }
      if (path === "/api/v1/agents") {
        return Promise.resolve({ error: { message: "Internal server error" } });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <LaunchesList />
        </MemoryRouter>
      </QueryClientProvider>
    );

    // List renders normally without crashing or showing error state
    await waitFor(() => {
      expect(screen.getByText("financial-fraud-detection-assistant")).toBeInTheDocument();
    });
    expect(screen.queryByText("Internal server error")).not.toBeInTheDocument();
  });

  it("renders status and progress in a single compact row", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches") {
        return Promise.resolve({ data: mockLaunches });
      }
      if (path === "/api/v1/agents") {
        return Promise.resolve({ data: mockAgents });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <LaunchesList />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText("75% · 15/20")).toBeInTheDocument();
    });
    const progressSpan = screen.getByText("75% · 15/20");
    expect(progressSpan.parentElement).toHaveClass("whitespace-nowrap");
  };

  it("status filter options must exactly match the 8 canonical LaunchStatus enum values", () => {
    renderComponent();

    const selectElements = screen.getAllByRole("combobox");
    // The first select is execution status, the second is quality conclusion
    const statusSelect = selectElements[0] as HTMLSelectElement;
    expect(statusSelect).toBeInTheDocument();

    const options = Array.from(statusSelect.options);
    const nonDefaultOptionValues = options.slice(1).map((opt) => opt.value);

    // Expected exactly 8 canonical states
    const expectedStatuses = [
      "PENDING",
      "QUEUED",
      "RUNNING",
      "COMPLETED",
      "PARTIAL_FAILED",
      "FAILED",
      "CANCELLING",
      "CANCELLED",
    ];

    expect(nonDefaultOptionValues).toEqual(expectedStatuses);
    expect(LAUNCH_STATUSES).toEqual(expectedStatuses);
  });

  it("must include COMPLETED option and must NOT include RETRY_WAIT or SUCCEEDED", () => {
    renderComponent();

    const statusSelect = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
    const optionValues = Array.from(statusSelect.options).map((opt) => opt.value);

    // Must include COMPLETED
    expect(optionValues).toContain("COMPLETED");

    const completedOption = Array.from(statusSelect.options).find(
      (opt) => opt.value === "COMPLETED"
    );
    expect(completedOption?.textContent).toContain("COMPLETED (已完成)");

    // Must NOT include RETRY_WAIT (Item status) or legacy SUCCEEDED
    expect(optionValues).not.toContain("RETRY_WAIT");
    expect(optionValues).not.toContain("SUCCEEDED");
  });

  it("triggers query with status=COMPLETED when COMPLETED is selected", async () => {
    renderComponent();

    const statusSelect = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
    fireEvent.change(statusSelect, { target: { value: "COMPLETED" } });

    await waitFor(() => {
      expect(api.GET).toHaveBeenCalledWith(
        "/api/v1/experiment-launches",
        expect.objectContaining({
          params: expect.objectContaining({
            query: expect.objectContaining({
              status: "COMPLETED",
            }),
          }),
        })
      );
    });
  });

  it("clears status filter and resets API query when reset button is clicked", async () => {
    renderComponent();

    const statusSelect = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
    fireEvent.change(statusSelect, { target: { value: "COMPLETED" } });

    // The reset button should appear when a filter is active
    const resetButton = await screen.findByRole("button", { name: "重置筛选" });
    expect(resetButton).toBeInTheDocument();

    fireEvent.click(resetButton);

    expect(statusSelect.value).toBe("");
    await waitFor(() => {
      expect(api.GET).toHaveBeenLastCalledWith(
        "/api/v1/experiment-launches",
        expect.objectContaining({
          params: expect.objectContaining({
            query: expect.objectContaining({
              status: undefined,
            }),
          }),
        })
      );
    });
  });
});
