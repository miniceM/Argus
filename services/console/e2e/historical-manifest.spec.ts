import { test, expect } from "@playwright/test";

test.describe("E2E-02: Frozen Manifest Absolute Immutability Verification", () => {
  test("historical launch displays frozen manifest even after agent updates in registry", async ({ page }) => {
    const historicalLaunchId = "launch-hist-20260901-001";
    const frozenEndpoint = "http://demo-agent-v1.legacy.svc:8080/invoke";
    const currentLatestEndpoint = "http://demo-agent-v2.current.svc:8080/invoke";

    const frozenManifest = {
      manifest_version: "1.0",
      dataset: {
        name: "calc-agent-regression",
        version: "2026-09-01T00:00:00Z",
      },
      agent: {
        id: "calc-agent",
        version: "1.0.0",
        endpoint: frozenEndpoint,
        method: "POST",
        spec_digest: "sha256:111111111111111111111111",
        credential_ref: "vault:secrets/calc#v1",
        execution_policy: {
          timeout_seconds: 15,
          max_retries: 1,
          rate_limit_per_minute: 30,
          max_concurrency: 1,
          is_idempotent: true,
        },
      },
      evaluators: [
        { name: "exact_match", version: "1.0.0", type: "deterministic" },
      ],
      runner: {
        runner_version: "0.1.0",
        concurrency: 1,
      },
      created_at: "2026-09-01T00:00:00Z",
    };

    const historicalLaunch = {
      id: historicalLaunchId,
      name: "calc-agent-regression-run-1",
      status: "SUCCEEDED",
      quality_conclusion: "PASS",
      dataset_name: "calc-agent-regression",
      dataset_version: "2026-09-01T00:00:00Z",
      agent_id: "calc-agent",
      agent_version: "1.0.0",
      agent_version_id: "ver-100",
      manifest: frozenManifest,
      langfuse_experiment_url: "http://localhost:3000/project/poc-project/experiments/exp-hist-1",
      langfuse_sync_status: "SYNCED",
      created_at: "2026-09-01T00:00:00Z",
      started_at: "2026-09-01T00:00:01Z",
      completed_at: "2026-09-01T00:00:05Z",
    };

    // System info
    await page.route("**/api/v1/system/info", async (route) => {
      await route.fulfill({
        json: {
          service: "argus-eval-runner",
          version: "0.2.0",
          build_id: "e2e-build",
          environment: "test",
        },
      });
    });

    // Launch route
    await page.route("**/api/v1/experiment-launches**", async (route) => {
      await route.fulfill({ json: historicalLaunch });
    });

    // Items route
    await page.route("**/api/v1/experiment-launch-items**", async (route) => {
      await route.fulfill({
        json: [
          {
            id: "item-1",
            launch_id: historicalLaunchId,
            dataset_item_id: "case-001",
            execution_status: "SUCCEEDED",
            eval_status: "COMPLETED",
            quality_conclusion: "PASS",
            scores: { exact_match: 1.0 },
            attempt_count: 1,
            final_attempt_http_status: 200,
            final_attempt_latency_ms: 120,
            started_at: "2026-09-01T00:00:01Z",
          },
        ],
      });
    });

    // Notice: The current Agent Registry route has updated version 2.0.0!
    await page.route("**/api/v1/agent-versions**", async (route) => {
      await route.fulfill({
        json: [
          {
            id: "ver-200",
            agent_id: "calc-agent",
            version: "2.0.0",
            endpoint: currentLatestEndpoint,
            is_active: true,
          },
        ],
      });
    });

    // Visit historical launch detail page
    await page.goto(`/launches/${historicalLaunchId}`);

    // Verify Title & Launch ID
    await expect(page.getByRole("heading", { name: historicalLaunchId })).toBeVisible();

    // Verify Status & Quality Badges are decoupled
    await expect(page.getByTestId("status-badge").first()).toContainText("SUCCEEDED");
    await expect(page.getByTestId("quality-badge").first()).toContainText("PASS");

    // CRITICAL: The page MUST display the frozen endpoint from manifest, NOT currentLatestEndpoint
    await expect(page.getByText(frozenEndpoint)).toBeVisible();
    await expect(page.getByText(currentLatestEndpoint)).not.toBeVisible();

    // Verify 4-dimension overview is present
    await expect(page.getByText("四维不可变冻结快照")).toBeVisible();
    await expect(page.getByText("exact_match", { exact: true })).toBeVisible();

    // Verify Raw JSON Viewer toggle
    await page.getByRole("button", { name: "查看完整 Manifest JSON" }).click();
    await expect(page.getByText("Immutable Manifest JSON")).toBeVisible();
  });
});
