import {test,expect} from "@playwright/test";
const control="http://127.0.0.1:18110";
test.beforeEach(async({page,request})=>{await page.goto("/login");await page.getByLabel("账户",{exact:true}).fill("flow-admin");await page.getByLabel("密码",{exact:true}).fill("local-test-password-123");await page.getByRole("button",{name:"进入工作空间"}).click();await page.waitForURL("http://localhost:13110/");});

test("draft revision clears approval and revoked action cannot execute",async({page,request})=>{
  const state=await (await request.get(`${control}/test/state`)).json();
  await page.goto(`/repos/${state.repo_id}/collaboration`);
  await page.getByRole("button",{name:"操作草稿",exact:true}).click();
  await page.getByRole("combobox",{name:"选择Issue / PR",exact:true}).selectOption("1");
  await page.getByLabel("完整正文",{exact:true}).fill("浏览器验收草稿，不发送 GitHub");
  await page.getByRole("button",{name:"保存待审批草稿"}).click();
  const card=page.locator("article.platform-item").filter({hasText:"浏览器验收草稿，不发送 GitHub"});
  await card.getByRole("button",{name:"批准上述完整内容"}).click();
  await expect(card.getByRole("button",{name:"执行已批准操作"})).toBeVisible();
  await page.reload();
  await page.getByRole("button",{name:"操作草稿",exact:true}).click();
  await expect(card.getByRole("button",{name:"执行已批准操作"})).toBeVisible();
  await card.getByRole("button",{name:"修订并清除批准"}).click();
  await page.getByLabel("完整正文",{exact:true}).fill("修订后的草稿");
  await page.getByRole("button",{name:"保存待审批草稿"}).click();
  const revised=page.locator("article.platform-item").filter({hasText:"修订后的草稿"});
  await expect(revised).toContainText("v2");
  await expect(revised.getByRole("button",{name:"执行已批准操作"})).toHaveCount(0);
  await revised.getByRole("button",{name:"批准上述完整内容"}).click();
  await revised.getByRole("button",{name:"撤销",exact:true}).click();
  await expect(revised).toContainText("revoked");
  await expect(revised.getByRole("button",{name:"执行已批准操作"})).toHaveCount(0);
});

test("memory source and evidence can be selected, approved and revoked",async({page,request})=>{
  const state=await(await request.get(`${control}/test/state`)).json();
  await page.goto(`/repos/${state.repo_id}/collaboration`);
  await page.getByRole("button",{name:"项目记忆",exact:true}).click();
  const sources=page.getByRole("combobox",{name:"来源分析",exact:true});
  await expect(sources.locator("option")).toHaveCount(2);
  const id=await sources.locator("option").nth(1).getAttribute("value");
  await sources.selectOption(id!);
  await page.getByRole("checkbox",{name:/所有外部操作需要批准版本与哈希一致/}).check();
  await page.getByLabel("经验键（用于冲突检测）").fill("browser-policy");
  await page.getByLabel("经验内容",{exact:true}).fill("审批需绑定版本");
  await page.getByRole("button",{name:"提交候选"}).click();
  await page.getByRole("button",{name:"批准经验",exact:true}).click();
  await expect(page.getByRole("heading",{name:"browser-policy · active"})).toBeVisible();
  await page.getByRole("button",{name:"撤销经验",exact:true}).click();
  await expect(page.getByRole("heading",{name:"browser-policy · revoked"})).toBeVisible();
});

test("cancel request survives refresh and terminal SSE is replayed",async({page,request})=>{
  const seeded=await(await request.post(`${control}/test/seed-run`,{data:{},headers:{Origin:"http://localhost:13110"}})).json();
  await page.goto(`/repos/${seeded.repo_id}/conversations/${seeded.conversation_id}`);
  await page.getByRole("button",{name:"取消当前分析"}).click();
  await expect(page.getByText("已提交取消请求，等待当前步骤结束。")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button",{name:"正在取消…"})).toBeDisabled();
  await request.post(`${control}/test/settle/${seeded.run_id}`,{headers:{Origin:"http://localhost:13110"}});
  await expect(page.locator(".run-card").getByText("已取消",{exact:true})).toBeVisible();
  await page.reload();
  await expect(page.locator(".run-card").getByText("已取消",{exact:true})).toBeVisible();
  await expect(page.getByRole("button",{name:"取消当前分析"})).toHaveCount(0);
});

test("expired workflow lease recovers and reuses completed child after refresh",async({page,request})=>{
  const seeded=await(await request.post(`${control}/test/seed-run`,{data:{workflow:true},headers:{Origin:"http://localhost:13110"}})).json();
  await page.goto(`/repos/${seeded.repo_id}/conversations/${seeded.conversation_id}`);
  await expect(page.getByText("PLAN & EXECUTE", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading",{name:"协作任务"})).toBeVisible();
  const before=await(await request.get(`${control}/test/state`)).json();
  await request.post(`${control}/test/recover`,{headers:{Origin:"http://localhost:13110"}});
  await expect(page.locator(".run-card").getByText("恢复中",{exact:true})).toBeVisible();
  await page.reload();
  await expect(page.locator(".run-card").getByText("恢复中",{exact:true})).toBeVisible();
  const response=await request.post(`${control}/test/resume/${seeded.run_id}`,{headers:{Origin:"http://localhost:13110"}});
  expect(response.ok()).toBeTruthy();
  await expect(page.locator(".run-card").getByText("已完成",{exact:true})).toBeVisible();
  const after=await(await request.get(`${control}/test/state`)).json();
  expect(after.child_runs).toBe(before.child_runs);
  await expect(page.getByRole("heading",{name:"协作任务"})).toBeVisible();
  await expect(page.getByRole("heading",{name:"Observer 检查"})).toBeVisible();
  await expect(page.getByText(/置信度/).first()).toBeVisible();
});

test("CI choices follow selected PR commit and permission check is explicit",async({page,request})=>{
  const state=await(await request.get(`${control}/test/state`)).json();
  await page.goto(`/repos/${state.repo_id}/collaboration`);
  await page.getByRole("combobox",{name:"选择PR",exact:true}).selectOption("2");
  await expect(page.getByLabel("同提交 CI").locator("option")).toHaveCount(2);
  await page.getByLabel("同提交 CI").selectOption("0");
  await page.getByRole("button",{name:"检查 GitHub 读取权限"}).click();
  await expect(page.getByText("Actions 列表可读不代表日志可读",{exact:false})).toBeVisible();
  await page.getByText("手动指定 CI（高级）",{exact:true}).click();
  await expect(page.getByLabel("CI run ID（可选）",{exact:true})).toHaveValue("99");
  await expect(page.getByLabel("CI attempt",{exact:true})).toHaveValue("2");
});

test("CI selector keeps distinct attempts of the same run",async({page,request})=>{
  const state=await(await request.get(`${control}/test/state`)).json();
  await page.goto(`/repos/${state.repo_id}/conversations/${state.empty_conversation_id}`);
  await page.getByRole("combobox",{name:"类型",exact:true}).selectOption("ci_debug");
  const picker=page.getByRole("combobox",{name:"选择CI 运行",exact:true});
  await picker.selectOption("99:1");
  await expect(page.getByLabel("Attempt",{exact:true})).toHaveValue("1");
  await picker.selectOption("99:2");
  await expect(page.getByLabel("Attempt",{exact:true})).toHaveValue("2");
  await expect(picker).toHaveValue("99:2");
});
