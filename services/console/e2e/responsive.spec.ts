import { test, expect, type Page, type Locator } from "@playwright/test";

import type { components } from "../src/api/schema";

type Agent = components["schemas"]["AgentResponse"];
type Version = components["schemas"]["AgentVersionResponse"];
type Evaluator = components["schemas"]["EvaluatorResponse"];
type CreateRequest = components["schemas"]["ExperimentLaunchCreateRequest"];

const AGENT_ID = "financial-fraud-detection-assistant-enterprise-prod";
const LONG_ENDPOINT = `https://agents.example.test/${"immutable-release/".repeat(12)}invoke`;
const LONG_MAPPING = `{{ input.${"financial_transaction_".repeat(20)}user_message }}`;
const LAUNCH_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6";

const ROUTES = [
  "/launches",
  "/launches/new",
  `/launches/${LAUNCH_ID}`,
  "/agents",
  `/agents/${AGENT_ID}`,
  `/agents/${AGENT_ID}/versions/1.0.0`,
];

const agent: Agent = {
  id: AGENT_ID,
  name: "反欺诈风控助手",
  status: "active",
  version_count: 2,
  latest_version: "1.0.0",
  launch_count: 1,
  active_launch_count: 0,
  created_at: "2026-09-24T00:00:00Z",
  updated_at: "2026-09-24T00:00:00Z",
};

const versions: Version[] = [
  {
    id: "ver-1",
    agent_id: AGENT_ID,
    version: "1.0.0",
    endpoint: LONG_ENDPOINT,
    protocol: "http", method: "POST", timeout_seconds: 30, max_retries: 2,
    rate_limit_per_minute: 60, max_concurrency: 3, trace_propagation: "w3c",
    is_active: true,
    is_idempotent: true,
    spec_digest: `sha256:${"a".repeat(64)}`,
    environment: "production",
    credential_ref: "env://API_TOKEN",
    artifact_ref: "git:abc1234",
    request_mapping: { query: LONG_MAPPING },
    request_schema: { type: "object", properties: { query: { type: "string" } } },
    response_schema: { type: "object", properties: { answer: { type: "string" } } },
    created_at: "2026-09-24T00:00:00Z",
  },
  {
    id: "ver-0",
    agent_id: AGENT_ID,
    version: "0.9.0",
    endpoint: LONG_ENDPOINT,
    protocol: "http", method: "POST", timeout_seconds: 30, max_retries: 2,
    rate_limit_per_minute: 60, max_concurrency: 3, trace_propagation: "w3c",
    is_active: false,
    is_idempotent: false,
    spec_digest: "sha256:specs111111111",
    environment: "staging",
    credential_ref: null,
    artifact_ref: null,
    request_mapping: {},
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
      items_count: 2,
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
    total: 2,
    completed: 2,
    percentage: 100,
    pending: 0,
    queued: 0,
    running: 0,
    retry_wait: 0,
    succeeded: 2,
    failed: 0,
    timed_out: 0,
    cancelled: 0,
  },
};

