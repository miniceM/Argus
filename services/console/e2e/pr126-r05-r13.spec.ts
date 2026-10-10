import { test, expect, type Page, type TestInfo } from "@playwright/test";

const R05_LAUNCH_ID = "launch-pr126-r05-evaluation-only";
const R13_ACTIONS_LAUNCH_ID = "launch-pr126-r13-action-matrix";
const R13_DRAWER_LAUNCH_ID = "launch-pr126-r13-attempt-drawer";
const S1 = "snapshot-r05-s1";
const S2 = "snapshot-r05-s2";

const frozenItem = (datasetItemId: string) => ({
  dataset_item_id: datasetItemId,
  execution_status: "SUCCEEDED",
  eval_status: "FAILED",
  quality_conclusion: "unknown",
  scores: {},
  trace_url: "https://langfuse.example/project/demo/traces/r05",
  latency_ms: 84,
});

const revision = (snapshotId: string, number: number, latest: boolean) => ({
  snapshot_id: snapshotId,
  revision: number,
  is_latest: latest,
  created_at: `2026-10-10T10:00:0${number}Z`,
  evidence_state: "COMPLETE",
  evidence_reasons: [],
  quality_pass_count: 0,
  quality_fail_count: 0,
  quality_unknown_count: 1,
  total_cases: 1,
  source_result_digest: `sha256:r05-${number}`,
  manifest_digest: "sha256:manifest-r05",
});

function launchPayload(
  launchId: string,
  status: "RUNNING" | "COMPLETED",
  allowedActions: string[],
) {
  const running = status === "RUNNING";
  return {
    id: launchId,
    name: launchId,
    status,
    quality_conclusion: "unknown",
    dataset_name: "pr126-acceptance-dataset",
    dataset_version: "v1",
    agent_id: "acceptance-agent",
    agent_version: "v1",
    langfuse_sync_status: "PENDING",
    langfuse_experiment_url: null,
    created_at: "2026-10-10T10:00:00Z",
    started_at: "2026-10-10T10:00:01Z",
    completed_at: running ? null : "2026-10-10T10:00:02Z",
    allowed_actions: allowedActions,
    manifest: {
      schema_version: "1.0",
      comparison: { environment: "production", baseline_snapshot_id: null },
      dataset: { dataset_name: "pr126-acceptance-dataset", items_count: 1 },
      agent: { id: "acceptance-agent", version: "v1" },
      evaluators: [],
      quality_policy: { policy_id: "acceptance-policy", version: "1.0", rules: [] },
    },
    progress: {
      total: 1,
      completed: running ? 0 : 1,
      percentage: running ? 0 : 100,
      pending: 0,
      queued: 0,
      running: running ? 1 : 0,
      retry_wait: 0,
      succeeded: running ? 0 : 1,
      failed: 0,
      timed_out: 0,
      cancelled: 0,
      attempts: 1,
      retries: 0,
    },
  };
}

function liveItem(
  launchId: string,
  evaluationStatus: string,
  executionStatus = "succeeded",
) {
  return {
    id: `execution-${launchId}`,
    launch_id: launchId,
    dataset_item_id: `case-${launchId}`,
    execution_status: executionStatus,
    evaluation_status: evaluationStatus,
    quality_conclusion: evaluationStatus === "completed" ? "pass" : "unknown",
    attempt_count: 2,
    final_attempt_http_status: 200,
    final_attempt_latency_ms: 84,
    langfuse_trace_url: `https://langfuse.example/project/demo/traces/${launchId}`,
    evaluation_results: [],
  };
}

