"use client";

export type PlanTrace = { tool: string; status: string; error_code?: string | null };
export type PlanToolCall = { tool: string; status: string; evidence_ids: string[] };
export type PlanTask = { key: string; agent: string; state: string; result?: { summary?: string } | null };

type Step = { key: string; title: string; description: string; tools: string[]; final?: boolean };

const PLANS: Record<string, { title: string; subtitle: string; steps: Step[] }> = {
  pr_review: {
    title: "PR Review 过程",
    subtitle: "展示 Review 计划、执行步骤、证据收集和合入判断。",
    steps: [
      { key: "understand", title: "理解 PR", description: "读取标题、描述、分支、作者与改动文件列表", tools: ["get_pr"] },
      { key: "diff", title: "拆分 diff 风险", description: "逐文件拆分改动，标记高风险路径与覆盖度", tools: ["get_pr_diff"] },
      { key: "code", title: "读取代码和测试", description: "按固定提交读取相关代码与测试片段", tools: ["read_file", "search_code"] },
      { key: "ci", title: "检查 CI", description: "读取与目标提交匹配的 CI 运行与失败任务", tools: ["get_ci_runs"] },
      { key: "context", title: "检索上下文", description: "检索历史资料、Issue 与已批准项目经验", tools: ["search_knowledge", "get_issue", "list_issues"] },
      { key: "summarize", title: "汇总结论", description: "结构化输出风险、证据缺口与合入建议", tools: [], final: true },
    ],
  },
  repo_health: {
    title: "仓库健康检查",
    subtitle: "读取仓库概要、Issue、PR、CI 与知识库，形成健康摘要。",
    steps: [
      { key: "summary", title: "读取仓库概要", description: "统计 Issue、PR、CI 与最近同步范围", tools: ["get_repo_summary"] },
      { key: "knowledge", title: "检索项目知识", description: "检索项目文档与历史经验", tools: ["search_knowledge"] },
      { key: "details", title: "检查明细", description: "按需读取 Issue、PR 与 CI 记录", tools: ["get_issue", "get_pr", "get_ci_runs", "list_issues", "list_pull_requests"] },
      { key: "summarize", title: "汇总结论", description: "输出健康摘要、证据缺口与建议", tools: [], final: true },
    ],
  },
  knowledge_qa: {
    title: "知识问答过程",
    subtitle: "先检索项目档案，再结合当前代码与同步数据回答。",
    steps: [
      { key: "summary", title: "确认仓库背景", description: "读取仓库概要，确定回答范围", tools: ["get_repo_summary"] },
      { key: "knowledge", title: "检索项目知识", description: "混合检索文档、Issue 与项目经验", tools: ["search_knowledge"] },
      { key: "code", title: "核对当前代码", description: "按固定提交读取或搜索代码", tools: ["read_file", "search_code"] },
      { key: "summarize", title: "汇总结论", description: "输出带引用的回答与局限性", tools: [], final: true },
    ],
  },
  issue_triage: {
    title: "Issue 分诊过程",
    subtitle: "读取问题与仓库背景，给出分类、优先级和行动项。",
    steps: [
      { key: "issue", title: "读取 Issue", description: "读取标题、正文、标签与状态", tools: ["get_issue", "list_issues"] },
      { key: "context", title: "读取仓库背景", description: "读取仓库概要和相关 PR/CI", tools: ["get_repo_summary"] },
      { key: "knowledge", title: "检索历史上下文", description: "检索相关文档与项目经验", tools: ["search_knowledge"] },
      { key: "summarize", title: "汇总结论", description: "输出分类、优先级、负责人候选与缺失信息", tools: [], final: true },
    ],
  },
  ci_debug: {
    title: "CI 排障过程",
    subtitle: "定位失败运行、提取错误块，再给出根因假设与验证步骤。",
    steps: [
      { key: "locate", title: "定位 CI 运行", description: "读取目标 head 匹配的 CI 运行与失败任务", tools: ["get_ci_runs"] },
      { key: "code", title: "读取代码线索", description: "读取相关 PR、代码与测试", tools: ["get_pr", "read_file", "search_code"] },
      { key: "context", title: "检索历史上下文", description: "检索文档、Issue 与项目经验", tools: ["search_knowledge"] },
      { key: "summarize", title: "汇总结论", description: "输出根因假设、错误块与修复方向", tools: [], final: true },
    ],
  },
  release_check: {
    title: "多 Agent 发布检查",
    subtitle: "Planner 拆解任务，专用 Agent 分工分析，Observer 检查证据，Synthesis 汇总。",
    steps: [
      { key: "planner", title: "Planner 规划", description: "固定 PR 提交，拆解 Review、CI 等任务", tools: [] },
      { key: "agents", title: "专用 Agent 分析", description: "PR、CI、Issue 等 Agent 按依赖执行", tools: [] },
      { key: "observer", title: "Observer 检查", description: "检查证据缺口、结论冲突与安全条件", tools: [] },
      { key: "synthesis", title: "Synthesis 汇总", description: "汇总发布建议、待确认项与下一步", tools: [], final: true },
    ],
  },
  weekly_report: {
    title: "活动报告过程",
    subtitle: "按 UTC 时间窗口统计已同步活动，生成报告并可回写知识库。",
    steps: [
      { key: "collect", title: "统计活动", description: "按事件发生时间汇总已同步记录", tools: [] },
      { key: "render", title: "生成报告", description: "输出摘要、计数与证据引用", tools: [] },
      { key: "save", title: "保存结果", description: "保存报告，等待人工批准后加入知识库", tools: [], final: true },
    ],
  },
};

