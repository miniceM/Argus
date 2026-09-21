import { test, expect } from "@playwright/test";

test.describe("E2E-03 ~ E2E-05: Launch Creation, Execution, Dual Badges and Attempt Drawer", () => {
  test("creates launch, runs evaluation, verifies decoupled dual badges and lazy attempts", async ({ page }) => {
    const launchId = `launch-e2e-${Date.now()}`;
    const langfuseUrl = "http://localhost:3000/project/poc-project/experiments/exp-e2e-123";

    let currentLaunchStatus = "PENDING";
    let currentQualityConclusion = "UNKNOWN";

    const launchObj = {
      id: launchId,
      name: `run-${launchId}`,
      status: currentLaunchStatus,
      quality_conclusion: currentQualityConclusion,
      dataset_name: "calc-agent-eval",
      dataset_version: "latest",
      agent_id: "demo-banking-agent",
      agent_version: "1.0.0",
      agent_version_id: "ver-1",
      manifest: {
        manifest_version: "1.0",
        dataset: { name: "calc-agent-eval", version: "latest" },
        agent: {
          id: "demo-banking-agent",
          version: "1.0.0",
          endpoint: "http://demo-agent:8080/invoke",
          execution_policy: { timeout_seconds: 30, max_retries: 2 },
        },
        evaluators: [{ name: "exact_match", version: "1.0.0", type: "deterministic" }],
        runner: { runner_version: "0.1.0", concurrency: 2 },
      },
      langfuse_experiment_url: null as string | null,
      langfuse_sync_status: "PENDING",
      created_at: new Date().toISOString(),
      started_at: null as string | null,
      completed_at: null as string | null,
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

    // Agents & Versions for form dropdown
    await page.route("**/api/v1/agents", async (route) => {
      await route.fulfill({
        json: [
          {
            id: "demo-banking-agent",
            name: "Banking Core Agent",
            status: "active",
            version_count: 1,
            latest_version: "1.0.0",
            created_at: new Date().toISOString(),
          },
        ],
      });
    });

    await page.route("**/api/v1/agent-versions**", async (route) => {
      await route.fulfill({
        json: [
          {
            id: "ver-1",
            agent_id: "demo-banking-agent",
            version: "1.0.0",
            endpoint: "http://demo-agent:8080/invoke",
            is_active: true,
          },
        ],
      });
    });

    // Evaluators specs
    await page.route("**/api/v1/evaluators", async (route) => {
      await route.fulfill({
        json: [
          {
            id: "exact_match",
            version: "1.0.0",
            scope: "output",
            threshold: 1.0,
            description: "精确匹配期望输出",
          },
        ],
      });
    });

    // Launch run endpoint
    await page.route("**/api/v1/experiment-launches/run**", async (route) => {
      // Simulate execution completion
      currentLaunchStatus = "SUCCEEDED";
      currentQualityConclusion = "FAIL"; // Intentionally FAIL to test dual-status decoupling!
      launchObj.status = "SUCCEEDED";
      launchObj.quality_conclusion = "FAIL";
      launchObj.langfuse_experiment_url = langfuseUrl;
      launchObj.started_at = new Date(Date.now() - 3000).toISOString();
      launchObj.completed_at = new Date().toISOString();
      await route.fulfill({ json: launchObj });
    });

    // Launches endpoints (captures query strings like ?id=...)
    await page.route("**/api/v1/experiment-launches**", async (route) => {
      if (route.request().url().includes("/run")) {
        await route.fallback();
        return;
      }
      if (route.request().method() === "POST") {
        await route.fulfill({ status: 201, json: launchObj });
        return;
      }
      launchObj.status = currentLaunchStatus;
      launchObj.quality_conclusion = currentQualityConclusion;
      await route.fulfill({ json: launchObj });
    });

    // Launch items endpoint
    await page.route("**/api/v1/experiment-launch-items**", async (route) => {
      if (currentLaunchStatus === "PENDING") {
        await route.fulfill({ json: [] });
      } else {
        await route.fulfill({
          json: [
            {
              id: "item-exec-101",
              launch_id: launchId,
              dataset_item_id: "case-transfer-01",
              execution_status: "SUCCEEDED",
              eval_status: "COMPLETED",
              quality_conclusion: "FAIL",
              scores: { exact_match: 0.0 },
              attempt_count: 2,
              final_attempt_http_status: 200,
              final_attempt_latency_ms: 185,
              started_at: new Date().toISOString(),
            },
          ],
        });
      }
    });

    // Execution attempts endpoint (lazy-loaded when drawer opens)
    let attemptsFetched = false;
    await page.route("**/api/v1/execution-attempts**", async (route) => {
      attemptsFetched = true;
      await route.fulfill({
        json: [
          {
            id: "att-1",
            item_execution_id: "item-exec-101",
            attempt_no: 1,
            status: "FAILED",
            http_status: 504,
            error_type: "GatewayTimeout",
            error_message: "Upstream agent gateway timed out",
            latency_ms: 3000,
            trace_context_received: true,
            started_at: new Date(Date.now() - 4000).toISOString(),
          },
          {
            id: "att-2",
            item_execution_id: "item-exec-101",
            attempt_no: 2,
            status: "SUCCEEDED",
            http_status: 200,
            latency_ms: 185,
            trace_context_received: true,
            started_at: new Date(Date.now() - 1000).toISOString(),
          },
        ],
      });
    });

    // 1. Visit Create Launch page
    await page.goto("/launches/new");
    await expect(page.getByRole("heading", { name: "发起新评测任务" })).toBeVisible();

    // Verify Evaluators selection
    await expect(page.getByText("exact_match")).toBeVisible();

    // Submit Launch
    await page.getByRole("button", { name: "创建评测任务" }).click();

    // 2. Navigates to Launch Detail
    await page.waitForURL(`**/launches/${launchId}`);
    await expect(page.getByRole("heading", { name: launchId })).toBeVisible();

    // Verify initial PENDING state and Run button visible
    await expect(page.getByTestId("status-badge").first()).toContainText("PENDING");
    const runBtn = page.getByRole("button", { name: "立即执行评测" });
    await expect(runBtn).toBeVisible();

    // 3. Click Run Evaluation
    await runBtn.click();

    // 4. Verify Dual-Badge Decoupling (Execution: SUCCEEDED, Quality: FAIL)
    await expect(page.getByTestId("status-badge").first()).toContainText("SUCCEEDED");
    await expect(page.getByTestId("quality-badge").first()).toContainText("FAIL");

    // 5. Verify Langfuse Deep Link from backend
    const langfuseLink = page.getByRole("link", { name: "在 Langfuse 中查看" });
    await expect(langfuseLink).toBeVisible();
    await expect(langfuseLink).toHaveAttribute("href", langfuseUrl);

    // 6. Verify Cases Table and Aggregated Attempts
    await expect(page.getByText("case-transfer-01")).toBeVisible();
    await expect(page.getByText("2 次尝试")).toBeVisible();
    expect(attemptsFetched).toBe(false); // Proves lazy loading! Attempts not fetched until requested!

    // 7. Click Attempt button to trigger lazy loading drawer
    await page.getByRole("button", { name: "2 次尝试" }).click();

    // Verify AttemptDrawer opens and shows attempts
    await expect(page.getByText("用例执行调用历史")).toBeVisible();
    expect(attemptsFetched).toBe(true); // Attempt query triggered!

    // Verify attempts content
    await expect(page.getByText("第 1 次调用尝试")).toBeVisible();
    await expect(page.getByText("HTTP 504")).toBeVisible();
    await expect(page.getByText("Upstream agent gateway timed out")).toBeVisible();

    await expect(page.getByText("第 2 次调用尝试")).toBeVisible();
    await expect(page.getByText("HTTP 200")).toBeVisible();
    await expect(page.getByText("已成功传播 traceparent").first()).toBeVisible();
  });
});
