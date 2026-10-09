import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const credentials = JSON.parse(readFileSync(resolve(process.cwd(), "../var/initial-admin.json"), "utf8"));

test("login, bounded fixture loop, persisted history and logout", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel("账户", { exact: true }).fill(credentials.username);
  await page.getByLabel("密码", { exact: true }).fill(credentials.password);
  const loginResponse = page.waitForResponse((response) => response.url().endsWith("/api/v1/auth/login"), { timeout: 15000 });
  await page.getByRole("button", { name: "进入工作空间" }).click();
  expect((await loginResponse).status()).toBe(200);
  await expect(page.getByRole("heading", { name: "验证基础，开始协作。" })).toBeVisible();
  await expect(page.getByRole("button", { name: "验证真实模型" })).toBeDisabled();
  await page.getByLabel("验证样例").selectOption("failed_ci");
  await page.getByRole("button", { name: "运行离线验证" }).click();
  await expect(page.getByRole("heading", { name: "验证通过", exact: true })).toBeVisible();
  await expect(page.getByText("DEVFLOW_FAILED_CI_V1", { exact: true })).toBeVisible();
  await expect(page.getByText("离线协议回放").first()).toBeVisible();
  await page.reload();
  await expect(page.getByText("failed_ci", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});

test("mobile login remains usable and invalid password is explained", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/login");
  await page.getByLabel("密码", { exact: true }).fill("invalid-password");
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "账户或密码不正确" })).toHaveText("账户或密码不正确");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});
