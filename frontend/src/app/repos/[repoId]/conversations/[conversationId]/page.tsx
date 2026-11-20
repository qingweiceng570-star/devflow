"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api/client";
import WorkspaceShell from "@/components/workspace-shell";
import EvidenceChip from "@/components/evidence-chip";
import {TargetPicker} from "@/components/target-picker";
import { useBackgroundRefresh } from "@/components/use-background-refresh";
import KnowledgeReadiness from "@/components/knowledge-readiness";
import PlanPanel from "@/components/plan-panel";
import type { PlanToolCall } from "@/components/plan-panel";

type User = { username: string; role: string };
type MessageView = { id: string; seq: number; role: string; content: string; run_id: string | null; created_at: string };
type RunView = {
  id: string; status: string; kind: string; target: Record<string, unknown>; stop_reason: string | null;
  cancel_requested_at?: string | null;
  error: { code: string; message: string } | null; created_at: string;
  result: AnalysisResult | null; partial_result: Record<string, unknown> | null;
};
type Observer = {
  agents?: { key: string; agent: string | null; state: string; summary?: string | null; verdict?: string | null; coverage?: string; freshness?: string; evidence_count: number; confidence: number; warnings: string[] }[];
  warnings?: string[];
  pending?: string[];
  failed_required?: string[];
  policy_configured?: boolean;
  followup_rounds?: number;
};
type AnalysisResult = {
  details?: { log_diagnostics?: {job_id:number;code:string;message:string;remedy:string}[]; step_failures?: {job_id:number;name:string;conclusion:string}[]; tasks?: {key: string; agent: string; state: string; child_run_id: string; result?: AnalysisResult}[]; counts?: Record<string, number>; event_count?: number; evidence_id?: string; head_sha?: string; category?: string; priority?: string; priority_reason?: string; complexity?: string;
    missing_fields?: string[]; reproduction?: string[]; verification_steps?: string[]; fix_suggestions?: string[];
    cause_category?: string; run?: number; attempt?: number; status?: string; conclusion?: string;
    owner_candidates?: {value: string; reason: string; evidence_ids: string[]}[];
    duplicate_candidates?: {value: string; reason: string; evidence_ids: string[]}[];
    error_blocks?: {job_id: number; job_name: string; line_start: number; line_end: number; text: string; evidence_id: string}[];
    observer?: Observer };
  summary: string; verdict: string; coverage: string; freshness: string;
  findings: { severity: string; claim: string; evidence_ids: string[]; location: string; suggestion: string }[];
  hypotheses: { claim: string; supporting_evidence_ids: string[]; counter_evidence_ids?: string[]; verification_steps?: string[] }[];
  missing_evidence: { item: string; reason: string; impact: string; next_action: string }[];
  next_actions: { description: string; owner_hint?: string | null }[];
  limitations: string[];
};
type RunEvent = { id: number; type: string; payload: Record<string, unknown> };
type ToolTrace = { tool: string; status: string; error_code?: string | null };

const EVENT_TYPES = [
  "workflow.checkpoint", "run.queued", "run.started", "run.recovering", "tool.started", "tool.finished",
  "evidence.added", "answer.delta", "run.completed", "run.failed", "run.cancelled", "run.timed_out",
];
const TERMINAL = new Set(["run.completed", "run.failed", "run.cancelled", "run.timed_out"]);
const activeStatus = (status: string | undefined) => status === "queued" || status === "running" || status === "recovering";
const verdictNames: Record<string, string> = {
  recommend_release: "建议发布", recommend_merge: "建议合入", hold: "建议暂缓", insufficient_evidence: "证据不足",
};
const severityNames: Record<string, string> = {
  critical: "严重", high: "高", medium: "中", low: "低", info: "提示",
};

