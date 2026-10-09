"use client";

import type { ReactNode } from "react";

type ShellProps = {
  user: { username: string; role: string } | null;
  active: "dashboard" | "repos";
  note: { stage: string; title: string; text: string };
  onLogout: () => void;
  children: ReactNode;
};

export default function WorkspaceShell({ user, active, note, onLogout, children }: ShellProps) {
  return (
    <div className="workspace">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-icon">D</span> DevFlow <span className="muted">AI</span>
        </div>
        <p className="nav-caption">工作空间</p>
        <a className={`nav-item ${active === "dashboard" ? "active" : ""}`} href="/">
          ◈ &nbsp; 基础验证
        </a>
        <a className={`nav-item ${active === "repos" ? "active" : ""}`} href="/repos">
          ⌘ &nbsp; 仓库协作
        </a>
        <div className="nav-item disabled">
          ▤ &nbsp; 知识库 <span>M3</span>
        </div>
        <div className="sidebar-note">
          <span className="pill">{note.stage}</span>
          <strong>{note.title}</strong>
          <p>{note.text}</p>
        </div>
        <div className="user-row">
          <span className="avatar">{user?.username[0]?.toUpperCase() || "?"}</span>
          <div>
            <strong>{user?.username}</strong>
            <small>本地管理员</small>
          </div>
          <button className="link-button" onClick={onLogout}>
            退出
          </button>
        </div>
      </aside>
      <main className="dashboard">{children}</main>
    </div>
  );
}
