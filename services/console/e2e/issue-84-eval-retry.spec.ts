import { test, expect, type Page } from "@playwright/test";

/**
 * Issue #84 acceptance — the reviewer-visible contract of an evaluation-only
 * retry:
 *
 *   1. a case whose evaluation failed shows UNKNOWN plus a reason, and the
 *      Launch offers a *separate* "retry evaluation" action (not "retry case"),
 *   2. pressing it says the Agent output is reused and the Agent is NOT called
 *      again,
 *   3. while re-judging, the case shows "重评中" and the Launch keeps polling
 *      even though its status is already terminal,
 *   4. once re-judged the case shows "已恢复", and
 *   5. a blocked case (checkpoint gone) is explained, never silently ignored.
 *
 * These are mocked at the network layer because the timing/CAS behaviour is
 * covered by the backend suite; this spec proves the Console tells the truth
 * to a user and never implies the Agent was re-invoked.
 */

const AGENT_ID = "banking-agent";
const LAUNCH_ID = "launch-84";

type ItemSpec = Record<string, unknown>;

const baseItem = {
  id: "item-84-a",
  launch_id: LAUNCH_ID,
  dataset_item_id: "case-a",
  execution_status: "succeeded",
  eval_status: "failed",
  quality_conclusion: "unknown",
  execution_error: null,
  eval_error: null,
  evaluation_status: "none",
  evaluation_recoverable: true,
  evaluation_error: "EVALUATOR_TIMEOUT: pii_safe 评测超时",
  evaluation_generation: 0,
  trace_id: "trace-84",
  observation_id: null,
  final_attempt_id: "att-84-a",
  scores: {},
  evaluation_results: [],
  quality_evaluation: {
    conclusion: "unknown",
    policy_id: "custom",
    policy_version: "1.0",
    policy_digest: "sha256:" + "d".repeat(64),
    decided_by: "QUALITY_POLICY",
    releasable: false,
    unknown_reasons: ["必要指标 'pii_safe' 证据不足（评测执行失败），无法判定质量结论。"],
    rules: [
      {
        evaluator_id: "pii_safe",
        result_type: "boolean",
        required: true,
        critical: true,
        operator: "==",
        expected: true,
        observed_value: null,
        observed_status: "failed",
        conclusion: "unknown",
        reason_code: "EVALUATOR_TIMEOUT",
        explanation: "必要指标 'pii_safe' 证据不足（评测执行失败），无法判定质量结论。",
      },
    ],
  },
  attempt_count: 1,
  final_attempt_http_status: 200,
  final_attempt_latency_ms: 20,
  started_at: "2026-09-20T00:00:02Z",
  completed_at: "2026-09-20T00:00:03Z",
};

function buildLaunch(allowed: string[]) {
  return {
    id: LAUNCH_ID,
    name: "issue-84 launch",
    // Terminal already: a re-judgement does not move the Launch out of a
    // terminal status, which is exactly why polling must be driven by the
    // item-level evaluation_status instead.
    status: "PARTIAL_FAILED",
    quality_conclusion: "unknown",
    dataset_name: "banking-regression",
    dataset_version: "2026-09-20T00:00:00Z",
    agent_id: AGENT_ID,
    agent_version: "v2",
    langfuse_sync_status: "SYNCED",
    created_at: "2026-09-20T00:00:00Z",
    started_at: "2026-09-20T00:00:01Z",
    completed_at: "2026-09-20T00:00:12Z",
    manifest: { schema_version: "1.2", dataset: { items_count: 1 } },
    allowed_actions: allowed,
    progress: {
      total: 1,
      pending: 0,
      queued: 0,
      running: 0,
      retry_wait: 0,
      succeeded: 1,
      failed: 0,
      timed_out: 0,
      cancelled: 0,
      completed: 1,
      percentage: 100,
      attempts: 1,
      retries: 0,
      recoverable_evaluation_count: 1,
      allowed_actions: allowed,
    },
  };
}

/**
 * Route the Launch, the items and the retry endpoint. `itemsPhase` lets a test
 * flip the served item from "not re-judged" to "re-judging" to "recovered" after
 * the retry POST, so the polling contract can be observed.
 */
async function mockLaunchApi(
  page: Page,
  opts: {
    allowed?: string[];
    retryResponse?: Record<string, unknown>;
    itemsPhase?: () => ItemSpec[];
  } = {},
) {
  const allowed = opts.allowed ?? ["retry_evaluation"];

  await page.route("**/api/v1/experiment-launches/*/items**", async (route) => {
    const items = opts.itemsPhase ? opts.itemsPhase() : [baseItem];
    await route.fulfill({ json: items });
  });
  await page.route("**/api/v1/experiment-launches/*/retry-evaluation", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      json: opts.retryResponse ?? {
        launch: buildLaunch(allowed),
        submitted: ["case-a"],
        already_running: [],
        blocked: [],
        message: "已提交 1 个用例仅重试评测（复用原 Agent 输出，不会再次调用 Agent）。",
      },
    });
  });
  await page.route("**/api/v1/experiment-launches/*", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({ json: buildLaunch(allowed) });
  });
  await page.route("**/api/v1/experiment-launches", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({ json: [buildLaunch(allowed)] });
  });
  // Auxiliary panels must not receive a Launch payload.
  for (const suffix of ["summary", "comparison", "baselines"]) {
    await page.route(`**/api/v1/experiment-launches/${LAUNCH_ID}/${suffix}**`, async (route) => {
      await route.fulfill({ json: null });
    });
  }
}