export default function ConversationPage() {
  const router = useRouter();
  const params = useParams<{ repoId: string; conversationId: string }>();
  const { repoId, conversationId } = params;
  const [user, setUser] = useState<User | null>(null);
  const [repoName, setRepoName] = useState("");
  const [title, setTitle] = useState("会话");
  const [messages, setMessages] = useState<MessageView[]>([]);
  const [run, setRun] = useState<RunView | null>(null);
  const [workflowTasks,setWorkflowTasks]=useState<NonNullable<AnalysisResult["details"]>["tasks"]>([]);
  const [streamState,setStreamState]=useState("connecting");
  const [trace, setTrace] = useState<ToolTrace[]>([]);
  const [toolCalls, setToolCalls] = useState<PlanToolCall[]>([]);
  const [prNumber, setPrNumber] = useState("");
  const [kind, setKind] = useState("pr_review");
  const [attempt, setAttempt] = useState("1");
  const [jobId, setJobId] = useState("");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const sourceRef = useRef<EventSource | null>(null);
  const lastSeqRef = useRef(0);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const detail = await api<{ conversation: { title: string }; repo: { full_name: string }; active_run: RunView | null }>(
      `/conversations/${conversationId}`, undefined, undefined, signal,
    );
    const page = await api<{ items: MessageView[] }>(`/conversations/${conversationId}/messages?limit=200`, undefined, undefined, signal);
    const lastRunId = [...page.items].reverse().find((message) => message.run_id)?.run_id;
    const current = detail.active_run || (lastRunId ? await api<RunView>(`/runs/${lastRunId}`, undefined, undefined, signal) : null);
    const tasks = current?.kind === "release_check"
      ? await api<NonNullable<AnalysisResult["details"]>["tasks"]>(`/repos/${repoId}/runs/${current.id}/tasks`, undefined, undefined, signal) : [];
    const toolRows = current
      ? await api<PlanToolCall[]>(`/runs/${current.id}/tools`, undefined, undefined, signal) : [];
    signal?.throwIfAborted();
    setTitle(detail.conversation.title);
    setRepoName(detail.repo.full_name);
    setMessages(page.items);
    setRun(current);
    setWorkflowTasks(tasks);
    setToolCalls((previous) => current && activeStatus(current.status) && previous.length ? previous : toolRows);
    return detail.active_run?.id ?? null;
  }, [conversationId,repoId]);
  const background = useBackgroundRefresh(refresh);
  const refreshBackground = background.refreshNow;

  const subscribe = useCallback((runId: string) => {
    sourceRef.current?.close();
    lastSeqRef.current = 0;
    setTrace([]);
    setToolCalls([]);
    const source = new EventSource(`/api/v1/runs/${runId}/events`);
    sourceRef.current = source;
    setStreamState("connecting");
    source.onopen=()=>setStreamState("connected");
    source.onerror=()=>{if(sourceRef.current===source)setStreamState("reconnecting");};
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (raw) => {
        const data = JSON.parse((raw as MessageEvent).data) as { seq: number; type: string; payload: Record<string, unknown> };
        if (data.seq <= lastSeqRef.current) return;
        lastSeqRef.current = data.seq;
        if(type==="workflow.checkpoint"||type==="run.recovering") void refreshBackground();
        if (type === "tool.finished") {
          const evidence = Array.isArray(data.payload.evidence_ids)
            ? data.payload.evidence_ids.map(String) : [];
          setTrace((current) => [...current, { tool: String(data.payload.tool), status: String(data.payload.status), error_code: data.payload.error_code as string | null }]);
          setToolCalls((current) => [...current, {
            tool: String(data.payload.tool),
            status: String(data.payload.status),
            evidence_ids: evidence,
          }]);
        }
        if (TERMINAL.has(type)) {
          source.close();
          sourceRef.current = null;
          setStreamState("closed");
          void refreshBackground();
          setBusy(false);
        }
      });
    }
  }, [refreshBackground]);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const identity = await api<User>("/auth/me");
        if (!active) return;
        setUser(identity);
        const activeRunId = await refresh();
        if (active && activeRunId) subscribe(activeRunId);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) router.replace("/login");
        else if (active) setError(err instanceof Error ? err.message : "加载失败");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
      sourceRef.current?.close();
      sourceRef.current = null;
    };
  }, [refresh, router, subscribe]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, trace.length]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!draft.trim()) return;
    let target: Record<string, number> | null = null;
    if (!["knowledge_qa", "repo_health"].includes(kind)) {
      const prValue = Number(prNumber);
      if (!Number.isInteger(prValue) || prValue < 1) {
        setError("请输入有效的目标编号");
        return;
      }
      target = { [kind === "pr_review" ? "pr_number" : kind === "issue_triage" ? "issue_number" : "ci_run_id"]: prValue };
      if (kind === "ci_debug") {
        if (!Number.isInteger(Number(attempt)) || Number(attempt) < 1 || (jobId && (!Number.isInteger(Number(jobId)) || Number(jobId) < 1))) {
          setError("请输入有效的 attempt 和可选 job ID"); return;
        }
        target.attempt = Number(attempt);
        if (jobId) target.job_id = Number(jobId);
      }
    }
    setBusy(true); setError("");
    try {
      const created = await api<RunView>(`/conversations/${conversationId}/runs`, {
        message: draft.trim(), kind, ...(target ? { target } : {}),
      });
      setDraft("");
      setRun(created);
      await refresh();
      subscribe(created.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败");
      setBusy(false);
    }
  }

  async function cancel() {
    if (!run) return;
    try {
      setRun(await api<RunView>(`/runs/${run.id}/cancel`, {}, "POST"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "取消失败");
    }
  }

  async function logout() {
    try { await api("/auth/logout", {}, "POST"); router.replace("/login"); }
    catch (err) { setError(err instanceof Error ? err.message : "退出失败"); }
  }

  if (loading) return <main className="loading" aria-live="polite">正在打开工作空间…</main>;
  const running = activeStatus(run?.status);
  const runQuestion = run
    ? ([...messages].reverse().find((message) => message.role === "user" && message.run_id === run.id)?.content
      ?? [...messages].reverse().find((message) => message.role === "user")?.content
      ?? null)
    : null;
  return <WorkspaceShell
    user={user}
    active="repos"
    knowledgeHref={`/repos/${repoId}/knowledge`}
    note={{ stage: "M3", title: "带证据的结论", text: "PR 审查与知识问答共用同一套取证链路；引用可追溯到本次运行收集的证据。" }}
    onLogout={logout}
  >
    <header>
      <div>
        <p className="eyebrow">CONVERSATION</p>
        <h1>{title}</h1>
        <p className="muted"><a className="link-button" href={`/repos/${repoId}`}>← 返回 {repoName}</a></p>
      </div>
      {running && <button className="button secondary small" disabled={!!run?.cancel_requested_at} onClick={cancel}>{run?.cancel_requested_at?"正在取消…":"取消当前分析"}</button>}
    </header>
    {error && <div role="alert" className="error-box">{error} <button className="link-button" onClick={() => setError("")}>关闭</button></div>}
    {background.error && <div role="alert" className="error-box">{background.error} <button className="link-button" disabled={background.refreshing} onClick={() => void refreshBackground()}>重试加载</button></div>}
    {background.retrying && <p role="status">暂时无法更新结果，正在重试…</p>}
    <div className="run-layout">
      <PlanPanel
        kind={run?.kind ?? kind}
        status={run?.status ?? "queued"}
        question={runQuestion}
        trace={trace}
        toolCalls={toolCalls}
        tasks={workflowTasks ?? []}
      />
      <section className="panel chat-panel">
      <div className="chat-messages">
        {messages.length === 0 && <p className="empty">选择分析类型，填写目标和关注点，开始带证据的分析。</p>}
        {messages.map((message) => (
          <article key={message.id} className={message.role === "user" ? "msg-user" : "msg-assistant"}>
            <p className="msg-role">{message.role === "user" ? "你" : "DevFlow"}</p>
            <pre className="msg-content">{message.content}</pre>
          </article>
        ))}
        {running&&streamState==="reconnecting"&&<p role="status">连接中断，正在重连并补回进度；任务仍在后台执行。</p>}
        {run?.cancel_requested_at&&running&&<p role="status">已提交取消请求，等待当前步骤结束。</p>}
        {run && <RunCard run={run} trace={trace} liveTasks={workflowTasks} />}
        <div ref={bottomRef} />
      </div>
      {kind === "knowledge_qa" && <KnowledgeReadiness repoId={repoId} />}
      <form className="chat-form" onSubmit={submit}>
        <label htmlFor="chat-kind" className="chat-kind-label">类型
          <select id="chat-kind" value={kind} onChange={(event) => { setKind(event.target.value); setPrNumber(""); }} disabled={running}>
            <option value="pr_review">PR 审查</option>
            <option value="repo_health">仓库健康检查</option>
            <option value="knowledge_qa">知识问答</option>
            <option value="issue_triage">Issue 分诊</option>
            <option value="ci_debug">CI 排障</option>
          </select>
        </label>
        {!["knowledge_qa", "repo_health"].includes(kind) && (
          <TargetPicker repoId={repoId} kind={kind} value={prNumber} onChange={setPrNumber} onAttempt={setAttempt} attempt={attempt} disabled={running}/>
        )}
        {kind === "ci_debug" && <>
          <label className="chat-pr-label" htmlFor="ci-attempt">Attempt<input id="ci-attempt" value={attempt} onChange={e => setAttempt(e.target.value)} inputMode="numeric" disabled={running} /></label>
          <label className="chat-pr-label" htmlFor="ci-job">Job ID（可选）<input id="ci-job" value={jobId} onChange={e => setJobId(e.target.value)} inputMode="numeric" disabled={running} /></label>
        </>}
        <label htmlFor="chat-input" className="chat-input-label">关注点
          <textarea id="chat-input" value={draft} onChange={(event) => setDraft(event.target.value)}
            placeholder={kind === "pr_review" ? "例如：这个 PR 有哪些风险？" : kind === "issue_triage" ? "例如：影响范围、优先级和缺失信息是什么？" : kind === "ci_debug" ? "例如：失败原因有哪些假设，如何验证？" : "例如：写操作的权限要求是什么？"}
            rows={2} disabled={running} />
        </label>
        <button className="button primary" disabled={busy || running || !draft.trim()}>
          {running ? "分析进行中…" : busy ? "正在提交…" : kind === "pr_review" ? "提交分析" : "提交提问"}
        </button>
      </form>
      </section>
    </div>
  </WorkspaceShell>;
}

