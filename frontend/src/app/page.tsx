"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api/client";

type User = { username: string; role: string };
type Capabilities = {
  model_configured: boolean; configured_provider: string; model: string | null; worker_online: boolean;
  latest_probe: { provider: string; passed: boolean } | null;
};
type Probe = {
  id: string; passed: boolean; provider: string; synthetic_input: boolean; live_model_verified: boolean;
  answer?: { summary: string; observed_value: string; evidence_id: string };
  checks?: Record<string, boolean>; trace?: { step?: number; event: string; tool?: string; status?: string; error_code?: string }[];
  usage?: { total_tokens: number; model_steps: number; tool_calls: number; estimated: boolean };
  elapsed_ms?: number; error?: { code: string; message: string }; case_id: string;
};

const checkNames: Record<string, string> = { tool_calling: "工具调用", tool_result_continuation: "结果回传", structured_output: "结构化输出", evidence_binding: "证据绑定" };

export default function Dashboard() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [probe, setProbe] = useState<Probe | null>(null);
  const [history, setHistory] = useState<Probe[]>([]);
  const [caseId, setCaseId] = useState("normal_pr");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async () => {
    const [capabilities, records] = await Promise.all([api<Capabilities>("/settings/capabilities"), api<Probe[]>("/settings/capabilities/probes")]);
    setCaps(capabilities); setHistory(records);
  }, []);
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const identity = await api<User>("/auth/me");
        if (!active) return;
        setUser(identity); await refresh();
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) router.replace("/login");
        else if (active) setError(err instanceof Error ? err.message : "加载失败");
      } finally { if (active) setLoading(false); }
    })();
    return () => { active = false; };
  }, [refresh, router]);
  async function run(source: "fixture" | "configured") {
    setBusy(true); setError(""); setProbe(null);
    try { setProbe(await api<Probe>("/settings/capabilities/probe", { source, case_id: caseId })); await refresh(); }
    catch (err) {
      if (err instanceof ApiError && err.status === 401) router.replace("/login");
      else setError(err instanceof Error ? err.message : "验证失败");
    } finally { setBusy(false); }
  }
  async function logout() {
    try { await api("/auth/logout", {}, "POST"); router.replace("/login"); }
    catch (err) { setError(err instanceof Error ? err.message : "退出失败"); }
  }
  if (loading) return <main className="loading" aria-live="polite">正在打开工作空间…</main>;
  return <div className="workspace">
    <aside className="sidebar">
      <div className="brand"><span className="brand-icon">D</span> DevFlow <span className="muted">AI</span></div>
      <p className="nav-caption">工作空间</p><a className="nav-item active" href="/">◈ &nbsp; 基础验证</a>
      <div className="nav-item disabled">⌘ &nbsp; 仓库协作 <span>M1</span></div>
      <div className="nav-item disabled">▤ &nbsp; 知识库 <span>M3</span></div>
      <div className="sidebar-note"><span className="pill">M0</span><strong>基础能力已建立</strong><p>先验证身份、模型与工具协议，逐步接入真实研发数据。</p></div>
      <div className="user-row"><span className="avatar">{user?.username[0]?.toUpperCase() || "?"}</span><div><strong>{user?.username}</strong><small>本地管理员</small></div><button className="link-button" onClick={logout}>退出</button></div>
    </aside>
    <main className="dashboard">
      <header><div><p className="eyebrow">FOUNDATION / M0</p><h1>验证基础，开始协作。</h1><p className="muted">确认模型能够调用工具、接收结果，并返回可校验的结构。</p></div><span className="pill">本地工作空间</span></header>
      {error && <div role="alert" className="error-box">{error} <button className="link-button" onClick={() => window.location.reload()}>重新加载</button></div>}
      <section className="status-grid" aria-label="服务状态">
        <Status label="身份与会话" value={user ? "已验证" : "未连接"} detail="HttpOnly 会话与 CSRF 校验" good={!!user} />
        <Status label="任务进程" value={caps?.worker_online ? "在线" : "等待启动"} detail="M0 心跳 · 业务任务从 M1 接入" good={!!caps?.worker_online} />
        <Status label="模型连接" value={caps?.model_configured ? "待能力验证" : "待配置"} detail={caps?.model || "OpenAI 兼容接口"} good={false} />
      </section>
      <section className="panel">
        <div className="panel-heading"><div><span className="section-number">01</span><h2>工具调用验证</h2></div><span className="tag">只读 · 合成输入</span></div>
        <p className="muted">使用固定样例检查完整调用链。离线回放验证协议实现；真实模型验证会调用你配置的服务。</p>
        <div className="probe-controls"><label htmlFor="case">验证样例<select id="case" value={caseId} onChange={(e) => setCaseId(e.target.value)} disabled={busy}>
          <option value="normal_pr">普通 PR · 参数边界</option><option value="risk_pr">风险 PR · 权限检查</option><option value="failed_ci">失败 CI · 测试日志</option>
        </select></label><div className="button-row"><button className="button secondary" disabled={busy} onClick={() => run("fixture")}>运行离线验证</button><button className="button primary" disabled={busy || !caps?.model_configured} onClick={() => run("configured")}>验证真实模型</button></div></div>
        {!caps?.model_configured && <p className="hint">在本地 .env 填写 LLM_BASE_URL、LLM_MODEL、LLM_API_KEY，然后重启后端即可启用真实验证。</p>}
        {caps?.configured_provider === "fixture" && <p className="hint">当前配置为 fixture；配置验证仍是离线回放，不代表真实模型已通过。</p>}
        {busy && <div className="progress" role="status"><span className="spinner" /> 正在完成工具调用与证据校验，最多等待 60 秒…</div>}
        {probe && <div className="probe-result" aria-live="polite">
          <div className="result-title"><h3>{probe.passed ? "验证通过" : "验证未通过"}</h3><span className="tag">{probe.live_model_verified ? "真实模型 / 合成输入" : "离线协议回放"}</span></div>
          {probe.error && <p role="alert" className="error-box">{probe.error.code} · {probe.error.message}</p>}
          {probe.checks && <div className="check-grid">{Object.entries(probe.checks).map(([key, ok]) => <div key={key}><span className="checkmark">{ok ? "✓" : "!"}</span>{checkNames[key] || key}</div>)}</div>}
          {probe.answer && <><p>{probe.answer.summary}</p><dl className="evidence-meta"><div><dt>工具返回值</dt><dd>{probe.answer.observed_value}</dd></div><div><dt>证据 ID</dt><dd>{probe.answer.evidence_id}</dd></div></dl></>}
          {probe.trace && <details open><summary>调用轨迹 · {probe.usage?.model_steps} 次模型决策 / {probe.usage?.tool_calls} 次工具调用</summary><ol className="trace">{probe.trace.map((event, i) => <li key={i}><span>{event.event === "tool.finished" ? "工具执行" : "模型响应"}</span><code>{event.tool || `step ${event.step}`}</code><span className="muted">{event.status || "完成"}</span></li>)}</ol></details>}
          {probe.usage && <p className="form-note">{probe.elapsed_ms} ms · {probe.usage.total_tokens} Token{probe.provider === "fixture" ? "（合成用量）" : probe.usage.estimated ? "（估算）" : "（服务返回）"}</p>}
        </div>}
      </section>
      <div className="bottom-grid"><section className="panel"><div className="panel-heading"><div><span className="section-number">02</span><h2>最近验证</h2></div><button className="link-button" onClick={() => refresh().catch((err) => setError(err instanceof Error ? err.message : "刷新失败"))}>刷新</button></div>
        {history.length ? <ul className="history">{history.slice(0, 5).map((item) => <li key={item.id}><span className={item.passed ? "checkmark" : "muted"}>{item.passed ? "✓" : "!"}</span><div><strong>{item.case_id}</strong><small>{item.provider === "fixture" ? "离线协议回放" : "真实模型探测"}</small></div><span className="tag">{item.passed ? "通过" : "未通过"}</span></li>)}</ul> : <p className="empty">尚无验证记录。运行一次样例即可开始。</p>}</section>
        <section className="panel next-stage"><span className="eyebrow">NEXT / M1</span><h2>接入真实研发数据</h2><p>下一阶段连接 GitHub 仓库、同步 Issue / PR / CI，并建立固定版本的代码证据。</p><ul><li>仓库身份与访问范围</li><li>增量同步与可追溯快照</li><li>固定 commit 的只读工作区</li></ul><span className="tag">当前未接入 GitHub</span></section></div>
    </main>
  </div>;
}

function Status({ label, value, detail, good }: { label: string; value: string; detail: string; good: boolean }) {
  return <article className="status-card"><p>{label}</p><strong><span className={`status-dot ${good ? "" : "waiting"}`} />{value}</strong><small>{detail}</small></article>;
}
