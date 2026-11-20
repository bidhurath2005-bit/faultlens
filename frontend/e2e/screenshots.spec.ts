/**
 * Captures the README screenshots from the REAL running application (no mocks).
 * Skipped unless CAPTURE=1:   $env:CAPTURE="1"; npx playwright test e2e/screenshots.spec.ts
 * Optional INCIDENT_ID selects the incident shown on the RCA and Incident pages.
 * Output: ../docs/screenshots/*.png
 */
import { test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { login } from "./helpers";

const OUT = process.env.SHOT_DIR ?? resolve(dirname(fileURLToPath(import.meta.url)), "../../docs/screenshots");

test.skip(process.env.CAPTURE !== "1", "screenshot capture runs only with CAPTURE=1");

test("capture README screenshots", async ({ page }) => {
  test.setTimeout(180_000);
  mkdirSync(OUT, { recursive: true });
  await login(page);
  const inc = process.env.INCIDENT_ID;
  const shots: [string, string, (() => Promise<void>)?][] = [
    ["overview", "/"],
    ["services", "/services"],
    ["service-detail", "/services/payment-service"],
    ["dependency-graph", "/graph"],
    ["traces", "/traces?x=1", async () => {
      await page.getByLabel("status filter").selectOption("error");
      await page.waitForTimeout(1500);
      await page.getByRole("row").nth(1).click();
      await page.getByRole("tree").waitFor();
    }],
    ["logs", "/logs?level=ERROR,WARNING&minutes=15"],
    ["anomalies", "/anomalies"],
    ["rca", inc ? `/rca/${inc}` : "/rca"],
    ["incidents", inc ? `/incidents/${inc}` : "/incidents"],
    ["fault-lab", "/lab"],
    ["settings", "/settings"],
  ];
  for (const [name, path, action] of shots) {
    await page.goto(path);
    await page.waitForLoadState("networkidle").catch(() => undefined);
    await page.waitForTimeout(1500);
    if (action) await action();
    await page.waitForTimeout(700);
    await page.screenshot({ path: `${OUT}/${name}.png` });
  }
});
