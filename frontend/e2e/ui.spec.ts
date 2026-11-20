import { expect, test } from "@playwright/test";
import { apiContext, apiToken, ensureUser, envValue, login } from "./helpers";

test.describe("FaultLens UI against the live stack", () => {
  test("login, navigate all pages with real data, logout", async ({ page }) => {
    await login(page);
    await expect(page.getByText("Live", { exact: true })).toBeVisible({ timeout: 15_000 }); // WebSocket authenticated
    const pages: [string, string][] = [
      ["Services", "Services"], ["Tracing", "Distributed Tracing"], ["Logs", "Logs Explorer"], ["Anomalies", "Anomaly Detection"],
      ["Root Cause", "Root Cause Analysis"], ["Dependencies", "Service Dependency Graph"], ["Incidents", "Incidents"],
      ["Fault Lab", "Fault Injection Lab"], ["Settings", "Settings"],
    ];
    for (const [nav, heading] of pages) {
      await page.getByRole("navigation", { name: "primary" }).getByRole("link", { name: nav }).click();
      await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
      await expect(page.getByRole("alert")).toHaveCount(0);
    }
    await page.getByRole("navigation", { name: "primary" }).getByRole("link", { name: "Services" }).click();
    await expect(page.getByRole("link", { name: "payment-service" })).toBeVisible();
    await page.getByRole("button", { name: "sign out" }).click();
    await expect(page.getByRole("form", { name: "sign in" })).toBeVisible();
  });

  test("dependency graph is built from observed traffic", async ({ page }) => {
    await login(page);
    await page.goto("/graph");
    for (const s of ["order-service", "payment-service", "inventory-service"]) {
      await expect(page.locator(".react-flow__node").filter({ hasText: s })).toBeVisible();
    }
    expect(await page.locator(".react-flow__edge").count()).toBeGreaterThanOrEqual(4);
  });

  test("trace waterfall and trace-to-log navigation", async ({ page }) => {
    await login(page);
    await page.goto("/traces");
    await page.getByRole("row").nth(1).click();
    await expect(page.getByRole("tree", { name: "span waterfall" })).toBeVisible();
    expect(await page.getByRole("treeitem").count()).toBeGreaterThan(3);
    await page.getByRole("link", { name: "Correlated logs" }).click();
    await expect(page).toHaveURL(/\/logs\?trace_id=[0-9a-f]{32}/);
    await expect(page.getByRole("heading", { name: "Logs Explorer" })).toBeVisible();
  });

  test("a new incident appears live without reload; viewer is read-only", async ({ page }) => {
    await ensureUser(page, "pw-viewer", "viewer", "Pw-Viewer-Passw0rd");
    await login(page, "pw-viewer", "Pw-Viewer-Passw0rd");
    await page.goto("/incidents");
    await expect(page.getByRole("button", { name: /Declare incident/ })).toHaveCount(0);
    const title = `ui-live-${Date.now()}`;
    const admin = await apiToken(page, envValue("BOOTSTRAP_ADMIN_USERNAME"), envValue("BOOTSTRAP_ADMIN_PASSWORD"));
    const api = await apiContext();
    const r = await api.post("/api/v1/incidents", { headers: { Authorization: `Bearer ${admin}` },
      data: { title, severity: "low", services: ["user-service"] } });
    expect(r.status()).toBe(201);
    await expect(page.getByText(title)).toBeVisible({ timeout: 15_000 }); // delivered via Kafka -> Redis -> WebSocket
    await page.getByText(title).click();
    await expect(page.getByText(/Read-only: your role cannot change incident state/)).toBeVisible();
    const id = (await r.json()).id;
    await api.post(`/api/v1/incidents/${id}/acknowledge`, { headers: { Authorization: `Bearer ${admin}` }, data: {} });
    await api.post(`/api/v1/incidents/${id}/resolve`, { headers: { Authorization: `Bearer ${admin}` }, data: { note: "ui test cleanup" } });
    await api.dispose();
  });
});
