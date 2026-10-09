import AxeBuilder from "@axe-core/playwright";
import { test, expect, type Page } from "@playwright/test";

const AGENT_ID = "demo-banking-agent";
const LAUNCH_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6";

const agent = {
  id: AGENT_ID,
  name: "反欺诈风控助手",
  status: "active",
  version_count: 2,
  latest_version: "1.0.0",
  launch_count: 0,
  active_launch_count: 0,
  created_at: "2026-09-24T00:00:00Z",
};

const versions = [
  {
    id: "ver-1",
    agent_id: AGENT_ID,
    version: "1.0.0",
    endpoint: "http://demo-agent:8080/invoke",
    is_active: true,
    is_idempotent: true,
    spec_digest: "sha256:specs987654321",
    environment: "production",
    credential_ref: "env://API_TOKEN",
    artifact_ref: "git:abc1234",
    created_at: "2026-09-24T00:00:00Z",
  },
  {
    id: "ver-0",
    agent_id: AGENT_ID,
    version: "0.9.0",
    endpoint: "http://demo-agent:8080/invoke",
    is_active: false,
    is_idempotent: false,
    spec_digest: "sha256:specs111111111",
    environment: "staging",
    credential_ref: null,
    artifact_ref: null,
    created_at: "2026-09-20T00:00:00Z",
  },
];

const launch = {
  id: LAUNCH_ID,
  name: "run-regression-suite",
  status: "COMPLETED",
  quality_conclusion: "pass",
  dataset_name: "financial-transactions-regression-benchmark-dataset-v2",
  dataset_version: "2026-09-24T00:00:00Z",
  agent_id: AGENT_ID,
  agent_version: "1.0.0",
  agent_version_id: "ver-1",
  manifest: {
    schema_version: "1.0",
    dataset: {
      dataset_name: "financial-transactions-regression-benchmark-dataset-v2",
      dataset_version: "2026-09-24T00:00:00Z",
      snapshot_digest: "sha256:e2edigest12345678",
      items_count: 6,
    },
    agent: {
      id: AGENT_ID,
      version: "1.0.0",
      endpoint: "http://demo-agent:8080/invoke",
      spec_digest: "sha256:specs987654321",
    },
    evaluators: [{ id: "intent_match", version: "1.0.0", scope: "item" }],
    execution_policy: { timeout_seconds: 30, max_retries: 2, max_concurrency: 2 },
    runner: { runner_version: "0.1.0", mapping_engine_version: "sha256-mapping-engine-v1" },
  },
  langfuse_experiment_url: null,
  langfuse_sync_status: "SYNCED",
  created_at: "2026-09-24T00:00:00Z",
  started_at: "2026-09-24T00:01:00Z",
  completed_at: "2026-09-24T00:09:00Z",
  progress: {
    total: 6,
    completed: 6,
    percentage: 100,
    pending: 0,
    queued: 0,
    running: 0,
    retry_wait: 0,
    succeeded: 4,
    failed: 1,
    timed_out: 1,
    cancelled: 0,
  },
};

