import { test, expect } from "@playwright/test";

// 实际 Console + API + DB；真实 Java/Worker 验收见 Python 集成测试，不混为同一证据。
test("HTTP 配置：API Key + 秘密引用 + 普通 Header + 输出提取及拒绝", async ({ page, request }) => {
  const suffix = Date.now();
  const agent = `http-auth-${suffix}`;
  const headers = { Authorization: "Bearer argus-e2e-admin-token" };
  const createCredential = async (type: string, name: string) => {
    await page.goto("/credentials");
    await page.getByRole("button", { name: "创建凭据" }).click();
    await page.getByLabel("凭据名称", { exact: true }).fill(name);
    await page.getByLabel("凭据类型").selectOption(type);
    await page.getByLabel("允许的目标 Origin（每行一个）").fill("https://agent.example");
    await page.getByLabel("Agent Token（仅输入一次）").fill(`example-${type}-secret`);
    await page.getByLabel("管理授权 Token").fill("argus-e2e-admin-token");
    await page.getByRole("button", { name: "确认", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const records = await (await request.get("/api/v1/credentials")).json();
    return records.find((item: { name: string }) => item.name === name);
  };
  const primary = await createCredential("api_key", `API Key ${suffix}`);
  const extra = await createCredential("header_value", `Header ${suffix}`);
  expect((await request.post("/api/v1/agents", { data: { id: agent, name: agent } })).status()).toBe(201);
  try {
    await page.goto(`/agents/${agent}`);
    await page.getByRole("button", { name: "创建新版本" }).click();
    await page.getByRole("textbox", { name: /版本号/ }).fill("key-v1");
    await page.getByRole("textbox", { name: "运行环境", exact: true }).fill("production");
    await page.getByLabel("远程调用端点 (HTTP POST)").fill("https://agent.example/send");
    await page.getByLabel("HTTP 认证方式").selectOption("api_key");
    await page.getByRole("combobox", { name: /选择凭据/ }).selectOption(primary.id);
    await page.getByLabel("API Key Header 名称").fill("Authorization");
    await page.getByLabel("API Key 固定前缀").fill("Token ");
    await page.getByRole("button", { name: "添加 HTTP Header" }).click();
    await page.getByLabel("Header 1 名称").fill("X-Tenant");
    await page.getByLabel("Header 1 普通值").fill("evaluation");
    await page.getByRole("button", { name: "添加 HTTP Header" }).click();
    await page.getByLabel("Header 2 名称").fill("X-App-Secret");
    await page.getByLabel("Header 2 值来源").selectOption("credential");
    await page.getByLabel("Header 2 凭据").selectOption(extra.id);
    await page.getByLabel("回答字段路径").fill("reply");
    await page.getByLabel("凭据绑定管理授权 Token").fill("argus-e2e-admin-token");
    await page.getByRole("button", { name: "确认创建版本" }).click();
    await expect(page.getByRole("cell", { name: "key-v1" })).toBeVisible();
    const version = await (await request.get(`/api/v1/agent-versions?agent_id=${agent}&version=key-v1`)).json();
    expect(version.http_auth).toMatchObject({ scheme: "api_key", header_name: "Authorization", prefix: "Token " });
    expect(version.http_headers[1].credential_id).toBe(extra.id);
    expect(version.response_mapping).toEqual({ answer_path: "reply" });
    expect(JSON.stringify(version)).not.toContain("example-api_key-secret");
    await page.goto(`/agents/${agent}/versions/key-v1`);
    await expect(page.getByText("HTTP 认证方式", { exact: true })).toBeVisible();
    await expect(page.getByText("X-App-Secret", { exact: false })).toBeVisible();
    await expect(page.getByText("回答字段：reply", { exact: true })).toBeVisible();
    const invalid = await request.post("/api/v1/agent-versions", { headers, data: {
      agent_id: agent, version: "rejected", endpoint: "https://agent.example/send",
      http_headers: [{ name: "Authorization", value: "example-do-not-echo-secret" }],
    } });
    expect(invalid.status()).toBe(422);
    expect(await invalid.text()).not.toContain("example-do-not-echo-secret");
    expect((await request.delete(`/api/v1/credentials/${extra.id}?confirm_name=${encodeURIComponent(extra.name)}`, { headers })).status()).toBe(409);
    expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain("argus-e2e-admin-token");
  } finally {
    await request.delete(`/api/v1/agents?id=${agent}`).catch(() => undefined);
    for (const credential of [primary, extra]) await request.delete(`/api/v1/credentials/${credential.id}?confirm_name=${encodeURIComponent(credential.name)}`, { headers }).catch(() => undefined);
  }
});

test("Basic 凭据创建和轮换仅输入一次、不回显", async ({ page, request }) => {
  const name = `Basic ${Date.now()}`;
  await page.goto("/credentials");
  await page.getByRole("button", { name: "创建凭据" }).click();
  await page.getByLabel("凭据名称", { exact: true }).fill(name);
  await page.getByLabel("凭据类型").selectOption("basic_auth");
  await page.getByLabel("允许的目标 Origin（每行一个）").fill("https://agent.example");
  await page.getByLabel("Basic 用户名").fill("example-basic-user");
  await page.getByLabel("Basic 密码").fill("example-basic-password");
  await page.getByLabel("管理授权 Token").fill("argus-e2e-admin-token");
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const record = (await (await request.get("/api/v1/credentials")).json()).find((item: { name: string }) => item.name === name);
  try {
    const card = page.locator("article").filter({ has: page.getByRole("heading", { name, exact: true }) });
    await card.getByRole("button", { name: /^轮换/ }).click();
    await expect(page.getByLabel("Basic 用户名")).toBeEmpty();
    await expect(page.getByLabel("Basic 密码")).toBeEmpty();
    await page.getByLabel("Basic 用户名").fill("example-next-user");
    await page.getByLabel("Basic 密码").fill("example-next-password");
    await page.getByLabel("管理授权 Token").fill("argus-e2e-admin-token");
    await page.getByRole("button", { name: "确认", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(card).toContainText("Basic Auth");
    await expect(card).toContainText("修订 2");
    expect(await page.locator("body").innerText()).not.toContain("example-next-password");
  } finally {
    await request.delete(`/api/v1/credentials/${record.id}?confirm_name=${encodeURIComponent(name)}`, { headers: { Authorization: "Bearer argus-e2e-admin-token" } });
  }
});
