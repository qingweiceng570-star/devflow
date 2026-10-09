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
    <section className="login-story">
      <div className="brand"><span className="brand-icon">D</span> DevFlow <span className="muted">AI</span></div>
      <div><p className="eyebrow">ENGINEERING, WITH EVIDENCE</p><h1>让研发判断，<br />有据可循。</h1><p className="lead">连接项目上下文，追踪每次工具调用，<br />把分析过程变成可核查的依据。</p></div>
      <div className="story-footer"><span className="status-dot" /> 本地工作空间 · M0 基础阶段</div>
    </section>
    <section className="login-form-wrap"><form className="login-form" onSubmit={submit}>
      <span className="pill">LOCAL WORKSPACE</span><h2>登录工作空间</h2><p className="muted">使用本地创建的管理员账户。</p>
      <label htmlFor="username">账户</label><input id="username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required maxLength={80} />
      <label htmlFor="password">密码</label><input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required maxLength={128} />
      {error && <p role="alert" className="error-box">{error}</p>}
      <button className="button primary full" disabled={busy}>{busy ? "正在验证…" : "进入工作空间 →"}</button>
      <p className="form-note">初次启动生成的登录信息保存在本机<br /><code>var/initial-admin.json</code></p>
    </form></section>
  </main>;
}
