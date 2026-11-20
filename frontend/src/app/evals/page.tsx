"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api/client";
import WorkspaceShell from "@/components/workspace-shell";

type User = { username: string; role: string };
type OfflineRow = { id: string; split: string; group: string; passed: boolean; actual: string | null; duration_ms: number | null };
type Offline = {
  mode: string; model_quality_evaluated: boolean; dataset_version: string;
  dataset_sha256: string; created_at: string; passed: number; total: number;
  model_requests: number; groups: Record<string, number>; rows: OfflineRow[];
};
type Real = {
  mode: string; dataset_version: string; sample_count: number; repository_count: number;
  split: string; reviewer_type: string; human_review_status: string; model_requests: number;
  findings: number; adjudicated_findings: number; uncertain_findings: number;
  provisional_factual_precision: number | null; provisional_false_discovery_rate: number | null;
  fully_supported_citation_rate: number | null; reference_verdict_accuracy: number | null;
  median_duration_seconds: number | null; total_recorded_tokens: number | null;
  recall: number | null; recall_note: string; generalization_claim: boolean;
  cases: { case: string; kind: string; findings: number; truth: Record<string, number> | null }[];
};
type Payload = { offline: Offline | null; real: Real | null };

const percent = (value: number | null | undefined) =>
  value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;

export default function EvalsPage() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setData(await api<Payload>("/evals/latest"));
  }, []);

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

  async function logout() {
    try { await api("/auth/logout", {}, "POST"); router.replace("/login"); }
    catch (err) { setError(err instanceof Error ? err.message : "退出失败"); }
  }

  if (loading) return <main className="loading" aria-live="polite">正在读取评测结果…</main>;
  const offline = data?.offline ?? null;
  const real = data?.real ?? null;

  return <WorkspaceShell
    user={user}
    active="evals"
    note={{ stage: "M7", title: "评测与回归", text: "固定评测集、硬规则和真实案例基线。" }}
    onLogout={logout}
  >
    <header>
      <div>
        <p className="eyebrow">EVALS / 评测</p>
        <h1>评测与基线</h1>
        <p className="muted">固定评测集、分组通过率、工具与裁决规则；离线结果不代表真实模型质量。</p>
      </div>
      <button className="button secondary small" onClick={() => void refresh()}>刷新</button>
    </header>
    {error && <div role="alert" className="error-box">{error}</div>}

    <section className="kpi-grid" aria-label="评测指标">
      <article className="kpi-card">
        <span>离线用例</span>
        <strong>{offline ? `${offline.passed}/${offline.total}` : "—"}</strong>
        <small>确定性规则回放</small>
      </article>
      <article className="kpi-card">
        <span>模型请求</span>
        <strong>{offline ? offline.model_requests : "—"}</strong>
        <small>离线评测不调用模型</small>
      </article>
      <article className={`kpi-card ${offline && offline.passed < offline.total ? "warn" : ""}`}>
        <span>失败用例</span>
        <strong>{offline ? offline.total - offline.passed : "—"}</strong>
        <small>固定基线与回归</small>
      </article>
      <article className="kpi-card">
        <span>真实案例</span>
        <strong>{real ? real.sample_count : "—"}</strong>
        <small>{real ? `${real.repository_count} 个仓库` : "未生成基线"}</small>
      </article>
      <article className="kpi-card">
        <span>事实精确率</span>
        <strong>{real ? percent(real.provisional_factual_precision) : "—"}</strong>
        <small>暂定值，人工复核中</small>
      </article>
      <article className="kpi-card">
        <span>人工复核</span>
        <strong>{real ? (real.human_review_status === "pending" ? "待复核" : real.human_review_status) : "—"}</strong>
        <small>{real?.reviewer_type || "无真实基线"}</small>
      </article>
    </section>

    {!offline && !real && <section className="panel"><p className="empty">还没有生成评测结果。运行 <code>scripts/evaluate.py</code> 后会写入固定结果。</p></section>}

    {offline && <section className="panel">
      <div className="panel-heading"><div><h2>离线固定评测</h2></div><span className="tag">{offline.mode}</span></div>
      <p className="form-note">
        数据集 v{offline.dataset_version} · 指纹 <code>{offline.dataset_sha256.slice(0, 12)}</code> · {offline.created_at?.slice(0, 19).replace("T", " ")}
      </p>
      <div className="count-chips">
        {Object.entries(offline.groups).map(([group, count]) => <span key={group} className="chip">{group} · {count}</span>)}
      </div>
      <div className="eval-table-wrap">
        <table className="eval-table">
          <thead><tr><th>用例</th><th>分组</th><th>split</th><th>结果</th><th>实际输出</th><th>耗时</th></tr></thead>
          <tbody>
            {offline.rows.map((row) => <tr key={row.id}>
              <td><code>{row.id}</code></td>
              <td>{row.group}</td>
              <td>{row.split}</td>
              <td><span className={`mini-state ${row.passed ? "success" : "failure"}`}>{row.passed ? "通过" : "失败"}</span></td>
              <td>{row.actual || "—"}</td>
              <td>{row.duration_ms === null ? "—" : `${row.duration_ms} ms`}</td>
            </tr>)}
          </tbody>
        </table>
      </div>
    </section>}

    {real && <section className="panel">
      <div className="panel-heading"><div><h2>真实案例基线</h2></div><span className="tag">{real.mode}</span></div>
      <p className="form-note">样本 {real.sample_count} · 发现 {real.findings} 条 · 已裁决 {real.adjudicated_findings} · 待定 {real.uncertain_findings}；{real.recall_note}</p>
      <div className="eval-metrics">
        <div><span>暂定事实精确率</span><strong>{percent(real.provisional_factual_precision)}</strong></div>
        <div><span>暂定误发现率</span><strong>{percent(real.provisional_false_discovery_rate)}</strong></div>
        <div><span>引用完全支持率</span><strong>{percent(real.fully_supported_citation_rate)}</strong></div>
        <div><span>参考裁决准确率</span><strong>{percent(real.reference_verdict_accuracy)}</strong></div>
        <div><span>记录 tokens</span><strong>{real.total_recorded_tokens ?? "—"}</strong></div>
        <div><span>中位耗时</span><strong>{real.median_duration_seconds ? `${real.median_duration_seconds}s` : "—"}</strong></div>
      </div>
      <ul className="history">
        {real.cases.map((item) => <li key={item.case}>
          <div>
            <strong><code>{item.case}</code> · {item.kind}</strong>
            <small>{item.truth ? `支持 ${item.truth.supported ?? 0} / 反驳 ${item.truth.refuted ?? 0} / 待定 ${item.truth.uncertain ?? 0}` : "无裁决"}</small>
          </div>
          <span className="tag">{item.findings} 条发现</span>
        </li>)}
      </ul>
    </section>}

    <section className="panel">
      <h2>如何解读</h2>
      <ul className="plain-list">
        <li>离线评测只验证工具、规则、裁决降级和证据校验，不调用真实模型，<b>不代表模型质量</b>。</li>
        <li>真实案例基线使用冻结的公开案例，人工复核未完成前所有指标均为暂定值。</li>
        <li>修改 Prompt、检索配置或工具逻辑后，应重跑同一组用例并对比通过率、证据支持率和失败原因。</li>
      </ul>
      <p className="form-note">相关脚本：<code>scripts/evaluate.py</code>、<code>scripts/evaluate_real.py</code>、<code>evals/README.md</code></p>
    </section>
  </WorkspaceShell>;
}