async function installLaunchRoutes(
  page: Page,
  options: {
    launchId: string;
    getPhase: () => string;
    getAllowedActions: (phase: string) => string[];
    getEvaluationStatus: (phase: string) => string;
    getHistory?: (phase: string) => { latest_snapshot_id: string | null; revisions: unknown[] };
    onHistoryRequest?: (phase: string, latestSnapshotId: string | null) => void;
    onSnapshotDetailRequest?: (snapshotId: string) => void;
    onRetryEvaluation?: () => void;
    onMutationRequest?: (pathname: string) => void;
  },
) {
  const { launchId } = options;
  const launchPath = `/api/v1/experiment-launches/${launchId}`;

  await page.route("**/api/v1/system/info", (route) =>
    route.fulfill({
      json: {
        service: "argus-eval-runner",
        version: "0.2.0",
        build_id: "pr126-r05-r13-e2e",
        environment: "test",
      },
    }),
  );
  await page.route("**/api/v1/agents**", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/execution-attempts**", (route) =>
    route.fulfill({
      json: [
        {
          id: `attempt-${launchId}`,
          item_execution_id: `execution-${launchId}`,
          attempt_no: 1,
          status: "SUCCEEDED",
          http_status: 200,
          latency_ms: 84,
          trace_context_received: true,
          started_at: "2026-10-10T10:00:01Z",
        },
      ],
    }),
  );

  await page.route("**/api/v1/experiment-launches**", async (route) => {
    const { pathname } = new URL(route.request().url());
    const method = route.request().method();
    const phase = options.getPhase();
    if (method === "POST") options.onMutationRequest?.(pathname);

    if (method === "GET" && pathname === "/api/v1/experiment-launches") {
      await route.fulfill({ json: [] });
      return;
    }
    if (method === "GET" && pathname === launchPath) {
      const status = phase === "running" ? "RUNNING" : "COMPLETED";
      await route.fulfill({
        json: launchPayload(launchId, status, options.getAllowedActions(phase)),
      });
      return;
    }
    if (method === "GET" && pathname === `${launchPath}/items`) {
      const evaluationStatus = options.getEvaluationStatus(phase);
      const executionStatus = phase === "running" ? "running" : "succeeded";
      await route.fulfill({ json: [liveItem(launchId, evaluationStatus, executionStatus)] });
      return;
    }
    if (method === "GET" && pathname === `${launchPath}/result-snapshots`) {
      const history = options.getHistory?.(phase) ?? {
        latest_snapshot_id: null,
        revisions: [],
      };
      options.onHistoryRequest?.(phase, history.latest_snapshot_id);
      await route.fulfill({
        json: {
          launch_id: launchId,
          latest_snapshot_id: history.latest_snapshot_id,
          latest_revision: history.revisions.length,
          revisions: history.revisions,
        },
      });
      return;
    }
    const detailMatch = pathname.match(
      new RegExp(`^${launchPath}/result-snapshots/([^/]+)$`),
    );
    if (method === "GET" && detailMatch) {
      const snapshotId = decodeURIComponent(detailMatch[1]);
      options.onSnapshotDetailRequest?.(snapshotId);
      const isS2 = snapshotId === S2;
      await route.fulfill({
        json: {
          launch_id: launchId,
          snapshot_id: snapshotId,
          revision: isS2 ? 2 : 1,
          created_at: "2026-10-10T10:00:01Z",
          source_result_digest: "sha256:r05",
          manifest_digest: "sha256:manifest-r05",
          evidence_state: "COMPLETE",
          evidence_reasons: [],
          releasable: true,
          versions: {},
          summary: {},
          items: [frozenItem(`case-${launchId}`)],
        },
      });
      return;
    }
    if (method === "GET" && pathname === `${launchPath}/summary`) {
      const snapshotId = new URL(route.request().url()).searchParams.get("snapshot_id");
      await route.fulfill({
        json: {
          launch_id: launchId,
          snapshot_id: snapshotId,
          summary: {
            execution: { total: 1, succeeded: 1, failed: 0 },
            quality: { pass: 0, fail: 0, unknown: 1 },
          },
          versions: {},
        },
      });
      return;
    }
    if (method === "POST" && pathname === `${launchPath}/retry-evaluation`) {
      options.onRetryEvaluation?.();
      await route.fulfill({
        json: {
          launch: { id: launchId, status: "COMPLETED" },
          submitted: [`execution-${launchId}`],
          already_running: [],
          blocked: [],
        },
      });
      return;
    }

    await route.fulfill({ status: 404, json: { detail: `Unhandled test route: ${pathname}` } });
  });
}

async function attachJson(testInfo: TestInfo, name: string, value: unknown) {
  await testInfo.attach(name, {
    body: JSON.stringify(value, null, 2),
    contentType: "application/json",
  });
}

