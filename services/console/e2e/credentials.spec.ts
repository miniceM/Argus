import { test, expect } from "@playwright/test";

test("真实 API：创建凭据、Agent 选择、轮换、引用保护和停用", async ({ page, request }) => {
  const name = `Credential ${Date.now()}`;
  const agent = `credential-e2e-${Date.now()}`;
  await page.goto("/credentials");
  await page.getByRole("button", { name: "创建凭据" }).click();
  await page.getByLabel("凭据名称", { exact: true }).fill(name);
  await page.getByLabel("Agent Token（仅输入一次）").fill("example-e2e-first-token");
  await page.getByLabel("管理授权 Token").fill("argus-e2e-admin-token");
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  const credentials = await (await request.get("/api/v1/credentials")).json();
  const credential = credentials.find((item: { name: string }) => item.name === name);
  expect(credential).toBeTruthy();
  expect(JSON.stringify(credentials)).not.toContain("example-e2e-first-token");
  try {
    expect((await request.post("/api/v1/agents", { data: { id: agent, name: agent } })).status()).toBe(201);
    await page.goto(`/agents/${agent}`);
    await page.getByRole("button", { name: "创建新版本" }).click();
    await page.getByRole("textbox", { name: /版本号/ }).fill("secured-v1");
    await page.getByRole("textbox", { name: "运行环境", exact: true }).fill(" Production ");
    await page.getByRole("combobox", { name: /选择凭据/ }).selectOption(credential.id);
    await page.getByRole("button", { name: "确认创建版本" }).click();
    await expect(page.getByRole("cell", { name: "secured-v1" })).toBeVisible();
    const version = await (await request.get(`/api/v1/agent-versions?agent_id=${agent}&version=secured-v1`)).json();
    expect(version.credential_id).toBe(credential.id);
    expect(version.credential_ref).toBeNull();
    await page.goto("/credentials");
    const card = page.locator("article").filter({ has: page.getByRole("heading", { name, exact: true }) });
    await card.getByRole("button", { name: `轮换 ${name} (${credential.id.slice(-8)})`, exact: true }).click();
    await page.getByLabel("Agent Token（仅输入一次）").fill("example-e2e-second-token");
    await page.getByLabel("管理授权 Token").fill("argus-e2e-admin-token");
    await page.getByRole("button", { name: "确认", exact: true }).click();
    await expect(card).toContainText("修订 2");
    await card.getByRole("button", { name: `查看使用关系 ${name} (${credential.id.slice(-8)})`, exact: true }).click();
    await expect(page.getByRole("dialog")).toContainText(`${agent} / secured-v1`);
    await page.keyboard.press("Escape");
    await card.getByRole("button", { name: `删除 ${name} (${credential.id.slice(-8)})`, exact: true }).click();
    await page.getByLabel(`输入凭据名称确认：${name}`).fill(name);
    await page.getByLabel("管理授权 Token").fill("argus-e2e-admin-token");
    await page.getByRole("button", { name: "确认", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("凭据仍被版本引用");
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await card.getByRole("button", { name: `停用 ${name} (${credential.id.slice(-8)})`, exact: true }).click();
    await page.getByLabel(`输入凭据名称确认：${name}`).fill(name);
    await page.getByLabel("管理授权 Token").fill("argus-e2e-admin-token");
    await page.getByRole("button", { name: "确认", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("凭据仍被版本引用");
    await page.getByRole("checkbox").check();
    await page.getByRole("button", { name: "确认", exact: true }).click();
    await expect(card).toContainText("已停用");
    expect(await page.evaluate(() => Object.values(localStorage).some(value => value.includes("example-e2e-") || value.includes("argus-e2e-admin-token")))).toBe(false);
  } finally {
    await request.delete(`/api/v1/agents?id=${agent}&purge=true`).catch(() => undefined);
    await request.delete(`/api/v1/credentials/${credential.id}?confirm_name=${encodeURIComponent(name)}`, { headers: { Authorization: "Bearer argus-e2e-admin-token" } }).catch(() => undefined);
  }
});
