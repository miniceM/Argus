#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const [sourceRootArg, baselineArg, mode] = process.argv.slice(2);
if (!sourceRootArg || !baselineArg) {
  console.error(
    "usage: check-hardcoded-ui.mjs <patched-upstream> <baseline.json> [--write-baseline]",
  );
  process.exit(2);
}

const sourceRoot = path.resolve(sourceRootArg);
const baselinePath = path.resolve(baselineArg);
const requireFromUpstream = createRequire(
  path.join(sourceRoot, "web", "package.json"),
);
let ts = requireFromUpstream("typescript");
if (!ts.ScriptTarget) {
  const pnpmStore = path.join(sourceRoot, "node_modules", ".pnpm");
  const typescript6 = fs
    .readdirSync(pnpmStore)
    .find((entry) => entry.startsWith("@typescript+typescript6@"));
  if (!typescript6) {
    throw new Error("TypeScript compiler API package is not installed");
  }
  ts = requireFromUpstream(
    path.join(
      pnpmStore,
      typescript6,
      "node_modules",
      "@typescript",
      "typescript6",
    ),
  );
}
const defaultScopes = [
  "web/src/components/nav",
  "web/src/components/layouts/routes.tsx",
  "web/src/components/layouts/page-header.tsx",
  "web/src/components/layouts/breadcrumb.tsx",
  "web/src/components/layouts/page-tabs.tsx",
  "web/src/pages/project/[projectId]/datasets/index.tsx",
  "web/src/pages/project/[projectId]/experiments/index.tsx",
  "web/src/pages/project/[projectId]/traces/index.tsx",
  "web/src/pages/project/[projectId]/observations/index.tsx",
  "web/src/pages/project/[projectId]/scores/index.tsx",
  "web/src/pages/project/[projectId]/settings/index.tsx",
  "web/src/pages/organization/[organizationId]/settings/index.tsx",
];

const baseline = fs.existsSync(baselinePath)
  ? JSON.parse(fs.readFileSync(baselinePath, "utf8"))
  : { scopes: defaultScopes, entries: {} };
const scopes = baseline.scopes ?? defaultScopes;

function listFiles(target) {
  if (!fs.existsSync(target)) return [];
  const stat = fs.statSync(target);
  if (stat.isFile()) return [target];
  return fs
    .readdirSync(target, { withFileTypes: true })
    .flatMap((entry) => listFiles(path.join(target, entry.name)));
}

const files = scopes
  .flatMap((scope) => listFiles(path.join(sourceRoot, scope)))
  .filter((file) => /\.tsx?$/.test(file))
  .filter((file) => !/\.(?:clienttest|servertest|test|spec|stories)\.[^.]+$/.test(file));

const visibleAttributeNames = new Set([
  "aria-label",
  "description",
  "heading",
  "label",
  "placeholder",
  "title",
]);
const visiblePropertyNames = new Set([
  "description",
  "header",
  "label",
  "name",
  "subTitle",
  "title",
]);
const counts = {};

function normalize(value) {
  return value.replace(/\s+/g, " ").trim();
}

function looksLikeUiCopy(value) {
  const text = normalize(value);
  return /[A-Za-z]{2,}/.test(text) && !/^(?:https?:|\/|[a-z]+:)/.test(text);
}

function record(relative, kind, value) {
  const text = normalize(value);
  if (!looksLikeUiCopy(text)) return;
  const key = `${relative}|${kind}|${text}`;
  counts[key] = (counts[key] ?? 0) + 1;
}

for (const file of files) {
  const relative = path.relative(sourceRoot, file).split(path.sep).join("/");
  const source = fs.readFileSync(file, "utf8");
  const sourceFile = ts.createSourceFile(
    file,
    source,
    ts.ScriptTarget.Latest,
    true,
    file.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );

  function visit(node) {
    if (ts.isJsxText(node)) record(relative, "jsx", node.getText(sourceFile));

    if (
      ts.isJsxAttribute(node) &&
      visibleAttributeNames.has(node.name.getText(sourceFile)) &&
      node.initializer &&
      ts.isStringLiteral(node.initializer)
    ) {
      record(relative, `attribute:${node.name.getText(sourceFile)}`, node.initializer.text);
    }

    if (
      ts.isPropertyAssignment(node) &&
      visiblePropertyNames.has(node.name.getText(sourceFile).replace(/["']/g, "")) &&
      ts.isStringLiteralLike(node.initializer)
    ) {
      record(
        relative,
        `property:${node.name.getText(sourceFile).replace(/["']/g, "")}`,
        node.initializer.text,
      );
    }

    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      node.expression.expression.getText(sourceFile) === "toast" &&
      ["error", "info", "success", "warning"].includes(node.expression.name.text) &&
      node.arguments[0] &&
      ts.isStringLiteralLike(node.arguments[0])
    ) {
      record(relative, `toast:${node.expression.name.text}`, node.arguments[0].text);
    }

    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
}

if (mode === "--write-baseline") {
  fs.writeFileSync(
    baselinePath,
    `${JSON.stringify({ version: 1, scopes, entries: counts }, null, 2)}\n`,
  );
  console.log(`wrote ${Object.keys(counts).length} baseline entries`);
  process.exit(0);
}

const unexpected = Object.entries(counts).filter(
  ([key, count]) => count > (baseline.entries?.[key] ?? 0),
);
if (unexpected.length > 0) {
  console.error("New hard-coded UI copy detected in the covered i18n scope:");
  for (const [key, count] of unexpected) {
    console.error(`- ${key} (found ${count}, baseline ${baseline.entries?.[key] ?? 0})`);
  }
  console.error("Translate the copy or update the reviewed baseline with a reason.");
  process.exit(1);
}

console.log(
  `hard-coded UI baseline: OK (${files.length} files, ${Object.keys(counts).length} tracked entries)`,
);
