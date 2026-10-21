import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// 网络替身只验证 UI 契约；真实 Rule / Flowise / Gate 验收单独记录，不混称。
const id = "launch-219";
const contract = { project_id: "project-219", execution_semantics: "langfuse_latest", scores: [
  { name: "judge", data_type: "boolean", required: true, rule_id: "rule-judge" },
  { name: "code", data_type: "boolean", required: true, rule_id: "rule-code" },
] };
const manifest = { schema_version: "1.5", evaluation_source: "langfuse_managed", managed_evaluation: contract,
  agent: { id: "flowise", version: "v1" }, dataset: { items_count: 1, items: [{ id: "case", input: { question: "6 * 7" } }] },
  comparison: { environment: "test" }, quality_policy: { rules: [] }, evaluators: [] };
const result = (name: string, value = true) => ({ evaluator_id: name, result_type: "boolean", status: "succeeded", value,
  provenance: { executor_type: "langfuse_managed", contract_status: "MANAGED_SCORE_VERIFIED" },
  evidence: { score: { id: `score-${name}`, source: "EVAL", dataType: "BOOLEAN", value: value ? 1 : 0 },
    score_url: `https://langfuse.example/project/project-219/traces/trace?observation=observation`,
    observed_at: "2026-10-21T06:00:00Z" } });
function fixtureState() {
  return { frozen: false, quality: "unknown", eval: "pending", sync: "SYNCED", diagnosticsDown: false,
    results: [] as ReturnType<typeof result>[], liveManifest: manifest as Record<string, unknown>, diagnosticsRequests: 0 };
}
async function setup(page: Page, state: ReturnType<typeof fixtureState>) {
  const frozenItem = () => ({ dataset_item_id: "case", execution_status: "succeeded", eval_status: state.eval,
    quality_conclusion: state.quality, trace_url: "https://langfuse.example/project/p/traces/t", evaluation_results: state.results,
    final_attempt_http_status: 200, managed_evidence_reasons: state.quality === "unknown" ? ["REQUIRED_SCORE_MISSING:code"] : [] });
  // Snapshot 副本只在首次冻结时捕获，后续远程评分不修改它。
  let snapshot: Record<string, unknown> | null = null;
  const revision = () => ({ snapshot_id: "S1", revision: 1, created_at: "2026-10-21T06:00:02Z", source_result_digest: "sha256:result", manifest_digest: "sha256:manifest", is_latest: true, evidence_state: state.quality === "unknown" ? "DIAGNOSTIC" : "COMPLETE",
    quality_pass_count: state.quality === "pass" ? 1 : 0, quality_fail_count: state.quality === "fail" ? 1 : 0,
    quality_unknown_count: state.quality === "unknown" ? 1 : 0, total_cases: 1, releasable: state.quality === "pass" });
  const gate = () => ({ id: "gate-219", candidate_launch_id: id, candidate_snapshot_id: "S1", candidate_revision: 1,
    decision: state.quality === "pass" ? "PASS" : state.quality === "fail" ? "FAIL" : "UNKNOWN", releasable: state.quality === "pass",
    policy: { name: "release-test", version: "1" }, policy_digest: "sha256:policy", candidate_source_result_digest: "sha256:result", rules: [],
    reason_codes: state.quality === "unknown" ? ["CANDIDATE_DIAGNOSTIC"] : [] });
  let storedGate: ReturnType<typeof gate> | null = null;
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/system/info")) return route.fulfill({ json: { service: "argus-eval-runner", version: "0.2.0", build_id: "issue219-mock", environment: "test" } });
    if (path.endsWith("/release-gates/evaluate")) {
      expect(route.request().postDataJSON().candidate_snapshot_id).toBe("S1");
      storedGate = gate(); return route.fulfill({ json: storedGate });
    }
    if (path.endsWith("/release-gates/gate-219")) return route.fulfill({ json: storedGate ?? gate() });
    if (path.endsWith("/managed-scores")) {
      state.diagnosticsRequests++;
      if (state.diagnosticsDown) return route.fulfill({ status: 503, json: { detail: "diagnostic unavailable" } });
      return route.fulfill({ json: { launch_id: id, item_execution_id: "item-219", results: [], evidence: [
        { id: "audit", score_name: "code", decision: "REJECTED", reason: "SOURCE_MISMATCH", score_id: "online-score", evidence: {} },
      ] } });
    }
    if (path.endsWith("/result-snapshots")) {
      if (state.frozen && !snapshot) snapshot = structuredClone({ launch_id: id, ...revision(), manifest, items: [frozenItem()], summary: {} });
      return route.fulfill({ json: { launch_id: id, latest_snapshot_id: state.frozen ? "S1" : null, revisions: state.frozen ? [revision()] : [] } });
    }
    if (path.endsWith("/result-snapshots/S1")) return route.fulfill({ json: snapshot });
    if (path.endsWith("/items")) return route.fulfill({ json: [{ ...frozenItem(), id: "item-219", launch_id: id,
      langfuse_trace_url: "https://langfuse.example/project/p/traces/t", evaluation_status: state.eval === "pending" ? "evaluating" : "recovered", evaluation_recoverable: false, attempt_count: 1 }] });
    if (path.endsWith("/summary")) return route.fulfill({ json: { launch_id: id, snapshot_id: state.frozen ? "S1" : null,
      summary: { total_cases: 1, evaluation_coverage: state.results.length / 2 }, versions: {},
      langfuse_sync: { overall: state.sync, item_trace: { status: state.sync }, run_score: { status: "NOT_APPLICABLE" } } } });
    if (path.endsWith("/comparison")) return route.fulfill({ json: { candidate_launch_id: id, candidate_snapshot_id: "S1", items: [], versions: { candidate: {} }, summary: {} } });
    if (path.endsWith(`/experiment-launches/${id}`)) return route.fulfill({ json: {
      id, name: "Flowise Managed", status: "COMPLETED", quality_conclusion: state.quality, agent_id: "flowise", agent_version: "v1", environment: "test",
      dataset_name: "managed-test", manifest: state.liveManifest, langfuse_sync_status: state.sync,
      langfuse_experiment_url: "https://langfuse.example/project/p/experiments/e", created_at: "2026-10-21T06:00:00Z", allowed_actions: [],
      progress: { total: 1, succeeded: 1, completed: 1, percentage: 100, evaluating: state.eval === "pending" ? 1 : 0 },
    } });
    return route.fulfill({ json: {} });
  });
}
async function expand(page: Page) {
  await page.getByRole("button", { name: "展开", exact: true }).click();
  return page.getByRole("region", { name: "托管评分证据" });
}