async function mockApi(page: Page): Promise<void> {
  await page.route("**/api/v1/system/info", (route) =>
    route.fulfill({
      json: {
        service: "argus-eval-runner",
        version: "0.2.0",
        build_id: "a11y-build",
        environment: "test",
      },
    }),
  );

  // Playwright matches routes newest-first, so the concrete detail and item
  // paths are registered after the collection globs they would otherwise be
  // swallowed by.
  // `?id=` / `?agent_id=` query strings are part of the matched URL, so the
  // collection globs need a trailing `*` or they never fire.
  await page.route("**/api/v1/experiment-launches*", (route) =>
    route.fulfill({ json: [launch] }),
  );
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/items`, (route) =>
    route.fulfill({
      json: [
        {
          id: "item-1",
          // LaunchDetail rejects items whose `launch_id` does not match the
          // page (the Issue #29 identity guard). The mock has to carry it or
          // the whole detail view falls into its error state.
          launch_id: LAUNCH_ID,
          dataset_item_id: "case-1",
          // ItemTable reads `execution_status` / `attempt_count`; an earlier
          // mock used `status` / `attempts` and rendered a table of blanks,
          // which made the attempt button unselectable.
          execution_status: "SUCCEEDED",
          quality_conclusion: "pass",
          attempt_count: 1,
          dispatch_generation: 1,
          scores: { intent_match: 1 },
          final_attempt_http_status: 200,
          final_attempt_latency_ms: 120,
          execution_error: null,
          eval_error: null,
        },
        {
          id: "item-2",
          launch_id: LAUNCH_ID,
          dataset_item_id: "case-2",
          execution_status: "FAILED",
          quality_conclusion: "fail",
          attempt_count: 2,
          dispatch_generation: 2,
          scores: { intent_match: 0 },
          final_attempt_http_status: 500,
          final_attempt_latency_ms: 3400,
          execution_error: "upstream returned 500",
          eval_error: null,
        },
      ],
    }),
  );
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/summary`, (route) =>
    route.fulfill({
      json: {
        launch_id: LAUNCH_ID,
        snapshot_id: "snap-1",
        versions: {
          agent: { id: AGENT_ID, version: "1.0.0" },
          evaluator: { id: "intent_match", version: "1.0.0" },
        },
        classification_counts: { pass: 4, fail: 2 },
        items: [],
      },
    }),
  );
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}`, (route) =>
    route.fulfill({ json: launch }),
  );

  await page.route("**/api/v1/execution-attempts*", (route) =>
    route.fulfill({
      json: [
        {
          id: "attempt-1",
          item_execution_id: "item-1",
          attempt_no: 1,
          status: "SUCCEEDED",
          http_status: 200,
          latency_ms: 120,
          started_at: "2026-09-24T00:01:00Z",
        },
      ],
    }),
  );

  await page.route("**/api/v1/agents*", (route) => route.fulfill({ json: [agent] }));
  await page.route("**/api/v1/agent-versions*", (route) =>
    route.fulfill({ json: versions }),
  );

  await page.route("**/api/v1/datasets*", (route) =>
    route.fulfill({
      json: [
        {
          name: "financial-transactions-regression-benchmark-dataset-v2",
          version: "2026-09-24T00:00:00Z",
          items_count: 6,
        },
      ],
    }),
  );
}

async function audit(page: Page, routePath: string): Promise<void> {
  await page.goto(routePath);
  await page.waitForLoadState("networkidle");

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();

  const summary = results.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    help: violation.help,
    nodes: violation.nodes.map(
      (node) =>
        `${node.target.join(" ")} :: ${(node.failureSummary ?? "").split("\n")[0] ?? ""}`,
    ),
  }));

  expect(summary, `${routePath} has WCAG 2.2 AA violations`).toEqual([]);
}

test.describe("WCAG 2.2 AA: rendered console routes", () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  for (const routePath of [
    "/launches",
    "/launches/new",
    `/launches/${LAUNCH_ID}`,
    "/agents",
    `/agents/${AGENT_ID}`,
    `/agents/${AGENT_ID}/versions/1.0.0`,
  ]) {
    test(`${routePath} has no violations`, async ({ page }) => {
      await audit(page, routePath);
    });
  }
});

async function auditOpen(page: Page, routePath: string, open: () => Promise<void>): Promise<void> {
  await page.goto(routePath);
  await page.waitForLoadState("networkidle");
  await open();

  // Guard against a vacuous pass. If the overlay never opened, axe would audit
  // the bare route underneath and report the same clean result it already
  // reports for that route on its own — the overlay would be untested while the
  // test showed green. Every overlay in this file is a dialog or a drawer, and
  // both carry role="dialog".
  const dialog = page.getByRole("dialog");
  await expect(dialog.first()).toBeVisible();
  await expect(dialog.first()).toContainText(/./);
  await page.waitForTimeout(200);

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();

  const summary = results.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    help: violation.help,
    nodes: violation.nodes.map(
      (node) =>
        `${node.target.join(" ")} :: ${(node.failureSummary ?? "").split("\n")[0] ?? ""}`,
    ),
  }));

  expect(summary, `${routePath} overlay has WCAG 2.2 AA violations`).toEqual([]);
}

test.describe("WCAG 2.2 AA: overlay surfaces", () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  test("register-agent dialog", async ({ page }) => {
    await auditOpen(page, "/agents", () =>
      page.getByRole("button", { name: /注册 Agent/ }).first().click(),
    );
  });

  test("create-version dialog", async ({ page }) => {
    await auditOpen(page, `/agents/${AGENT_ID}`, () =>
      page.getByRole("button", { name: "创建新版本" }).click(),
    );
  });

  test("delete-agent dialog", async ({ page }) => {
    await auditOpen(page, `/agents/${AGENT_ID}`, () =>
      page.getByRole("button", { name: "删除 Agent" }).click(),
    );
  });

  test("attempt drawer", async ({ page }) => {
    await auditOpen(page, `/launches/${LAUNCH_ID}?tab=cases`, () =>
      page.getByRole("button", { name: /次尝试/ }).first().click(),
    );
  });
});

/**
 * WCAG 2.2 SC 2.2.2 Pause, Stop, Hide covers information that starts
 * automatically, lasts more than five seconds and sits alongside other
 * content. A loading spinner that can run indefinitely qualifies; so does a
 * popover that slides and scales itself into place. `prefers-reduced-motion`
 * is the user's request to be spared that, and the console honoured no such
 * request anywhere before this.
 *
 * Disabling the motion is safe here precisely because every animation in the
 * console is paired with a text label -- `LoadingState` always takes a
 * message, the refresh buttons have names, the running badge carries a
 * count -- so the status survives without the movement.
 */
test.describe("prefers-reduced-motion", () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  test("the popover does not slide or scale itself in", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`/agents/${AGENT_ID}`);
    await page.waitForLoadState("networkidle");

    // The FieldHelp triggers live inside the create-version dialog, not on the
    // agent detail page itself.
    await page.getByRole("button", { name: "创建新版本" }).click();
    await page.locator("button[aria-expanded]").first().click();

    const popover = page.getByRole("dialog").last();
    await expect(popover).toBeVisible();

    const motion = await popover.evaluate((el) => {
      const cs = getComputedStyle(el);
      return {
        duration: cs.animationDuration,
        transitionDuration: cs.transitionDuration,
      };
    });

    // The entrance animation is neutralised rather than removed outright, so
    // the popover still becomes visible -- just without the movement.
    const instant = (value: string) =>
      value.split(",").every((part) => parseFloat(part) <= 0.001);

    expect(
      instant(motion.duration),
      `animation-duration ${motion.duration}`,
    ).toBe(true);
    expect(
      instant(motion.transitionDuration),
      `transition-duration ${motion.transitionDuration}`,
    ).toBe(true);
  });
});
