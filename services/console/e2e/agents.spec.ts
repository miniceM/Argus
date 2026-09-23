import { test, expect } from "@playwright/test";

test.describe("E2E-01: Agent Registry & Immutable Version UX Flow", () => {
  test("registers new agent, creates immutable version, and inspects masked credentials", async ({ page }) => {
    const dynamicId = `agent-e2e-${Date.now()}-${Math.floor(Math.random() * 1000)}`;
    const dynamicName = `Financial Advisor (${dynamicId})`;

    // Mock API State for deterministic, ultra-fast E2E test
    let agentList: any[] = [
      {
        id: "demo-banking-agent",
        name: "Banking Core Agent",
        description: "Banking test agent",
        owner: "core-team",
        status: "active",
        version_count: 2,
        latest_version: "2.0.0",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ];

    let versionList: any[] = [];

    await page.route("**/api/v1/system/info", async (route) => {
      await route.fulfill({
        json: {
          service: "argus-eval-runner",
          version: "0.2.0",
          build_id: "e2e-test-build",
          environment: "test",
        },
      });
    });

    await page.route("**/api/v1/agents**", async (route) => {
      const url = new URL(route.request().url());
      const queryId = url.searchParams.get("id");

      if (route.request().method() === "POST") {
        const body = JSON.parse(route.request().postData() || "{}");
        const newAgent = {
          id: body.id,
          name: body.name,
          description: body.description || "",
          owner: body.owner || "",
          status: "active",
          version_count: 0,
          latest_version: null,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        agentList.unshift(newAgent);
        await route.fulfill({ status: 201, json: newAgent });
        return;
      }

      if (route.request().method() === "DELETE") {
        if (queryId) {
          agentList = agentList.filter((a) => a.id !== queryId);
          await route.fulfill({ status: 200, json: { id: queryId, deleted: true, launches_deleted: 0 } });
        } else {
          await route.fulfill({ status: 400, json: { detail: "Missing id" } });
        }
        return;
      }

      if (queryId) {
        const match = agentList.find((a) => a.id === queryId);
        if (match) {
          await route.fulfill({ json: match });
        } else {
          await route.fulfill({ status: 404, json: { detail: "Agent not found" } });
        }
        return;
      }

      await route.fulfill({ json: agentList });
    });

    await page.route("**/api/v1/agent-versions**", async (route) => {
      const url = new URL(route.request().url());
      const queryVer = url.searchParams.get("version");

      if (route.request().method() === "POST") {
        const body = JSON.parse(route.request().postData() || "{}");
        const newVer = {
          id: `ver-${Date.now()}`,
          agent_id: body.agent_id,
          version: body.version,
          spec_digest: "sha256:abc123def45678901234567890",
          endpoint: body.endpoint,
          protocol: body.protocol || "http",
          method: body.method || "POST",
          request_mapping: body.request_mapping || { input: "{{ query }}" },
          request_schema: {},
          response_schema: {},
          credential_ref: body.credential_ref || "vault:secrets/agents#key",
          timeout_seconds: body.timeout_seconds || 30,
          max_retries: body.max_retries || 2,
          rate_limit_per_minute: 60,
          max_concurrency: 3,
          is_idempotent: true,
          trace_propagation: "w3c",
          is_active: true,
          created_at: new Date().toISOString(),
        };
        versionList.unshift(newVer);
        await route.fulfill({ status: 201, json: newVer });
        return;
      }

      if (queryVer) {
        const match = versionList.find((v) => v.version === queryVer);
        if (match) {
          await route.fulfill({ json: match });
        } else {
          await route.fulfill({ status: 404, json: { detail: "Version not found" } });
        }
        return;
      }

      await route.fulfill({ json: versionList });
    });

    // 1. Visit Agents page
    await page.goto("/agents");
    await expect(page.getByRole("heading", { name: "Agent Registry" })).toBeVisible();

    // 2. Open Register Agent Dialog
    await page.getByRole("button", { name: "注册 Agent" }).click();
    await expect(page.getByRole("heading", { name: "注册新 Agent" })).toBeVisible();

    // Fill form with dynamic ID
    await page.fill('input[placeholder="e.g. banking-agent"]', dynamicId);
    await page.fill('input[placeholder="e.g. 银行核心业务助手"]', dynamicName);
    await page.fill('input[placeholder="e.g. retail-ai-team"]', "fintech-team");
    await page.getByRole("button", { name: "确认注册" }).click();

    // Dialog automatically navigates to Agent Detail page on success
    await page.waitForURL(`**/agents/${dynamicId}`);
    await expect(page.getByRole("heading", { name: dynamicName })).toBeVisible();
    await expect(page.getByText(dynamicId, { exact: true })).toBeVisible();

    // 4. Create New Version
    await page.getByRole("button", { name: "创建新版本" }).click();
    await expect(page.getByRole("heading", { name: "创建 AgentVersion 规格快照" })).toBeVisible();

    await page.fill('input[placeholder="e.g. 1.0.0 或 v2"]', "1.0.0");
    await page.fill('input[type="url"]', "http://demo-agent:8080/invoke");
    await page.getByRole("button", { name: "确认创建版本" }).click();

    // Verify version 1.0.0 appears with ACTIVE badge
    await expect(page.getByRole("cell", { name: "1.0.0" })).toBeVisible();
    await expect(page.getByText("ACTIVE").first()).toBeVisible();

    // 5. Inspect Version Details
    await page.getByRole("link", { name: "查看配置" }).first().click();
    await expect(page.getByRole("heading", { level: 2 }).filter({ hasText: dynamicId })).toBeVisible();
    await expect(page.getByText("不可变快照保证")).toBeVisible();

    // Verify SecretRef is masked by default
    const secretRef = page.getByTestId("secret-ref");
    await expect(secretRef).toBeVisible();
    await expect(secretRef.getByText("vaul***")).toBeVisible();

    // Click reveal button to see full credential reference
    await page.getByTitle("显示引用名").click();
    await expect(page.getByText("vault:secrets/agents#key")).toBeVisible();

    // Mock purge route as well
    await page.route("**/api/v1/agents/purge**", async (route) => {
      const body = JSON.parse(route.request().postData() || "{}");
      agentList = agentList.filter((a) => a.id !== body.agent_id);
      await route.fulfill({ status: 200, json: { id: body.agent_id, deleted: true, launches_deleted: 1 } });
    });

    // 6. Delete Agent (since launch_count is 0, normal safe delete applies directly)
    await page.goto(`/agents/${dynamicId}`);
    await page.getByRole("button", { name: "删除 Agent" }).click();
    await expect(page.getByRole("heading", { name: "删除 Agent" })).toBeVisible();

    // Normal safe delete button is enabled without needing name confirmation
    const confirmBtn = page.getByRole("button", { name: "确认删除" });
    await expect(confirmBtn).toBeEnabled();
    await confirmBtn.click();

    // Navigated back to /agents and agent is removed
    await page.waitForURL("**/agents");
    await expect(page.getByText(dynamicId)).not.toBeVisible();
  });
});
