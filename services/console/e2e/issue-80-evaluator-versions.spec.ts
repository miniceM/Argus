import { test, expect } from "@playwright/test";
import {
  buildEvaluatorCatalog,
  itemEvaluatorFixture,
  type EvaluatorFixture,
} from "./fixtures/evaluators";

const AGENT_ID = "banking-agent";

const twoVersionIntent: EvaluatorFixture = itemEvaluatorFixture(
  "intent_match",
  "意图匹配评测器",
  {
    default_selected: true,
    default_version: "1.0.0",
    versions: [
      {
        version: "1.0.0",
        result_type: "numeric",
        scope: "item",
        threshold: 1.0,
        direction: "higher_is_better",
        critical: false,
        input_contract: { type: "object", description: "v1 输入契约" },
        output_contract: { type: "number" },
        param_schema: { type: "object", properties: {} },
        implementation_ref: "builtin:intent_match@1.0.0",
        executor_type: "builtin_python",
        content_digest: "sha256:intent-v1",
        release_eligible: true,
        eligibility_reasons: [],
        eligibility_messages: [],
      },
      {
        version: "2.0.0",
        result_type: "numeric",
        scope: "item",
        threshold: 0.8,
        direction: "higher_is_better",
        critical: false,
        input_contract: { type: "object", description: "v2 输入契约" },
        output_contract: { type: "number" },
        param_schema: { type: "object", properties: {} },
        implementation_ref: "builtin:intent_match@2.0.0",
        executor_type: "builtin_python",
        content_digest: "sha256:intent-v2",
        release_eligible: true,
        eligibility_reasons: [],
        eligibility_messages: [],
      },
    ],
  },
);

const onlineOnly: EvaluatorFixture = itemEvaluatorFixture("online_tone_rule", "Langfuse 在线规则", {
  default_selected: false,
  release_eligible: false,
  eligibility_reasons: ["EXECUTION_OWNER_NOT_ARGUS"],
  default_version: "1.0.0",
  versions: [
    {
      version: "1.0.0",
      result_type: "numeric",
      scope: "item",
      threshold: 1.0,
      direction: "higher_is_better",
      critical: false,
      input_contract: {},
      output_contract: { type: "number" },
      param_schema: {},
      implementation_ref: "langfuse:rule@1",
      executor_type: "builtin_python",
      content_digest: "sha256:online-rule",
      release_eligible: false,
      eligibility_reasons: ["EXECUTION_OWNER_NOT_ARGUS"],
      eligibility_messages: ["该版本由 Langfuse 在线执行，不由 Argus 冻结执行，无法作为发布评测证据。"],
    },
  ],
});

async function mockCreatePage(
  page: import("@playwright/test").Page,
  catalog: () => EvaluatorFixture[],
  createdPayloads: Record<string, unknown>[],
) {
  await page.route("**/api/v1/agents", async (route) => {
    await route.fulfill({
      json: [{ id: AGENT_ID, name: "Banking Agent", version_count: 1, latest_version: "v1" }],
    });
  });
  await page.route("**/api/v1/agent-versions**", async (route) => {
    await route.fulfill({
      json: [{ id: "ver-1", agent_id: AGENT_ID, version: "v1", is_active: true }],
    });
  });
  await page.route("**/api/v1/evaluators", async (route) => {
    await route.fulfill({ json: catalog() });
  });
  await page.route("**/api/v1/experiment-launches", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }
    createdPayloads.push(route.request().postDataJSON());
    await route.fulfill({
      status: 201,
      json: {
        id: "launch-80",
        name: "issue-80",
        status: "PENDING",
        quality_conclusion: "unknown",
        dataset_name: "banking-agent-regression",
        agent_id: AGENT_ID,
        agent_version: "v1",
        agent_version_id: "ver-1",
        manifest: {},
        langfuse_sync_status: "PENDING",
        created_at: new Date().toISOString(),
      },
    });
  });
}

test.describe("Issue #80: exact Evaluator version and release eligibility", () => {
  test("pins the user-confirmed version and survives a catalog default bump", async ({ page }) => {
    const payloads: Record<string, unknown>[] = [];
    let catalogDefault = "1.0.0";

    const buildCatalog = () => {
      const promoted = {
        ...twoVersionIntent,
        default_version: catalogDefault,
        versions: twoVersionIntent.versions,
      };
      return [promoted];
    };

    await mockCreatePage(page, buildCatalog, payloads);
    await page.goto("/launches/new");

    const versionSelect = page.getByLabel("intent_match 版本");
    await expect(versionSelect).toHaveValue("1.0.0");

    // The user confirms v1 and reads its contract.
    await versionSelect.selectOption("1.0.0");
    await page.getByText("查看输入/输出契约与制品标识").click();
    await expect(page.getByText(/v1 输入契约/)).toBeVisible();
    await expect(page.getByText(/sha256:intent-v1/)).toBeVisible();

    // A catalog refresh promotes v2 as the new default.
    catalogDefault = "2.0.0";
    await page.reload();
    await page.getByLabel("intent_match 版本").waitFor();

    // The default has moved on, but nothing is silently substituted for the
    // version the user already confirmed in this form session.
    await page.getByLabel("intent_match 版本").selectOption("1.0.0");
    await page.getByRole("button", { name: /创建评测任务/ }).click();
    await expect.poll(() => payloads.length).toBe(1);
    expect(payloads[0].evaluator_selections).toEqual([{ id: "intent_match", version: "1.0.0" }]);
  });

  test("shows the ineligible reason and blocks submission for a Langfuse-owned rule", async ({
    page,
  }) => {
    const payloads: Record<string, unknown>[] = [];
    await mockCreatePage(page, () => [onlineOnly], payloads);
    await page.goto("/launches/new");

    // The reason is visible on the card before the user commits to it.
    await expect(page.getByText("不可用于发布评测").first()).toBeVisible();
    await expect(
      page.getByText("该版本由 Langfuse 在线执行，不由 Argus 冻结执行，无法作为发布评测证据。"),
    ).toBeVisible();

    // Selecting it surfaces the blocking reason and prevents creation.
    await page.getByRole("checkbox", { name: /online_tone_rule/ }).check();
    await expect(page.getByRole("alert")).toContainText(
      "该版本由 Langfuse 在线执行，不由 Argus 冻结执行，无法作为发布评测证据。",
    );
    await expect(page.getByRole("button", { name: /创建评测任务/ })).toBeDisabled();
    expect(payloads).toHaveLength(0);
  });

  test("keeps the four built-in diagnostics selectable", async ({ page }) => {
    const payloads: Record<string, unknown>[] = [];
    await mockCreatePage(page, () => buildEvaluatorCatalog(), payloads);
    await page.goto("/launches/new");

    await expect(page.getByText("已选 4 项")).toBeVisible();
    for (const id of ["escalation_match", "intent_match", "pii_safe", "required_tool_match"]) {
      await expect(page.getByRole("checkbox", { name: new RegExp(id) })).toBeChecked();
    }
  });
});
