import { test, expect } from "@playwright/test";

test.describe("Real API Acceptance E2E (Zero Mock)", () => {
  test("walks through Agents -> Version Specs -> Create Launch -> Launch Frozen Manifest on real backend", async ({
    page,
  }) => {
    // 1. Visit Agents List (Backed by real database & agents.yaml auto-import)
    await page.goto("/agents");
    await expect(page.getByRole("heading", { name: "Agent Registry" })).toBeVisible();

    // Verify imported banking-agent is visible in real DB
    await expect(page.getByText("banking-agent")).toBeVisible();
    await expect(page.getByText("银行客服 Agent")).toBeVisible();

    // 2. Click through to Agent Detail via "管理" link
    await page.getByRole("link", { name: "管理" }).first().click();
    await page.waitForURL("**/agents/banking-agent");
    await expect(page.getByRole("heading", { name: "银行客服 Agent" })).toBeVisible();

    // Verify real active versions exist (v1 & v2 from config/agents.yaml)
    await expect(page.getByText("v1").first()).toBeVisible();
    await expect(page.getByText("v2").first()).toBeVisible();

    // 3. Navigate to New Launch page
    await page.goto("/launches/new");
    await expect(page.getByRole("heading", { name: /发起新评测任务/ })).toBeVisible();

    // Verify real Evaluators loaded from GET /api/v1/evaluators
    await expect(page.getByText("intent_match", { exact: true })).toBeVisible();
    await expect(page.getByText("run_pass_rate", { exact: true })).toBeVisible();
    await expect(page.getByRole("radio", { name: /逐项诊断/ })).toBeChecked();
    await expect(page.getByRole("radio", { name: /复合结论/ })).toBeEnabled();
    await expect(page.getByText("已选 4 项")).toBeVisible();

    // Critical Invariant: run_pass_rate (run scope) must be disabled and not selected!
    await expect(page.getByText(/聚合指标，暂不支持在单次 Launch 中直接运行/).first()).toBeVisible();

    // Fill custom Launch Name
    const customName = `real-e2e-${Date.now()}`;
    await page.getByPlaceholder("例如：release-v1.0-benchmark").fill(customName);

    // Concurrency adjustment
    await page.getByRole("spinbutton").fill("2");

    // 4. Submit Launch Creation to Real Backend (POST /api/v1/experiment-launches)
    await page.getByRole("button", { name: /创建评测任务/ }).click();

    // 5. Navigate to created real Launch Detail page
    await page.waitForURL(/\/launches\/[a-f0-9-]+/);

    // Verify Status Banner
    await expect(page.getByTestId("status-badge").first()).toContainText("PENDING");
    await expect(page.getByTestId("langfuse-sync-badge")).toBeVisible();

    // 6. Verify 4-Dimension Frozen Manifest rendered from real backend database
    await expect(page.getByTestId("manifest-schema-version")).toContainText("Schema v1.0");

    // Dimension 1: Agent snapshot (banking-agent)
    await expect(page.getByText("banking-agent").first()).toBeVisible();

    // Dimension 2: Dataset snapshot & digest
    await expect(page.getByTestId("dataset-snapshot-digest")).toBeVisible();

    // Dimension 3: Evaluators (item-scope selected, no run_pass_rate)
    await expect(page.getByText("intent_match").first()).toBeVisible();
    await expect(page.getByText("3. 评测门禁指标 (4)")).toBeVisible();
    for (const id of ["escalation_match", "intent_match", "pii_safe", "required_tool_match"]) {
      await expect(page.getByText(id, { exact: true })).toBeVisible();
    }
    await expect(page.getByText("overall_pass", { exact: true })).toHaveCount(0);

    // Dimension 4: Runner and Concurrency
    await expect(page.getByTestId("runner-version")).toBeVisible();
    await expect(page.getByText("Concurrency:").first()).toBeVisible();

    // Run button is present in PENDING state
    await expect(page.getByRole("button", { name: /立即执行评测/ })).toBeVisible();
  });
});
