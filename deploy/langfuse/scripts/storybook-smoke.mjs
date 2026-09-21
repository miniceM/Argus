#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const upstreamDir = path.resolve(process.env.LANGFUSE_UPSTREAM_DIR ?? ".");
const requireFromUpstream = createRequire(
  path.join(upstreamDir, "web", "package.json"),
);
const { chromium } = requireFromUpstream("@playwright/test");

const baseUrl = process.env.STORYBOOK_URL ?? "http://127.0.0.1:6006";
const outputDir = path.resolve(process.env.I18N_SCREENSHOT_DIR ?? "artifacts/i18n");
fs.mkdirSync(outputDir, { recursive: true });

const response = await fetch(`${baseUrl}/index.json`);
if (!response.ok) throw new Error(`Storybook index returned ${response.status}`);
const index = await response.json();
const stories = Object.values(index.entries).filter((entry) => entry.type === "story");

const targets = [
  ["sidebar", /AppSidebar/i],
  ["table", /DataTable/i],
  ["dialog", /Design\/Components\/DialogController\b/i],
].map(([kind, pattern]) => {
  const story = stories.find((entry) => pattern.test(`${entry.title} ${entry.name}`));
  if (!story) throw new Error(`No real upstream ${kind} story matched ${pattern}`);
  return { kind, id: story.id };
});

const browser = await chromium.launch();
try {
  for (const locale of ["en", "zh-CN"]) {
    for (const target of targets) {
      const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
      const errors = [];
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text());
      });
      page.on("pageerror", (error) => errors.push(error.message));
      const url = new URL("iframe.html", baseUrl);
      url.searchParams.set("id", target.id);
      url.searchParams.set("viewMode", "story");
      url.searchParams.set("globals", `locale:${locale}`);
      await page.goto(url.toString(), { waitUntil: "networkidle" });
      await page.locator("body").screenshot({
        path: path.join(outputDir, `${target.kind}-${locale}.png`),
      });
      const storyError = await page.locator(".sb-errordisplay").isVisible();
      if (storyError || errors.length > 0) {
        throw new Error(
          `${target.kind}/${locale} failed: ${errors.join("; ") || "Storybook error panel"}`,
        );
      }
      await page.close();
    }
  }
} finally {
  await browser.close();
}

console.log(`storybook i18n smoke: OK (${targets.length * 2} screenshots)`);
