import { test, expect, type BrowserContext } from "@playwright/test";

let cookies: Awaited<ReturnType<BrowserContext["cookies"]>> = [];

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/login");
  await page.getByLabel("账户", { exact: true }).fill("flow-admin");
  await page.getByLabel("密码", { exact: true }).fill("local-test-password-123");
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await page.waitForURL("http://localhost:13110/");
  cookies = await context.cookies();
  await context.close();
});

test.beforeEach(async ({ page }) => { await page.context().addCookies(cookies); });

test("knowledge import shows progress, indexed documents and their GitHub source", async ({ page, request }) => {
  const { repo_id: repoId } = await (await request.get("http://127.0.0.1:18110/test/state")).json();
  const base = `**/api/v1/repos/${repoId}`;
  const sha = "a".repeat(40);
  let stage = "empty";
  const result = { commit_sha: sha, discovered: 2, imported: 1, unchanged: 0, removed: 0, skipped_count: 1, skipped: [{ path: "docs/empty.md", reason: "文档内容为空" }] };
  await page.route(`${base}/knowledge/status`, route => route.fulfill({ json: { data: {
    document_count: stage === "ready" ? 1 : 0,
    document_sources: { repository: stage === "ready" ? 1 : 0, uploaded: 0, generated: 0 },
    active_generation: stage === "ready" ? { id: "gen-1", chunk_count: 3, activated_at: "2026-10-26T06:00:00Z", embedded: false, degraded_reason: "not_configured" } : null,
    pending_changes: 0, building: false, latest_failure: null,
    import_job: stage === "empty" ? null : { id: "job-1", status: stage === "ready" ? "succeeded" : "leased", last_error: null, result: stage === "ready" ? result : null },
    index_job: null,
  } } }));
  await page.route(`${base}/documents?*`, route => route.fulfill({ json: { data: { items: stage === "ready" ? [{ id: "doc-1", title: "项目说明", source_key: "git:README.md", source_revision: sha, origin: "source", state: "active", version: 1, byte_size: 80, current_version_id: "v1" }] : [], next_cursor: null } } }));
  await page.route(`${base}/knowledge/import`, route => {
    stage = "importing";
    return route.fulfill({ status: 202, json: { data: { accepted: true, job: { id: "job-1", status: "queued" } } } });
  });
  await page.goto(`/repos/${repoId}/knowledge`);
  await expect(page.getByRole("status")).toContainText("知识库为空");
  await page.getByRole("button", { name: "从 GitHub 导入 / 更新文档" }).click();
  await expect(page.getByRole("status")).toContainText("正在提取仓库文档");
  await expect(page.getByRole("button", { name: "正在导入仓库文档…" })).toBeDisabled();
  stage = "ready";
  await expect(page.getByRole("status")).toContainText("知识库已就绪");
  await expect(page.getByText("仓库文档 1 · 手动上传 0 · 生成报告 0")).toBeVisible();
  await expect(page.getByRole("link", { name: /查看 GitHub 原文/ })).toHaveAttribute("href", `https://github.com/fixture/browser-flows/blob/${sha}/README.md`);
  await page.getByText("查看跳过原因").click();
  await expect(page.getByText("docs/empty.md：文档内容为空")).toBeVisible();
});

test("knowledge question identifies an empty library and offers document import", async ({ page, request }) => {
  const { repo_id: repoId, empty_conversation_id: conversationId } = await (await request.get("http://127.0.0.1:18110/test/state")).json();
  await page.route(`**/api/v1/repos/${repoId}/knowledge/status`, route => route.fulfill({ json: { data: { document_count: 0, active_generation: null, import_job: null, index_job: null, pending_changes: 0, building: false } } }));
  await page.goto(`/repos/${repoId}/conversations/${conversationId}`);
  await page.getByRole("combobox", { name: "类型", exact: true }).selectOption("knowledge_qa");
  await expect(page.getByLabel("知识库准备状态")).toContainText("当前知识库为空");
  await expect(page.getByRole("button", { name: "导入 GitHub 仓库文档" })).toBeVisible();
  await expect(page.getByRole("link", { name: "管理项目知识库" })).toHaveAttribute("href", `/repos/${repoId}/knowledge`);
});
