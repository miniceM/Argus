import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { CreateLaunch } from "../../features/launches/CreateLaunch";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";

vi.mock("../../api/client", () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}));

const baselineIds = ["escalation_match", "intent_match", "pii_safe", "required_tool_match"];
const evaluators = [
  ...baselineIds.map((id) => ({
    id,
    version: "1.0.0",
    scope: "item",
    threshold: 1.0,
    description: `${id} diagnostic`,
    default_selected: true,
    composed_of: [],
  })),
  {
    id: "overall_pass",
    version: "1.0.0",
    scope: "item",
    threshold: 1.0,
    description: "Legacy composite",
    default_selected: false,
    composed_of: baselineIds,
  },
  {
    id: "run_pass_rate",
    version: "1.0.0",
    scope: "run",
    threshold: 1.0,
    description: "Launch aggregate",
    default_selected: false,
    composed_of: [],
  },
];

const renderCreateLaunch = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <CreateLaunch />
      </BrowserRouter>
    </QueryClientProvider>
  );
  return { ...result, queryClient };
};

const setupApiMocks = () => {
  vi.mocked(api.GET).mockImplementation((path: string) => {
    if (path === "/api/v1/agents") {
      return Promise.resolve({
        data: [{ id: "agent-1", name: "Agent One", version_count: 1, latest_version: "v1" }],
      }) as never;
    }
    if (path === "/api/v1/agent-versions") {
      return Promise.resolve({
        data: [{ id: "ver-1", agent_id: "agent-1", version: "v1", is_active: true }],
      }) as never;
    }
    if (path === "/api/v1/evaluators") {
      return Promise.resolve({ data: evaluators }) as never;
    }
    return Promise.resolve({ data: [] }) as never;
  });
};

describe("CreateLaunch Evaluator selection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupApiMocks();
  });
  afterEach(() => cleanup());

  it("defaults to the four diagnostic evaluators and keeps run-scope evaluators disabled", async () => {
    renderCreateLaunch();

    await waitFor(() => expect(screen.getByText("已选 4 项")).toBeInTheDocument());
    expect(screen.getByRole("radio", { name: /逐项诊断/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /复合结论/ })).not.toBeChecked();
    for (const id of baselineIds) {
      expect(screen.getByRole("checkbox", { name: new RegExp(id) })).toBeChecked();
    }
    expect(screen.getByRole("radio", { name: /复合结论/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /run_pass_rate/ })).toBeDisabled();
    expect(screen.getByText(/执行成功不等于质量通过/)).toBeInTheDocument();
  });

  it("switches exclusively between diagnostic and composite results", async () => {
    renderCreateLaunch();
    await waitFor(() => expect(screen.getByText("已选 4 项")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("radio", { name: /复合结论/ }));
    expect(screen.getByText("已选 1 项")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /复合结论/ })).toBeChecked();
    for (const id of baselineIds) {
      expect(screen.queryByRole("checkbox", { name: id })).not.toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("radio", { name: /逐项诊断/ }));
    expect(screen.getByText("已选 4 项")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /复合结论/ })).not.toBeChecked();
  });

  it("preserves an empty selection and blocks submission instead of selecting everything again", async () => {
    renderCreateLaunch();
    await waitFor(() => expect(screen.getByText("已选 4 项")).toBeInTheDocument());

    for (const id of baselineIds) {
      fireEvent.click(screen.getByRole("checkbox", { name: new RegExp(id) }));
    }
    await waitFor(() => expect(screen.getByText("已选 0 项")).toBeInTheDocument());
    expect(screen.getByText("已选 0 项")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /创建评测任务/ }));
    await waitFor(() => expect(screen.getByText("请至少选择一个评测指标 (Evaluator)")).toBeInTheDocument());
    expect(api.POST).not.toHaveBeenCalled();
  });

  it("submits only overall_pass in composite mode", async () => {
    vi.mocked(api.POST).mockResolvedValue({ data: { id: "launch-created-1", status: "PENDING" } } as never);
    renderCreateLaunch();
    await waitFor(() => expect(screen.getByText("已选 4 项")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("radio", { name: /复合结论/ }));
    fireEvent.click(screen.getByRole("button", { name: /创建评测任务/ }));

    await waitFor(() => {
      expect(api.POST).toHaveBeenCalledWith("/api/v1/experiment-launches", {
        body: expect.objectContaining({
          agent_id: "agent-1",
          agent_version: "v1",
          evaluator_ids: ["overall_pass"],
        }),
      });
    });
  });

  it("allows a custom diagnostic subset without adding the composite", async () => {
    vi.mocked(api.POST).mockResolvedValue({ data: { id: "launch-created-2", status: "PENDING" } } as never);
    renderCreateLaunch();
    await waitFor(() => expect(screen.getByText("已选 4 项")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("checkbox", { name: /intent_match/ }));
    fireEvent.click(screen.getByRole("button", { name: /创建评测任务/ }));

    await waitFor(() => {
      expect(api.POST).toHaveBeenCalledWith("/api/v1/experiment-launches", {
        body: expect.objectContaining({
          evaluator_ids: ["escalation_match", "pii_safe", "required_tool_match"],
        }),
      });
    });
  });

  it("does not default newly-added non-composite evaluators unless the API marks them", async () => {
    vi.mocked(api.GET).mockImplementation((path: string) => {
      if (path === "/api/v1/evaluators") {
        return Promise.resolve({
          data: [...evaluators, {
            id: "new_quality_check",
            version: "1.0.0",
            scope: "item",
            threshold: 1.0,
            description: "Optional diagnostic",
            default_selected: false,
            composed_of: [],
          }],
        }) as never;
      }
      if (path === "/api/v1/agents") {
        return Promise.resolve({ data: [{ id: "agent-1", name: "Agent One", version_count: 1, latest_version: "v1" }] }) as never;
      }
      if (path === "/api/v1/agent-versions") {
        return Promise.resolve({ data: [{ id: "ver-1", agent_id: "agent-1", version: "v1", is_active: true }] }) as never;
      }
      return Promise.resolve({ data: [] }) as never;
    });
    renderCreateLaunch();

    await waitFor(() => expect(screen.getByText("已选 4 项")).toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: /new_quality_check/ })).not.toBeChecked();
  });

  it("requires re-selection when a selected evaluator disappears from a refreshed catalog", async () => {
    const { queryClient } = renderCreateLaunch();
    await waitFor(() => expect(screen.getByText("已选 4 项")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("radio", { name: /复合结论/ }));

    queryClient.setQueryData(queryKeys.evaluators.list(), evaluators.filter((e) => e.id !== "overall_pass"));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Evaluator 目录已变化"));
    expect(screen.getByRole("button", { name: /创建评测任务/ })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "重新选择逐项诊断默认指标" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(screen.getByText("已选 4 项")).toBeInTheDocument();
  });
});
