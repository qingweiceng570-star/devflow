"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api/client";
import WorkspaceShell from "@/components/workspace-shell";
import WorkspacePanel from "@/components/workspace-panel";
import type { WorkspacePayload } from "@/components/workspace-panel";
import { useBackgroundRefresh } from "@/components/use-background-refresh";

type User = { username: string; role: string };
type Repo = { id: string; full_name: string; default_branch: string | null; state: string; version: number };
type SyncRun = { id: string; status: string; counts: Record<string, unknown>; errors: { code: string; message: string }[] };
type Conversation = { id: string; title: string; created_at: string; updated_at: string };
type RunItem = { id: string; status: string; kind: string; created_at: string; verdict?: string | null };
type Workspace = WorkspacePayload & { repo: Repo; latest_sync: SyncRun | null };

const QUICK_PROMPTS = [
  "检查当前仓库的 CI 状态，失败的话下一步先看哪里？",
  "最近有没有需要优先处理的高风险 Issue 或 PR？",
  "总结最近工作流的健康状况和待确认项。",
  "如果这个版本要发布，还缺哪些证据？",
];

const KPI = [
  ["open_issues", "待处理 Issue", "issues"],
  ["open_pulls", "待 Review PR", "pulls"],
  ["closed_issues", "已处理 Issue", "issues"],
  ["rejected_issues", "已拒绝 Issue", "issues"],
  ["failed_ci", "失败 CI", "ci"],
  ["merged_pulls", "已合并 PR", "pulls"],
] as const;

