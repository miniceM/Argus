import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * Issue #44: a Langfuse link that arrives after the launch already reported SYNCED
 * must appear without a manual reload, and unsafe or missing links must degrade
 * into a stated reason instead of a dead external link.
 */

const launchId = "launch-link-backfill-44";
const linkUrl = "https://cloud.langfuse.example.com/project/proj-1/datasets/ds-real/runs/run-real";

type Phase = "syncing" | "synced-no-link" | "synced-linked";

function launchPayload(phase: Phase) {
  const base = {
    id: launchId,
    name: "backfill launch",
    status: "COMPLETED",
    quality_conclusion: "pass",
    dataset_name: "banking-agent-regression",
    dataset_version: "2026-09-30T00:00:00Z",
    agent_id: "banking-agent",
    agent_version: "v1",
    agent_version_id: "ver-1",
    manifest: { dataset: { source: "langfuse" } },
    created_at: "2026-09-30T08:00:00Z",
    started_at: "2026-09-30T08:00:01Z",
    completed_at: "2026-09-30T08:00:05Z",
  };

  if (phase === "syncing") {
    return {
      ...base,
      langfuse_sync_status: "SYNCING",
      langfuse_experiment_id: null,
      langfuse_experiment_url: null,
    };
  }
  if (phase === "synced-no-link") {
    return {
      ...base,
      langfuse_sync_status: "SYNCED",
      langfuse_experiment_id: "run-real",
      langfuse_experiment_url: null,
    };
  }
  return {
    ...base,
    langfuse_sync_status: "SYNCED",
    langfuse_experiment_id: "run-real",
    langfuse_experiment_url: linkUrl,
    links: { langfuse_experiment: linkUrl },
  };
}

interface Counters {
  launches: number;
  items: number;
}

async function stageLink(page: Page, counters: Counters) {
  let phase: Phase = "syncing";

  await page.route("**/api/v1/system/info", (route) =>
    route.fulfill({
      json: {
        service: "argus-eval-runner",
        version: "0.2.0",
        build_id: "issue-44-e2e",
        environment: "test",
      },
    })
  );
  await page.route("**/api/v1/agents**", (route) => route.fulfill({ json: [] }));

  const handler = async (route: Route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/api/v1/experiment-launches") {
      counters.launches += 1;
      await route.fulfill({ json: [launchPayload(phase)] });
      return;
    }
    if (pathname.endsWith("/items")) {
      counters.items += 1;
      await route.fulfill({ json: [] });
      return;
    }
    if (pathname === `/api/v1/experiment-launches/${launchId}`) {
      counters.launches += 1;
      await route.fulfill({ json: launchPayload(phase) });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: "not found" } });
  };

  await page.route("**/api/v1/experiment-launches**", handler);
  return {
    advance: (next: Phase) => {
      phase = next;
    },
  };
}

test.describe("Langfuse link backfill (Issue #44)", () => {
  test("list surfaces the link automatically once the API returns it", async ({ page }) => {
    const counters: Counters = { launches: 0, items: 0 };
    const stage = await stageLink(page, counters);

    await page.goto("/launches");
    await expect(page.getByRole("table")).toBeVisible();
    await expect(page.getByText("Langfuse 同步中")).toBeVisible();

    stage.advance("synced-no-link");
    await expect(page.getByText("已同步，链接暂不可用")).toBeVisible({ timeout: 15_000 });

    stage.advance("synced-linked");
    const link = page.getByRole("link", { name: "在 Langfuse 中查看" });
    await expect(link).toBeVisible({ timeout: 15_000 });
    await expect(link).toHaveAttribute("href", linkUrl);
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  test("detail surfaces the link without a manual reload", async ({ page }) => {
    const counters: Counters = { launches: 0, items: 0 };
    const stage = await stageLink(page, counters);

    await page.goto(`/launches/${launchId}`);
    await expect(page.getByTestId("langfuse-sync-badge")).toBeVisible();
    await expect(page.getByText("Langfuse 同步中")).toBeVisible();

    stage.advance("synced-linked");
    const link = page.getByRole("link", { name: "在 Langfuse 中查看" });
    await expect(link).toBeVisible({ timeout: 15_000 });
    await expect(link).toHaveAttribute("href", linkUrl);
  });

  test("item traffic does not grow while only the link is pending", async ({ page }) => {
    const counters: Counters = { launches: 0, items: 0 };
    const stage = await stageLink(page, counters);

    await page.goto(`/launches/${launchId}`);
    await expect(page.getByTestId("langfuse-sync-badge")).toBeVisible();
    const itemsAfterLoad = counters.items;

    stage.advance("synced-no-link");
    await expect(page.getByText("已同步，链接暂不可用")).toBeVisible({ timeout: 15_000 });
    // Give several poll intervals a chance to fire.
    await page.waitForTimeout(6_000);
    expect(counters.items).toBe(itemsAfterLoad);
    expect(counters.launches).toBeGreaterThan(1);
  });
});

test.describe("Langfuse link degradation", () => {
  const cases: Array<{ name: string; payload: Record<string, unknown>; expected: string }> = [
    {
      name: "dangerous URL",
      payload: {
        langfuse_sync_status: "SYNCED",
        langfuse_experiment_id: "run-real",
        langfuse_experiment_url: "javascript:alert(1)",
      },
      expected: "Langfuse 地址无效",
    },
    {
      name: "sync failed",
      payload: {
        langfuse_sync_status: "FAILED",
        langfuse_experiment_id: null,
        langfuse_experiment_url: null,
      },
      expected: "Langfuse 同步失败",
    },
    {
      name: "not applicable",
      payload: {
        langfuse_sync_status: "NOT_APPLICABLE",
        langfuse_experiment_id: null,
        langfuse_experiment_url: null,
      },
      expected: "Langfuse 同步不适用",
    },
    {
      name: "seed dataset without a run",
      payload: {
        langfuse_sync_status: "SYNCED",
        langfuse_experiment_id: null,
        langfuse_experiment_url: null,
        manifest: { dataset: { source: "seed" } },
      },
      expected: "未创建 Langfuse Run",
    },
  ];

  for (const testCase of cases) {
    test(`renders a reason for ${testCase.name}`, async ({ page }) => {
      await page.route("**/api/v1/system/info", (route) =>
        route.fulfill({
          json: {
            service: "argus-eval-runner",
            version: "0.2.0",
            build_id: "issue-44-e2e",
            environment: "test",
          },
        })
      );
      await page.route("**/api/v1/agents**", (route) => route.fulfill({ json: [] }));
      await page.route("**/api/v1/experiment-launches**", async (route) => {
        const pathname = new URL(route.request().url()).pathname;
        if (pathname.endsWith("/items")) {
          await route.fulfill({ json: [] });
          return;
        }
        const payload = {
          ...launchPayload("synced-linked"),
          ...testCase.payload,
        };
        await route.fulfill({
          json: pathname === "/api/v1/experiment-launches" ? [payload] : payload,
        });
      });

      await page.goto("/launches");
      await expect(page.getByRole("table")).toBeVisible();
      await expect(page.getByText(testCase.expected)).toBeVisible();
      await expect(page.getByRole("link", { name: "在 Langfuse 中查看" })).toHaveCount(0);
    });
  }
});
