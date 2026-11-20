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
  await expect(page.getByRole("heading", { name: "工作空间", exact: true })).toBeVisible();
  // The model is configured in this environment, so the live-verification
  // button is enabled (the M0-only state assumed it disabled).
  await page.getByText("模型连接与验证",{exact:true}).click();
  await expect(page.getByRole("button", { name: "验证真实模型" })).toBeEnabled();
  await page.getByLabel("验证样例").selectOption("failed_ci");
  await page.getByRole("button", { name: "运行离线验证" }).click();
  await expect(page.getByRole("heading", { name: "验证通过", exact: true })).toBeVisible();
  await expect(page.getByText("DEVFLOW_FAILED_CI_V1", { exact: true })).toBeVisible();
  await expect(page.getByText("离线协议回放").first()).toBeVisible();
  await page.reload();
  await page.getByText("模型连接与验证",{exact:true}).click();
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

test("repository workspace is reachable after login", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("账户", { exact: true }).fill(credentials.username);
  await page.getByLabel("密码", { exact: true }).fill(credentials.password);
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await page.locator("aside").getByRole("link", { name: /仓库协作/ }).click();
  await expect(page.getByRole("heading", { name: "仓库协作", exact: true })).toBeVisible();
  await expect(page.getByLabel("GitHub 仓库链接或 owner/repo")).toBeVisible();
  await page.getByRole("button", { name: "刷新" }).click();
});

test("repository import explains GitHub link formats before submitting", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("账户", { exact: true }).fill(credentials.username);
  await page.getByLabel("密码", { exact: true }).fill(credentials.password);
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await page.locator("aside").getByRole("link", { name: /仓库协作/ }).click();

  const input = page.getByLabel("GitHub 仓库链接或 owner/repo");
  await input.fill("https://github.com/octo/demo/tree/main");
  await expect(page.getByRole("status")).toHaveText("将导入：octo/demo");
  await input.fill("git clone git@github.com:octo/demo.git");
  await expect(page.getByRole("status")).toHaveText("将导入：octo/demo");
  await input.fill("ssh://git@github.com/octo/demo.git");
  await expect(page.getByRole("status")).toHaveText("将导入：octo/demo");
  await input.fill("https://gitlab.com/octo/demo");
  await page.getByRole("button", { name: "接入仓库" }).click();
  await expect(page.locator(".error-box[role='alert']")).toContainText("仅支持 github.com 仓库链接");
});

test("repository overview lists conversations and runs", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("账户", { exact: true }).fill(credentials.username);
  await page.getByLabel("密码", { exact: true }).fill(credentials.password);
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await page.waitForURL("http://localhost:3100/");
  await page.goto("/repos");
  const enter = page.getByRole("link", { name: "进入" }).first();
  await expect(enter).toBeVisible({ timeout: 15000 });
  await enter.click();
  await expect(page.getByRole("heading", { name: "分析会话" })).toBeVisible();
  await expect(page.getByRole("button", { name: "新建分析会话" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "最近运行" })).toBeVisible();
  await expect(page.getByText("Workspace", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /待处理 Issue/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeVisible();
});

test("evals page shows the deterministic suite and its limits", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("账户", { exact: true }).fill(credentials.username);
  await page.getByLabel("密码", { exact: true }).fill(credentials.password);
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await page.waitForURL("http://localhost:3100/");
  await page.goto("/evals");
  await expect(page.getByRole("heading", { name: "评测与基线" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "离线固定评测" })).toBeVisible();
  await expect(page.getByText(/不代表模型质量/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "真实案例基线" })).toBeVisible();
});
