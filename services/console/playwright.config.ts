import { defineConfig, devices } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, "../..");
const venvPython = path.join(rootDir, ".venv/bin/python");
const pythonBin = fs.existsSync(venvPython) ? venvPython : "python";
const evalRunnerDir = path.join(rootDir, "services/eval-runner");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: "list",
  timeout: 30000,
  use: {
    baseURL: "http://127.0.0.1:18083",
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
      command: `rm -f /tmp/argus_playwright_e2e.db && DATABASE_URL=sqlite:////tmp/argus_playwright_e2e.db ARGUS_DB_MODE=test ARGUS_AUTO_IMPORT_YAML=true ${pythonBin} -m uvicorn app.main:app --app-dir "${evalRunnerDir}" --port 18080 --host 127.0.0.1`,
      url: "http://127.0.0.1:18080/api/v1/system/info",
      reuseExistingServer: !process.env.CI,
      timeout: 30000,
    },
    {
      command: "npx vite preview --port 18083 --host 127.0.0.1",
      url: "http://127.0.0.1:18083",
      reuseExistingServer: !process.env.CI,
      timeout: 30000,
    },
  ],
});
