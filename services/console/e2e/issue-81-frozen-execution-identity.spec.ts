import { test, expect, type Page } from "@playwright/test";

/**
 * Issue #81 acceptance: the Launch detail must expose the frozen evaluation
 * execution identity (version, implementation, digests, verification status)
 * and turn an unrecoverable frozen artifact into an actionable failure.
 */

const FROZEN_DIGEST = "sha256:" + "a".repeat(64);
const ARTIFACT_DIGEST = "sha256:" + "c".repeat(64);

const frozenBinding = {
  id: "intent_match",
  version: "1.0.0",
  scope: "item",
  threshold: 1,
  binding_id: "bind_0123456789abcdef",
  binding_digest: FROZEN_DIGEST,
  binding_schema_version: "1.2",
  definition_digest: "sha256:" + "b".repeat(64),
  content_digest: "b".repeat(64),
  implementation_ref: "builtin:intent_match@1.0.0",
  executor_type: "builtin_python",
  contract_status: "FROZEN_VERIFIED",
  verification_status: "RECORDED",
  implementation_artifact: {
    kind: "python_source",
    locator: "app.evaluators:intent_match",
    digest: ARTIFACT_DIGEST,
    runtime: "cpython",
  },
  runner: {
    runner_version: "0.1.0",
    build_id: "issue-81-e2e",
    mapping_engine_version: "sha256-mapping-engine-v1",
  },
};

const baseLaunch = {
  id: "launch-issue-81",
  name: "frozen identity launch",
  status: "PENDING",
  quality_conclusion: "unknown",
  dataset_name: "banking-regression",
  dataset_version: "2026-09-20T00:00:00Z",
  agent_id: "banking-agent",
  agent_version: "v2",
  langfuse_sync_status: "PENDING",
  langfuse_experiment_url: null,
  created_at: "2026-09-20T00:00:00Z",
  started_at: null,
  completed_at: null,
};

const baseItem = {
  id: "item-issue-81",
  launch_id: "launch-issue-81",
  dataset_item_id: "case-001",
  execution_status: "succeeded",
  eval_status: "succeeded",
  quality_conclusion: "pass",
  execution_error: null,
  eval_error: null,
  trace_id: null,
  observation_id: null,
  final_attempt_id: null,
  scores: { intent_match: 1 },
  attempt_count: 1,
  final_attempt_http_status: 200,
  final_attempt_latency_ms: 20,
  started_at: "2026-09-20T00:00:01Z",
  completed_at: "2026-09-20T00:00:02Z",
};

async function mockDetail(
  page: Page,
  { schemaVersion, evaluators, items = [baseItem] }: {
    schemaVersion: string;
    evaluators: unknown[];
    items?: unknown[];
  },
) {
  await page.route("**/api/v1/system/info", (route) =>
    route.fulfill({
      json: { service: "argus-eval-runner", version: "0.2.0", build_id: "issue-81", environment: "test" },
    }),
  );
  await page.route("**/api/v1/experiment-launches**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/api/v1/experiment-launches") {
      await route.fulfill({ json: [baseLaunch] });
      return;
    }
    if (pathname.endsWith("/items")) {
      await route.fulfill({ json: items });
      return;
    }
    await route.fulfill({
      json: {
        ...baseLaunch,
        manifest: {
          schema_version: schemaVersion,
          dataset: { dataset_name: "banking-regression", items_count: items.length, snapshot_digest: "sha256:ds81" },
          agent: { id: "banking-agent", version: "v2", endpoint: "http://127.0.0.1:18082/invoke", spec_digest: "sha256:spec81" },
          evaluators,
          execution_policy: { max_concurrency: 2, timeout_seconds: 30, max_retries: 1 },
          runner: { runner_version: "0.1.0", mapping_engine_version: "sha256-mapping-engine-v1" },
        },
      },
    });
  });
}

test.describe("Issue #81: frozen evaluation execution identity", () => {
  test("shows the frozen version, implementation and verification status", async ({ page }) => {
    await mockDetail(page, { schemaVersion: "1.2", evaluators: [frozenBinding] });
    await page.goto("/launches/launch-issue-81");

    await expect(page.getByTestId("manifest-schema-version")).toContainText("Schema v1.2");
    await expect(page.getByTestId("binding-verification-intent_match")).toHaveText("已冻结校验");
    await expect(page.getByText("builtin:intent_match@1.0.0")).toBeVisible();

    await page.getByText("查看冻结摘要与制品标识").click();
    await expect(page.getByText("bind_0123456789abcdef")).toBeVisible();
    await expect(page.getByText(FROZEN_DIGEST)).toBeVisible();
    await expect(
      page.getByText(`${ARTIFACT_DIGEST} (app.evaluators:intent_match)`),
    ).toBeVisible();
  });

  test("marks a pre-#81 Manifest as 历史契约未记录 without crashing", async ({ page }) => {
    await mockDetail(page, {
      schemaVersion: "1.1",
      evaluators: [{ id: "pii_safe", version: "1.0.0", scope: "item", threshold: 1 }],
    });
    await page.goto("/launches/launch-issue-81");

    await expect(page.getByTestId("manifest-schema-version")).toContainText("Schema v1.1");
    await expect(page.getByTestId("binding-verification-pii_safe")).toHaveText("历史契约未记录");
    await expect(page.getByText("查看冻结摘要与制品标识")).toHaveCount(0);
  });

  test("turns a missing frozen artifact into an actionable failure", async ({ page }) => {
    await mockDetail(page, {
      schemaVersion: "1.2",
      evaluators: [frozenBinding],
      items: [
        {
          ...baseItem,
          execution_status: "failed",
          eval_status: "skipped",
          quality_conclusion: "unknown",
          scores: {},
          attempt_count: 0,
          final_attempt_http_status: null,
          final_attempt_latency_ms: null,
          execution_error:
            "EVALUATOR_ARTIFACT_DIGEST_MISMATCH: expected=" +
            ARTIFACT_DIGEST +
            " actual=sha256:" +
            "d".repeat(64),
        },
      ],
    });
    await page.goto("/launches/launch-issue-81");

    // Stable error code plus the recovery path, instead of a silent re-run.
    await expect(page.getByText(/EVALUATOR_ARTIFACT_DIGEST_MISMATCH/)).toBeVisible();
    await expect(page.getByTestId("frozen-recovery-case-001")).toContainText(
      "存在篡改或版本漂移",
    );
    await expect(page.getByTestId("frozen-recovery-case-001")).toContainText("另建 Launch");
  });
});
