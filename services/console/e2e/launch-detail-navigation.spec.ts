import { test, expect, type Page } from "@playwright/test";

const activeLaunchId = "launch-active-issue-29";
const historicalLaunchId = "launch-historical-issue-29";

const launches = [
  {
    id: activeLaunchId,
    name: "active evaluation",
    status: "RUNNING",
    quality_conclusion: "unknown",
    dataset_name: "regression-dataset",
    dataset_version: "v3",
    agent_id: "banking-agent",
    agent_version: "v2",
    manifest: {},
    langfuse_sync_status: "PENDING",
    created_at: "2026-09-24T08:00:00Z",
  },
  {
    id: historicalLaunchId,
    name: "historical evaluation",
    status: "COMPLETED",
    quality_conclusion: "pass",
    dataset_name: "regression-dataset",
    dataset_version: "v2",
    agent_id: "banking-agent",
    agent_version: "v1",
    manifest: { schema_version: "1.0", agent: { version: "v1" } },
    langfuse_sync_status: "SYNCED",
    langfuse_experiment_url: "https://langfuse.example/project/demo/experiments/exp-29",
    created_at: "2026-09-23T08:00:00Z",
    started_at: "2026-09-23T08:00:01Z",
    completed_at: "2026-09-23T08:00:05Z",
  },
];

async function mockLaunchApi(page: Page) {
  await page.route("**/api/v1/system/info", (route) =>
    route.fulfill({
      json: {
        service: "argus-eval-runner",
        version: "0.2.0",
        build_id: "issue-29-e2e",
        environment: "test",
      },
    })
  );
  await page.route("**/api/v1/agents**", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/experiment-launches**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/api/v1/experiment-launches") {
      await route.fulfill({ json: launches });
      return;
    }

    const match = pathname.match(/\/api\/v1\/experiment-launches\/([^/]+)(\/items)?$/);
    const id = match?.[1];
    if (!id) {
      await route.fulfill({ status: 404, json: { detail: "Launch endpoint not found" } });
      return;
    }

    if (match?.[2] === "/items") {
      if (!launches.some((launch) => launch.id === id)) {
        await route.fulfill({ status: 404, json: { detail: `Launch '${id}' not found` } });
        return;
      }
      await route.fulfill({ json: [] });
      return;
    }

    const launch = launches.find((candidate) => candidate.id === id);
    if (!launch) {
      await route.fulfill({ status: 404, json: { detail: `Launch '${id}' not found` } });
      return;
    }
    await route.fulfill({ json: launch });
  });
}

test.describe("Issue #29: Launch detail navigation and identity", () => {
  test.beforeEach(async ({ page }) => {
    await mockLaunchApi(page);
  });

  test("keeps a detail action on active and historical rows and opens the matching record", async ({ page }) => {
    await page.goto("/experiments");
    await expect(page.getByRole("table")).toBeVisible();

    const activeLink = page.getByRole("link", { name: `查看 Launch ${activeLaunchId} 详情` });
    const historicalLink = page.getByRole("link", { name: `查看 Launch ${historicalLaunchId} 详情` });
    await expect(activeLink).toHaveAttribute("href", `/launches/${activeLaunchId}`);
    await expect(historicalLink).toHaveAttribute("href", `/launches/${historicalLaunchId}`);
    await expect(page.getByRole("link", { name: "在 Langfuse 中查看" })).toHaveAttribute(
      "target",
      "_blank"
    );

    await activeLink.click();
    await expect(page.getByRole("heading", { name: activeLaunchId })).toBeVisible();
    await page.getByRole("link", { name: "返回评测列表" }).click();

    await page.getByRole("link", { name: `查看 Launch ${historicalLaunchId} 详情` }).click();
    await expect(page.getByRole("heading", { name: historicalLaunchId })).toBeVisible();
    await expect(page.getByText("v1", { exact: true })).toBeVisible();

    await page.reload();
    await expect(page.getByRole("heading", { name: historicalLaunchId })).toBeVisible();
  });

  test("shows a not-found error for an unknown ID without rendering another Launch", async ({ page }) => {
    const missingId = "launch-does-not-exist-issue-29";
    await page.goto(`/launches/${missingId}`);

    await expect(page.getByTestId("error-state")).toContainText("不存在或已删除");
    await expect(page.getByRole("heading", { name: activeLaunchId })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: historicalLaunchId })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "返回评测列表" })).toBeVisible();
  });
});
