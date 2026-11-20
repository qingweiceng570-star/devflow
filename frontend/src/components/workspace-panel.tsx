"use client";

import { useMemo, useState } from "react";

export type WorkspaceCounts = {
  open_issues: number; open_pulls: number; closed_issues: number;
  rejected_issues: number; failed_ci: number; merged_pulls: number;
};
export type WorkspaceIssue = {
  number: number; title: string; state: string; labels: string[];
  author: string | null; updated_at: string;
};
export type WorkspacePull = {
  number: number; title: string; state: string; merged: boolean; draft: boolean;
  author: string | null; updated_at: string;
};
export type WorkspaceCi = {
  run_id: number | null; attempt: number; name: string; status: string;
  conclusion: string | null; head_sha: string; created_at: string;
};
export type WorkspacePayload = {
  counts: WorkspaceCounts;
  issues: WorkspaceIssue[];
  pulls: WorkspacePull[];
  ci: WorkspaceCi[];
  team: { username: string; role: string }[];
  memories: { id: string; key: string | null; content: string | null; paths: string[] }[];
  knowledge: {
    document_count?: number;
    pending_changes?: number;
    building?: boolean;
    active_generation?: { chunk_count: number; embedded?: boolean } | null;
  };
  extensions: { id: string; name: string; kind: string; version: number; state: string }[];
};

const TABS = [
  ["issues", "Issue"], ["pulls", "PR"], ["ci", "CI"], ["team", "团队"],
  ["memory", "记忆&知识库"], ["extensions", "技能&MCP"],
] as const;
type Tab = (typeof TABS)[number][0];

const stateNames: Record<string, string> = { open: "open", closed: "closed", merged: "merged" };
const conclusionNames: Record<string, string> = {
  success: "成功", failure: "失败", cancelled: "取消", timed_out: "超时",
  skipped: "跳过", action_required: "需处理", neutral: "中性", stale: "过期",
};

