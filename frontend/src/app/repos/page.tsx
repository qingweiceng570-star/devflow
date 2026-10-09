"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api/client";
import WorkspaceShell from "@/components/workspace-shell";

type User = { username: string; role: string };
type Capabilities = { worker_online: boolean };
type Repo = {
  id: string; full_name: string; default_branch: string | null; state: string; version: number;
  created_at: string; updated_at: string; created?: boolean;
};
type ResourceCounts = {
  pages?: number; fetched?: number; created?: number; updated?: number;
  skipped?: number | string; capped?: boolean; coverage?: string; error?: string;
};
type SyncRun = {
  id: string; status: string; resources: string[];
  counts: Record<string, ResourceCounts | string>; errors: { resource?: string; code: string; message: string }[];
  started_at: string | null; finished_at: string | null; created_at: string;
};
type RepoDetail = { repo: Repo; latest_sync: SyncRun | null };

const resourceNames: Record<string, string> = { issues: "Issue", pulls: "PR", reviews: "Review", ci: "CI", events: "事件" };
const statusNames: Record<string, string> = { queued: "排队中", running: "同步中", succeeded: "已完成", failed: "失败" };

export default function ReposPage() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [repos, setRepos] = useState<Repo[]>([]);
  const [details, setDetails] = useState<Record<string, SyncRun | null>>({});
  const [fullName, setFullName] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const pollTimers = useRef<Record<string, ReturnType<typeof setInterval>>>({});

  const refresh = useCallback(async () => {
    const list = await api<{ items: Repo[] }>("/repos");
    setRepos(list.items);
    const pairs = await Promise.all(
      list.items.map(async (repo) => {
        const detail = await api<RepoDetail>(`/repos/${repo.id}`);
        return [repo.id, detail.latest_sync] as const;
      }),
    );
    setDetails(Object.fromEntries(pairs));
  }, []);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const identity = await api<User>("/auth/me");
        if (!active) return;
        setUser(identity);
        const capabilities = await api<Capabilities>("/settings/capabilities");
        if (active) setCaps(capabilities);
        await refresh();
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) router.replace("/login");
        else if (active) setError(err instanceof Error ? err.message : "加载失败");
      } finally {
        if (active) setLoading(false);
      }
    })();
    const timers = pollTimers.current;
    return () => {
      active = false;
      Object.values(timers).forEach((timer) => clearInterval(timer));
    };
  }, [refresh, router]);

  function watchRun(repoId: string) {
    if (pollTimers.current[repoId]) clearInterval(pollTimers.current[repoId]);
    pollTimers.current[repoId] = setInterval(async () => {
      try {
        const detail = await api<RepoDetail>(`/repos/${repoId}`);
        setDetails((current) => ({ ...current, [repoId]: detail.latest_sync }));
        if (!detail.latest_sync || !["queued", "running"].includes(detail.latest_sync.status)) {
          clearInterval(pollTimers.current[repoId]);
          delete pollTimers.current[repoId];
        }
      } catch {
        clearInterval(pollTimers.current[repoId]);
        delete pollTimers.current[repoId];
      }
    }, 2500);
  }

  async function addRepo(event: React.FormEvent) {
    event.preventDefault();
    if (!fullName.trim()) return;
    setBusy("add"); setError("");
    try {
      await api<Repo>("/repos", { full_name: fullName.trim() });
      setFullName("");
      await refresh();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) router.replace("/login");
      else setError(err instanceof Error ? err.message : "添加失败");
    } finally {
      setBusy("");
    }
  }

  async function sync(repo: Repo) {
    setBusy(`sync-${repo.id}`); setError("");
    try {
      const run = await api<SyncRun>(`/repos/${repo.id}/sync`, {});
      setDetails((current) => ({ ...current, [repo.id]: run }));
      watchRun(repo.id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) router.replace("/login");
      else setError(err instanceof Error ? err.message : "同步请求失败");
    } finally {
      setBusy("");
    }
  }

  async function toggleState(repo: Repo) {
    setBusy(`state-${repo.id}`); setError("");
    try {
      const updated = await api<Repo>(
        `/repos/${repo.id}`,
        { expected_version: repo.version, state: repo.state === "active" ? "disabled" : "active" },
        "PATCH",
      );
      setRepos((current) => current.map((item) => (item.id === repo.id ? updated : item)));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) router.replace("/login");
      else setError(err instanceof Error ? err.message : "更新失败");
    } finally {
      setBusy("");
    }
  }

  async function logout() {
    try { await api("/auth/logout", {}, "POST"); router.replace("/login"); }
    catch (err) { setError(err instanceof Error ? err.message : "退出失败"); }
  }

  if (loading) return <main className="loading" aria-live="polite">正在打开工作空间…</main>;
  const syncing = Object.values(details).filter((run) => run && ["queued", "running"].includes(run.status)).length;
  return <WorkspaceShell
    user={user}
    active="repos"
    note={{ stage: "M1", title: "只读同步与证据", text: "按资源增量同步，重复执行幂等；代码读取固定到具体 commit 并保留可核查证据。" }}
    onLogout={logout}
  >
    <header>
      <div>
        <p className="eyebrow">REPOSITORIES / M1</p>
        <h1>接入仓库，沉淀证据。</h1>
        <p className="muted">同步 Issue、PR、Review、CI 与活动事件；Worker 按队列执行，全部只读。</p>
      </div>
      <span className="pill">本地工作空间</span>
    </header>
    {error && <div role="alert" className="error-box">{error} <button className="link-button" onClick={() => setError("")}>关闭</button></div>}
    <section className="status-grid" aria-label="同步状态">
      <Status label="已接入仓库" value={String(repos.length)} detail="按 GitHub 仓库 ID 去重" good={repos.length > 0} />
      <Status label="同步中的任务" value={String(syncing)} detail="每仓库同资源串行执行" good={syncing === 0} />
      <Status label="任务进程" value={caps?.worker_online ? "在线" : "等待启动"} detail="租约队列 · 心跳检测" good={!!caps?.worker_online} />
    </section>
    <section className="panel">
      <div className="panel-heading"><div><span className="section-number">01</span><h2>添加仓库</h2></div><span className="tag">只读接入</span></div>
      <form className="repo-form" onSubmit={addRepo}>
        <label htmlFor="repo-name">仓库（owner/repo）
          <input id="repo-name" value={fullName} onChange={(event) => setFullName(event.target.value)} placeholder="例如 simonw/ttok" autoComplete="off" />
        </label>
        <button className="button primary" disabled={busy === "add" || !fullName.trim()}>
          {busy === "add" ? "正在验证…" : "接入仓库"}
        </button>
      </form>
      <p className="hint">公开仓库可以匿名只读；访问私有仓库需要在本地 .env 配置 GITHUB_READ_TOKEN 后重启后端。凭据不会进入浏览器或日志。</p>
    </section>
    <section className="panel">
      <div className="panel-heading"><div><span className="section-number">02</span><h2>仓库列表</h2></div><button className="link-button" onClick={() => refresh().catch((err) => setError(err instanceof Error ? err.message : "刷新失败"))}>刷新</button></div>
      {repos.length === 0 ? <p className="empty">还没有接入仓库。输入 owner/repo 开始第一次同步。</p> : <ul className="repo-list">
        {repos.map((repo) => {
          const run = details[repo.id];
          const active = run && ["queued", "running"].includes(run.status);
          return <li key={repo.id} className="repo-card">
            <div className="repo-head">
              <div>
                <strong>{repo.full_name}</strong>
                <small className="muted">默认分支 {repo.default_branch || "未知"} · {repo.state === "active" ? "已启用" : "已禁用"}</small>
              </div>
              <div className="repo-actions">
                <button className="button secondary small" disabled={!!busy || repo.state !== "active" || !!active} onClick={() => sync(repo)}>
                  {active ? "同步中…" : "同步"}
                </button>
                <button className="link-button" disabled={!!busy} onClick={() => toggleState(repo)}>
                  {repo.state === "active" ? "禁用" : "启用"}
                </button>
              </div>
            </div>
            {run ? <div className="sync-line">
              <span className={`tag ${run.status === "failed" ? "tag-warn" : ""}`}>{statusNames[run.status] || run.status}</span>
              <span className="muted">{run.started_at ? new Date(run.started_at).toLocaleString() : ""}</span>
              {typeof run.counts.coverage === "string" && <span className="muted">覆盖：{run.counts.coverage === "complete" ? "完整" : "部分"}</span>}
            </div> : <p className="muted sync-line">尚未同步过。点击「同步」拉取最近数据。</p>}
            {run && Object.entries(run.counts).filter(([key]) => key !== "coverage").length > 0 && <div className="count-chips">
              {Object.entries(run.counts).filter(([key]) => key !== "coverage").map(([key, value]) => {
                if (typeof value === "string") return <span key={key} className="chip">{resourceNames[key] || key} · {value}</span>;
                const parts = [`新增 ${value.created ?? 0}`, `更新 ${value.updated ?? 0}`];
                if (value.skipped !== undefined && typeof value.skipped === "number" && value.skipped > 0) parts.push(`跳过 ${value.skipped}`);
                if (value.capped) parts.push("达到页数上限");
                return <span key={key} className="chip">{resourceNames[key] || key} · {parts.join(" / ")}</span>;
              })}
            </div>}
            {run && run.errors.length > 0 && <div className="error-box">{[...new Set(run.errors.map((item) => `${item.code}：${item.message}`))].join("；")}</div>}
          </li>;
        })}
      </ul>}
    </section>
  </WorkspaceShell>;
}

function Status({ label, value, detail, good }: { label: string; value: string; detail: string; good: boolean }) {
  return <article className="status-card"><p>{label}</p><strong><span className={`status-dot ${good ? "" : "waiting"}`} />{value}</strong><small>{detail}</small></article>;
}