async function mockApi(page: Page): Promise<void> {
  await page.route("**/api/v1/system/info", (route) =>
    route.fulfill({
      json: {
        service: "argus-eval-runner",
        version: "0.2.0",
        build_id: "responsive-build",
        environment: "test",
      },
    }),
  );

  // Newest-first: concrete paths before the collection globs.
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/items`, (route) =>
    route.fulfill({
      json: [
        {
          id: "item-1",
          launch_id: LAUNCH_ID,
          dataset_item_id: "case-1",
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
      ],
    }),
  );
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/summary`, (route) =>
    route.fulfill({
      json: {
        launch_id: LAUNCH_ID,
        snapshot_id: "snap-1",
        versions: { agent: { id: AGENT_ID, version: "1.0.0" } },
        classification_counts: { pass: 2, fail: 0 },
        items: [],
      },
    }),
  );
  await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}`, (route) =>
    route.fulfill({ json: launch }),
  );
  await page.route("**/api/v1/experiment-launches*", (route) =>
    route.fulfill({ json: [launch] }),
  );
  await page.route("**/api/v1/agents*", (route) => {
    const id = new URL(route.request().url()).searchParams.get("id");
    return route.fulfill({ json: id ? agent : [agent] });
  });
  await page.route("**/api/v1/agent-versions*", (route) => {
    const version = new URL(route.request().url()).searchParams.get("version");
    return route.fulfill({ json: version ? versions.find((v) => v.version === version) : versions });
  });
  const evaluators: Evaluator[] = [
    { id: "intent_match", version: "1.0.0", scope: "item", threshold: 1,
      description: "确定性意图匹配", default_selected: true, composed_of: [],
      direction: "higher_is_better", critical: false },
    { id: "overall_pass", version: "1.0.0", scope: "item", threshold: 1,
      description: "复合质量结论", default_selected: false, composed_of: ["intent_match"],
      direction: "higher_is_better", critical: true },
    { id: "run_pass_rate", version: "1.0.0", scope: "run", threshold: 1,
      description: "运行级质量聚合", default_selected: false, composed_of: [],
      direction: "higher_is_better", critical: false },
  ];
  await page.route("**/api/v1/evaluators", (route) => route.fulfill({ json: evaluators }));
  await page.route("**/api/v1/datasets*", (route) =>
    route.fulfill({
      json: [
        {
          name: "financial-transactions-regression-benchmark-dataset-v2",
          version: "2026-09-24T00:00:00Z",
          items_count: 2,
        },
      ],
    }),
  );
}

/**
 * Controls that sit outside the viewport are only acceptable when a
 * scrollable ancestor can bring them into view. An `overflow: hidden`
 * ancestor makes the control unreachable: it is still in the accessibility
 * tree, still focusable by tab, and still announced — but a pointer or touch
 * user can never activate it.
 */
async function unreachableControls(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const found: string[] = [];
    for (const el of Array.from(
      document.querySelectorAll<HTMLElement>("button, a, input, select, textarea"),
    )) {
      const box = el.getBoundingClientRect();
      if (box.width === 0 || box.right <= vw + 1) continue;

      let scrollable = false;
      let clipped = false;
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const overflowX = getComputedStyle(p).overflowX;
        if (/auto|scroll/.test(overflowX)) {
          scrollable = true;
          break;
        }
        if (/hidden|clip/.test(overflowX)) {
          clipped = true;
          break;
        }
      }
      if (clipped && !scrollable) {
        const label =
          el.getAttribute("aria-label") ??
          el.getAttribute("title") ??
          (el.textContent ?? "").trim();
        found.push(`<${el.tagName.toLowerCase()}> ${label.slice(0, 40)}`);
      }
    }
    return found;
  });
}

async function expectPageReady(page: Page, routePath: string) {
  const heading = page.getByRole("heading", { level: 1 });
  await expect(heading).toBeVisible();
  if (routePath === "/agents") {
    await expect(heading).toHaveText("Agent Registry");
    await expect(page.getByRole("cell", { name: new RegExp(agent.name) })).toBeVisible();
  } else if (routePath === `/agents/${AGENT_ID}`) {
    await expect(heading).toContainText(agent.name);
    await expect(page.getByRole("link", { name: "查看配置" })).toHaveCount(2);
  } else if (routePath.includes("/versions/")) {
    await expect(heading).toContainText("1.0.0");
    await expect(page.getByText(LONG_ENDPOINT, { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "归档此版本" })).toBeVisible();
    await expect(page.locator("pre").first()).toContainText(LONG_MAPPING);
  } else if (routePath === "/launches/new") {
    await expect(heading).toContainText("发起新评测任务");
    await expect(page.getByRole("combobox", { name: /选择版本规格/ })).toHaveValue("1.0.0");
    await expect(page.getByRole("checkbox", { name: /intent_match/ })).toBeChecked();
  } else if (routePath === "/launches") {
    await expect(page.locator("tbody tr")).toHaveCount(1);
  } else {
    await expect(page.getByTestId("status-badge").first()).toHaveText(/COMPLETED/);
  }
}

async function expectNoPageOverflow(page: Page) {
  const overflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    main: document.querySelector("main")!.scrollWidth - document.querySelector("main")!.clientWidth,
  }));
  expect(overflow.document, "整页不能横向滚动").toBeLessThanOrEqual(1);
  expect(overflow.main, "核心内容只能纵向滚动，宽表格/JSON 应局部横向滚动").toBeLessThanOrEqual(1);
}

async function expectReachable(locator: Locator) {
  await locator.scrollIntoViewIfNeeded();
  await expect(locator).toBeVisible();
  await expect(locator).toBeInViewport();
  const geometry = await locator.evaluate((el) => {
    const rect = el.getBoundingClientRect();
    const main = el.closest("main")!.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
    return {
      contained: rect.left >= main.left - 1 && rect.right <= main.right + 1 &&
        rect.top >= main.top - 1 && rect.bottom <= main.bottom + 1,
      unobstructed: hit !== null && el.contains(hit),
    };
  });
  expect(geometry, "元素必须位于内容视口内，且不能被其它元素遮挡").toEqual({ contained: true, unobstructed: true });
}

async function expectSeparated(a: Locator, b: Locator) {
  const first = await a.boundingBox();
  const second = await b.boundingBox();
  expect(first).not.toBeNull();
  expect(second).not.toBeNull();
  if (first && second) {
    const overlapX = Math.min(first.x + first.width, second.x + second.width) - Math.max(first.x, second.x);
    const overlapY = Math.min(first.y + first.height, second.y + second.height) - Math.max(first.y, second.y);
    expect(overlapX > 1 && overlapY > 1, "标题与主要操作不得相互覆盖").toBe(false);
  }
}

test.describe("narrow viewports", () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  for (const width of [390, 768]) {
    for (const routePath of ROUTES) {
      test(`${routePath} keeps every control reachable at ${width}px`, async ({
        page,
      }) => {
        await page.setViewportSize({ width, height: 900 });
        await page.goto(routePath);
        await expectPageReady(page, routePath);

        expect(
          await unreachableControls(page),
          `${routePath} at ${width}px has controls clipped out of reach`,
        ).toEqual([]);
      });
    }
  }

  for (const routePath of ROUTES) {
    test(`${routePath} does not scroll the page sideways at 390px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: 390, height: 900 });
      await page.goto(routePath);
      await expectPageReady(page, routePath);

      // Wide tables are fine as long as they scroll inside their own
      // container; a sideways-scrolling page loses the sidebar and the
      // header, which do not scroll with it.
      const overflowX = await page.evaluate(
        () =>
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      );
      expect(
        overflowX,
        `${routePath} scrolls sideways at 390px`,
      ).toBeLessThanOrEqual(1);
    });
  }
});


