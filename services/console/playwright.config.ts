import { defineConfig, devices } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { randomBytes } from "node:crypto";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, "../..");
const venvPython = path.join(rootDir, ".venv/bin/python");
const pythonBin = fs.existsSync(venvPython) ? venvPython : "python";
const evalRunnerDir = path.join(rootDir, "services/eval-runner");

function getPort(name: string, fallback: number): number {
  const port = Number(process.env[name] ?? fallback);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`${name} must be an integer between 1 and 65535`);
  }
  return port;
}

const apiPort = getPort("ARGUS_E2E_API_PORT", 18080);
const consolePort = getPort("ARGUS_E2E_CONSOLE_PORT", 18083);
const testDatabase = `/tmp/argus_playwright_e2e_${apiPort}.db`;
// 隔离真实 API 的凭据测试配置；不使用开发者/生产主密钥。
const secretDir = fs.mkdtempSync(path.join(os.tmpdir(), "argus-e2e-secrets-"));
const keyFile = path.join(secretDir, "keyring.json");
const adminFile = path.join(secretDir, "admin-token");
fs.writeFileSync(keyFile, JSON.stringify({ active_key_id: "test-key", keys: { "test-key": randomBytes(32).toString("base64") } }), { mode: 0o600 });
fs.writeFileSync(adminFile, "argus-e2e-admin-token", { mode: 0o600 });

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: "list",
  timeout: 30000,
  use: {
    baseURL: `http://127.0.0.1:${consolePort}`,
    trace: "on-first-retry",
    headless: true,
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        channel: "chrome",
      },
    },
  ],
  webServer: [
    {
      command: `rm -f "${testDatabase}" && DATABASE_URL=sqlite:////${testDatabase.replace(/^\//, "")} ARGUS_DB_MODE=test ARGUS_AUTO_IMPORT_YAML=true ARGUS_BUILD_ID=playwright-e2e ${pythonBin} -m uvicorn app.main:app --app-dir "${evalRunnerDir}" --port ${apiPort} --host 127.0.0.1`,
      url: `http://127.0.0.1:${apiPort}/api/v1/system/info`,
      env: { ARGUS_MANAGED_SECRET_KEY_FILE: keyFile, ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE: adminFile },
      // The real-API E2E creates Launches; never reuse a developer or Compose service.
      reuseExistingServer: false,
      timeout: 30000,
    },
    {
      command: `npx vite preview --port ${consolePort} --host 127.0.0.1`,
      url: `http://127.0.0.1:${consolePort}`,
      // The real-API E2E creates Launches; never reuse a developer or Compose service.
      reuseExistingServer: false,
      timeout: 30000,
    },
  ],
});
