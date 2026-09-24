import { defineConfig, devices } from "@playwright/test";

// The suite owns port 4174 and always starts its own server, so it grades the
// current source. A developer dev server on 4173 must never be picked up.
const PORT = 4174;
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.spec.ts",
  fullyParallel: true,
  workers: 2,
  reporter: "list",
  // The analyze endpoint is mocked per test (page.route), so results land fast;
  // the headroom covers Vite's first on-demand compile under parallel load.
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : {},
  },
  webServer: {
    command: `npm run dev -- --port ${PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: false,
  },
});
