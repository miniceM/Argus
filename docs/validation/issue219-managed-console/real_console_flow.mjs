/** 真实浏览器驱动：不使用 page.route、不写 Score、不调用手动 collect。配置从 stdin 读取。 */
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../../../', import.meta.url));
const require = createRequire(root + 'services/console/package.json');
const { chromium, expect } = require('@playwright/test');
const config = JSON.parse(readFileSync(0, 'utf8'));
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const out = config.out;
mkdirSync(out + '/real-ui', { recursive: true });
const audit = [];
page.on('request', (req) => {
  const url = new URL(req.url());
  if (url.pathname.startsWith('/api/')) audit.push({ method: req.method(), path: url.pathname, at: new Date().toISOString() });
});
const save = (name, data) => writeFileSync(`${out}/${config.label}-${name}.json`, JSON.stringify(data, null, 2) + '\n');
const expand = async () => {
  const button = page.getByRole('button', { name: '展开', exact: true });
  const panel = page.getByRole('region', { name: '托管评分证据' });
  await expect.poll(async () => {
    if (await panel.isVisible()) return true;
    if (await button.first().isVisible()) await button.first().click();
    return panel.isVisible();
  }, { timeout: 30000 }).toBe(true);
  return panel;
};
try {
  if (config.mode === 'verify-history') {
    await page.goto(config.report_url);
    await page.getByRole('tab', { name: /用例排查/ }).click();
    const panel = await expand();
    expect(await panel.innerText()).toBe(config.panel_text);
    await panel.screenshot({ path: `${out}/real-ui/${config.label}-score-evidence.png` });
    expect(await panel.getByRole('link').evaluateAll((links) => links.map((a) => a.href))).toEqual(config.score_links);
    await page.getByRole('tab', { name: /Gate & Comparison/ }).click();
    await expect(page.getByTestId('release-gate-result')).toContainText(config.gate_id);
    expect(await page.getByTestId('release-gate-result').innerText()).toBe(config.gate_text);
    await page.getByRole('region', { name: 'Release Gate', exact: true }).screenshot({ path: `${out}/real-ui/${config.label}-fixed-gate.png` });
    save('history', { status: 'PASS', report_url: config.report_url, score_links_unchanged: true, gate_unchanged: true });
  } else {
    await page.goto(config.ui_url + '/launches/new');
    await page.getByLabel('选择 Agent').selectOption(config.agent_id);
    await page.getByLabel('选择版本规格 (Active Version)').selectOption(config.version);
    await page.getByLabel('Environment').fill(config.environment);
    await page.getByLabel('数据集名称 (Dataset Name)').fill(config.dataset);
    await page.getByLabel('评测来源 (Evaluation Source)').selectOption('langfuse_managed');
    await page.getByLabel('Langfuse Project ID').fill(config.project_id);
    for (let i = 0; i < config.scores.length; i++) {
      if (i) await page.getByRole('button', { name: '添加外部 Score', exact: true }).click();
      const score = config.scores[i], index = i + 1;
      await page.getByLabel(`Score 名称 ${index}`).fill(score.name);
      await page.getByLabel(`Score 类型 ${index}`).selectOption('boolean');
      await page.getByLabel(`Rule ID ${index}`).fill(score.rule_id);
      await page.getByLabel(`Evaluator ID ${index}`).fill(score.evaluator_id);
    }
    await page.screenshot({ path: `${out}/real-ui/${config.label}-create.png`, fullPage: true });
    const createdPromise = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/v1/experiment-launches' && r.request().method() === 'POST');
    await page.getByRole('button', { name: /创建评测任务/ }).click();
    const created = await createdPromise;
    expect(created.status()).toBe(201);
    const launch = await created.json();
    await page.waitForURL(new RegExp('/launches/' + launch.id));
    await page.getByRole('button', { name: '启动评测 (Run)', exact: true }).click();
    await page.getByRole('tab', { name: /用例排查/ }).click();
    // 等真实 HTTP 完成；只 GET 读取，不发起评分收集。
    const timeline = [];
    let item, pending = false, snapshotId;
    const deadline = Date.now() + 650000;
    while (Date.now() < deadline) {
      const response = await page.request.get(`${config.ui_url}/api/v1/experiment-launches/${launch.id}/items`);
      expect(response.ok()).toBeTruthy();
      const items = await response.json();
      item = items[0];
      if (item) {
        const state = { at: new Date().toISOString(), execution: item.execution_status, evaluation: item.eval_status,
          quality: item.quality_conclusion, status: item.evaluation_status, attempt_count: item.attempt_count };
        if (JSON.stringify({ ...state, at: null }) !== JSON.stringify({ ...timeline.at(-1), at: null })) timeline.push(state);
        save('timeline', timeline);
        if (item.execution_status === 'failed') throw new Error('Agent execution failed; no retry');
        if (!pending && item.execution_status === 'succeeded' && item.eval_status === 'pending') {
          const panel = await expand();
          await expect(panel.getByText('等待托管评分', { exact: true })).toBeVisible();
          await expect(panel.getByText('质量结论：UNKNOWN')).toBeVisible();
          await page.screenshot({ path: `${out}/real-ui/${config.label}-pending.png`, fullPage: true });
          pending = true;
        }
        if (['recovered', 'failed'].includes(item.evaluation_status)) break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    expect(pending).toBeTruthy();
    expect(item.attempt_count).toBe(1);
    expect(item.quality_conclusion).toBe(config.expected.toLowerCase());
    await page.reload();
    let panel = await expand();
    await expect(panel.getByText('质量结论：' + config.expected, { exact: true })).toBeVisible({ timeout: 60000 });
    await expect(panel.getByText(config.label === 'missing' ? '评分失败' : '评分完成', { exact: true })).toBeVisible();
    await expect(panel.getByText(`必要评分 ${config.label === 'missing' ? 1 : 2} / 2 已采纳`)).toBeVisible();
    snapshotId = new URL(page.url()).searchParams.get('snapshot_id');
    expect(snapshotId).toBeTruthy();
    const panelText = await panel.innerText();
    const links = await panel.getByRole('link').evaluateAll((elements) => elements.map((a) => a.href));
    expect(links).toHaveLength(config.label === 'missing' ? 1 : 2);
    for (const link of links) {
      const url = new URL(link);
      expect(url.username + url.password + url.search).not.toContain('secret');
      expect(url.protocol).toBe('https:');
      expect(url.pathname).toContain('traces');
    }
    const external = await page.context().newPage();
    const externalResponse = await external.goto(links[0]);
    expect(externalResponse && externalResponse.status()).toBeLessThan(400);
    await external.screenshot({ path: `${out}/real-ui/${config.label}-langfuse-navigation.png` });
    save('external-navigation', { target: links[0], status: externalResponse.status(), final_url: external.url(),
      note: '外部页面可能要求用户登录；未传递凭据，不冒充已登录查看 Score。' });
    await external.close();
    await page.screenshot({ path: `${out}/real-ui/${config.label}-frozen-scores.png`, fullPage: true });
    await page.getByRole('tab', { name: /Gate & Comparison/ }).click();
    const gate = page.getByRole('region', { name: 'Release Gate', exact: true });
    await gate.getByLabel('ReleasePolicy 名称').fill(config.policy);
    await gate.getByLabel('ReleasePolicy 版本').fill('1.0.0');
    await gate.getByRole('button', { name: '计算并保存 Gate', exact: true }).click();
    const result = page.getByTestId('release-gate-result');
    await expect(result).toContainText(config.expected);
    const gateId = new URL(page.url()).searchParams.get('gate_id');
    expect(gateId).toBeTruthy();
    const reportUrl = page.url(), gateText = await result.innerText();
    await page.reload();
    await expect(result).toContainText(gateId);
    expect(await result.innerText()).toBe(gateText);
    await page.screenshot({ path: `${out}/real-ui/${config.label}-gate.png`, fullPage: true });
    save('browser', { status: 'PASS', launch_id: launch.id, snapshot_id: snapshotId, gate_id: gateId,
      expected: config.expected, report_url: reportUrl, panel_text: panelText, gate_text: gateText,
      score_links: links, pending_observed: pending, network_mocking: false,
      injection: config.label === 'pass' ? null : config.label === 'fail' ? 'HTTP synthetic wrong output, real native Langfuse evaluators' : 'HTTP wrong output plus own Code Rule disabled' });
  }
} catch (error) {
  await page.screenshot({ path: `${out}/real-ui/${config.label}-failure.png`, fullPage: true }).catch(() => {});
  console.error(error);
  process.exitCode = 1;
} finally {
  save('browser-audit', audit);
  await browser.close();
}
