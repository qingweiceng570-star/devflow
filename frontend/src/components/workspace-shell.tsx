"use client";

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { usePathname } from "next/navigation";
import { api } from "@/lib/api/client";

type ShellProps = {
  user: { username: string; role: string } | null;
  active: "dashboard" | "repos" | "knowledge" | "collaboration" | "evals";
  note: { stage: string; title: string; text: string };
  knowledgeHref?: string;
  onLogout: () => void;
  children: ReactNode;
};

type Project = { id: string; full_name: string };

function NavIcon({kind}:{kind:"home"|"repos"|"knowledge"|"collaboration"}) {
  const paths={home:"M3 10 12 3l9 7M5 9v11h5v-6h4v6h5V9",repos:"M5 4h14v16H5zM9 4v16M12 8h4M12 12h4",knowledge:"M12 5v15M3 4h5a4 4 0 0 1 4 2 4 4 0 0 1 4-2h5v14h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3z",collaboration:"M5 5h5v5H5zM14 14h5v5h-5zM10 7h7v7M7 10v7h7"};
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[kind]}/></svg>;
}

function currentRepoId(pathname: string): string | null {
  const match = pathname.match(/^\/repos\/([^/]+)/);
  return match ? match[1] : null;
}

export default function WorkspaceShell({ user, active, knowledgeHref, onLogout, children }: ShellProps) {
  const pathname = usePathname();
  const repoId = currentRepoId(pathname);
  const [projects, setProjects] = useState<Project[]>([]);

  useEffect(() => {
    let mounted = true;
    api<{ items: Project[] }>("/repos?limit=8")
      .then((data) => { if (mounted) setProjects(data.items); })
      .catch(() => undefined);
    return () => { mounted = false; };
  }, [repoId]);

  const tabs = [
    { label: "工作空间", href: "/", match: pathname === "/" },
    { label: "项目知识", href: repoId ? `/repos/${repoId}/knowledge` : "/repos", match: pathname.endsWith("/knowledge") },
    { label: "任务工作室", href: repoId ? `/repos/${repoId}/collaboration` : "/repos", match: pathname.endsWith("/collaboration") },
    { label: "Repos", href: "/repos", match: pathname === "/repos" },
    { label: "Drafts", href: repoId ? `/repos/${repoId}/collaboration?tab=actions` : "/repos", match: false },
    { label: "Runs", href: repoId ? `/repos/${repoId}#recent-runs` : "/repos", match: false },
    { label: "Reports", href: repoId ? `/repos/${repoId}/collaboration?tab=workflows` : "/repos", match: false },
    { label: "Evals", href: "/evals", match: pathname === "/evals" },
  ];

  return (
    <div className="workspace">
      <aside className="sidebar" aria-label="主导航">
        <a className="brand" href="/" aria-label="DevFlow AI 首页">
          <span className="brand-icon">D</span> DevFlow <span className="muted">AI</span>
        </a>
        <p className="brand-caption">研发数据、PR 与 Issue 智能协作</p>
        <div className="project-rail">
          <div className="project-rail-head">
            <span>项目</span>
            <a href="/repos">＋ 添加项目</a>
          </div>
          {projects.length === 0
            ? <p className="project-empty">还没有项目</p>
            : <ul>
              {projects.map((project) => (
                <li key={project.id}>
                  <a className={`project-item ${project.id === repoId ? "active" : ""}`} href={`/repos/${project.id}`}>
                    <span className="project-mark" aria-hidden="true">▣</span>
                    <span>{project.full_name}</span>
                  </a>
                </li>
              ))}
            </ul>}
        </div>
        <nav className="workspace-nav" aria-label="工作空间">
        <a className={`nav-item ${active === "dashboard" ? "active" : ""}`} aria-current={active==="dashboard"?"page":undefined} href="/">
          <NavIcon kind="home"/> 工作空间
        </a>
        <a className={`nav-item ${active === "repos" ? "active" : ""}`} href="/repos">
          <NavIcon kind="repos"/> 仓库协作
        </a>
        {knowledgeHref ? (
          <a className={`nav-item ${active === "knowledge" ? "active" : ""}`} href={knowledgeHref}>
            <NavIcon kind="knowledge"/> 知识库
          </a>
        ) : null}
        {knowledgeHref && <a className={`nav-item ${active === "collaboration" ? "active" : ""}`} aria-current={active==="collaboration"?"page":undefined} href={knowledgeHref.replace(/\/knowledge$/, "/collaboration")}><NavIcon kind="collaboration"/> 工作流与审批</a>}
        </nav>
        <p className="workspace-caption"><span className="status-dot"/>本地工作空间</p>
        <div className="user-row">
          <span className="avatar">{user?.username[0]?.toUpperCase() || "?"}</span>
          <div>
            <strong>{user?.username}</strong>
            <small>{user?.role==="admin"?"管理员":"已登录"}</small>
          </div>
          <button className="link-button" onClick={onLogout}>
            退出
          </button>
        </div>
      </aside>
      <main className="dashboard">
        <nav className="topnav" aria-label="工作区导航">
          {tabs.map((tab) => (
            <a key={tab.label} className={tab.match ? "active" : ""} href={tab.href}>{tab.label}</a>
          ))}
        </nav>
        {children}
      </main>
    </div>
  );
}
