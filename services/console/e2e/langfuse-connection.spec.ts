import { test, expect } from "@playwright/test";

test("真实 API：绑定独立 Langfuse 项目、展示来源并保留凭据引用", async ({ page, request }) => {
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
    await page.getByRole("button", { name: "创建新版本" }).click();
    await page.getByRole("textbox", { name: /版本号/ }).fill("bound-b");
    const bindProject = page.getByRole("checkbox", { name: "绑定独立 Langfuse 项目" });
    await bindProject.focus();
    await page.keyboard.press("Space");
    await expect(bindProject).toBeChecked();
    await page.getByLabel("Langfuse Base URL", { exact: false }).fill("https://lf.example");
    await page.getByLabel("Langfuse Project ID", { exact: false }).fill("project-b");
    await page.getByLabel("Langfuse 凭据", { exact: false }).selectOption(credential.id);
    await page.getByLabel("凭据绑定管理授权 Token").fill("argus-e2e-admin-token");
    await page.screenshot({ path: "/tmp/argus-255-binding-desktop.png", fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByLabel("Langfuse 凭据", { exact: false }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: "/tmp/argus-255-binding-mobile.png", fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.getByRole("button", { name: "确认创建版本" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByRole("cell", { name: "bound-b" })).toBeVisible();
    const version = await (await request.get(`/api/v1/agent-versions?agent_id=${agent}&version=bound-b`)).json();
    expect(version.langfuse_connection).toEqual({ base_url: "https://lf.example", project_id: "project-b", credential_id: credential.id });
    expect(JSON.stringify(version)).not.toContain("sk-example-b");
    await page.goto(`/agents/${agent}/versions/bound-b`);
    const connection = page.getByRole("region", { name: "Langfuse 项目连接" });
    await expect(connection).toContainText("BOUND");
    await expect(connection).toContainText("project-b");
    await expect(connection).toContainText(credential.id);
    expect((await request.delete(`/api/v1/credentials/${credential.id}?confirm_name=${agent}`, { headers })).status()).toBe(409);
    expect(await page.evaluate(() => Object.values(localStorage).some(v => v.includes("argus-e2e-admin-token")))).toBe(false);
  } finally {
    await request.delete(`/api/v1/agents?id=${agent}&confirm_name=${agent}`);
    await request.delete(`/api/v1/credentials/${credential.id}?confirm_name=${agent}`, { headers });
  }
});
