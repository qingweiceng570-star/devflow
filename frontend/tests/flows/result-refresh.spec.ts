import { test, expect } from "@playwright/test";

const control = "http://127.0.0.1:18110";
const headers = { Origin: "http://localhost:13110" };

test.beforeEach(async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("账户", { exact: true }).fill("flow-admin");
  await page.getByLabel("密码", { exact: true }).fill("local-test-password-123");
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await page.waitForURL("http://localhost:13110/");
});

for (const exhausted of [false, true]) {
  test(exhausted ? "terminal refresh shows a retry button after bounded failures" : "terminal refresh recovers after a temporary outage", async ({ page, request }) => {
    const seeded = await (await request.post(`${control}/test/seed-run`, { data: {}, headers })).json();
    await page.goto(`/repos/${seeded.repo_id}/conversations/${seeded.conversation_id}`);
    await page.getByRole("button", { name: "取消当前分析" }).click();
    await expect(page.getByText("已提交取消请求，等待当前步骤结束。")).toBeVisible();
    let attempts = 0;
    let failing = true;
    await page.route(`**/api/v1/conversations/${seeded.conversation_id}`, async route => {
      attempts++;
      if (failing && (exhausted || attempts === 1)) {
        await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: { code: "TEMPORARY", message: "测试断线" } }) });
      } else await route.continue();
    });
    const settled = await request.post(`${control}/test/settle/${seeded.run_id}`, { headers });
    expect(settled.ok()).toBeTruthy();
    if (exhausted) {
      await expect(page.getByRole("alert").filter({ hasText: "更新失败" })).toBeVisible();
      expect(attempts).toBe(3);
      failing = false;
      await page.getByRole("button", { name: "重试加载", exact: true }).click();
    }
    await expect(page.locator(".run-card").getByText("已取消", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "重试加载", exact: true })).toHaveCount(0);
    expect(attempts).toBe(exhausted ? 4 : 2);
  });
}
