import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
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
  webServer: {
    command: "npx vite preview --port 18083 --host 127.0.0.1",
    url: "http://127.0.0.1:18083",
    reuseExistingServer: !process.env.CI,
    timeout: 30000,
  },
});
