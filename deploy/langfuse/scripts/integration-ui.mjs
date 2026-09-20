#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const required = [
  "LANGFUSE_UPSTREAM_DIR",
  "LANGFUSE_BASE_URL",
  "LANGFUSE_ADMIN_EMAIL",
  "LANGFUSE_ADMIN_PASSWORD",
  "LANGFUSE_PROJECT_ID",
];
for (const name of required) {
  if (!process.env[name]) throw new Error(`missing ${name}`);
}

const upstreamDir = path.resolve(process.env.LANGFUSE_UPSTREAM_DIR);
const requireFromUpstream = createRequire(
  path.join(upstreamDir, "web", "package.json"),
);
const { chromium } = requireFromUpstream("@playwright/test");
const baseUrl = process.env.LANGFUSE_BASE_URL.replace(/\/$/, "");
const projectId = process.env.LANGFUSE_PROJECT_ID;
const evidenceDir = path.resolve(process.env.I18N_EVIDENCE_DIR ?? "artifacts/i18n-integration");
fs.mkdirSync(evidenceDir, { recursive: true });

const routes = [
  ["datasets", "Datasets", "数据集"],
  ["experiments", "Experiments", "实验"],
  ["traces", "Tracing", "链路追踪"],
  ["scores", "Scores", "评分"],
  ["settings", "Settings", "设置"],
];

async function login(locale) {
  const context = await browser.newContext();
  await context.addCookies([
    { name: "langfuse-locale", value: locale, url: baseUrl, sameSite: "Lax" },
  ]);
  const page = await context.newPage();
  const failures = [];
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(message.text());
  });
  page.on("pageerror", (error) => failures.push(error.message));
  await page.goto(`${baseUrl}/auth/sign-in`, { waitUntil: "networkidle" });
  await page.fill('input[name="email"]', process.env.LANGFUSE_ADMIN_EMAIL);
  await page.fill('input[type="password"]', process.env.LANGFUSE_ADMIN_PASSWORD);
  await page.click('button[data-testid="submit-email-password-sign-in-form"]');
  await page.waitForURL((url) => !url.pathname.includes("/auth/sign-in"));
  return { context, page, failures };
}

const browser = await chromium.launch();
try {
  const sessions = await Promise.all([login("en"), login("zh-CN")]);
  for (const [localeIndex, locale] of ["en", "zh-CN"].entries()) {
    const { page, failures } = sessions[localeIndex];
    for (const [route, enTitle, zhTitle] of routes) {
      const expected = locale === "zh-CN" ? zhTitle : enTitle;
      await page.goto(`${baseUrl}/project/${projectId}/${route}`, {
        waitUntil: "networkidle",
      });
      await page
        .locator('[data-testid="page-header-title"]')
        .filter({ hasText: expected })
        .waitFor();
      if (route === "datasets" && process.env.LANGFUSE_DATASET_NAME) {
        await page.getByText(process.env.LANGFUSE_DATASET_NAME).first().waitFor();
      }
      await page.screenshot({
        path: path.join(evidenceDir, `${route}-${locale}.png`),
        fullPage: true,
      });
    }
    if (failures.some((message) => /hydration/i.test(message))) {
      throw new Error(`${locale} hydration errors: ${failures.join("; ")}`);
    }
  }

  const zhPage = sessions[1].page;
  await zhPage.goto(`${baseUrl}/project/${projectId}/datasets?view=active`, {
    waitUntil: "networkidle",
  });
  await zhPage.getByRole("link", { name: "链路追踪" }).first().click();
  await zhPage.waitForURL(
    (url) => url.pathname === `/project/${projectId}/traces`,
  );
  await zhPage
    .locator('[data-testid="page-header-title"]')
    .filter({ hasText: "链路追踪" })
    .waitFor();

  await zhPage.goto(`${baseUrl}/project/${projectId}/datasets?view=active`, {
    waitUntil: "networkidle",
  });
  await zhPage.getByRole("combobox", { name: "语言" }).selectOption("en");
  await zhPage.waitForLoadState("networkidle");
  if (!zhPage.url().includes(`/project/${projectId}/datasets?view=active`)) {
    throw new Error("language switch did not preserve route and query state");
  }
  await zhPage.reload({ waitUntil: "networkidle" });
  await zhPage
    .locator('[data-testid="page-header-title"]')
    .filter({ hasText: "Datasets" })
    .waitFor();

  await Promise.all(sessions.map(({ context }) => context.close()));
} finally {
  await browser.close();
}

console.log("remote Langfuse bilingual UI integration: PASS");
