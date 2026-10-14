import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { trajectoryFixture } from "../src/features/launches/__tests__/trajectoryFixtures";

const revisions = [2, 1].map(n => ({ snapshot_id: `snapshot-${n}`, revision: n, is_latest: n === 2,
  created_at: "2026-10-14T00:00:00Z", manifest_digest: "manifest-digest", source_result_digest: `source-${n}`,
  evidence_state: "COMPLETE", evidence_reasons: [], total_cases: 1, quality_pass_count: 0, quality_fail_count: 1, quality_unknown_count: 0 }));
const manifest = { schema_version: "1.3", evidence_contract_version: "trajectory-1.0",
  dataset: { items_count: 1, items: [{ id: "case-1", input: "synthetic task" }] },
  agent: { agent_id: "agent-1", version: "v1" }, comparison: { environment: "production" }, evaluators: [], runner: {} };
const launch = { id: "launch-1", name: "冻结轨迹诊断", status: "COMPLETED", quality_conclusion: "fail",
  agent_id: "agent-1", agent_version: "v1", dataset_name: "trajectory-slice", dataset_version: "v1", manifest,
  allowed_actions: [], progress: { total: 1, completed: 1, allowed_actions: [] }, created_at: "2026-10-14T00:00:00Z" };
const item = { dataset_item_id: "case-1", execution_status: "succeeded", eval_status: "partial", quality_conclusion: "fail",
  quality_evaluation: { conclusion: "fail", required_evidence_status: "COMPLETE", rules: [] }, required_evidence_status: "COMPLETE",
  scores: {}, evaluation_results: [], final_attempt_id: "attempt-1", dispatch_generation: 1, latency_ms: 12,
  trace_url: "https://langfuse.example/trace/trace-1" };
const metrics = { total_cases: 1, evaluated_cases: 1, passed_cases: 0, pass_rate: 0, evaluation_coverage: 1,
  quality_pass_count: 0, quality_fail_count: 1, quality_unknown_count: 0, p95_latency_ms: 12, critical_failure_count: 1,
  execution_error_count: 0, evaluator_error_count: 1, required_evidence_status: "COMPLETE" };

async function mock(page: Page, historical = false, summaryOverride: (id: string) => Record<string, unknown> = () => ({})) {
  const requested: string[] = [];
  await page.route("**/api/v1/agents/*/baselines**", route => route.fulfill({ status: 404, json: { detail: "No baseline" } }));
  await page.route("**/api/v1/experiment-launches/launch-1**", async route => {
    const url = new URL(route.request().url());
    const id = url.searchParams.get("snapshot_id") ?? url.pathname.split("/").pop()!;
    const revision = revisions.find(r => r.snapshot_id === id) ?? revisions[0];
    let data: unknown;
    if (url.pathname.endsWith("/trajectory")) {
      requested.push(id);
      data = historical ? { ...trajectoryFixture(id), acquisition_state: "UNAVAILABLE", reason_code: "HISTORICAL_TRAJECTORY_UNAVAILABLE",
        trajectory: null, completeness: null, evidence_ref: null, step_argument_display_json: {}, rule_results: [] }
        : trajectoryFixture(id);
    } else if (url.pathname.endsWith("/result-snapshots")) {
      data = { launch_id: "launch-1", latest_snapshot_id: "snapshot-2", latest_revision: 2, revisions };
    } else if (url.pathname.includes("/result-snapshots/")) {
      data = { ...revision, launch_id: "launch-1", manifest, versions: {}, summary: metrics, items: [item], releasable: true };
    } else if (url.pathname.endsWith("/summary")) {
      data = { ...revision, launch_id: "launch-1", summary: metrics, versions: {}, langfuse_score_sync_status: "SYNCED", ...summaryOverride(id) };
    } else if (url.pathname.endsWith("/comparison/case")) {
      data = { launch_id: "launch-1", candidate_snapshot_id: id, dataset_item_id: "case-1",
        baseline: { output_status: "NO_REFERENCE" }, candidate: { output_status: "NO_REFERENCE" } };
    } else if (url.pathname.endsWith("/comparison")) {
      data = { launch_id: "launch-1", candidate_snapshot_id: id, items: [], summary: {}, classification_counts: {} };
    } else if (url.pathname.endsWith("/items")) {
      data = [];
    } else data = launch;
    await route.fulfill({ json: data });
  });
  return requested;
}

async function open(page: Page) {
  await page.goto("/launches/launch-1?snapshot_id=snapshot-1");
  await page.getByRole("tab", { name: /用例排查与 Trace/ }).click();
}

test("固定 Case 懒加载，三态和在线标签独立于测量状态", async ({ page }) => {
  const requested = await mock(page);
  await open(page);
  expect(requested).toEqual([]);
  await page.getByRole("button", { name: "展开", exact: true }).click();
  const diagnosis = page.getByRole("region", { name: "固定轨迹诊断", exact: true });
  await expect(diagnosis.getByText("违规 FAIL", { exact: true })).toBeVisible();
  await expect(diagnosis.getByText("通过 PASS", { exact: true })).toBeVisible();
  await expect(diagnosis.getByText("证据不足 UNKNOWN", { exact: true })).toBeVisible();
  await expect(diagnosis.getByText("无测量结果", { exact: true })).toBeVisible();
  await expect(diagnosis.getByText(/答案规则仍按冻结策略评测/)).toBeVisible();
  await expect(diagnosis.getByRole("link", { name: /在线观测内容/ })).toHaveAttribute("rel", "noopener noreferrer");
  expect(requested).toEqual(["snapshot-1"]);
});