export default function WorkspacePanel({
  repoId, data, refreshing, onRefresh, tab: controlledTab, onTab,
}: {
  repoId: string;
  data: WorkspacePayload | null;
  refreshing?: boolean;
  onRefresh: () => void;
  tab?: Tab;
  onTab?: (tab: Tab) => void;
}) {
  const [internalTab, setInternalTab] = useState<Tab>("issues");
  const [query, setQuery] = useState("");
  const tab = controlledTab ?? internalTab;
  const selectTab = (next: Tab) => onTab ? onTab(next) : setInternalTab(next);

  const issues = useMemo(() => {
    const rows = data?.issues ?? [];
    if (!query.trim()) return rows;
    const needle = query.trim().toLowerCase();
    return rows.filter((row) => row.title.toLowerCase().includes(needle) || String(row.number).includes(needle));
  }, [data, query]);
  const pulls = useMemo(() => {
    const rows = data?.pulls ?? [];
    if (!query.trim()) return rows;
    const needle = query.trim().toLowerCase();
    return rows.filter((row) => row.title.toLowerCase().includes(needle) || String(row.number).includes(needle));
  }, [data, query]);

  return <aside className="workspace-panel" aria-label="项目数据">
    <header>
      <div>
        <h2>Workspace</h2>
        <p>Issues {data?.counts.open_issues ?? "—"} · PRs {data?.counts.open_pulls ?? "—"}</p>
      </div>
      <button className="link-button" onClick={onRefresh} disabled={refreshing}>{refreshing ? "刷新中…" : "刷新"}</button>
    </header>
    <nav className="panel-tabs" aria-label="项目数据分类">
      {TABS.map(([key, label]) => (
        <button key={key} className={tab === key ? "active" : ""} onClick={() => selectTab(key)}>{label}</button>
      ))}
    </nav>
    {(tab === "issues" || tab === "pulls") && (
      <label className="panel-search">
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="关键词搜索标题、编号" />
      </label>
    )}
    <div className="panel-body">
      {!data && <p className="empty">正在读取项目数据…</p>}
      {data && tab === "issues" && (
        issues.length === 0 ? <p className="empty">还没有已同步的 Issue。</p> :
        <ul className="panel-list">{issues.map((issue) => (
          <li key={issue.number} className="panel-item">
            <div className="panel-item-head">
              <span className={`mini-state ${issue.state}`}>{stateNames[issue.state] || issue.state}</span>
              <strong>#{issue.number} {issue.title}</strong>
            </div>
            <p className="muted">{issue.labels.length ? issue.labels.join(" · ") : "无标签"}{issue.author ? ` · ${issue.author}` : ""}</p>
          </li>
        ))}</ul>
      )}
      {data && tab === "pulls" && (
        pulls.length === 0 ? <p className="empty">还没有已同步的 PR。</p> :
        <ul className="panel-list">{pulls.map((pull) => (
          <li key={pull.number} className="panel-item">
            <div className="panel-item-head">
              <span className={`mini-state ${pull.merged ? "merged" : pull.state}`}>{pull.merged ? "merged" : pull.draft ? "draft" : pull.state}</span>
              <strong>#{pull.number} {pull.title}</strong>
            </div>
            <p className="muted">{pull.author || "未知作者"}</p>
          </li>
        ))}</ul>
      )}
      {data && tab === "ci" && (
        data.ci.length === 0 ? <p className="empty">还没有已同步的 CI 运行。</p> :
        <ul className="panel-list">{data.ci.map((run, index) => (
          <li key={`${run.run_id}-${run.attempt}-${index}`} className="panel-item">
            <div className="panel-item-head">
              <span className={`mini-state ${run.conclusion || run.status}`}>{conclusionNames[run.conclusion || ""] || run.conclusion || run.status}</span>
              <strong>{run.name}</strong>
            </div>
            <p className="muted">run {run.run_id ?? "—"} · attempt {run.attempt}</p>
          </li>
        ))}</ul>
      )}
      {data && tab === "team" && (
        <ul className="panel-list">{data.team.map((member) => (
          <li key={member.username} className="panel-item">
            <div className="panel-item-head"><span className="avatar small">{member.username[0]?.toUpperCase()}</span><strong>{member.username}</strong></div>
            <p className="muted">{member.role}</p>
          </li>
        ))}</ul>
      )}
      {data && tab === "memory" && (
        <div className="panel-stack">
          <div className="panel-metric">
            <span>知识库文档</span><strong>{data.knowledge.document_count ?? 0}</strong>
            <small>{data.knowledge.active_generation ? `${data.knowledge.active_generation.chunk_count} 个可检索段落` : "尚未建立索引"}{data.knowledge.pending_changes ? ` · ${data.knowledge.pending_changes} 个待处理变更` : ""}</small>
          </div>
          {data.memories.length === 0
            ? <p className="empty">还没有已批准的项目经验。</p>
            : <ul className="panel-list">{data.memories.map((memory) => (
              <li key={memory.id} className="panel-item">
                <div className="panel-item-head"><strong>{memory.key || "经验"}</strong></div>
                <p>{memory.content}</p>
                {memory.paths.length > 0 && <p className="muted">适用：{memory.paths.join(", ")}</p>}
              </li>
            ))}</ul>}
          <a className="link-button" href={`/repos/${repoId}/knowledge`}>打开项目知识库 →</a>
        </div>
      )}
      {data && tab === "extensions" && (
        data.extensions.length === 0
          ? <p className="empty">还没有安装 Skill 或 MCP 扩展。</p>
          : <ul className="panel-list">{data.extensions.map((extension) => (
            <li key={extension.id} className="panel-item">
              <div className="panel-item-head">
                <span className="mini-state">{extension.kind}</span>
                <strong>{extension.name} v{extension.version}</strong>
              </div>
              <p className="muted">{extension.state}</p>
            </li>
          ))}</ul>
      )}
    </div>
  </aside>;
}
