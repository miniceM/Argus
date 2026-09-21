#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const [sourceRootArg] = process.argv.slice(2);
if (!sourceRootArg) {
  console.error("usage: check-icu.mjs <patched-upstream>");
  process.exit(2);
}

const sourceRoot = path.resolve(sourceRootArg);
const pnpmStore = path.join(sourceRoot, "node_modules", ".pnpm");
const parserPackage = fs
  .readdirSync(pnpmStore)
  .find((entry) => entry.startsWith("@formatjs+icu-messageformat-parser@"));
if (!parserPackage) throw new Error("ICU parser dependency is not installed");

const requireFromUpstream = createRequire(
  path.join(sourceRoot, "web", "package.json"),
);
const { parse } = requireFromUpstream(
  path.join(
    pnpmStore,
    parserPackage,
    "node_modules",
    "@formatjs",
    "icu-messageformat-parser",
  ),
);

function flatten(value, prefix = "") {
  if (typeof value === "string") return [[prefix, value]];
  return Object.entries(value).flatMap(([key, child]) =>
    flatten(child, prefix ? `${prefix}.${key}` : key),
  );
}

for (const locale of ["en", "zh-CN"]) {
  const messages = JSON.parse(
    fs.readFileSync(path.join(sourceRoot, "web", "messages", `${locale}.json`)),
  );
  for (const [key, message] of flatten(messages)) {
    try {
      parse(message, { requiresOtherClause: true });
    } catch (error) {
      throw new Error(`${locale}:${key}: ${error.message}`);
    }
  }
}

console.log("ICU messages: OK");