test("Managed 自动 pending → 双 Score → Frozen Snapshot → 固定 Gate", async ({ page }, info) => {
  const state = fixtureState(); await setup(page, state);
  await page.goto(`/launches/${id}?tab=cases`);
  let panel = await expand(page);
  await expect(panel.getByText("等待托管评分")).toBeVisible();
  await expect(panel.getByText("必要评分 0 / 2 已采纳")).toBeVisible();
  await expect(page.getByText("OTLP 同步（实时，非评分结论）")).toBeVisible();
  await page.screenshot({ path: info.outputPath("managed-pending.png"), fullPage: true });
  state.eval = "succeeded"; state.quality = "pass"; state.results = [result("judge"), result("code")]; state.frozen = true;
  await expect(page).toHaveURL(/snapshot_id=S1/, { timeout: 20000 });
  panel = await expand(page);
  await expect(panel.getByText("必要评分 2 / 2 已采纳")).toBeVisible();
  await expect(panel.getByText("评分完成")).toBeVisible();
  expect(await panel.getByRole("link").count()).toBe(2);
  await page.getByRole("tab", { name: /Gate & Comparison/ }).click();
  await page.getByLabel("ReleasePolicy 名称").fill("release-test");
  await page.getByLabel("ReleasePolicy 版本").fill("1");
  await page.getByRole("button", { name: "计算并保存 Gate" }).click();
  await expect(page.getByTestId("release-gate-result")).toContainText("PASS · 可发布");
  await page.reload();
  await expect(page.getByTestId("release-gate-result")).toContainText("gate-219");
  await page.getByRole("region", { name: "Release Gate", exact: true }).screenshot({ path: info.outputPath("frozen-gate-pass.png") });
});

