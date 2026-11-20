import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("账户", { exact: true }).fill("flow-admin");
  await page.getByLabel("密码", { exact: true }).fill("local-test-password-123");
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await page.waitForURL("http://localhost:13110/");
});

test("repository link import accepts clone forms and registers the repository", async ({ page }) => {
  await page.locator("aside").getByRole("link", { name: /仓库协作/ }).click();
  const input = page.getByLabel("GitHub 仓库链接或 owner/repo");

  await input.fill("git clone https://github.com/octo/demo.git");
  await expect(page.getByRole("status")).toHaveText("将导入：octo/demo");

  await input.fill("ssh://git@github.com/octo/demo.git");
  await expect(page.getByRole("status")).toHaveText("将导入：octo/demo");

  await page.getByRole("button", { name: "接入仓库" }).click();
  await expect(page.locator(".repo-list")).toContainText("octo/demo");
  await expect(input).toHaveValue("");
});
