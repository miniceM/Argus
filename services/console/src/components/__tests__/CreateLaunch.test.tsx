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

const version = (v: string, overrides: Record<string, unknown> = {}) => ({
  version: v,
  result_type: "numeric",
  scope: "item",
  threshold: 1.0,
  direction: "higher_is_better",
  critical: false,
  input_contract: { type: "object" },
  output_contract: { type: "number" },
  param_schema: { type: "object", properties: {} },
  implementation_ref: `builtin:demo@${v}`,
  executor_type: "builtin_python",
  content_digest: `digest-${v}`,
  release_eligible: true,
  eligibility_reasons: [],
  eligibility_messages: [],
  ...overrides,
});

const itemEvaluator = (id: string, description: string, extra: Record<string, unknown> = {}) => ({
  id,
  name: `${id} name`,
  version: "1.0.0",
  scope: "item",
  threshold: 1.0,
  description,
  default_selected: true,
  composed_of: [],
  result_type: "numeric",
  definition_source: "ARGUS_BUILTIN",
  execution_owner: "ARGUS",
  implementation_ref: "builtin:demo@1.0.0",
  executor_type: "builtin_python",
  content_digest: "digest-1.0.0",
  release_eligible: true,
  eligibility_reasons: [],
  default_version: "1.0.0",
  versions: [version("1.0.0")],
  ...extra,
});

