import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { LaunchDetail } from "../../features/launches/LaunchDetail";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}));

describe("LaunchDetail Frozen Manifest Structured Audit View", () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  const mockLaunch = {
    id: "launch-freeze-001",
    name: "Audit Verification Launch",
    status: "SUCCEEDED",
    quality_conclusion: "pass",
    dataset_name: "banking-regression",
    dataset_version: "2026-09-20T00:00:00Z",
    agent_id: "banking-agent",
    agent_version: "v2",
    langfuse_sync_status: "SYNCED",
    langfuse_experiment_url: "http://localhost:3000/runs/123",
    created_at: "2026-09-20T00:00:00Z",
    started_at: "2026-09-20T00:00:01Z",
    completed_at: "2026-09-20T00:00:05Z",
    manifest: {
      schema_version: "1.0",
      dataset: {
        dataset_name: "banking-regression",
        dataset_version: "2026-09-20T00:00:00Z",
        snapshot_digest: "sha256:abcd1234efgh5678",
        items_count: 6,
      },
      agent: {
        id: "banking-agent",
        version: "v2",
        endpoint: "http://127.0.0.1:18082/invoke",
        spec_digest: "sha256:spec9876543210",
      },
      evaluators: [
        { id: "intent_match", version: "1.0.0", scope: "item" },
        { id: "pii_safe", version: "1.0.0", scope: "item" },
      ],
      execution_policy: {
        max_concurrency: 4,
        timeout_seconds: 10,
        max_retries: 2,
      },
      runner: {
        runner_version: "0.1.0",
        mapping_engine_version: "sha256-mapping-engine-v1",
      },
    },
  };

  it("renders all critical frozen snapshot audit fields structured in UI", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches") {
        return Promise.resolve({ data: mockLaunch });
      }
      if (path === "/api/v1/experiment-launch-items") {
        return Promise.resolve({ data: [] });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/launches/launch-freeze-001"]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    // 1. Schema Version badge
    await waitFor(() => {
      expect(screen.getByTestId("manifest-schema-version")).toHaveTextContent("Schema v1.0");
    });

    // 2. Langfuse Sync Status badge in primary banner
    expect(screen.getByTestId("langfuse-sync-badge")).toHaveTextContent("SYNCED");

    // 3. Dataset Snapshot Digest
    expect(screen.getByTestId("dataset-snapshot-digest")).toHaveTextContent("sha256:abcd1...");

    // 4. Runner Version & Engine
    expect(screen.getByTestId("runner-version")).toHaveTextContent("0.1.0");
    expect(screen.getByText("sha256-mapping-engine-v1")).toBeInTheDocument();

    // 5. Evaluators
    expect(screen.getByText("intent_match")).toBeInTheDocument();
    expect(screen.getByText("pii_safe")).toBeInTheDocument();
  });
});