test.describe("Issue #84 evaluation-only retry", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(`/launches/${LAUNCH_ID}`);
  });

  test("a failed evaluation is UNKNOWN with a reason and offers retry-evaluation, not retry-case", async ({
    page,
  }) => {
    await mockLaunchApi(page);
    await page.reload();

    // The failed evaluation is visible as its own failure reason.
    const recoveryCell = page.getByTestId("evaluation-recovery-case-a");
    await expect(recoveryCell).toContainText("EVALUATOR_TIMEOUT");

    // The action is a distinct "retry evaluation", never "retry failed case".
    const button = page.getByTestId("retry-evaluation-button");
    await expect(button).toBeVisible();
    await expect(page.getByText("重试评测失败 (Retry Evaluation)")).toBeVisible();

    // It must be explicit that the Agent is not invoked again.
    await expect(button).toHaveAttribute("title", /不会再次调用 Agent/);
  });

  test("submitting the retry reports reuse of the Agent output and re-judgement progress", async ({
    page,
  }) => {
    let retried = false;
    // Before the retry the case is idle; afterwards it is re-judged and then
    // recovered, which is what the terminal-status polling has to observe.
    await mockLaunchApi(page, {
      itemsPhase: () => [
        {
          ...baseItem,
          evaluation_status: retried ? "evaluating" : "none",
          evaluation_recoverable: !retried,
          evaluation_error: retried ? null : baseItem.evaluation_error,
        },
      ],
    });
    await page.route("**/api/v1/experiment-launches/*/retry-evaluation", async (route) => {
      retried = true;
      await route.fulfill({
        json: {
          launch: buildLaunch(["retry_evaluation"]),
          submitted: ["case-a"],
          already_running: [],
          blocked: [],
          message: "已提交 1 个用例仅重试评测（复用原 Agent 输出，不会再次调用 Agent）。",
        },
      });
    });
    await page.reload();

    await page.getByTestId("retry-evaluation-button").click();

    // The success notice states that the original Agent output is reused.
    const notice = page.getByTestId("retry-evaluation-notice");
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("复用原 Agent 输出");
    await expect(notice).toContainText("不会再次调用 Agent");

    // The case is now shown as re-judged: "重评中", and the reuse guarantee is
    // restated with no stale error.
    await expect(page.getByText("重评中")).toBeVisible();
    const recoveryCell = page.getByTestId("evaluation-recovery-case-a");
    await expect(recoveryCell).toContainText("复用原 Agent 输出");
    await expect(recoveryCell).not.toContainText("EVALUATOR_TIMEOUT");
  });

  test("a recovered case shows 已恢复 and no longer advertises recoverability", async ({ page }) => {
    await mockLaunchApi(page, {
      itemsPhase: () => [
        {
          ...baseItem,
          eval_status: "succeeded",
          evaluation_status: "recovered",
          evaluation_recoverable: false,
          evaluation_error: null,
        },
      ],
    });
    await page.reload();
    await expect(page.getByText("已恢复")).toBeVisible();
    // A settled, recovered case shows no error and no pending action.
    await expect(page.getByTestId("evaluation-recovery-case-a")).toHaveCount(0);
  });

  test("a blocked case is explained instead of silently doing nothing", async ({ page }) => {
    await mockLaunchApi(page, {
      retryResponse: {
        launch: buildLaunch(["retry_evaluation"]),
        submitted: [],
        already_running: [],
        blocked: [
          {
            item_execution_id: "item-84-a",
            dataset_item_id: "case-a",
            code: "CHECKPOINT_EXPIRED",
            message: "检查点已过期，无法仅重试评测。",
            hint: "请重新执行该用例。",
          },
        ],
        message:
          "所有候选评测都已在重评中，未产生重复任务。 1 个用例因检查点不可用被阻止。",
      },
    });
    await page.reload();

    await page.getByTestId("retry-evaluation-button").click();
    const notice = page.getByTestId("retry-evaluation-notice");
    await expect(notice).toContainText("检查点已过期");
  });

  test("no retry-evaluation action is offered when the Launch does not allow it", async ({ page }) => {
    await mockLaunchApi(page, { allowed: [] });
    await page.reload();
    await expect(page.getByTestId("retry-evaluation-button")).toHaveCount(0);
  });
});
