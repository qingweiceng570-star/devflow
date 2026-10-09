"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api/client";
import WorkspaceShell from "@/components/workspace-shell";
import EvidenceChip from "@/components/evidence-chip";

type User = { username: string; role: string };
type MessageView = { id: string; seq: number; role: string; content: string; run_id: string | null; created_at: string };
type RunView = {
  id: string; status: string; kind: string; target: Record<string, unknown>; stop_reason: string | null;
  error: { code: string; message: string } | null; created_at: string;
  result: AnalysisResult | null; partial_result: Record<string, unknown> | null;
};
type AnalysisResult = {
  summary: string; verdict: string; coverage: string; freshness: string;
  findings: { severity: string; claim: string; evidence_ids: string[]; location: string; suggestion: string }[];
  hypotheses: { claim: string; supporting_evidence_ids: string[] }[];
  missing_evidence: { item: string; reason: string; impact: string; next_action: string }[];
  next_actions: { description: string; owner_hint?: string | null }[];
  limitations: string[];
};
type RunEvent = { id: number; type: string; payload: Record<string, unknown> };
type ToolTrace = { tool: string; status: string; error_code?: string | null };

const EVENT_TYPES = [
  "run.queued", "run.started", "run.recovering", "tool.started", "tool.finished",
  "evidence.added", "answer.delta", "run.completed", "run.failed", "run.cancelled", "run.timed_out",
];
const TERMINAL = new Set(["run.completed", "run.failed", "run.cancelled", "run.timed_out"]);
const activeStatus = (status: string | undefined) => status === "queued" || status === "running" || status === "recovering";
const verdictNames: Record<string, string> = {
  recommend_merge: "建议合入", hold: "建议暂缓", insufficient_evidence: "证据不足",
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
  const [trace, setTrace] = useState<ToolTrace[]>([]);
  const [prNumber, setPrNumber] = useState("");
  const [kind, setKind] = useState<"pr_review" | "knowledge_qa">("pr_review");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const sourceRef = useRef<EventSource | null>(null);
  const lastSeqRef = useRef(0);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async () => {
    const detail = await api<{ conversation: { title: string }; repo: { full_name: string }; active_run: RunView | null }>(
      `/conversations/${conversationId}`,
    );
    setTitle(detail.conversation.title);
    setRepoName(detail.repo.full_name);
    const page = await api<{ items: MessageView[] }>(`/conversations/${conversationId}/messages?limit=200`);
    setMessages(page.items);
    const lastRunId = [...page.items].reverse().find((message) => message.run_id)?.run_id;
    if (detail.active_run) {
      setRun(detail.active_run);
    } else if (lastRunId) {
      setRun(await api<RunView>(`/runs/${lastRunId}`));
    }
    return detail.active_run?.id ?? null;
  }, [conversationId]);

  const subscribe = useCallback((runId: string) => {
    sourceRef.current?.close();
    lastSeqRef.current = 0;
    setTrace([]);
    const source = new EventSource(`/api/v1/runs/${runId}/events`);
    sourceRef.current = source;
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (raw) => {
        const data = JSON.parse((raw as MessageEvent).data) as { seq: number; type: string; payload: Record<string, unknown> };
        if (data.seq <= lastSeqRef.current) return;
        lastSeqRef.current = data.seq;
        if (type === "tool.finished") {
          setTrace((current) => [...current, { tool: String(data.payload.tool), status: String(data.payload.status), error_code: data.payload.error_code as string | null }]);
        }
        if (TERMINAL.has(type)) {
          source.close();
          sourceRef.current = null;
          refresh().catch(() => undefined);
          setBusy(false);
        }
      });
    }
  }, [refresh]);

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
    let target: { pr_number: number } | null = null;
    if (kind === "pr_review") {
      const prValue = Number(prNumber);
      if (!Number.isInteger(prValue) || prValue < 1) {
        setError("请输入有效的 PR 编号");
        return;
      }
      target = { pr_number: prValue };
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
      {running && <button className="button secondary small" onClick={cancel}>取消当前分析</button>}
    </header>
    {error && <div role="alert" className="error-box">{error} <button className="link-button" onClick={() => setError("")}>关闭</button></div>}
    <section className="panel chat-panel">
      <div className="chat-messages">
        {messages.length === 0 && <p className="empty">输入 PR 编号并描述关注点，开始第一轮审查。</p>}
        {messages.map((message) => (
          <article key={message.id} className={message.role === "user" ? "msg-user" : "msg-assistant"}>
            <p className="msg-role">{message.role === "user" ? "你" : "DevFlow"}</p>
            <pre className="msg-content">{message.content}</pre>
          </article>
        ))}
        {run && <RunCard run={run} trace={trace} />}
        <div ref={bottomRef} />
      </div>
      <form className="chat-form" onSubmit={submit}>
        <label htmlFor="chat-kind" className="chat-kind-label">类型
          <select id="chat-kind" value={kind} onChange={(event) => setKind(event.target.value as "pr_review" | "knowledge_qa")} disabled={running}>
            <option value="pr_review">PR 审查</option>
            <option value="knowledge_qa">知识问答</option>
          </select>
        </label>
        {kind === "pr_review" && (
          <label htmlFor="pr-number" className="chat-pr-label">PR 编号
            <input id="pr-number" value={prNumber} onChange={(event) => setPrNumber(event.target.value)}
              placeholder="例如 22" inputMode="numeric" disabled={running} />
          </label>
        )}
        <label htmlFor="chat-input" className="chat-input-label">关注点
          <textarea id="chat-input" value={draft} onChange={(event) => setDraft(event.target.value)}
            placeholder={kind === "pr_review" ? "例如：这个 PR 有哪些风险？可以合入吗？" : "例如：写操作的权限要求是什么？"}
            rows={2} disabled={running} />
        </label>
        <button className="button primary" disabled={busy || running || !draft.trim()}>
          {running ? "分析进行中…" : busy ? "正在提交…" : kind === "pr_review" ? "提交分析" : "提交提问"}
        </button>
      </form>
    </section>
  </WorkspaceShell>;
}

function RunCard({ run, trace }: { run: RunView; trace: ToolTrace[] }) {
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
        {trace.length === 0 ? "正在读取 PR 并准备差异范围…" : `已执行 ${trace.length} 次工具调用…`}
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
    {result && <div className="run-result">
      <p className="result-summary">{result.summary}</p>
      <p className="form-note">覆盖度：{result.coverage === "complete" ? "完整" : "部分"} · 新鲜度：{
        result.freshness === "fresh" ? "当前版本" : result.freshness === "stale" ? "版本已变化" : "未知"}</p>
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
        <ul className="plain-list">{result.hypotheses.map((item, index) => <li key={index}>{item.claim}</li>)}</ul>
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