function RunCard({ run, trace, liveTasks }: { run: RunView; trace: ToolTrace[]; liveTasks: NonNullable<AnalysisResult["details"]>["tasks"] }) {
  const statusNames: Record<string, string> = {
    queued: "排队中", running: "分析中", recovering: "恢复中", completed: "已完成",
    failed: "失败", cancelled: "已取消", timed_out: "超时",
  };
  const result = run.result;
  return <article className="run-card">
    <div className="run-head">
      <span className={`tag ${run.status === "failed" ? "tag-warn" : ""}`}>{statusNames[run.status] || run.status}</span>
      {run.stop_reason && <span className="tag">停止原因：{run.stop_reason}</span>}
      {result && result.verdict !== "not_applicable" && (
        <span className={`verdict verdict-${result.verdict}`}>{verdictNames[result.verdict] || result.verdict}</span>
      )}
    </div>
    {activeStatus(run.status) && (
      <div className="progress" role="status">
        <span className="spinner" />
        {trace.length === 0 ? "正在采集目标与证据…" : `已执行 ${trace.length} 次工具调用…`}
      </div>
    )}
    {trace.length > 0 && <ol className="trace">{trace.map((item, index) => (
      <li key={index}><span>工具</span><code>{item.tool}</code>
        <span className={item.status === "ok" ? "muted" : "tag-warn"}>{item.status}{item.error_code ? ` · ${item.error_code}` : ""}</span>
      </li>
    ))}</ol>}
    {run.error && <div className="error-box">{run.error.code}：{run.error.message}</div>}
    {!result && run.partial_result && !run.error && (
      <p className="hint">{String((run.partial_result as { note?: string }).note || "分析未完成，仅保留部分轨迹。")}</p>
    )}
    {!result&&!!liveTasks?.length&&<SpecialistDetails details={{tasks:liveTasks}}/>}
    {result && <div className="run-result">
      <p className="result-summary">{result.summary}</p>
      <p className="form-note">覆盖度：{result.coverage === "complete" ? "完整" : "部分"} · 新鲜度：{
        result.freshness === "fresh" ? "当前版本" : result.freshness === "stale" ? "版本已变化" : "未知"}</p>
      {result.details && <SpecialistDetails details={result.details} />}
      {result.findings.length > 0 && <section className="result-block">
        <h3>风险发现</h3>
        <ul className="finding-list">{result.findings.map((finding, index) => (
          <li key={index} className={`finding severity-${finding.severity}`}>
            <span className="severity-badge">{severityNames[finding.severity] || finding.severity}</span>
            <div>
              <p>{finding.claim}</p>
              <p className="muted"><code>{finding.location}</code> · {finding.suggestion}</p>
              <p>{finding.evidence_ids.map((id) => <EvidenceChip key={id} id={id} />)}</p>
            </div>
          </li>
        ))}</ul>
      </section>}
      {result.hypotheses.length > 0 && <section className="result-block">
        <h3>待验证的推测</h3>
        <ul className="plain-list">{result.hypotheses.map((item, index) => <li key={index}>{item.claim}
          <p>支持：{item.supporting_evidence_ids.map(id => <EvidenceChip key={id} id={id} />)}</p>
          {!!item.counter_evidence_ids?.length && <p>反对：{item.counter_evidence_ids.map(id => <EvidenceChip key={id} id={id} />)}</p>}
          {item.verification_steps?.map((step, i) => <p key={i}>验证建议：{step}</p>)}
        </li>)}</ul>
      </section>}
      {result.missing_evidence.length > 0 && <section className="result-block">
        <h3>证据缺口</h3>
        <ul className="plain-list">{result.missing_evidence.map((item, index) => (
          <li key={index}><b>{item.item}</b>：{item.impact}（{item.reason}；建议：{item.next_action}）</li>
        ))}</ul>
      </section>}
      {result.next_actions.length > 0 && <section className="result-block">
        <h3>建议下一步</h3>
        <ul className="plain-list">{result.next_actions.map((item, index) => (
          <li key={index}>{item.description}{item.owner_hint ? `（${item.owner_hint}）` : ""}</li>
        ))}</ul>
      </section>}
      {result.limitations.length > 0 && <details className="result-block">
        <summary>局限性（{result.limitations.length}）</summary>
        <ul className="plain-list">{result.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>
      </details>}
    </div>}
  </article>;
}

function SpecialistDetails({ details }: { details: NonNullable<AnalysisResult["details"]> }) {
  const labels: Record<string, string> = {bug: "缺陷", feature: "功能", question: "问题", documentation: "文档", maintenance: "维护", unknown: "未知", code: "代码", test: "测试", dependency: "依赖", environment: "环境", resource: "资源"};
  return <section className="result-block">
    {details.log_diagnostics?.map(d=><p className="hint" key={d.job_id}>job {d.job_id}：{d.message} {d.remedy}</p>)}
    {!!details.step_failures?.length&&<><h3>已记录的失败步骤</h3>{details.step_failures.map((s,i)=><p key={i}>{s.name}：{s.conclusion}（job {s.job_id}）</p>)}</>}
    {details.head_sha && <p>固定提交：<code>{details.head_sha}</code></p>}
    {details.observer && <ObserverDetails observer={details.observer} />}
    {details.tasks && <><h3>协作任务</h3>{details.tasks.map(task => <details key={task.key}><summary>{task.key} · {task.agent} · {task.state}</summary><p>{task.result?.summary || "未形成完整结论"}</p><p>子运行：<code>{task.child_run_id}</code></p><p>{task.result?.limitations?.join("；")}</p></details>)}</>}
    {details.counts && <><h3>活动统计（{details.event_count} 条）</h3>{Object.entries(details.counts).map(([kind, count]) => <p key={kind}>{kind}：{count}</p>)}{details.evidence_id && <EvidenceChip id={details.evidence_id}/>}</>}
    {details.category && <><h3>Issue 分诊</h3><p>分类：{labels[details.category] || details.category} · 优先级：{details.priority} · 复杂度：{details.complexity}</p><p>{details.priority_reason}</p></>}
    {details.run && <><h3>CI 诊断</h3><p>Run {details.run} · Attempt {details.attempt} · {details.status} / {details.conclusion || "尚无结论"}</p><p>候选根因：{labels[details.cause_category || "unknown"]}</p></>}
    {([["复现信息", details.reproduction], ["需要补充", details.missing_fields], ["验证步骤（仅建议）", details.verification_steps], ["修复方向", details.fix_suggestions]] as [string, string[] | undefined][]).map(([label, items]) => !!items?.length && <div key={label}><h4>{label}</h4><ul className="plain-list">{items.map((text, i) => <li key={i}>{text}</li>)}</ul></div>)}
    {([["负责人候选", details.owner_candidates], ["可能重复的 Issue", details.duplicate_candidates]] as [string, {value: string; reason: string; evidence_ids: string[]}[] | undefined][]).map(([label, items]) => !!items?.length && <div key={label}><h4>{label}</h4>{items.map((item, i) => <p key={i}>{item.value}：{item.reason} {item.evidence_ids.map(id => <EvidenceChip key={id} id={id} />)}</p>)}</div>)}
    {details.error_blocks?.map((block, i) => <details key={i}><summary>{block.job_name || block.job_id} · 日志 {block.line_start}–{block.line_end} 行</summary><pre className="msg-content">{block.text}</pre><EvidenceChip id={block.evidence_id} /></details>)}
  </section>;
}

function ObserverDetails({ observer }: { observer: Observer }) {
  const warnings = observer.warnings ?? [];
  const agents = observer.agents ?? [];
  const pending = observer.pending ?? [];
  return <section className="observer-block">
    <h3>Observer 检查</h3>
    {warnings.length === 0
      ? <p className="muted">未发现证据缺口或结论冲突。</p>
      : <ul className="observer-warnings">
        {warnings.map((warning) => <li key={warning}>WARNING：{warning}</li>)}
      </ul>}
    {agents.length > 0 && <>
      <h3>决策依据与待确认项</h3>
      <ul className="agent-list">
        {agents.map((agent) => (
          <li key={agent.key}>
            <div className="agent-head">
              <strong>{agent.key}</strong>
              {agent.agent && <span className="muted">{agent.agent}</span>}
              <span className={`confidence-badge ${agent.confidence < 0.5 ? "low" : ""}`}>置信度 {agent.confidence.toFixed(2)}</span>
              <span className="muted">证据 {agent.evidence_count} 条</span>
            </div>
            <p>{agent.summary || (agent.state === "completed" ? "未形成完整结论" : `任务${agent.state}`)}</p>
            {agent.warnings.length > 0 && <p className="muted">待确认：{agent.warnings.join("；")}</p>}
          </li>
        ))}
      </ul>
      {pending.length > 0 && <>
        <h4>待确认</h4>
        <ul className="plain-list">{pending.map((item) => <li key={item}>{item}</li>)}</ul>
      </>}
    </>}
  </section>;
}
