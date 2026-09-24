import { test, expect } from "@playwright/test";

test.describe("UX-26: Launches List High-Density Table Layout and Row Height Verification", () => {
  const longUuid = "3fa85f64-5717-4562-b3fc-2c963f66afa6";
  const longAgentId = "financial-fraud-detection-assistant-enterprise-prod";
  const longDatasetName = "financial-transactions-regression-benchmark-dataset-v2";

  const mockLaunches = [
    {
      id: longUuid,
      name: "run-fraud-regression-test-full",
      status: "RUNNING",
      quality_conclusion: "unknown",
      dataset_name: longDatasetName,
      dataset_version: "2026-09-24T00:00:00Z",
      agent_id: longAgentId,
      agent_version: "v2.1.0",
      agent_version_id: "ver-1",
      manifest: {},
      langfuse_sync_status: "SYNCED",
      created_at: new Date().toISOString(),
      progress: {
        total: 20,
        completed: 15,
        percentage: 75.0,
      },
    },
    {
      id: "8a2df9aa-f370-4f52-8700-60b616782d49",
      name: "run-simple",
      status: "COMPLETED",
      quality_conclusion: "pass",
      dataset_name: "simple-eval",
      dataset_version: "v1.0",
      agent_id: "unregistered-agent-id",
      agent_version: "1.0.0",
      agent_version_id: "ver-2",
      manifest: {},
      langfuse_sync_status: "PENDING",
      created_at: new Date().toISOString(),
    },
  ];

  const mockAgents = [
    {
      id: longAgentId,
      name: "反欺诈风控助手",
      status: "active",
      version_count: 2,
    },
  ];

  test.beforeEach(async ({ page }) => {
    // Mock system info
    await page.route("**/api/v1/system/info", async (route) => {
      await route.fulfill({
        json: {
          service: "argus-eval-runner",
          version: "0.2.0",
          build_id: "e2e-build",
          environment: "test",
        },
      });
    });

    // Mock launches list
    await page.route("**/api/v1/experiment-launches*", async (route) => {
      await route.fulfill({ json: mockLaunches });
    });

    // Mock agents list
    await page.route("**/api/v1/agents*", async (route) => {
      await route.fulfill({ json: mockAgents });
    });
  });

  test("verifies table row height stability and no abnormal wrapping at 1440x720", async ({ page, context }) => {
    // Set 1440x720 viewport (desktop baseline in issue #26)
    await page.setViewportSize({ width: 1440, height: 720 });
    // Grant clipboard permissions for copy button
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);

    await page.goto("/launches");

    const table = page.locator("table");
    await expect(table).toBeVisible();

    // Verify table has table-fixed and space for the consistently labeled actions
    await expect(table).toHaveClass(/table-fixed/);
    await expect(table).toHaveClass(/min-w-\[1200px\]/);

    // Verify row height is controlled and doesn't exceed 64px
    const row = table.locator("tbody tr").first();
    const boundingBox = await row.boundingBox();
    expect(boundingBox).not.toBeNull();
    if (boundingBox) {
      // Prior to fix, 4-line wrapping resulted in 100px+ height.
      // Expected controlled row height is around 48px - 60px.
      expect(boundingBox.height).toBeLessThanOrEqual(64);
      expect(boundingBox.height).toBeGreaterThanOrEqual(44);
    }

    // Verify readable Agent name is displayed
    await expect(row.getByText("反欺诈风控助手")).toBeVisible();
    await expect(row.getByText("v2.1.0")).toBeVisible();
    await expect(row.getByText(longAgentId)).toBeVisible();

    // Verify Status & Progress on a single line
    await expect(row.getByText("75% · 15/20")).toBeVisible();

    // Verify Copy button functionality
    const copyBtn = row.getByRole("button", { name: `复制 Launch ID ${longUuid}` });
    await expect(copyBtn).toBeVisible();
    await copyBtn.click();

    // Check feedback icon changes to check
    await expect(copyBtn.locator(".lucide-check")).toBeVisible();
  });

  test("verifies horizontal scroll overflow on narrower screens (1024px) without collapsing layout", async ({ page }) => {
    // Narrow viewport (1024x720)
    await page.setViewportSize({ width: 1024, height: 720 });

    await page.goto("/launches");

    const table = page.locator("table");
    await expect(table).toBeVisible();

    // Table should maintain its min-width and trigger scroll in wrapper
    const overflowContainer = page.locator("div.overflow-x-auto");
    await expect(overflowContainer).toBeVisible();

    const scrollWidth = await overflowContainer.evaluate((el) => el.scrollWidth);
    const clientWidth = await overflowContainer.evaluate((el) => el.clientWidth);

    // At 1024px viewport (minus sidebar), the 1200px table triggers horizontal scrolling
    expect(scrollWidth).toBeGreaterThan(clientWidth);

    // Row height should still remain stable under 64px even on narrow viewport
    const row = table.locator("tbody tr").first();
    const boundingBox = await row.boundingBox();
    expect(boundingBox).not.toBeNull();
    if (boundingBox) {
      expect(boundingBox.height).toBeLessThanOrEqual(64);
    }
  });

  test("verifies graceful fallback when agents query fails", async ({ page }) => {
    await page.route("**/api/v1/agents*", async (route) => {
      await route.abort();
    });

    await page.goto("/launches");

    const table = page.locator("table");
    await expect(table).toBeVisible();

    // Falls back to agent_id as primary display without crashing the page
    const row = table.locator("tbody tr").first();
    await expect(row.getByText(longAgentId)).toBeVisible();
  });
});
