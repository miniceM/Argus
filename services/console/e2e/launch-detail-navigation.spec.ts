import { readFile } from "node:fs/promises";
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
    progress: {
      total: 6,
      completed: 6,
      percentage: 100,
      pending: 0,
      queued: 0,
      running: 0,
      retry_wait: 0,
      succeeded: 6,
      failed: 0,
      timed_out: 0,
      cancelled: 0,
    },
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
    await expect(page.getByText("v1 (Candidate)")).toBeVisible();

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

test.describe("Issue #119: launch detail first-screen hierarchy", () => {
  test.beforeEach(async ({ page }) => {
    await mockLaunchApi(page);
    // Realistic frozen report: two revisions so the snapshot panel renders its content.
    await page.route("**/api/v1/experiment-launches/*/result-snapshots", (route) =>
      route.fulfill({
        json: {
          launch_id: historicalLaunchId,
          latest_snapshot_id: "snap-v2",
          latest_revision: 2,
          revisions: [
            {
              snapshot_id: "snap-v1",
              revision: 1,
              created_at: "2026-09-23T08:00:02Z",
              source_result_digest: "sha256:aaaaaaaaaaaaaaaa",
              manifest_digest: "sha256:bbbbbbbbbbbbbbbb",
              evidence_state: "COMPLETE",
              evidence_reasons: [],
              total_cases: 6,
              quality_pass_count: 2,
              quality_fail_count: 4,
              quality_unknown_count: 0,
              is_latest: false,
            },
            {
              snapshot_id: "snap-v2",
              revision: 2,
              created_at: "2026-09-23T08:00:05Z",
              source_result_digest: "sha256:cccccccccccccccc",
              manifest_digest: "sha256:dddddddddddddddd",
              evidence_state: "COMPLETE",
              evidence_reasons: [],
              total_cases: 6,
              quality_pass_count: 6,
              quality_fail_count: 0,
              quality_unknown_count: 0,
              is_latest: true,
            },
          ],
        },
      }),
    );
    await page.route("**/api/v1/experiment-launches/*/result-snapshots/*", (route) =>
      route.fulfill({
        json: {
          launch_id: historicalLaunchId,
          snapshot_id: "snap-v2",
          revision: 2,
          items: [
            {
              dataset_item_id: "case-0",
              execution_status: "SUCCEEDED",
              quality_conclusion: "pass",
              scores: { intent_match: 1 },
            },
          ],
          releasable: true,
        },
      }),
    );
    await page.route("**/api/v1/experiment-launches/*/summary", (route) =>
      route.fulfill({
        json: {
          launch_id: historicalLaunchId,
          snapshot_id: "snap-v2",
          versions: {
            agent: { id: "banking-agent", version: "v1" },
            dataset: { name: "regression-dataset", version: "v2" },
            evaluators: [{ id: "intent_match", version: "1.0.0" }],
            runner: { runner_version: "v0.2.0", mapping_engine_version: "engine-v1" },
          },
          execution: { total: 6, succeeded: 6, failed: 0 },
          quality: { pass: 6, fail: 0, unknown: 0 },
        },
      }),
    );
    await page.route(/\/api\/v1\/experiment-launches\/[^/]+\/comparison(?:\?.*)?$/, (route) =>
      route.fulfill({
        json: {
          launch_id: historicalLaunchId,
          candidate_snapshot_id: "snap-v2",
          baseline_snapshot_id: "snap-v1",
          baseline_binding_revision: 1,
          versions: {
            candidate: { agent: { id: "banking-agent", version: "v1" } },
            baseline: { agent: { id: "banking-agent", version: "v1" } },
          },
          classification_counts: { REGRESSION: 1, IMPROVEMENT: 1, UNCHANGED: 4, NOT_COMPARABLE: 0 },
          comparability: {
            comparable: true,
            reason_codes: [],
            provenance: "VERIFIED",
            dimensions: [],
            suggestions: [],
          },
          formal: {
            available: true,
            verdict: "REGRESSION",
            required_cases: 1,
          },
          summary: {
            comparable_case_count: 6,
            baseline: { pass_rate: 0.33, score_means: { intent_match: 1 } },
            candidate: { pass_rate: 1, score_means: { intent_match: 1 } },
            comparable_cohort: {
              baseline: { pass_rate: 0.33, score_means: { intent_match: 1 } },
              candidate: { pass_rate: 1, score_means: { intent_match: 1 } },
            },
          },
          items: [
            {
              dataset_item_id: "regression-case-1",
              classification: "REGRESSION",
              reason: "Candidate score decreased",
              basis: "FORMAL",
              baseline_scores: { intent_match: 1 },
              candidate_scores: { intent_match: 0 },
              score_deltas: { intent_match: -1 },
            },
          ],
          next_cursor: null,
        },
      }),
    );
    await page.route("**/api/v1/agents/*/baselines*", (route) =>
      route.fulfill({
        json: {
          agent_id: "banking-agent",
          environment: "production",
          revision: 1,
          result_snapshot_id: "snap-v1",
          updated_by: "tester",
          updated_at: "2026-09-23T08:00:02Z",
          launch_id: historicalLaunchId,
          agent_version: "v1",
          dataset_name: "regression-dataset",
          dataset_version: "v2",
          summary: {},
        },
      }),
    );
    await page.route("**/api/v1/evaluators*", (route) => route.fulfill({ json: [] }));
  });

  for (const viewport of [
    { width: 1280, height: 800 },
    { width: 1440, height: 900 },
  ]) {
    test(`the tab bar and comparison entry are complete in the first screen at ${viewport.width}x${viewport.height}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      await page.goto(`/launches/${historicalLaunchId}?snapshot_id=snap-v2&tab=cases`);
      await expect(page.getByRole("heading", { name: historicalLaunchId })).toBeVisible();
      await expect(page.getByTestId("snapshot-revision").first()).toHaveText("Revision 2");
      expect(await page.evaluate(() => window.scrollY)).toBe(0);

      const tablist = page.getByRole("tablist").first();
      await expect(tablist).toBeVisible();
      const viewportSize = await page.evaluate(() => ({
        width: document.documentElement.clientWidth,
        height: document.documentElement.clientHeight,
      }));
      const box = await tablist.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x, "tab bar starts inside the viewport").toBeGreaterThanOrEqual(-1);
      expect(box!.x + box!.width, "tab bar is not clipped horizontally").toBeLessThanOrEqual(
        viewportSize.width + 1,
      );
      expect(box!.y, "tab bar starts above the fold").toBeGreaterThanOrEqual(0);
      expect(
        box!.y + box!.height,
        "tab bar bottom must be inside the first screen",
      ).toBeLessThanOrEqual(viewportSize.height + 1);

      // The three tabs and the actual comparison navigation entry stay usable.
      const casesTab = page.getByRole("tab", { name: /用例排查与 Trace/ });
      await expect(casesTab).toHaveAttribute("aria-selected", "true");
      const tabNames = [
        /门禁与版本对比/,
        /用例排查与 Trace/,
        /不可变快照与审计/,
      ];
      for (const tabName of tabNames) {
        const tab = page.getByRole("tab", { name: tabName });
        await expect(tab).toBeInViewport();
        const tabBox = await tab.boundingBox();
        expect(tabBox).not.toBeNull();
        expect(tabBox!.x, `${tabName} is fully visible on the left`).toBeGreaterThanOrEqual(-1);
        expect(tabBox!.x + tabBox!.width, `${tabName} is fully visible on the right`).toBeLessThanOrEqual(
          viewportSize.width + 1,
        );
        expect(tabBox!.y, `${tabName} is fully visible above the fold`).toBeGreaterThanOrEqual(-1);
        expect(tabBox!.y + tabBox!.height, `${tabName} is fully visible below the fold`).toBeLessThanOrEqual(
          viewportSize.height + 1,
        );
        const text = await tab.evaluate((el) => {
          const label = el.querySelector<HTMLElement>("span") ?? el;
          return {
            scrollWidth: label.scrollWidth,
            clientWidth: label.clientWidth,
            text: (label.textContent ?? "").trim(),
          };
        });
        expect(text.scrollWidth, `${tabName} label is not clipped`).toBeLessThanOrEqual(text.clientWidth + 1);
      }
      const comparisonEntry = page.getByRole("button", {
        name: "正式回归用例数，点击前往门禁对比",
      });
      await expect(comparisonEntry).toHaveCount(1);
      await expect(comparisonEntry).toBeVisible();
      await expect(comparisonEntry).toBeInViewport();
      const comparisonBox = await comparisonEntry.boundingBox();
      expect(comparisonBox).not.toBeNull();
      expect(comparisonBox!.x, "comparison entry is not clipped on the left").toBeGreaterThanOrEqual(0);
      expect(comparisonBox!.x + comparisonBox!.width, "comparison entry is not clipped on the right").toBeLessThanOrEqual(viewportSize.width);
      expect(comparisonBox!.y, "comparison entry starts inside the first screen").toBeGreaterThanOrEqual(0);
      expect(comparisonBox!.y + comparisonBox!.height, "comparison entry is complete above the fold").toBeLessThanOrEqual(viewportSize.height);

      const visibleEntryLabel = page.getByText("正式回归用例数", { exact: true });
      await expect(visibleEntryLabel).toBeVisible();
      const labelBox = await visibleEntryLabel.boundingBox();
      expect(labelBox).not.toBeNull();
      expect(labelBox!.x).toBeGreaterThanOrEqual(0);
      expect(labelBox!.x + labelBox!.width).toBeLessThanOrEqual(viewportSize.width);
      expect(labelBox!.y).toBeGreaterThanOrEqual(0);
      expect(labelBox!.y + labelBox!.height).toBeLessThanOrEqual(viewportSize.height);
      expect(
        await visibleEntryLabel.evaluate((el) => el.scrollWidth <= el.clientWidth + 1),
        "comparison entry label is not horizontally clipped",
      ).toBe(true);

      const clippedAncestors = await comparisonEntry.evaluate((el) => {
        const child = el.getBoundingClientRect();
        const clipping: string[] = [];
        let ancestor = el.parentElement;
        while (ancestor) {
          const style = window.getComputedStyle(ancestor);
          const parent = ancestor.getBoundingClientRect();
          const clipsX = ["hidden", "clip"].includes(style.overflowX);
          const clipsY = ["hidden", "clip"].includes(style.overflowY);
          if (
            (clipsX && (child.left < parent.left - 1 || child.right > parent.right + 1)) ||
            (clipsY && (child.top < parent.top - 1 || child.bottom > parent.bottom + 1))
          ) {
            clipping.push(`${ancestor.tagName}.${ancestor.className}`);
          }
          ancestor = ancestor.parentElement;
        }
        return clipping;
      });
      expect(clippedAncestors, "comparison entry is not clipped by an ancestor").toEqual([]);

      const tabBoxes = await Promise.all(
        [/门禁与版本对比/, /用例排查与 Trace/, /不可变快照与审计/].map(async (tabName) => ({
          name: String(tabName),
          box: await page.getByRole("tab", { name: tabName }).boundingBox(),
        })),
      );
      await testInfo.attach("first-screen-layout.json", {
        body: JSON.stringify({
          viewport: viewportSize,
          scrollY: await page.evaluate(() => window.scrollY),
          tablist: box,
          tabs: tabBoxes,
          comparisonEntry: comparisonBox,
          comparisonLabel: labelBox,
          revision: await page.getByTestId("snapshot-revision").first().innerText(),
        }, null, 2),
        contentType: "application/json",
      });

      // Start on Cases so this proves navigation, not merely the default Compare tab.
      await comparisonEntry.click();
      await expect(page).toHaveURL(/tab=compare/);
      await expect(page).toHaveURL(/snapshot_id=snap-v2/);
      await expect(page.getByRole("tab", { name: /门禁与版本对比/ })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      const regressionFilter = page.getByRole("button", { name: /^REGRESSION \(1\)$/ });
      await expect(regressionFilter).toHaveAttribute("aria-pressed", "true");
      await expect(
        page.getByRole("row").filter({ hasText: "regression-case-1" }),
      ).toBeVisible();
    });
  }

  test("Launch detail Tabs support Arrow, Home and End keyboard navigation", async ({ page }) => {
    await page.goto(`/launches/${historicalLaunchId}?snapshot_id=snap-v2&tab=cases`);
    const compareTab = page.getByRole("tab", { name: /门禁与版本对比/ });
    const casesTab = page.getByRole("tab", { name: /用例排查与 Trace/ });
    const auditTab = page.getByRole("tab", { name: /不可变快照与审计/ });

    await casesTab.focus();
    await page.keyboard.press("End");
    await expect(auditTab).toBeFocused();
    await expect(auditTab).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/tab=audit/);

    await page.keyboard.press("ArrowLeft");
    await expect(casesTab).toBeFocused();
    await expect(casesTab).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/tab=cases/);

    await page.keyboard.press("Home");
    await expect(compareTab).toBeFocused();
    await expect(compareTab).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/tab=compare/);

    await page.keyboard.press("ArrowRight");
    await expect(casesTab).toBeFocused();
    await expect(casesTab).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/tab=cases/);
    await expect(page).toHaveURL(/snapshot_id=snap-v2/);
  });

  for (const key of ["Enter", "Space"] as const) {
    test(`the Regression KPI opens the filtered Comparison tab with ${key}`, async ({ page }) => {
      await page.goto(`/launches/${historicalLaunchId}?snapshot_id=snap-v2&tab=cases`);
      const regressionEntry = page.getByRole("button", {
        name: "正式回归用例数，点击前往门禁对比",
      });
      await regressionEntry.focus();
      await expect(regressionEntry).toBeFocused();
      await page.keyboard.press(key);

      await expect(page).toHaveURL(/tab=compare/);
      await expect(page).toHaveURL(/snapshot_id=snap-v2/);
      await expect(page.getByRole("tab", { name: /门禁与版本对比/ })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      const regressionFilter = page.getByRole("button", { name: /^REGRESSION \(1\)$/ });
      await expect(regressionFilter).toHaveAttribute("aria-pressed", "true");
      await expect(
        page.getByRole("row").filter({ hasText: "regression-case-1" }),
      ).toBeVisible();
    });
  }

  test("cancel confirmation traps focus, Escape sends no request, and keyboard confirmation sends one", async ({ page }) => {
    let cancelPostCount = 0;
    await page.route(new RegExp(`/api/v1/experiment-launches/${historicalLaunchId}$`), (route) =>
      route.fulfill({
        json: {
          ...launches[1],
          status: "RUNNING",
          allowed_actions: ["cancel"],
          completed_at: null,
          progress: {
            total: 6,
            completed: 2,
            percentage: 33,
            pending: 0,
            queued: 0,
            running: 4,
            retry_wait: 0,
            succeeded: 2,
            failed: 0,
            timed_out: 0,
            cancelled: 0,
          },
        },
      }),
    );
    await page.route(/\/api\/v1\/experiment-launches\/launch-historical-issue-29\/cancel(?:\?.*)?$/, async (route) => {
      cancelPostCount += 1;
      await route.fulfill({ json: { id: historicalLaunchId, status: "CANCELLED" } });
    });

    await page.goto(`/launches/${historicalLaunchId}?snapshot_id=snap-v2&tab=cases`);
    const cancelTrigger = page.getByRole("button", { name: "取消评测 (Cancel)" });
    await expect(cancelTrigger).toBeVisible();
    await cancelTrigger.focus();
    await page.keyboard.press("Enter");

    const dialog = page.getByRole("dialog", { name: "确认取消评测任务" });
    await expect(dialog).toBeVisible();
    const close = dialog.getByRole("button", { name: "关闭" });
    const confirm = dialog.getByRole("button", { name: "确认取消" });
    await expect(close).toBeFocused();

    await page.keyboard.press("Shift+Tab");
    await expect(confirm).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(close).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(cancelTrigger).toBeFocused();
    expect(cancelPostCount).toBe(0);

    await page.keyboard.press("Enter");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "确认取消" }).focus();
    await page.keyboard.press("Enter");
    await expect(dialog).toHaveCount(0);
    expect(cancelPostCount).toBe(1);
  });

  test("downloads parseable JSON for the URL-pinned Launch and Snapshot", async ({ page }, testInfo) => {
    const selectedPayload = {
      launch_id: historicalLaunchId,
      snapshot_id: "snap-v1",
      revision: 1,
      created_at: "2026-09-23T08:00:02Z",
      evidence_state: "COMPLETE",
      evidence_reasons: [],
      releasable: true,
      items: [{ dataset_item_id: "frozen-case-1", quality_conclusion: "fail", scores: { intent_match: 0 } }],
    };
    await page.route("**/api/v1/experiment-launches/*/result-snapshots/*", (route) =>
      route.fulfill({ json: selectedPayload }),
    );
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(`/launches/${historicalLaunchId}?snapshot_id=snap-v1&tab=audit`);

    await expect(page.getByTestId("snapshot-revision").first()).toHaveText("Revision 1");
    const downloadButton = page.getByRole("button", { name: "下载原始 JSON" });
    await expect(downloadButton).toBeEnabled();
    const downloadPromise = page.waitForEvent("download");
    await downloadButton.click();
    const download = await downloadPromise;

    expect(download.suggestedFilename()).toBe(`snapshot-${historicalLaunchId}-snap-v1.json`);
    const filePath = testInfo.outputPath(download.suggestedFilename());
    await download.saveAs(filePath);
    const downloadedJson = JSON.parse(await readFile(filePath, "utf8"));
    expect(downloadedJson.launch_id).toBe(historicalLaunchId);
    expect(downloadedJson.snapshot_id).toBe("snap-v1");
    expect(downloadedJson.revision).toBe(1);
    expect(downloadedJson.items[0].dataset_item_id).toBe("frozen-case-1");
    await testInfo.attach("downloaded-snapshot.json", {
      body: JSON.stringify(downloadedJson, null, 2),
      contentType: "application/json",
    });
  });
});
