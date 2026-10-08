import { test, expect, type Page } from "@playwright/test";
import type { components } from "../src/api/schema";

type Launch = components["schemas"]["ExperimentLaunchResponse"];
type Agent = components["schemas"]["AgentResponse"];
const createdAt = "2026-10-01T00:00:00Z";
const agents: Agent[] = ["banking-agent", "fraud-agent"].map((id) => ({
  id, name: id, status: "active", version_count: 1, launch_count: 3, active_launch_count: 0,
  created_at: createdAt, updated_at: createdAt,
}));

// 每个维度都有反例，三条件交集不能被单条件或 OR 筛选冒充。
const launches: Launch[] = [
  ["bank-pass", "banking-agent", "COMPLETED", "pass"],
  ["bank-fail", "banking-agent", "COMPLETED", "fail"],
  ["bank-unknown", "banking-agent", "FAILED", "unknown"],
  ["fraud-pass", "fraud-agent", "COMPLETED", "pass"],
  ["fraud-fail", "fraud-agent", "FAILED", "fail"],
  ["fraud-unknown", "fraud-agent", "COMPLETED", "unknown"],
].map(([id, agent_id, status, quality_conclusion]) => ({
  id, name: id, agent_id, status, quality_conclusion,
  agent_version: "1.0.0", agent_version_id: `version-${agent_id}`,
  dataset_name: "filter-regression", dataset_version: createdAt,
  manifest: {}, langfuse_sync_status: "SYNCED", created_at: createdAt,
}));

async function expectResults(page: Page, ids: string[]) {
  await expect(page.locator("tbody tr")).toHaveCount(ids.length);
  await expect.poll(async () =>
    page.getByRole("link", { name: /^查看 Launch .* 详情$/ }).evaluateAll((links) =>
      links.map((link) => link.getAttribute("aria-label")),
    ),
  ).toEqual(ids.map((id) => `查看 Launch ${id} 详情`));
}

async function setFilter(page: Page, conditions: Record<string, string>, action: () => Promise<unknown>) {
  const response = page.waitForResponse((res) => {
    const url = new URL(res.url());
    return url.pathname === "/api/v1/experiment-launches" &&
      res.request().method() === "GET" &&
      url.searchParams.size === Object.keys(conditions).length &&
      Object.entries(conditions).every(([key, value]) => url.searchParams.get(key) === value);
  });
  await action();
  expect((await response).status()).toBe(200);
}

test.describe("Issue #46: Launch 筛选结果集", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/v1/system/info", (route) => route.fulfill({ json: {
      service: "argus-eval-runner", version: "0.2.0", build_id: "filter-e2e", environment: "test",
    } }));
    await page.route("**/api/v1/agents", (route) => route.fulfill({ json: agents }));
    await page.route("**/api/v1/experiment-launches*", (route) => {
      const url = new URL(route.request().url());
      const result = launches.filter((launch) =>
        (!url.searchParams.has("agent_id") || launch.agent_id === url.searchParams.get("agent_id")) &&
        (!url.searchParams.has("status") || launch.status === url.searchParams.get("status")) &&
        (!url.searchParams.has("quality_conclusion") || launch.quality_conclusion === url.searchParams.get("quality_conclusion")),
      );
      return route.fulfill({ json: result });
    });
    await page.goto("/launches");
    await expectResults(page, launches.map((launch) => launch.id));
  });

  test("Agent 条件只展示该 Agent 的 Launch", async ({ page }) => {
    await setFilter(page, { agent_id: "banking-agent" }, () =>
      page.getByRole("textbox", { name: "按 Agent ID 过滤" }).fill("banking-agent"),
    );
    await expectResults(page, ["bank-pass", "bank-fail", "bank-unknown"]);
    for (const row of await page.locator("tbody tr").all()) {
      await expect(row.getByText("banking-agent", { exact: true }).first()).toBeVisible();
    }
  });

  for (const [quality, ids] of [
    ["pass", ["bank-pass", "fraud-pass"]],
    ["fail", ["bank-fail", "fraud-fail"]],
    ["unknown", ["bank-unknown", "fraud-unknown"]],
  ] as const) {
    test(`质量 ${quality} 只展示对应结果`, async ({ page }) => {
      await setFilter(page, { quality_conclusion: quality }, () =>
        page.getByRole("combobox", { name: "按质量结论过滤" }).selectOption(quality),
      );
      await expectResults(page, [...ids]);
    });
  }

  test("Agent、执行状态、质量为交集；无匹配不残留，重置恢复全部结果", async ({ page }) => {
    const agent = page.getByRole("textbox", { name: "按 Agent ID 过滤" });
    const status = page.getByRole("combobox", { name: "按执行状态过滤" });
    const quality = page.getByRole("combobox", { name: "按质量结论过滤" });
    await setFilter(page, { agent_id: "fraud-agent" }, () => agent.fill("fraud-agent"));
    await expectResults(page, ["fraud-pass", "fraud-fail", "fraud-unknown"]);
    await setFilter(page, { agent_id: "fraud-agent", status: "COMPLETED" }, () => status.selectOption("COMPLETED"));
    await expectResults(page, ["fraud-pass", "fraud-unknown"]);
    await setFilter(page, { agent_id: "fraud-agent", status: "COMPLETED", quality_conclusion: "pass" }, () => quality.selectOption("pass"));
    await expectResults(page, ["fraud-pass"]);
    await setFilter(page, { agent_id: "fraud-agent", status: "COMPLETED", quality_conclusion: "fail" }, () => quality.selectOption("fail"));
    await expect(page.getByText("暂无评测记录", { exact: true })).toBeVisible();
    await expect(page.getByText("未匹配到符合当前过滤条件的评测任务，请尝试重置筛选。")).toBeVisible();
    await expect(page.locator("table")).toHaveCount(0);
    await expect(page.getByRole("link", { name: /^查看 Launch .* 详情$/ })).toHaveCount(0);
    await page.getByRole("button", { name: "重置筛选" }).click();
    await expect(agent).toHaveValue("");
    await expect(status).toHaveValue("");
    await expect(quality).toHaveValue("");
    await expect(page.getByRole("button", { name: "重置筛选" })).toHaveCount(0);
    await expect(page.getByText("暂无评测记录", { exact: true })).toHaveCount(0);
    await expectResults(page, launches.map((launch) => launch.id));
  });
});
