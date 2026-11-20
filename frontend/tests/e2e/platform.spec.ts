import {test, expect} from "@playwright/test";
import {readFileSync} from "node:fs";
import {resolve} from "node:path";

const credentials=JSON.parse(readFileSync(resolve(process.cwd(),"../var/initial-admin.json"),"utf8").replace(/^\uFEFF/,""));
test.beforeEach(async({page})=>{
  await page.goto("/login");
  await page.getByLabel("账户",{exact:true}).fill(credentials.username);
  await page.getByLabel("密码",{exact:true}).fill(credentials.password);
  await page.getByRole("button",{name:"进入工作空间"}).click();
  await page.waitForURL("http://localhost:3100/");
  await page.goto("/repos");
  await page.getByRole("link",{name:"进入",exact:true}).first().click();
  await page.getByRole("link",{name:/工作流与审批/}).click();
  await expect(page.getByRole("heading",{name:"工作流与审批",exact:true})).toBeVisible();
});

test("all platform sections render and invalid extension is explained",async({page})=>{
  const errors:string[]=[];page.on("pageerror",e=>errors.push(e.message));
  await expect(page.getByRole("heading",{name:"发布检查",exact:true})).toBeVisible();
  await page.screenshot({path:"test-results/platform-desktop.png",fullPage:true,animations:"disabled"});
  await page.getByRole("button",{name:"操作草稿",exact:true}).click();
  await expect(page.getByLabel("完整正文",{exact:true})).toBeVisible();
  await page.getByRole("button",{name:"项目记忆",exact:true}).click();
  await expect(page.getByLabel("来源分析")).toBeVisible();
  await page.getByRole("button",{name:"扩展",exact:true}).click();
  await page.getByLabel("扩展配置（JSON）").fill("not-json");
  await page.getByRole("button",{name:"校验并安装此版本"}).click();
  await expect(page.locator(".error-box[role=alert]")).toBeVisible();
  await page.getByRole("button",{name:"审计记录",exact:true}).click();
  await expect(page.getByRole("heading",{name:"最近 100 条审计记录"})).toBeVisible();
  expect(errors).toEqual([]);
});

test("weekly report completes without model calls and can be downloaded",async({page})=>{
  await page.getByRole("button",{name:"生成报告",exact:true}).click();
  await expect(page).toHaveURL(/\/conversations\//);
  await expect(page.locator(".run-card").getByText("已完成",{exact:true})).toBeVisible({timeout:30000});
  await expect(page.getByRole("heading",{name:/活动统计/})).toBeVisible();
  await page.getByRole("link",{name:/工作流与审批/}).click();
  const download=page.waitForEvent("download");
  await page.getByRole("button",{name:"下载报告",exact:true}).first().click();
  expect((await download).suggestedFilename()).toMatch(/^report-.*\.json$/);
});

test("mobile platform forms fit viewport",async({page})=>{
  await page.setViewportSize({width:390,height:844});
  for(const section of ["协作与报告","操作草稿","项目记忆","扩展","审计记录"]){
    await page.getByRole("button",{name:section,exact:true}).click();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
  }
  await page.screenshot({path:"test-results/platform-mobile.png",fullPage:true,animations:"disabled"});
});
