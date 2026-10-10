import { test, expect, type Locator } from "@playwright/test";
import { buildEvaluatorCatalog, DIAGNOSTIC_IDS } from "./fixtures/evaluators";

test.describe("E2E-03 ~ E2E-05: Launch Creation, Execution, Dual Badges and Attempt Drawer", () => {
  test("creates launch, runs evaluation, verifies decoupled dual badges and lazy attempts", async ({ page }) => {
    const launchId = `launch-e2e-${Date.now()}`;
    const langfuseUrl = "http://localhost:3000/project/poc-project/experiments/exp-e2e-123";

    let currentLaunchStatus = "PENDING";
    let currentQualityConclusion = "UNKNOWN";

    let interceptedCreationPayload: any = null;

    const launchObj = {
      id: launchId,
      name: `run-${launchId}`,
      status: currentLaunchStatus,
      quality_conclusion: currentQualityConclusion,
      dataset_name: "calc-agent-eval",
      dataset_version: "2026-09-20T00:00:00Z",
      agent_id: "demo-banking-agent",
      agent_version: "1.0.0",
      agent_version_id: "ver-1",
      manifest: {
        schema_version: "1.0",
        dataset: {
          dataset_name: "calc-agent-eval",
          dataset_version: "2026-09-20T00:00:00Z",
          snapshot_digest: "sha256:e2edigest12345678",
          items_count: 1,
        },
        agent: {
          id: "demo-banking-agent",
          version: "1.0.0",
          endpoint: "http://demo-agent:8080/invoke",
          spec_digest: "sha256:specs987654321",
        },
        evaluators: [
          { id: "escalation_match", version: "1.0.0", scope: "item" },
          { id: "intent_match", version: "1.0.0", scope: "item" },
          { id: "pii_safe", version: "1.0.0", scope: "item" },
          { id: "required_tool_match", version: "1.0.0", scope: "item" },
        ],
        execution_policy: { timeout_seconds: 30, max_retries: 2, max_concurrency: 2 },
        runner: { runner_version: "0.1.0", mapping_engine_version: "sha256-mapping-engine-v1" },
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

    // Evaluators catalog: default diagnostics, their composite, and unsupported run scope.
    await page.route("**/api/v1/evaluators", async (route) => {
      await route.fulfill({ json: buildEvaluatorCatalog() });
    });

    // Launch run endpoint (Resource-based POST /api/v1/experiment-launches/{id}/run)
    await page.route(/.*\/api\/v1\/experiment-launches\/.*run.*/, async (route) => {
      // Simulate execution completion
      currentLaunchStatus = "SUCCEEDED";
      currentQualityConclusion = "FAIL"; // Intentionally FAIL to test dual-status decoupling!
      launchObj.status = "SUCCEEDED";
      launchObj.quality_conclusion = "FAIL";
      launchObj.langfuse_experiment_url = langfuseUrl;
      launchObj.langfuse_sync_status = "SYNCED";
      launchObj.started_at = new Date(Date.now() - 3000).toISOString();
      launchObj.completed_at = new Date().toISOString();
      await route.fulfill({ json: launchObj });
    });

    // Launch collection and detail endpoints share a prefix; leave execution/items to their handlers.
    await page.route("**/api/v1/experiment-launches**", async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.endsWith("/run") || pathname.endsWith("/items")) {
        await route.fallback();
        return;
      }
      if (route.request().method() === "POST") {
        interceptedCreationPayload = route.request().postDataJSON();
        launchObj.manifest.evaluators = interceptedCreationPayload.evaluator_selections.map(
          (selection: { id: string; version: string }) => ({
            id: selection.id,
            version: selection.version,
            scope: "item",
          }),
        );
        await route.fulfill({ status: 201, json: launchObj });
        return;
      }
      launchObj.status = currentLaunchStatus;
      launchObj.quality_conclusion = currentQualityConclusion;
      await route.fulfill({ json: launchObj });
    });

    // Launch items endpoint
    await page.route(`**/api/v1/experiment-launches/${launchId}/items`, async (route) => {
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

    // Freeze the terminal result revision. A successful execution must make its immutable
    // result available, while pre-run state correctly reports an empty revision directory.
    await page.route(`**/api/v1/experiment-launches/${launchId}/result-snapshots`, async (route) => {
      if (currentLaunchStatus !== "SUCCEEDED") {
        await route.fulfill({
          json: { launch_id: launchId, latest_snapshot_id: null, latest_revision: 0, revisions: [] },
        });
        return;
      }
      await route.fulfill({
        json: {
          launch_id: launchId,
          latest_snapshot_id: "snap-launches-e2e",
          latest_revision: 1,
          revisions: [
            {
              snapshot_id: "snap-launches-e2e",
              revision: 1,
              created_at: new Date().toISOString(),
              source_result_digest: "sha256:launches-result",
              manifest_digest: "sha256:launches-manifest",
              evidence_state: "COMPLETE",
              evidence_reasons: [],
              releasable: false,
              total_cases: 1,
              quality_pass_count: 0,
              quality_fail_count: 1,
              quality_unknown_count: 0,
              is_latest: true,
            },
          ],
        },
      });
    });
    await page.route(`**/api/v1/experiment-launches/${launchId}/result-snapshots/snap-launches-e2e`, (route) =>
      route.fulfill({
        json: {
          launch_id: launchId,
          snapshot_id: "snap-launches-e2e",
          revision: 1,
          created_at: new Date().toISOString(),
          source_result_digest: "sha256:launches-result",
          manifest_digest: "sha256:launches-manifest",
          evidence_state: "COMPLETE",
          evidence_reasons: [],
          releasable: false,
          versions: {},
          summary: {},
          items: [
            {
              dataset_item_id: "case-transfer-01",
              execution_status: "succeeded",
              eval_status: "completed",
              quality_conclusion: "fail",
              scores: { exact_match: 0 },
              cost_evidence: { attempt_count: 2 },
              latency_ms: 185,
              final_attempt_id: "attempt-launches-e2e-final",
            },
          ],
        },
      }),
    );

    // Execution attempts endpoint must remain lazy, and frozen rows must not open a live timeline.
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

    // Verify Evaluators selection and scope restriction. #83: the metric id now
    // appears both on its card and on its quality rule, so scope the assertion
    // to the catalog rather than to the whole page.
    const catalog = page.getByTestId("evaluator-catalog");
    await expect(catalog.getByText("intent_match", { exact: true })).toBeVisible();
    await expect(catalog.getByText("pii_safe", { exact: true })).toBeVisible();
    await expect(catalog.getByText("run_pass_rate", { exact: true })).toBeVisible();
    await expect(page.getByText(/派生运行指标，不能作为用例指标选择/)).toBeVisible();
    // #83 removes the composite conclusion mode from the create flow entirely.
    await expect(page.getByRole("radio", { name: /逐项诊断/ })).toHaveCount(0);
    await expect(page.getByRole("radio", { name: /复合结论/ })).toHaveCount(0);
    await expect(page.getByText("已选 4 项")).toBeVisible();

    // Keyboard users can reach and operate native evaluator checkboxes without
    // submitting. #83 removed the diagnostic/composite radio that used to sit
    // before them, so focus starts from the first metric checkbox.
    const firstEvaluator = page.getByTestId("evaluator-toggle-escalation_match");
    await firstEvaluator.focus();
    await expect(firstEvaluator).toBeFocused();
    await expect(firstEvaluator).toHaveCSS("outline-style", "solid");

    // Each selected card also exposes its exact version selector and contract disclosure right
    // after the checkbox (#80), so the keyboard path must reach every stop without a mouse.
    const tabUntilFocused = async (target: Locator, maxTabs = 4) => {
      for (let i = 0; i < maxTabs; i += 1) {
        if (await target.evaluate((el) => el === document.activeElement)) return;
        await page.keyboard.press("Tab");
      }
      await expect(target).toBeFocused();
    };

    for (const id of ["escalation_match", "intent_match", "pii_safe", "required_tool_match"]) {
      const checkbox = page.getByTestId(`evaluator-toggle-${id}`);
      await expect(checkbox).toBeFocused();
      if (id === "required_tool_match") break;
      await page.keyboard.press("Tab");
      await expect(page.getByLabel(`${id} 版本`)).toBeFocused();
      await tabUntilFocused(page.getByTestId(`evaluator-toggle-${DIAGNOSTIC_IDS[DIAGNOSTIC_IDS.indexOf(id) + 1]}`));
    }

    // Toggle the last diagnostic so keyboard testing doesn't alter the initial request order.
    const lastEvaluator = page.getByTestId("evaluator-toggle-required_tool_match");
    await page.keyboard.press("Space");
    await expect(lastEvaluator).not.toBeChecked();
    await expect(page.getByText("已选 3 项")).toBeVisible();
    expect(interceptedCreationPayload).toBeNull();

    await page.keyboard.press("Enter");
    await expect(lastEvaluator).toBeChecked();
    await expect(page.getByText("已选 4 项")).toBeVisible();
    expect(interceptedCreationPayload).toBeNull();

    // #83 adds one numeric threshold input per metric, so the concurrency
    // spinner is addressed by its own accessible name.
    const concurrencyInput = page.getByLabel("最大并发执行数 (Concurrency)");
    await tabUntilFocused(concurrencyInput, 24);
    await expect(concurrencyInput).toBeFocused();

    // Adjust Concurrency
    await concurrencyInput.fill("2");

    // Submit Launch
    await page.getByRole("button", { name: "创建评测任务" }).click();

    // Verify Creation Payload Contract: must NOT include run_pass_rate, dataset_version undefined when latest
    expect(interceptedCreationPayload).not.toBeNull();
    expect(interceptedCreationPayload.evaluator_selections).toEqual([
      { id: "escalation_match", version: "1.0.0" },
      { id: "intent_match", version: "1.0.0" },
      { id: "pii_safe", version: "1.0.0" },
      { id: "required_tool_match", version: "1.0.0" },
    ]);
    expect(
      interceptedCreationPayload.evaluator_selections.map((s: { id: string }) => s.id),
    ).not.toContain("run_pass_rate");
    // The legacy id-only contract must never be sent: it would silently drift to a newer default.
    expect(interceptedCreationPayload.evaluator_ids).toBeUndefined();
    expect(interceptedCreationPayload.dataset_version).toBeUndefined();
    expect(interceptedCreationPayload.max_concurrency).toBe(2);

    // 2. Navigates to Launch Detail
    await page.waitForURL(`**/launches/${launchId}`);
    await expect(page.getByRole("heading", { name: launchId })).toBeVisible();

    // Verify initial PENDING state and Run button visible
    await expect(page.getByTestId("status-badge").first()).toContainText("PENDING");
    await expect(page.getByTestId("langfuse-sync-badge")).toContainText("PENDING");

    // Switch to Audit tab to inspect 4-dimension frozen manifest
    await page.getByRole("tab", { name: /快照与审计/ }).click();
    await expect(page.getByText("3. 评测门禁指标 (4)")).toBeVisible();
    for (const id of ["escalation_match", "intent_match", "pii_safe", "required_tool_match"]) {
      await expect(page.getByText(id, { exact: true }).first()).toBeVisible();
    }
    await expect(page.getByText("overall_pass", { exact: true })).toHaveCount(0);

    await expect(page.getByTestId("manifest-schema-version")).toContainText("Schema v1.0");
    await expect(page.getByTestId("dataset-snapshot-digest")).toContainText("sha256:e2edi");
    await expect(page.getByTestId("runner-version")).toContainText("0.1.0");
    await expect(page.getByText("sha256-mapping-engine-v1")).toBeVisible();

    const runBtn = page.getByRole("button", { name: /启动评测|立即执行评测/ });
    await expect(runBtn).toBeVisible();

    // 3. Click Run Evaluation
    await runBtn.click();

    // 4. Verify Dual-Badge Decoupling (Execution: SUCCEEDED, Quality: FAIL) & Langfuse sync status
    await expect(page.getByTestId("status-badge").first()).toContainText("SUCCEEDED");
    await expect(page.getByTestId("quality-badge").first()).toContainText("FAIL");
    await expect(page.getByTestId("langfuse-sync-badge")).toContainText("SYNCED");

    // 5. Verify Langfuse Deep Link from backend
    const langfuseLink = page.getByRole("link", { name: "在 Langfuse 中查看" });
    await expect(langfuseLink).toBeVisible();
    await expect(langfuseLink).toHaveAttribute("href", langfuseUrl);

    // 6. Frozen Case rows retain attempt counts but must not hydrate a mutable Attempt timeline.
    await page.getByRole("tab", { name: /用例排查与 Trace/ }).click();
    await expect(page.getByText("case-transfer-01")).toBeVisible();
    const frozenAttempts = page.getByRole("button", { name: "2 次尝试" });
    await expect(frozenAttempts).toBeDisabled();
    await expect(frozenAttempts).toHaveAttribute("title", "历史快照无实时 Attempt 执行记录");
    expect(attemptsFetched).toBe(false);

    // R13 separately proves that a live Launch can open the lazy Attempt Drawer with keyboard focus.
    // This terminal snapshot deliberately has no mutable execution ID to prevent S1/S2 leakage.
  });
});