test("Langfuse down / recover：诊断故障不伪造零分，恢复后显示拒绝来源", async ({ page }) => {
  const state = fixtureState(); state.diagnosticsDown = true; state.sync = "FAILED"; await setup(page, state);
  await page.goto(`/launches/${id}?tab=cases`); const panel = await expand(page);
  await expect(panel.getByRole("alert")).toContainText("诊断暂不可用");
  await expect(panel.getByText("质量结论：UNKNOWN")).toBeVisible();
  state.diagnosticsDown = false;
  await panel.getByRole("button", { name: "重试诊断" }).click();
  await panel.getByText(/未采纳诊断/).click();
  await expect(panel.getByText(/SOURCE_MISMATCH/)).toBeVisible();
  await expect(panel.getByText("等待托管评分")).toBeVisible();
});

for (const quality of ["fail", "unknown"]) test(`冻结 ${quality}：评分失败与质量失败分离、历史 Score / 链接不漂移`, async ({ page }, info) => {
  const state = fixtureState(); state.frozen = true; state.quality = quality;
  state.eval = quality === "fail" ? "succeeded" : "failed";
  state.results = quality === "fail" ? [result("judge", false), result("code", false)] : [result("judge")];
  state.liveManifest = { evaluation_source: "builtin" }; await setup(page, state);
  await page.goto(`/launches/${id}?snapshot_id=S1&tab=cases`); let panel = await expand(page);
  await expect(panel.getByText(quality === "fail" ? "评分完成" : "评分失败")).toBeVisible();
  await expect(panel.getByText(`质量结论：${quality.toUpperCase()}`)).toBeVisible();
  const before = await panel.innerText();
  const urls = await panel.getByRole("link").evaluateAll((links) => links.map((a) => a.getAttribute("href")));
  state.results = [result("judge", true), result("code", true)];
  state.liveManifest = { evaluation_source: "langfuse_managed", managed_evaluation: { ...contract, scores: [] } };
  await page.reload(); panel = await expand(page);
  expect(await panel.innerText()).toBe(before);
  expect(await panel.getByRole("link").evaluateAll((links) => links.map((a) => a.getAttribute("href")))).toEqual(urls);
  expect(state.diagnosticsRequests).toBe(0);
  await page.screenshot({ path: info.outputPath(`frozen-${quality}.png`), fullPage: true });
});

for (const width of [360, 390]) test(`Managed ${width}px 响应式、键盘和可访问性`, async ({ page }, info) => {
  await page.setViewportSize({ width, height: 844 });
  const state = fixtureState(); state.frozen = true; state.eval = "succeeded"; state.quality = "pass"; state.results = [result("judge"), result("code")];
  await setup(page, state); await page.goto(`/launches/${id}?snapshot_id=S1&tab=cases`);
  const toggle = page.getByRole("button", { name: "展开", exact: true });
  await toggle.focus(); await page.keyboard.press("Enter");
  const panel = page.getByRole("region", { name: "托管评分证据" });
  await expect(panel).toBeVisible();
  for (const element of await panel.locator("a, button, summary").all()) {
    const box = await element.boundingBox(); expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0); expect(box!.x + box!.width).toBeLessThanOrEqual(width);
  }
  const accessibility = await new AxeBuilder({ page }).include('[aria-label="托管评分证据"]').withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(accessibility.violations).toEqual([]);
  await page.setViewportSize({ width, height: 1800 });
  await panel.screenshot({ path: info.outputPath(`managed-${width}.png`) });
  await page.setViewportSize({ width, height: 844 });
  await page.getByRole("tab", { name: /Gate & Comparison/ }).click();
  const gate = page.getByRole("region", { name: "Release Gate", exact: true });
  await gate.getByLabel("ReleasePolicy 名称").fill("release-test");
  await gate.getByLabel("ReleasePolicy 版本").fill("1");
  await gate.getByRole("button", { name: "计算并保存 Gate" }).click();
  await expect(gate.getByText("PASS · 可发布")).toBeVisible();
  for (const element of await gate.locator("input, button, a").all()) {
    const box = await element.boundingBox(); expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0); expect(box!.x + box!.width).toBeLessThanOrEqual(width);
  }
  const gateA11y = await new AxeBuilder({ page }).include('[aria-label="Release Gate"]').withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(gateA11y.violations).toEqual([]);
  await gate.screenshot({ path: info.outputPath(`gate-${width}.png`) });
});
