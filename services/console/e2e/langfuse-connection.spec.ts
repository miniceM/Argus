import { test, expect } from "@playwright/test";

test("真实 API：Agent 配置一次项目，多个版本共用并保留凭据引用", async ({ page, request }) => {
  const agent = `lf-binding-${Date.now()}`;
  const headers = { Authorization: "Bearer argus-e2e-admin-token" };
  const response = await request.post("/api/v1/credentials", { headers, data: {
    name: agent, environment: "staging", type: "basic_auth", provider: "managed",
    allowed_origins: ["https://lf.example"], secret: { username: "pk-example-b", password: "sk-example-b" },
  } });
  expect(response.status()).toBe(201);
  const credential = await response.json();
  expect((await request.post("/api/v1/agents", { data: { id: agent, name: agent } })).status()).toBe(201);
  try {
    await page.goto(`/agents/${agent}`);
    await page.getByRole("button", { name: "配置 Langfuse 项目" }).click();
    const bindProject = page.getByRole("checkbox", { name: "使用 Agent 独立 Langfuse 项目" });
    await bindProject.focus();
    await page.keyboard.press("Space");
    await expect(bindProject).toBeChecked();
    await page.getByLabel("Langfuse Base URL", { exact: false }).fill("https://lf.example");
    await page.getByLabel("Langfuse Project ID", { exact: false }).fill("project-b");
    await page.getByLabel("Langfuse 凭据", { exact: false }).selectOption(credential.id);
    await page.getByLabel("项目连接管理授权 Token").fill("argus-e2e-admin-token");
    await page.screenshot({ path: "/tmp/argus-255-agent-desktop.png", fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByLabel("Langfuse 凭据", { exact: false }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: "/tmp/argus-255-agent-mobile.png", fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.getByRole("button", { name: "确认保存连接" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const connection = page.getByRole("region", { name: "Langfuse 项目连接" });
    await expect(connection).toContainText("全部版本共用");
    await expect(connection).toContainText("project-b");
    for (const version of ["bound-v1", "bound-v2"]) {
      await page.getByRole("button", { name: "创建新版本" }).click();
      await page.getByRole("textbox", { name: /版本号/ }).fill(version);
      await expect(page.getByRole("checkbox", { name: /Langfuse/ })).toHaveCount(0);
      await expect(page.getByText(/Langfuse 项目在 Agent 详情页统一配置/)).toBeVisible();
      await page.getByRole("button", { name: "确认创建版本" }).click();
      await expect(page.getByRole("dialog")).toHaveCount(0);
      await expect(page.getByRole("cell", { name: version })).toBeVisible();
      const result = await (await request.get(`/api/v1/agent-versions?agent_id=${agent}&version=${version}`)).json();
      expect(result.langfuse_connection).toBeUndefined();
    }
    const result = await (await request.get(`/api/v1/agents?id=${agent}`)).json();
    expect(result.langfuse_connection).toEqual({ base_url: "https://lf.example", project_id: "project-b", credential_id: credential.id });
    expect(JSON.stringify(result)).not.toContain("sk-example-b");
    const usage = await (await request.get(`/api/v1/credentials/${credential.id}/usage`)).json();
    expect(usage.agents).toEqual([{ agent_id: agent, is_current: true }]);
    expect(usage.versions).toEqual([]);
    expect((await request.delete(`/api/v1/credentials/${credential.id}?confirm_name=${agent}`, { headers })).status()).toBe(409);
    expect(await page.evaluate(() => Object.values(localStorage).some(v => v.includes("argus-e2e-admin-token")))).toBe(false);
    await page.goto(`/agents/${agent}/versions/bound-v2`);
    await expect(page.getByRole("region", { name: "Langfuse 项目连接" })).toContainText("其他版本共用项目连接");
  } finally {
    await request.delete(`/api/v1/agents?id=${agent}`);
    await request.delete(`/api/v1/credentials/${credential.id}?confirm_name=${agent}`, { headers });
  }
});