const evaluators = [
  ...baselineIds.map((id) => itemEvaluator(id, `${id} diagnostic`)),
  itemEvaluator("overall_pass", "Legacy composite", {
    default_selected: false,
    composed_of: baselineIds,
  }),
  itemEvaluator("run_pass_rate", "Launch aggregate", {
    default_selected: false,
    scope: "run",
    release_eligible: false,
    eligibility_reasons: ["EXECUTION_OWNER_NOT_ARGUS"],
    versions: [version("1.0.0", { scope: "run" })],
  }),
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

  it("toggles an evaluator with Enter without submitting the launch form", async () => {
    renderCreateLaunch();
    await waitFor(() => expect(screen.getByText("已选 4 项")).toBeInTheDocument());

    const checkbox = screen.getByRole("checkbox", { name: /escalation_match/ });
    fireEvent.keyDown(checkbox, { key: "Enter", code: "Enter" });

    expect(checkbox).not.toBeChecked();
    expect(screen.getByText("已选 3 项")).toBeInTheDocument();
    expect(api.POST).not.toHaveBeenCalled();

    fireEvent.keyDown(checkbox, { key: "Enter", code: "Enter", repeat: true });
    expect(checkbox).not.toBeChecked();
    expect(screen.getByText("已选 3 项")).toBeInTheDocument();

    fireEvent.keyDown(checkbox, { key: "Enter", code: "Enter" });
    expect(checkbox).toBeChecked();
    expect(screen.getByText("已选 4 项")).toBeInTheDocument();
    expect(api.POST).not.toHaveBeenCalled();
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
          // Issue #80: the exact user-confirmed version is submitted.
          evaluator_selections: [{ id: "overall_pass", version: "1.0.0" }],
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
          evaluator_selections: [
            { id: "escalation_match", version: "1.0.0" },
            { id: "pii_safe", version: "1.0.0" },
            { id: "required_tool_match", version: "1.0.0" },
          ],
        }),
      });
    });
  });

  it("does not default newly-added non-composite evaluators unless the API marks them", async () => {
    vi.mocked(api.GET).mockImplementation((path: string) => {
      if (path === "/api/v1/evaluators") {
        return Promise.resolve({
          data: [...evaluators, itemEvaluator("new_quality_check", "Optional diagnostic", {
            default_selected: false,
          })],
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

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("当前评测指标版本不可用")
    );
    expect(screen.getByRole("button", { name: /创建评测任务/ })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "重新选择当前可用的默认指标版本" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(screen.getByText("已选 4 项")).toBeInTheDocument();
  });
});

describe("CreateLaunch exact Evaluator version selection (Issue #80)", () => {
  const twoVersions = [
    itemEvaluator("intent_match", "intent diagnostic", {
      versions: [version("1.0.0"), version("2.0.0")],
    }),
  ];

  const mockCatalog = (catalog: unknown[]) => {
    vi.mocked(api.GET).mockImplementation((path: string) => {
      if (path === "/api/v1/evaluators") return Promise.resolve({ data: catalog }) as never;
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
      return Promise.resolve({ data: [] }) as never;
    });
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(() => cleanup());

  it("submits the exact version the user selected, not the catalog default", async () => {
    mockCatalog(twoVersions);
    vi.mocked(api.POST).mockResolvedValue({ data: { id: "launch-v1", status: "PENDING" } } as never);
    renderCreateLaunch();
    await waitFor(() => expect(screen.getByText("已选 1 项")).toBeInTheDocument());

    // The catalog default is 1.0.0; the user explicitly moves to 2.0.0.
    fireEvent.change(screen.getByLabelText("intent_match 版本"), { target: { value: "2.0.0" } });
    fireEvent.click(screen.getByRole("button", { name: /创建评测任务/ }));

    await waitFor(() => {
      expect(api.POST).toHaveBeenCalledWith("/api/v1/experiment-launches", {
        body: expect.objectContaining({
          evaluator_selections: [{ id: "intent_match", version: "2.0.0" }],
        }),
      });
    });
  });

  it("keeps the confirmed version when a catalog refresh promotes a new default", async () => {
    mockCatalog(twoVersions);
    const { queryClient } = renderCreateLaunch();
    await waitFor(() => expect(screen.getByText("已选 1 项")).toBeInTheDocument());

    // A later catalog revision defaults the evaluator to 2.0.0.
    queryClient.setQueryData(
      queryKeys.evaluators.list(),
      [
        itemEvaluator("intent_match", "intent diagnostic", {
          default_version: "2.0.0",
          versions: [version("1.0.0"), version("2.0.0")],
        }),
      ],
    );

    await waitFor(() =>
      expect(screen.getByLabelText("intent_match 版本")).toHaveValue("1.0.0")
    );
  });

  it("surfaces contract, executor and artifact identity for the selected version", async () => {
    mockCatalog(twoVersions);
    renderCreateLaunch();
    await waitFor(() => expect(screen.getByText("已选 1 项")).toBeInTheDocument());

    expect(screen.getByText("可用于发布评测")).toBeInTheDocument();
    expect(screen.getByText(/结果类型：numeric/)).toBeInTheDocument();

    fireEvent.click(screen.getByText("查看输入/输出契约与制品标识"));
    expect(screen.getByText(/builtin:demo@1.0.0/)).toBeInTheDocument();
    expect(screen.getByText(/digest-1.0.0/)).toBeInTheDocument();
    expect(screen.getByText(/builtin_python/)).toBeInTheDocument();
  });

  it("blocks submission and explains why when the pinned version is ineligible", async () => {
    mockCatalog([
      itemEvaluator("intent_match", "intent diagnostic", {
        default_version: "1.0.0",
        release_eligible: false,
        eligibility_reasons: ["EXECUTION_OWNER_NOT_ARGUS"],
        versions: [
          version("1.0.0", {
            release_eligible: false,
            eligibility_reasons: ["EXECUTION_OWNER_NOT_ARGUS"],
            eligibility_messages: ["该版本由 Langfuse 在线执行，无法作为发布评测证据。"],
          }),
        ],
      }),
    ]);
    renderCreateLaunch();

    await waitFor(() => expect(screen.getByText("不可用于发布评测")).toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent(
      "该版本由 Langfuse 在线执行，无法作为发布评测证据。"
    );
    expect(screen.getByRole("button", { name: /创建评测任务/ })).toBeDisabled();
    expect(api.POST).not.toHaveBeenCalled();
  });

  it("blocks submission when the pinned version disappears from the catalog", async () => {
    mockCatalog(twoVersions);
    const { queryClient } = renderCreateLaunch();
    await waitFor(() => expect(screen.getByText("已选 1 项")).toBeInTheDocument());

    queryClient.setQueryData(queryKeys.evaluators.list(), [
      itemEvaluator("intent_match", "intent diagnostic", {
        versions: [version("2.0.0")],
        default_version: "2.0.0",
      }),
    ]);

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("版本 1.0.0 已不在当前目录中")
    );
    expect(screen.getByRole("button", { name: /创建评测任务/ })).toBeDisabled();
  });
});
