import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { CreateLaunch } from "../CreateLaunch";
import { api } from "../../../api/client";

vi.mock("../../../api/client", () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}));

const baselineIds = ["escalation_match", "intent_match", "pii_safe", "required_tool_match"];

const version = (v: string) => ({
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
});

const itemEvaluator = (id: string, description: string) => ({
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
});

const evaluators = baselineIds.map((id) => itemEvaluator(id, `${id} diagnostic`));

describe("CreateLaunch Presets and Progressive Disclosure (Issue #229)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/agents") {
        return Promise.resolve({
          data: [{ id: "agent-1", name: "Agent One", version_count: 1, latest_version: "v1" }],
        });
      }
      if (path === "/api/v1/agent-versions") {
        return Promise.resolve({
          data: [{ id: "ver-1", agent_id: "agent-1", version: "v1", is_active: true }],
        });
      }
      if (path === "/api/v1/evaluators") {
        return Promise.resolve({ data: evaluators });
      }
      return Promise.resolve({ data: [] });
    });
  });

  const renderComponent = () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <CreateLaunch />
        </BrowserRouter>
      </QueryClientProvider>,
    );
  };

  it("defaults to full regression preset and supports quick smoke preset switching", async () => {
    renderComponent();

    // 1. Check presets container exists
    const presetsContainer = await screen.findByTestId("launch-presets-container");
    expect(presetsContainer).toBeInTheDocument();

    // 2. Default preset is full regression (recommended)
    const fullRegressionRadio = screen.getByTestId("preset-full-regression");
    expect(fullRegressionRadio).toBeChecked();

    // All 4 evaluators are selected by default
    expect(screen.getByTestId("selected-evaluators-count")).toHaveTextContent("已选 4 项");

    // 3. Switch to quick smoke preset
    const quickSmokeRadio = screen.getByTestId("preset-quick-smoke");
    fireEvent.click(quickSmokeRadio);

    // Now quick smoke is checked
    expect(quickSmokeRadio).toBeChecked();
    // Smoke preset selects only essential metrics (fewer than 4)
    await waitFor(() => {
      expect(screen.getByTestId("selected-evaluators-count")).toHaveTextContent(/已选 [1-2] 项/);
    });

    // 4. Switch back to full regression
    fireEvent.click(fullRegressionRadio);
    await waitFor(() => {
      expect(screen.getByTestId("selected-evaluators-count")).toHaveTextContent("已选 4 项");
    });
  });

  it("provides collapsible advanced configuration section for fine-tuning evaluator rules", async () => {
    renderComponent();

    // Check advanced config section exists
    const advancedSection = await screen.findByTestId("advanced-evaluator-config");
    expect(advancedSection).toBeInTheDocument();
    expect(screen.getByText("高级配置与规则微调 (Advanced Configuration)")).toBeInTheDocument();
  });
});
