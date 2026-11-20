import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, request, type APIRequestContext, type Page } from "@playwright/test";

const HERE = dirname(fileURLToPath(import.meta.url));
const BASE = process.env.FAULTLENS_UI ?? "http://localhost:3080";

/** Isolated API context: must not share cookies with the browser page (a refresh cookie would log the page in). */
export async function apiContext(): Promise<APIRequestContext> {
  return request.newContext({ baseURL: BASE });
}

export function envValue(key: string): string {
  if (process.env[key]) return process.env[key]!;
  const env = readFileSync(resolve(HERE, "../../.env"), "utf8");
  const line = env.split(/\r?\n/).find((l) => l.startsWith(`${key}=`));
  if (!line) throw new Error(`${key} missing from .env`);
  return line.slice(key.length + 1);
}

export async function login(page: Page, username = envValue("BOOTSTRAP_ADMIN_USERNAME"), password = envValue("BOOTSTRAP_ADMIN_PASSWORD")) {
  await page.goto("/login");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
}

export async function apiToken(_page: Page, username: string, password: string): Promise<string> {
  const ctx = await apiContext();
  const r = await ctx.post("/api/v1/auth/login", { data: { username, password } });
  expect(r.ok()).toBeTruthy();
  const token = (await r.json()).access_token;
  await ctx.dispose();
  return token;
}

export async function ensureUser(page: Page, username: string, role: string, password: string) {
  const admin = await apiToken(page, envValue("BOOTSTRAP_ADMIN_USERNAME"), envValue("BOOTSTRAP_ADMIN_PASSWORD"));
  const h = { Authorization: `Bearer ${admin}` };
  const ctx = await apiContext();
  const users = await (await ctx.get("/api/v1/users?limit=200", { headers: h })).json();
  const u = users.items.find((x: { username: string }) => x.username === username);
  if (!u) await ctx.post("/api/v1/users", { headers: h, data: { username, password, role } });
  else await ctx.patch(`/api/v1/users/${u.id}`, { headers: h, data: { password, role, is_active: true } });
  await ctx.dispose();
}