const TERMINAL = new Set(["completed", "failed", "cancelled", "timed_out"]);

export default function PlanPanel({
  kind, status, question, trace, toolCalls, tasks,
}: {
  kind: string;
  status: string;
  question?: string | null;
  trace: PlanTrace[];
  toolCalls: PlanToolCall[];
  tasks?: PlanTask[] | null;
}) {
  const plan = PLANS[kind] ?? PLANS.repo_health;
  const terminal = TERMINAL.has(status);
  const seen = new Set(trace.map((item) => item.tool));
  const evidence = new Set<string>();
  for (const call of toolCalls) for (const id of call.evidence_ids || []) evidence.add(id);

  let steps: Step[];
  if (kind === "release_check" && tasks && tasks.length > 0) {
    steps = tasks.map((task) => ({
      key: task.key,
      title: `${task.agent} · ${task.key}`,
      description: task.result?.summary || "等待该 Agent 返回结论",
      tools: [],
    }));
  } else {
    steps = plan.steps;
  }

  const doneMap = steps.map((step) => {
    if (kind === "release_check" && tasks && tasks.length > 0) {
      const task = tasks.find((item) => item.key === step.key);
      return task?.state === "completed";
    }
    if (step.final) return terminal;
    return step.tools.some((tool) => seen.has(tool));
  });
  let currentIndex = doneMap.findIndex((done, index) => !done && !(steps[index]?.final));
  if (currentIndex < 0 && !terminal) currentIndex = steps.length - 1;
  const doneCount = doneMap.filter(Boolean).length;

  return <aside className="plan-panel" aria-label="执行计划">
    <header>
      <p className="plan-eyebrow">PLAN &amp; EXECUTE</p>
      <h2>{plan.title}</h2>
      <p>{plan.subtitle}</p>
    </header>
    {question && <blockquote className="plan-question">{question}</blockquote>}
    <ol className="plan-steps">
      {steps.map((step, index) => {
        const done = doneMap[index];
        const running = !done && index === currentIndex && !terminal;
        return <li key={step.key} className={done ? "done" : running ? "running" : "pending"}>
          <span className="plan-index" aria-hidden="true">{done ? "✓" : index + 1}</span>
          <div>
            <strong>
              {running ? `执行 ${index + 1}/${steps.length}：` : ""}{step.title}
            </strong>
            <p>{step.description}</p>
          </div>
        </li>;
      })}
    </ol>
    <footer>
      <span>{doneCount}/{steps.length} 步完成 · 证据 {evidence.size} 条</span>
      <span className={terminal ? "plan-terminal" : "plan-live"}>{terminal ? "已结束" : "执行中"}</span>
    </footer>
  </aside>;
}