export default function RepoWorkspacePage() {
  const router = useRouter();
  const params = useParams<{ repoId: string }>();
  const repoId = params.repoId;
  const [user, setUser] = useState<User | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [panelTab, setPanelTab] = useState<"issues" | "pulls" | "ci" | "team" | "memory" | "extensions">("issues");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    const [data, convs, runList] = await Promise.all([
      api<Workspace>(`/repos/${repoId}/workspace`),
      api<{ items: Conversation[] }>(`/repos/${repoId}/conversations`),
      api<{ items: RunItem[] }>(`/repos/${repoId}/runs?limit=8`),
    ]);
    setWorkspace(data);
    setConversations(convs.items);
    setRuns(runList.items);
  }, [repoId]);

  const refreshSync = useCallback(async (signal: AbortSignal) => {
    const detail = await api<{ repo: Repo; latest_sync: SyncRun | null }>(
      `/repos/${repoId}`, undefined, undefined, signal,
    );
    signal.throwIfAborted();
    setWorkspace((current) => current ? { ...current, repo: detail.repo, latest_sync: detail.latest_sync } : current);
  }, [repoId]);
  const syncRefresh = useBackgroundRefresh(refreshSync);
  const refreshSyncBackground = syncRefresh.refreshNow;

  const latestSync = workspace?.latest_sync ?? null;
  const syncing = latestSync?.status === "queued" || latestSync?.status === "running";

  useEffect(() => {
    if (!syncing || syncRefresh.error) return;
    const timer = setInterval(() => { void refreshSyncBackground(); }, 2500);
    return () => clearInterval(timer);
  }, [syncing, refreshSyncBackground, syncRefresh.error]);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const identity = await api<User>("/auth/me");
        if (!active) return;
        setUser(identity);
        await refresh();
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) router.replace("/login");
        else if (active) setError(err instanceof Error ? err.message : "加载失败");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, [refresh, router]);

  async function reload() {
    setBusy("refresh"); setError("");
    try { await refresh(); }
    catch (err) { setError(err instanceof Error ? err.message : "刷新失败"); }
    finally { setBusy(""); }
  }

  async function startConversation() {
    setBusy("new"); setError("");
    try {
      const conversation = await api<Conversation>("/conversations", { repo_id: repoId });
      router.push(`/repos/${repoId}/conversations/${conversation.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建会话失败");
      setBusy("");
    }
  }

  async function sync() {
    setBusy("sync"); setError("");
    try {
      await api(`/repos/${repoId}/sync`, {});
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "同步请求失败");
    } finally {
      setBusy("");
    }
  }

  async function ask(event: React.FormEvent) {
    event.preventDefault();
    const text = message.trim();
    if (!text || busy) return;
    setBusy("ask"); setError("");
    try {
      const conversation = await api<Conversation>("/conversations", { repo_id: repoId });
      await api(`/conversations/${conversation.id}/runs`, { message: text, kind: "repo_health" });
      router.push(`/repos/${repoId}/conversations/${conversation.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败");
      setBusy("");
    }
  }

  async function logout() {
    try { await api("/auth/logout", {}, "POST"); router.replace("/login"); }
    catch (err) { setError(err instanceof Error ? err.message : "退出失败"); }
  }

  if (loading) return <main className="loading" aria-live="polite">正在打开工作空间…</main>;
  const repo = workspace?.repo;
  const counts = workspace?.counts;
  const coverage = latestSync && typeof latestSync.counts.coverage === "string" ? latestSync.counts.coverage : null;

  return <WorkspaceShell
    user={user}
    active="repos"
    knowledgeHref={`/repos/${repoId}/knowledge`}
    note={{ stage: "M7", title: "研发协作工作空间", text: "同步研发数据、发起带证据的分析，并在审批后执行写操作。" }}
    onLogout={logout}
  >
    <header className="overview-head">
      <div>
        <p className="eyebrow">WORKSPACE / 总览</p>
        <h1>{repo?.full_name || "工作空间"}</h1>
        <p className="muted">
          默认分支 {repo?.default_branch || "未知"} · {repo?.state === "active" ? "已启用" : "已禁用"}
          {coverage ? ` · 最近同步覆盖：${coverage === "complete" ? "完整" : "部分"}` : ""}
        </p>
      </div>
      <div className="button-row">
        <span className="workspace-badge">本地工作空间</span>
        <a className="button secondary small" href={`/repos/${repoId}/collaboration`}>管理项目</a>
        <button className="button secondary small" disabled={busy === "sync" || syncing || repo?.state !== "active"} onClick={sync}>
          {syncing ? "同步中…" : busy === "sync" ? "正在请求…" : "同步数据"}
        </button>
        <button className="button primary small" disabled={busy === "new"} onClick={startConversation}>
          新建分析会话
        </button>
        <button className="button secondary small" disabled={busy === "refresh"} onClick={reload}>
          {busy === "refresh" ? "刷新中…" : "刷新"}
        </button>
      </div>
    </header>
    {error && <div role="alert" className="error-box">{error}</div>}
    {syncRefresh.error && <div role="alert" className="error-box">{syncRefresh.error} <button className="link-button" disabled={syncRefresh.refreshing} onClick={() => void refreshSyncBackground()}>重试加载</button></div>}
    {syncRefresh.retrying && <p role="status">暂时无法更新同步进度，正在重试…</p>}

    <div className="overview-layout">
      <div className="overview-main">
        <section className="kpi-grid" aria-label="项目指标">
          {KPI.map(([key, label, tab]) => {
            const value = counts ? counts[key] : null;
            const warn = (key === "failed_ci" && (value ?? 0) > 0);
            return <button key={key} className={`kpi-card ${warn ? "warn" : ""}`} onClick={() => setPanelTab(tab)}>
              <span>{label}</span>
              <strong>{value ?? "—"}</strong>
              <small>{key === "failed_ci" ? "已同步 CI 结论" : key.includes("pull") ? "当前同步范围" : "当前同步范围"}</small>
            </button>;
          })}
        </section>

        <section className="panel overview-chat">
          <div className="quick-prompts" aria-label="快捷提问">
            {QUICK_PROMPTS.map((prompt) => (
              <button key={prompt} className="prompt-chip" onClick={() => setMessage(prompt)}>{prompt}</button>
            ))}
          </div>
          <form className="composer" onSubmit={ask}>
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="输入消息，询问当前仓库；例如：检查当前 Issue、PR 和失败 CI，判断这个版本是否可以发布。"
              rows={2}
              aria-label="询问当前仓库"
            />
            <button className="button primary" disabled={busy === "ask" || !message.trim()}>
              {busy === "ask" ? "提交中…" : "发送"}
            </button>
          </form>
          <p className="form-note">自动选择仓库健康分析；需要 PR、Issue 或 CI 专项分析时，可以在新建会话中选择类型。</p>
        </section>

        <div className="bottom-grid" id="recent-runs">
          <section className="panel">
            <div className="panel-heading"><div><h2>最近运行</h2></div>
              <button className="link-button" onClick={reload} disabled={busy === "refresh"}>刷新</button>
            </div>
            {runs.length === 0
              ? <p className="empty">暂无运行记录。</p>
              : <ul className="history">
                {runs.map((run) => (
                  <li key={run.id}>
                    <div>
                      <strong>{{pr_review:"PR 审查",issue_triage:"Issue 分诊",ci_debug:"CI 排障",knowledge_qa:"知识问答",repo_health:"仓库健康",release_check:"发布检查",weekly_report:"活动报告"}[run.kind]||run.kind}</strong>
                      <small>{run.created_at.slice(0, 19).replace("T", " ")}</small>
                    </div>
                    <span className={`tag ${run.status === "failed" ? "tag-warn" : ""}`}>{{queued:"排队中",running:"分析中",recovering:"恢复中",completed:"已完成",failed:"失败",cancelled:"已取消",timed_out:"超时"}[run.status]||run.status}</span>
                  </li>
                ))}
              </ul>}
          </section>
          <section className="panel">
            <div className="panel-heading"><div><h2>分析会话</h2></div>
              <a className="link-button" href={`/repos/${repoId}/collaboration`}>审批与报告</a>
            </div>
            {conversations.length === 0
              ? <p className="empty">还没有会话。在上方提问，或新建一个专项分析会话。</p>
              : <ul className="history">
                {conversations.slice(0, 8).map((conversation) => (
                  <li key={conversation.id}>
                    <div>
                      <strong>{conversation.title}</strong>
                      <small>创建于 {conversation.created_at.slice(0, 10)}</small>
                    </div>
                    <a className="button secondary small" href={`/repos/${repoId}/conversations/${conversation.id}`}>打开</a>
                  </li>
                ))}
              </ul>}
          </section>
        </div>
      </div>
      <WorkspacePanel
        repoId={repoId}
        data={workspace}
        tab={panelTab}
        onTab={setPanelTab}
        refreshing={busy === "refresh"}
        onRefresh={reload}
      />
    </div>
  </WorkspaceShell>;
}
