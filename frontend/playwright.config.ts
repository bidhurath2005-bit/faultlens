import { defineConfig } from "@playwright/test";

// Browser E2E against the running docker compose stack (UI on :3080).
// Uses the system Microsoft Edge (Chromium) via `channel`, so no browser download is required on Windows;
// set PW_CHANNEL=chromium (after `npx playwright install chromium`) on Linux/CI.
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.FAULTLENS_UI ?? "http://localhost:3080",
    channel: process.env.PW_CHANNEL ?? "msedge",
    viewport: { width: 1600, height: 1000 },
    colorScheme: "dark",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