test.describe("Issue #47: 核心页面布局与操作闭环", () => {
  test.beforeEach(async ({ page }) => { await mockApi(page); });

  for (const width of [1440, 1024]) {
    test(`Agents → Agent → Version 在 ${width}×720 可读且可操作`, async ({ page, context }) => {
      await context.grantPermissions(["clipboard-read", "clipboard-write"]);
      await page.setViewportSize({ width, height: 720 });
      await page.goto("/agents");
      await expectPageReady(page, "/agents");
      const heading = page.getByRole("heading", { level: 1 });
      const register = page.getByRole("button", { name: "注册 Agent", exact: true });
      await expectReachable(heading);
      await expectReachable(register);
      await expectSeparated(heading, register);
      await expectNoPageOverflow(page);
      // 横向滚动只在表格 wrapper 中发生，管理链接必须仍可真正点击。
      const manage = page.getByRole("link", { name: "管理", exact: true });
      await expectReachable(manage);
      await manage.click();
      await expect(page).toHaveURL(`/agents/${AGENT_ID}`);
      await expectPageReady(page, `/agents/${AGENT_ID}`);
      const createVersion = page.getByRole("button", { name: "创建新版本" });
      await expectReachable(heading);
      await expectReachable(createVersion);
      await expectSeparated(heading, createVersion);
      await createVersion.click();
      const dialog = page.getByRole("dialog");
      await expect(dialog.getByRole("heading", { name: "创建 AgentVersion 规格快照" })).toBeVisible();
      await expectReachable(dialog.getByRole("button", { name: "取消" }));
      await dialog.getByRole("button", { name: "取消" }).click();
      await expect(dialog).toHaveCount(0);
      await expectNoPageOverflow(page);
      const inspect = page.getByRole("row").filter({ has: page.getByRole("cell", { name: "1.0.0", exact: true }) }).getByRole("link", { name: "查看配置" });
      await expectReachable(inspect);
      await inspect.click();
      await expect(page).toHaveURL(`/agents/${AGENT_ID}/versions/1.0.0`);
      await expectPageReady(page, `/agents/${AGENT_ID}/versions/1.0.0`);
      const archive = page.getByRole("button", { name: "归档此版本" });
      await expectReachable(heading);
      await expectReachable(archive);
      await expectSeparated(heading, archive);
      await expectNoPageOverflow(page);
      await expectReachable(page.getByTitle("显示引用名"));
      await page.getByTitle("显示引用名").click();
      await expect(page.getByTestId("secret-ref")).toContainText("env://API_TOKEN");
      // JSON 长行必须保持在 pre 内局部滚动，而不是裁掉或撑开 main。
      const mapping = page.locator("pre").first();
      await mapping.scrollIntoViewIfNeeded();
      expect(await mapping.evaluate((el) => getComputedStyle(el).overflowX)).toMatch(/auto|scroll/);
      expect(await mapping.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
      expect(await mapping.evaluate((el) => { el.scrollLeft = el.scrollWidth; return el.scrollLeft; })).toBeGreaterThan(0);
      const copy = mapping.locator("..").getByRole("button");
      await expectReachable(copy);
      await copy.click();
      await expect(copy).toHaveText("已复制");
      await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(JSON.stringify(versions[0].request_mapping, null, 2));
      const back = page.getByRole("link", { name: `返回 Agent (${AGENT_ID}) 详情` });
      await expectReachable(back);
      await back.click();
      await expectPageReady(page, `/agents/${AGENT_ID}`);
    });

    test(`创建表单在 ${width}×720 经纵向滚动填写、提交并跳转`, async ({ page }) => {
      await page.setViewportSize({ width, height: 720 });
      let submitted: CreateRequest | undefined;
      await page.route("**/api/v1/experiment-launches", async (route) => {
        if (route.request().method() !== "POST") return route.fulfill({ json: [launch] });
        submitted = route.request().postDataJSON() as CreateRequest;
        return route.fulfill({ status: 201, json: { ...launch, name: submitted.name } });
      });
      await page.goto("/launches/new");
      await expectPageReady(page, "/launches/new");
      await expectReachable(page.getByRole("heading", { level: 1 }));
      await expectNoPageOverflow(page);
      expect(await page.locator("main").evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
      const name = page.getByRole("textbox", { name: /评测任务名称/ });
      const environment = page.getByRole("textbox", { name: /Environment/ });
      const agentSelect = page.getByRole("combobox", { name: "选择 Agent", exact: false });
      const versionSelect = page.getByRole("combobox", { name: /选择版本规格/ });
      const dataset = page.getByRole("textbox", { name: /数据集名称/ });
      const concurrency = page.getByRole("spinbutton", { name: /最大并发执行数/ });
      for (const control of [name, environment, agentSelect, versionSelect, dataset, concurrency]) {
        await expectReachable(control);
      }
      await expectSeparated(agentSelect, versionSelect);
      await name.fill(`responsive-${width}`);
      await environment.fill("staging");
      await agentSelect.selectOption(AGENT_ID);
      await versionSelect.selectOption("1.0.0");
      await expect(versionSelect.locator("option")).toHaveCount(1);
      await dataset.fill("responsive-regression");
      await page.getByRole("radio", { name: "指定快照时间戳" }).check();
      const snapshot = page.getByPlaceholder("例如: 2026-09-20T08:35:12Z");
      await expectReachable(snapshot);
      await snapshot.fill("2026-10-01T00:00:00Z");
      const evaluator = page.getByRole("checkbox", { name: /intent_match/ });
      await expectReachable(evaluator);
      await evaluator.uncheck();
      await evaluator.check();
      await concurrency.fill("2");
      const submit = page.getByRole("button", { name: /创建评测任务/ });
      const cancel = page.getByRole("link", { name: "取消", exact: true });
      await expectReachable(cancel);
      await expectReachable(submit);
      await expectSeparated(cancel, submit);
      await expectNoPageOverflow(page);
      expect(await page.locator("main").evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
      await expect(submit).toBeEnabled();
      await submit.click();
      await expect(page).toHaveURL(`/launches/${LAUNCH_ID}`);
      expect(submitted).toEqual({ name: `responsive-${width}`, agent_id: AGENT_ID,
        agent_version: "1.0.0", dataset_name: "responsive-regression", dataset_version: "2026-10-01T00:00:00Z",
        environment: "staging", evaluator_ids: ["intent_match"], max_concurrency: 2 });
      await expectPageReady(page, `/launches/${LAUNCH_ID}`);
    });
  }
});