test.describe("PR #126 R05/R13 acceptance evidence", () => {
  test("R05: evaluation-only retry discovers S2 without moving the URL-pinned S1", async ({ page }, testInfo) => {
    let phase = "retryable";
    const historyRequests: Array<{
      atMs: number;
      phase: string;
      latestSnapshotId: string | null;
    }> = [];
    const snapshotDetailRequests: string[] = [];
    let retryEvaluationPosts = 0;
    const startedAt = Date.now();

    await installLaunchRoutes(page, {
      launchId: R05_LAUNCH_ID,
      getPhase: () => phase,
      getAllowedActions: (currentPhase) =>
        currentPhase === "evaluating" ? [] : ["retry_evaluation"],
      getEvaluationStatus: (currentPhase) =>
        currentPhase === "retryable" ? "failed" : currentPhase,
      getHistory: (currentPhase) => {
        const showS2 = currentPhase === "completed";
        return showS2
          ? {
              latest_snapshot_id: S2,
              revisions: [revision(S2, 2, true), revision(S1, 1, false)],
            }
          : { latest_snapshot_id: S1, revisions: [revision(S1, 1, true)] };
      },
      onHistoryRequest: (currentPhase, latestSnapshotId) => {
        historyRequests.push({
          atMs: Date.now() - startedAt,
          phase: currentPhase,
          latestSnapshotId,
        });
      },
      onSnapshotDetailRequest: (snapshotId) => snapshotDetailRequests.push(snapshotId),
      onRetryEvaluation: () => {
        retryEvaluationPosts += 1;
        phase = "evaluating";
      },
    });

    await page.goto(`/launches/${R05_LAUNCH_ID}?snapshot_id=${S1}&tab=cases`);
    await expect(page.getByTestId("snapshot-revision")).toHaveText("Revision 1");
    await expect(page).toHaveURL(new RegExp(`snapshot_id=${S1}`));
    await expect(page.getByTestId("retry-evaluation-button")).toBeVisible();
    await expect(page.getByRole("button", { name: "展开诊断" })).toBeVisible();

    await page.getByTestId("retry-evaluation-button").click();
    await expect(page.getByTestId("retry-evaluation-notice")).toContainText("已提交 1 个用例");
    await expect(page.getByRole("button", { name: "收起诊断" })).toBeVisible();
    await expect(page.getByTestId("retry-evaluation-button")).toHaveCount(0);
    await expect(page.getByTestId("snapshot-revision")).toHaveText("Revision 1");
    expect(retryEvaluationPosts).toBe(1);

    // The browser keeps polling live items while evaluation_status=evaluating. Once the
    // evaluation completes, the revision directory exposes S2 and the discovery window ends.
    phase = "completed";
    await expect(page.getByTestId("snapshot-newer-available")).toContainText("Revision 2");
    await expect(page.getByTestId("snapshot-revision")).toHaveText("Revision 1");
    await expect(page).toHaveURL(new RegExp(`snapshot_id=${S1}`));

    const latestRequest = [...historyRequests].reverse().find((request) => request.latestSnapshotId === S2);
    expect(latestRequest).toBeDefined();
    expect(historyRequests.some((request) => request.phase === "evaluating")).toBe(true);
    expect(snapshotDetailRequests).toEqual([S1]);

    const countAfterDiscovery = historyRequests.length;
    await page.waitForTimeout(1700);
    expect(historyRequests).toHaveLength(countAfterDiscovery);

    await attachJson(testInfo, "r05-evaluation-revision-request-timeline.json", {
      launchId: R05_LAUNCH_ID,
      pinnedSnapshotId: S1,
      discoveredSnapshotId: latestRequest?.latestSnapshotId,
      visibleRevisionAfterDiscovery: "Revision 1",
      newerRevisionNotice: "Revision 2",
      retryEvaluationPosts,
      snapshotDetailRequests,
      historyRequests,
      historyRequestsAfterDiscoveryWait: historyRequests.length,
      noHistoryPollingAfterDiscovery: true,
    });
  });

  test("R13: RUNNING and terminal EVALUATING actions match allowed_actions and progress uses item state", async ({ page }, testInfo) => {
    let phase = "running";
    let mutationRequests = 0;
    const observedLaunchStates: Array<{ phase: string; allowedActions: string[] }> = [];
    await installLaunchRoutes(page, {
      launchId: R13_ACTIONS_LAUNCH_ID,
      getPhase: () => phase,
      getAllowedActions: (currentPhase) => {
        const allowedActions = currentPhase === "running"
          ? ["cancel"]
          : currentPhase === "terminal-retryable"
          ? ["retry_evaluation"]
          : [];
        observedLaunchStates.push({ phase: currentPhase, allowedActions });
        return allowedActions;
      },
      getEvaluationStatus: (currentPhase) =>
        currentPhase === "running"
          ? "idle"
          : currentPhase === "terminal-retryable"
          ? "failed"
          : "evaluating",
      onRetryEvaluation: () => {
        phase = "terminal-evaluating";
      },
      onMutationRequest: () => {
        mutationRequests += 1;
      },
    });

    await page.goto(`/launches/${R13_ACTIONS_LAUNCH_ID}?tab=cases`);
    const actions = {
      run: page.getByRole("button", { name: "启动评测 (Run)" }),
      cancel: page.getByRole("button", { name: "取消评测 (Cancel)" }),
      resume: page.getByRole("button", { name: "断点恢复 (Resume)" }),
      retryFailed: page.getByRole("button", { name: "重试失败用例 (Retry Failed)" }),
      retryEvaluation: page.getByTestId("retry-evaluation-button"),
    };

    await expect(actions.cancel).toBeVisible();
    await expect(actions.run).toHaveCount(0);
    await expect(actions.resume).toHaveCount(0);
    await expect(actions.retryFailed).toHaveCount(0);
    await expect(actions.retryEvaluation).toHaveCount(0);

    // Move to a terminal Launch with one recoverable evaluation. The only offered action
    // must now be the one returned in allowed_actions.
    phase = "terminal-retryable";
    await expect(actions.cancel).toHaveCount(0);
    await expect(actions.retryEvaluation).toBeVisible();
    await expect(actions.run).toHaveCount(0);
    await expect(actions.resume).toHaveCount(0);
    await expect(actions.retryFailed).toHaveCount(0);

    // Exercise the real UI action: its mutation invalidates items, which then reports the
    // terminal-but-EVALUATING state and removes actions while the item evaluation is active.
    await actions.retryEvaluation.click();
    await expect(page.getByTestId("retry-evaluation-notice")).toContainText("已提交 1 个用例");
    await expect(actions.retryEvaluation).toHaveCount(0);
    await expect(actions.cancel).toHaveCount(0);
    await expect(actions.run).toHaveCount(0);
    await expect(actions.resume).toHaveCount(0);
    await expect(actions.retryFailed).toHaveCount(0);
    // progress.evaluating is intentionally absent from the Launch response; expansion proves
    // the UI derived evaluation activity from the live item's evaluation_status field.
    await expect(page.getByRole("button", { name: "收起诊断" })).toBeVisible();
    expect(mutationRequests).toBe(1);

    await attachJson(testInfo, "r13-allowed-actions-and-evaluation-progress.json", {
      launchId: R13_ACTIONS_LAUNCH_ID,
      states: [
        {
          launchStatus: "RUNNING",
          allowedActions: ["cancel"],
          visibleActions: ["cancel"],
        },
        {
          launchStatus: "COMPLETED",
          itemEvaluationStatus: "failed",
          allowedActions: ["retry_evaluation"],
          visibleActions: ["retry_evaluation"],
        },
        {
          launchStatus: "COMPLETED",
          itemEvaluationStatus: "evaluating",
          allowedActions: [],
          visibleActions: [],
          progressExpandedFromLiveItemStatus: true,
          progressEvaluatingFieldInLaunchResponse: false,
        },
      ],
      observedLaunchStates,
      mutationRequests,
    });
  });

  test("R13: Attempt Drawer traps keyboard focus, closes on Escape, and restores its trigger", async ({ page }, testInfo) => {
    let attemptsGetCount = 0;
    let mutationRequests = 0;
    await installLaunchRoutes(page, {
      launchId: R13_DRAWER_LAUNCH_ID,
      getPhase: () => "terminal",
      getAllowedActions: () => [],
      getEvaluationStatus: () => "completed",
      onMutationRequest: () => {
        mutationRequests += 1;
      },
    });
    await page.route("**/api/v1/execution-attempts**", async (route) => {
      attemptsGetCount += 1;
      await route.fulfill({
        json: [
          {
            id: "attempt-r13-1",
            item_execution_id: `execution-${R13_DRAWER_LAUNCH_ID}`,
            attempt_no: 1,
            status: "SUCCEEDED",
            http_status: 200,
            latency_ms: 84,
            trace_context_received: true,
            started_at: "2026-10-10T10:00:01Z",
          },
        ],
      });
    });

    await page.goto(`/launches/${R13_DRAWER_LAUNCH_ID}?tab=cases`);
    const trigger = page.getByRole("button", { name: "2 次尝试" });
    await expect(trigger).toBeVisible();
    expect(attemptsGetCount).toBe(0);
    await trigger.focus();
    await expect(trigger).toBeFocused();
    await page.keyboard.press("Enter");

    const dialog = page.getByRole("dialog", { name: /用例执行调用历史/ });
    await expect(dialog).toBeVisible();
    await expect.poll(() => dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true);
    const traceLink = dialog.getByRole("link", { name: "在 Langfuse 中查看 Trace" });
    await expect(traceLink).toBeVisible();
    await expect.poll(() => attemptsGetCount).toBe(1);

    const closeButton = dialog.getByRole("button", { name: "关闭" });
    await closeButton.focus();
    await page.keyboard.press("Shift+Tab");
    await expect(traceLink).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(closeButton).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
    expect(attemptsGetCount).toBe(1);

    await attachJson(testInfo, "r13-attempt-drawer-keyboard-focus-flow.json", {
      initialFocusInsideDialog: true,
      firstFocusableBoundary: "close button + Shift+Tab wraps to Trace link",
      lastFocusableBoundary: "Trace link + Tab wraps to close button",
      escapeClosesDrawer: true,
      focusReturnsToTrigger: true,
      attemptsGetCount,
      mutationRequests,
    });
  });
});