test("键盘规则与 Step 双向定位、精确参数和因果分组可访问", async ({ page }, info) => {
  await mock(page);
  await open(page);
  await page.getByRole("button", { name: "展开", exact: true }).focus();
  await page.keyboard.press("Enter");
  const locate = page.getByRole("button", { name: "定位 trajectory.forbid_tool 的证据 Step exec-a" });
  await locate.focus();
  await page.keyboard.press("Enter");
  const step = page.getByTestId("trajectory-step-exec-a");
  await expect(step).toBeFocused();
  await step.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(step.locator("pre")).toHaveText('{"amount":0.12345678901234567890123456789,"n":9007199254740993}');
  const back = step.getByRole("button", { name: "返回规则 trajectory.forbid_tool" });
  await back.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("region", { name: "轨迹规则 trajectory.forbid_tool", exact: true })).toBeFocused();
  await locate.focus();
  await page.keyboard.press("Enter");
  await expect(step).toBeFocused();
  await back.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("region", { name: "轨迹规则 trajectory.forbid_tool", exact: true })).toBeFocused();
  await expect(page.getByTestId("trajectory-step-exec-b")).toContainText("BLOCKED");
  const audit = await new AxeBuilder({ page }).include('[aria-label="固定轨迹诊断"]').analyze();
  expect(audit.violations).toEqual([]);
  const diagnosis = page.getByRole("region", { name: "固定轨迹诊断", exact: true });
  // 页面使用内部滚动容器，截图时扩高视口以完整显示诊断，不截取空白裁切区域。
  await page.setViewportSize({ width: 1280, height: 2000 });
  await diagnosis.scrollIntoViewIfNeeded();
  await diagnosis.screenshot({ path: info.outputPath("trajectory-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(await diagnosis.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.setViewportSize({ width: 390, height: 3200 });
  await diagnosis.scrollIntoViewIfNeeded();
  await diagnosis.screenshot({ path: info.outputPath("trajectory-mobile.png") });
});

test("快照切换隔离 Case 与 Step 数据", async ({ page }) => {
  const requested = await mock(page);
  await open(page);
  await page.getByRole("button", { name: "展开", exact: true }).click();
  await expect(page.getByText("crm-snapshot-1", { exact: true })).toBeVisible();
  await page.getByTestId("snapshot-revision-2").click();
  await expect(page).toHaveURL(/snapshot_id=snapshot-2/);
  await expect(page.getByText("crm-snapshot-1", { exact: true })).toHaveCount(0);
  expect(requested).toEqual(["snapshot-1"]);
  await page.getByRole("button", { name: "展开", exact: true }).click();
  await expect(page.getByText("crm-snapshot-2", { exact: true })).toBeVisible();
  await expect(page.getByText("crm-snapshot-1", { exact: true })).toHaveCount(0);
  expect(requested).toEqual(["snapshot-1", "snapshot-2"]);
});

test("旧 Snapshot 显示有限缺证据原因", async ({ page }) => {
  await mock(page, true);
  await open(page);
  await page.getByRole("button", { name: "展开", exact: true }).click();
  const diagnosis = page.getByRole("region", { name: "固定轨迹诊断", exact: true });
  await expect(diagnosis.getByText("HISTORICAL_TRAJECTORY_UNAVAILABLE", { exact: true })).toBeVisible();
  await expect(diagnosis.getByText("通过 PASS", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("trajectory-step-exec-a")).toHaveCount(0);
});


test("冻结门禁观测区分未知、真实零和可选轨迹缺失，并随 Snapshot 隔离", async ({ page }, info) => {
  await mock(page, false, id => id === "snapshot-1" ? {
    summary: { ...metrics, decided_pass_rate: null, decision_coverage: 0, quality_unknown_count: 1 },
    required_case_counts: { total: 1, passed: 0, failed: 0, unknown: 1 }, trajectory_coverage: null,
  } : {
    summary: { ...metrics, decided_pass_rate: 0, decision_coverage: 1, quality_unknown_count: 0 },
    required_case_counts: { total: 1, passed: 0, failed: 1, unknown: 0 }, trajectory_coverage: 0,
  });
  await page.goto("/launches/launch-1?snapshot_id=snapshot-1");
  await page.getByRole("tab", { name: /门禁与版本对比/ }).click();
  const panel = page.getByTestId("frozen-gate-observations");
  await expect(panel.getByTestId("frozen-decided-pass-rate")).toHaveText("—");
  await expect(panel.getByTestId("frozen-decision-coverage")).toHaveText("0.0%");
  await expect(panel.getByTestId("frozen-trajectory-coverage")).toHaveText("—");
  await expect(panel.getByTestId("frozen-unknown-count")).toHaveText("1");
  await expect(panel.getByTestId("frozen-critical-count")).toHaveText("1");
  await page.getByTestId("snapshot-revision-2").focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/snapshot_id=snapshot-2/);
  await expect(panel.getByTestId("frozen-decided-pass-rate")).toHaveText("0.0%");
  await expect(panel.getByTestId("frozen-decision-coverage")).toHaveText("100.0%");
  await expect(panel.getByTestId("frozen-trajectory-coverage")).toHaveText("0.0%");
  await expect(panel.getByTestId("frozen-case-counts")).toHaveText("Dataset Case：1 / PASS 0 / FAIL 1 / UNKNOWN 0");
  await expect(panel).toContainText("必要 UNKNOWN 或 critical 违规会阻断发布");
  const audit = await new AxeBuilder({ page }).include('[data-testid="frozen-gate-observations"]').analyze();
  expect(audit.violations).toEqual([]);
  await panel.screenshot({ path: info.outputPath("frozen-gate-observations.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await panel.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
});
