"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api/client";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    const credentials = { username, password }; setPassword("");
    try { await api("/auth/login", credentials); router.replace("/"); }
    catch (err) { setError(err instanceof Error ? err.message : "登录失败"); }
    finally { setBusy(false); }
  }
  return <main className="login-shell">
    <section className="login-card">
      <div className="brand"><span className="brand-icon">D</span> DevFlow <span className="muted">AI</span></div>
      <form className="login-form" onSubmit={submit}>
      <h1>登录工作空间</h1><p className="muted">连接项目，开始有据可查的分析。</p>
      <label htmlFor="username">账户</label><input id="username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required maxLength={80} />
      <label htmlFor="password">密码</label><input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required maxLength={128} />
      {error && <p role="alert" className="error-box">{error}</p>}
      <button className="button primary full" disabled={busy}>{busy ? "正在验证…" : "进入工作空间"}</button>
      <details className="login-help"><summary>首次使用？</summary><p className="form-note">初始登录信息保存在本机 <code>var/initial-admin.json</code>。</p></details>
    </form></section><p className="login-footer">DevFlow AI · 研发协作助手</p>
  </main>;
}
