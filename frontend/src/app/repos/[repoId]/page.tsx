"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api/client";
import WorkspaceShell from "@/components/workspace-shell";

type User = { username: string; role: string };
type Repo = { id: string; full_name: string; default_branch: string | null; state: string; version: number };
type SyncRun = { id: string; status: string; counts: Record<string, unknown>; errors: { code: string; message: string }[] };
type Conversation = { id: string; title: string; created_at: string; updated_at: string };
type RunItem = { id: string; status: string; kind: string; created_at: string; verdict?: string | null };

export default function RepoOverviewPage() {
  const router = useRouter();
  const params = useParams<{ repoId: string }>();
  const repoId = params.repoId;
  const [user, setUser] = useState<User | null>(null);
  const [repo, setRepo] = useState<Repo | null>(null);
  const [latestSync, setLatestSync] = useState<SyncRun | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    const detail = await api<{ repo: Repo; latest_sync: SyncRun | null }>(`/repos/${repoId}`);
    setRepo(detail.repo);
    setLatestSync(detail.latest_sync);
    const convs = await api<{ items: Conversation[] }>(`/repos/${repoId}/conversations`);
    setConversations(convs.items);
    const runList = await api<{ items: RunItem[] }>(`/repos/${repoId}/runs?limit=10`);
    setRuns(runList.items);
  }, [repoId]);

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

  async function logout() {
    try { await api("/auth/logout", {}, "POST"); router.replace("/login"); }
    catch (err) { setError(err instanceof Error ? err.message : "退出失败"); }
  }

  if (loading) return <main className="loading" aria-live="polite">正在打开工作空间…</main>;
  const coverage = latestSync && typeof latestSync.counts.coverage === "string" ? latestSync.counts.coverage : null;
  return <WorkspaceShell
    user={user}
    active="repos"
    note={{ stage: "M2", title: "PR 分析闭环", text: "在会话中提交 PR 编号，ChatAgent 依据已同步数据与固定版本代码给出带证据的审查结论。" }}
    onLogout={logout}
  >
    <header>
      <div>
        <p className="eyebrow">REPOSITORY</p>
        <h1>{repo?.full_name || "仓库"}</h1>
        <p className="muted">
          默认分支 {repo?.default_branch || "未知"} · {repo?.state === "active" ? "已启用" : "已禁用"}
          {coverage ? ` · 最近同步覆盖：${coverage === "complete" ? "完整" : "部分"}` : ""}
        </p>
      </div>
      <div className="button-row">
        <button className="button secondary small" disabled={busy === "sync" || repo?.state !== "active"} onClick={sync}>
          {busy === "sync" ? "同步中…" : "同步数据"}
        </button>
        <button className="button primary small" disabled={busy === "new"} onClick={startConversation}>
          新建分析会话
        </button>
      </div>
    </header>
    {error && <div role="alert" className="error-box">{error}</div>}
    <div className="bottom-grid">
      <section className="panel">
        <div className="panel-heading"><div><span className="section-number">01</span><h2>分析会话</h2></div><button className="link-button" onClick={() => refresh().catch((err) => setError(err instanceof Error ? err.message : "刷新失败"))}>刷新</button></div>
        {conversations.length === 0
          ? <p className="empty">还没有会话。点击「新建分析会话」开始一次 PR 审查。</p>
          : <ul className="history">
            {conversations.map((conversation) => (
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
      <section className="panel">
        <div className="panel-heading"><div><span className="section-number">02</span><h2>最近运行</h2></div></div>
        {runs.length === 0
          ? <p className="empty">暂无运行记录。</p>
          : <ul className="history">
            {runs.map((run) => (
              <li key={run.id}>
                <div>
                  <strong>{run.kind}</strong>
                  <small>{run.created_at.slice(0, 19).replace("T", " ")}</small>
                </div>
                <span className={`tag ${run.status === "failed" ? "tag-warn" : ""}`}>{run.status}</span>
              </li>
            ))}
          </ul>}
        {latestSync?.errors?.length ? (
          <div className="error-box">{latestSync.errors.map((item) => `${item.code}：${item.message}`).join("；")}</div>
        ) : null}
      </section>
    </div>
  </WorkspaceShell>;
}
