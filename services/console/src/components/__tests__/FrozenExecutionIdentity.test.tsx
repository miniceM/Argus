import { afterEach, describe, it, expect, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { LaunchDetail } from "../../features/launches/LaunchDetail";
import { ItemTable } from "../../features/launches/ItemTable";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: { GET: vi.fn(), POST: vi.fn() },
}));

afterEach(cleanup);

const frozenBinding = {
  id: "intent_match",
  version: "1.0.0",
  scope: "item",
  threshold: 1,
  binding_id: "bind_0123456789abcdef",
  binding_digest: "sha256:" + "a".repeat(64),
  binding_schema_version: "1.2",
  definition_digest: "sha256:" + "b".repeat(64),
  content_digest: "b".repeat(64),
  implementation_ref: "builtin:intent_match@1.0.0",
  executor_type: "builtin_python",
  contract_status: "FROZEN_VERIFIED",
  verification_status: "RECORDED",
  implementation_artifact: {
    kind: "python_source",
    locator: "app.evaluators:intent_match",
    digest: "sha256:" + "c".repeat(64),
    runtime: "cpython",
  },
  runner: { runner_version: "0.1.0", build_id: "build-81", mapping_engine_version: "sha256-mapping-engine-v1" },
};

const launchBase = {
  id: "launch-issue-81",
  name: "frozen identity launch",
  status: "PENDING",
  quality_conclusion: "unknown",
  dataset_name: "banking-regression",
  dataset_version: "2026-09-20T00:00:00Z",
  agent_id: "banking-agent",
  agent_version: "v2",
  langfuse_sync_status: "PENDING",
  langfuse_experiment_url: null,
  created_at: "2026-09-20T00:00:00Z",
  started_at: null,
  completed_at: null,
};

function renderDetail(manifest: Record<string, unknown>, items: unknown[] = []) {
  (api.GET as any).mockImplementation((path: string) => {
    if (path === "/api/v1/experiment-launches/{launch_id}") {
      return Promise.resolve({ data: { ...launchBase, manifest } });
    }
    if (path === "/api/v1/experiment-launches/{launch_id}/items") {
      return Promise.resolve({ data: items });
    }
    return Promise.resolve({ data: null });
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/launches/launch-issue-81?tab=audit"]}>
        <Routes>
          <Route path="/launches/:launchId" element={<LaunchDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Issue #81: frozen evaluation execution identity in Launch Detail", () => {
  it("shows the frozen version, implementation ref and verification status", async () => {
    renderDetail({
      schema_version: "1.2",
      dataset: { dataset_name: "banking-regression", items_count: 2 },
      agent: { id: "banking-agent", version: "v2" },
      evaluators: [frozenBinding],
      execution_policy: { max_concurrency: 2, timeout_seconds: 30, max_retries: 1 },
      runner: { runner_version: "0.1.0", mapping_engine_version: "sha256-mapping-engine-v1" },
    });

    await waitFor(() => {
      expect(screen.getByTestId("binding-verification-intent_match")).toHaveTextContent("已冻结校验");
    });
    expect(screen.getByText("v1.0.0")).toBeInTheDocument();
    expect(screen.getByText("builtin:intent_match@1.0.0")).toBeInTheDocument();
    expect(
      screen.getByText(/冻结身份在创建时校验、执行前再次校验；版本或制品不可用时评测会明确停止并保持 UNKNOWN，不会改用其他版本。/),
    ).toBeInTheDocument();
  });

  it("exposes the full digests and artifact identity on demand", async () => {
    renderDetail({
      schema_version: "1.2",
      dataset: { items_count: 1 },
      agent: { id: "banking-agent", version: "v2" },
      evaluators: [frozenBinding],
      execution_policy: { max_concurrency: 1, timeout_seconds: 30, max_retries: 1 },
      runner: { runner_version: "0.1.0" },
    });

    await waitFor(() => expect(screen.getByTestId("binding-verification-intent_match")).toBeInTheDocument());
    fireEvent.click(screen.getByText("查看冻结摘要与制品标识"));
    expect(screen.getByText("bind_0123456789abcdef")).toBeInTheDocument();
    expect(screen.getByText("sha256:" + "a".repeat(64))).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, node) => node?.textContent === `sha256:${"c".repeat(64)} (app.evaluators:intent_match)`,
      ),
    ).toBeInTheDocument();
  });

  it("reports a legacy Manifest as 历史契约未记录 instead of claiming verification", async () => {
    renderDetail({
      schema_version: "1.1",
      dataset: { items_count: 2 },
      agent: { id: "banking-agent", version: "v2" },
      evaluators: [{ id: "pii_safe", version: "1.0.0", scope: "item" }],
      execution_policy: { max_concurrency: 1, timeout_seconds: 30, max_retries: 1 },
      runner: { runner_version: "0.1.0" },
    });

    await waitFor(() => {
      expect(screen.getByTestId("binding-verification-pii_safe")).toHaveTextContent("历史契约未记录");
    });
    expect(screen.queryByText("查看冻结摘要与制品标识")).not.toBeInTheDocument();
  });

  it("marks a binding without artifact evidence as 制品未记录", async () => {
    renderDetail({
      schema_version: "1.2",
      dataset: { items_count: 1 },
      agent: { id: "banking-agent", version: "v2" },
      evaluators: [{ ...frozenBinding, implementation_artifact: null }],
      execution_policy: { max_concurrency: 1, timeout_seconds: 30, max_retries: 1 },
      runner: { runner_version: "0.1.0" },
    });

    await waitFor(() => {
      expect(screen.getByTestId("binding-verification-intent_match")).toHaveTextContent("制品未记录");
    });
  });
});

describe("Issue #81: frozen identity failure explains the recovery path", () => {
  const baseItem = {
    id: "item-x",
    launch_id: "launch-issue-81",
    dataset_item_id: "case-001",
    execution_status: "failed",
    eval_status: "skipped",
    quality_conclusion: "unknown",
    execution_error: null,
    eval_error: null,
    trace_id: null,
    observation_id: null,
    final_attempt_id: null,
    scores: {},
    attempt_count: 0,
    final_attempt_http_status: null,
    final_attempt_latency_ms: null,
    started_at: null,
    completed_at: null,
  };

  const renderItems = (items: unknown[]) =>
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ItemTable items={items as any} />
      </QueryClientProvider>,
    );

  it("shows the stable error code and recovery advice for a missing frozen version", () => {
    renderItems([
      { ...baseItem, execution_error: "EVALUATOR_VERSION_UNAVAILABLE: intent_match@1.0.0" },
    ]);
    expect(screen.getByText(/EVALUATOR_VERSION_UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getByTestId("frozen-recovery-case-001")).toHaveTextContent(
      "恢复与冻结版本一致的评测制品后重试",
    );
  });

  it("explains tampering instead of silently re-running another version", () => {
    renderItems([
      { ...baseItem, execution_error: "EVALUATOR_ARTIFACT_DIGEST_MISMATCH: expected=sha256:aa actual=sha256:bb" },
    ]);
    expect(screen.getByTestId("frozen-recovery-case-001")).toHaveTextContent("存在篡改或版本漂移");
  });

  it("leaves ordinary failures untouched", () => {
    renderItems([{ ...baseItem, execution_error: "HTTP 504 upstream timeout" }]);
    expect(screen.queryByTestId("frozen-recovery-case-001")).not.toBeInTheDocument();
  });
});
