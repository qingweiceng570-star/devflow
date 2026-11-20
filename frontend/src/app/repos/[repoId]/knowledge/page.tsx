"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { api, apiUpload, ApiError } from "@/lib/api/client";
import WorkspaceShell from "@/components/workspace-shell";
import { useBackgroundRefresh } from "@/components/use-background-refresh";

type User = { username: string; role: string };
type DocumentView = {
  id: string; source_key: string; title: string | null; state: string; version: number;
  byte_size: number; updated_at: string; current_version_id: string | null;
  origin: string; source_revision: string | null;
};
type ImportResult = { commit_sha: string; discovered: number; imported: number; unchanged: number; removed: number; skipped_count: number; skipped: { path: string; reason: string }[] };
type JobStatus = { id: string; status: string; last_error: { code: string; message: string } | null; result?: ImportResult | null };
type IndexStatus = {
  active_generation: { id: string; chunk_count: number; embedded: boolean; degraded_reason: string | null; activated_at: string | null } | null;
  pending_changes: number; building: boolean; latest_failure: { code: string; message: string } | null;
  document_count: number; document_sources: { repository: number; uploaded: number; generated: number };
  import_job: JobStatus | null; index_job: JobStatus | null;
};
type SearchHit = {
  chunk_id: string; document: string; title: string | null; heading_path: string[];
  lines: number[]; score: number | null; text: string;
};
type SearchTrace = {
  generation_id: string | null; fused_candidates: number; degraded: string[]; reranked?: boolean;
  dropped_by_filtering: string[]; channels: Record<string, { enabled: boolean; count?: number; reason?: string }>;
};
type DocumentDetail = { versions: { id: string; created_at: string; parser_version: string }[]; current_chunk_count: number };

const degradedNames: Record<string, string> = {
  vector_unavailable: "向量通道不可用（降级为 BM25 单路）",
  bm25_unavailable: "BM25 通道不可用（降级为向量单路）",
  no_index: "尚未建立索引",
  no_reranker: "未配置重排模型（使用 RRF 结果）",
  rerank_unavailable: "重排服务不可用（降级为 RRF 结果）",
};
const jobActive = (job: JobStatus | null | undefined) => !!job && ["queued", "leased", "retry_wait"].includes(job.status);

export default function KnowledgePage() {
  const router = useRouter();
  const params = useParams<{ repoId: string }>();
  const repoId = params.repoId;
  const [user, setUser] = useState<User | null>(null);
  const [repoName, setRepoName] = useState("");
  const [documents, setDocuments] = useState<DocumentView[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [trace, setTrace] = useState<SearchTrace | null>(null);
  const [detail, setDetail] = useState<Record<string, DocumentDetail>>({});
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [repo, list, state] = await Promise.all([
      api<{ repo: { full_name: string } }>(`/repos/${repoId}`, undefined, undefined, signal),
      api<{ items: DocumentView[]; next_cursor: string | null }>(`/repos/${repoId}/documents?limit=50`, undefined, undefined, signal),
      api<IndexStatus>(`/repos/${repoId}/knowledge/status`, undefined, undefined, signal),
    ]);
    signal?.throwIfAborted();
    setRepoName(repo.repo.full_name);
    setDocuments(list.items);
    setNextCursor(list.next_cursor);
    setStatus(state);
  }, [repoId]);
  const background = useBackgroundRefresh(refresh);
  const processing = jobActive(status?.import_job) || jobActive(status?.index_job) || !!status?.building;

  useEffect(() => {
    if (!processing || background.error) return;
    const timer = setTimeout(() => { void background.refreshNow(); }, 2000);
    return () => clearTimeout(timer);
  }, [processing, status, background.error, background.refreshNow]);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    (async () => {
      try {
        const identity = await api<User>("/auth/me");
        if (!active) return;
        setUser(identity);
        await refresh(controller.signal);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) router.replace("/login");
        else if (active) setError(err instanceof Error ? err.message : "加载失败");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; controller.abort(); };
  }, [refresh, router]);

  async function importDocuments() {
    setBusy("import"); setError(""); setDetail({});
    try {
      await api(`/repos/${repoId}/knowledge/import`, {});
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "导入请求失败");
    } finally { setBusy(""); }
  }

  async function loadMore() {
    if (!nextCursor) return;
    setBusy("more"); setError("");
    try {
      const page = await api<{ items: DocumentView[]; next_cursor: string | null }>(`/repos/${repoId}/documents?limit=50&cursor=${encodeURIComponent(nextCursor)}`);
      setDocuments(current => [...current, ...page.items.filter(item => !current.some(existing => existing.id === item.id))]);
      setNextCursor(page.next_cursor);
    } catch (err) { setError(err instanceof Error ? err.message : "加载文档失败"); }
    finally { setBusy(""); }
  }

  async function upload(event: React.FormEvent) {
    event.preventDefault();
    const file = fileRef.current?.files?.[0];
    if (!file) { setError("请选择要上传的文档（.md/.markdown/.mdx/.rst/.txt）"); return; }
    setBusy("upload"); setError("");
    try {
      const result = await apiUpload<{ changed: boolean }>(`/repos/${repoId}/documents`, file);
      if (fileRef.current) fileRef.current.value = "";
      await refresh();
      if (!result.changed) setError("内容与当前版本一致，未产生新版本。");
    } catch (err) {
      setError(err instanceof Error ? err.message : "上传失败");
    } finally {
      setBusy("");
    }
  }

  async function removeDocument(document: DocumentView) {
    if (!window.confirm(`删除文档 ${document.source_key}？检索将立即排除该文档。`)) return;
    setBusy(`del-${document.id}`); setError("");
    try {
      await api(`/documents/${document.id}`, { expected_version: document.version }, "DELETE");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    } finally {
      setBusy("");
    }
  }

  async function showDetail(document: DocumentView) {
    if (detail[document.id]) {
      setDetail((current) => { const next = { ...current }; delete next[document.id]; return next; });
      return;
    }
    try {
      const payload = await api<{ document: DocumentView } & DocumentDetail>(`/documents/${document.id}`);
      setDetail((current) => ({ ...current, [document.id]: payload }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取详情失败");
    }
  }

  async function reindex() {
    setBusy("reindex"); setError("");
    try {
      await api(`/repos/${repoId}/knowledge/reindex`, { reason: "manual" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "重建请求失败");
    } finally {
      setBusy("");
    }
  }

  async function search(event: React.FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    setBusy("search"); setError("");
    try {
      const result = await api<{ results: SearchHit[]; trace: SearchTrace }>(
        `/repos/${repoId}/knowledge/search`, { query: query.trim() },
      );
      setHits(result.results);
      setTrace(result.trace);
    } catch (err) {
      setError(err instanceof Error ? err.message : "检索失败");
    } finally {
      setBusy("");
    }
  }

  async function logout() {
    try { await api("/auth/logout", {}, "POST"); router.replace("/login"); }
    catch (err) { setError(err instanceof Error ? err.message : "退出失败"); }
  }

  if (loading) return <main className="loading" aria-live="polite">正在打开工作空间…</main>;
  const importResult = status?.import_job?.result;
  const ready = !!status?.active_generation?.chunk_count && !processing && !status?.pending_changes;
  return <WorkspaceShell
    user={user}
    active="repos"
    knowledgeHref={`/repos/${repoId}/knowledge`}
    note={{ stage: "M3", title: "RAG 知识库", text: "文档按结构切分并做混合检索；引用可追溯到具体文档版本与行号，通道状态透明可见。" }}
    onLogout={logout}
  >
    <header>
      <div>
        <p className="eyebrow">KNOWLEDGE</p>
        <h1>知识库与检索</h1>
        <p className="muted"><a className="link-button" href={`/repos/${repoId}`}>← 返回 {repoName}</a></p>
      </div>
      <button className="button secondary small" disabled={busy === "reindex"} onClick={reindex}>
        {busy === "reindex" ? "已请求…" : "重建索引"}
      </button>
    </header>
    {error && <div role="alert" className="error-box">{error} <button className="link-button" onClick={() => setError("")}>关闭</button></div>}
    {background.error && <div role="alert" className="error-box">{background.error} <button className="link-button" onClick={() => void background.refreshNow()}>重试加载</button></div>}
    <section className="panel" aria-label="知识库来源">
      <div className="panel-heading"><div><span className="section-number">01</span><h2>项目资料</h2></div><span className="tag">{status?.document_count ?? 0} 份生效文档</span></div>
      <p>知识库属于当前仓库，支持直接导入 GitHub 文档，也支持上传自己的资料。新接入的仓库会自动导入一次。</p>
      <p className="form-note">从默认分支读取 README、根目录指南和 docs / doc / documentation 下的 Markdown、MDX、RST 与文本。重复导入会更新文档并自动建立索引。</p>
      <div className="button-row">
        <button className="button primary" disabled={!!busy || jobActive(status?.import_job)} onClick={importDocuments}>{jobActive(status?.import_job) ? "正在导入仓库文档…" : busy === "import" ? "提交中…" : "从 GitHub 导入 / 更新文档"}</button>
        <a className="button secondary" href={`/repos/${repoId}`}>前往项目问答</a>
        <a className="link-button" href={`/repos/${repoId}/collaboration`}>检查 GitHub 读取权限</a>
      </div>
      <p role="status" aria-live="polite">{jobActive(status?.import_job) ? "正在提取仓库文档，完成后自动建立索引，可稍后回来查看。" : processing ? "文档已保存，正在建立索引…" : ready ? "知识库已就绪，可以进行带引用的项目问答。" : (status?.document_count ?? 0) === 0 ? "知识库为空：请导入仓库文档或在下方上传资料后再提问。" : "文档尚未完成索引，请检查处理状态或重建索引。"}</p>
      {status?.document_sources && <p className="form-note">仓库文档 {status.document_sources.repository} · 手动上传 {status.document_sources.uploaded} · 生成报告 {status.document_sources.generated}</p>}
      {status?.import_job?.last_error && status.import_job.status !== "succeeded" && <div role="alert" className="error-box">导入{status.import_job.status === "retry_wait" ? "暂未完成，将自动重试" : "失败"}：{status.import_job.last_error.message}</div>}
      {importResult && <div className="form-note"><p>最近导入：新增或更新 {importResult.imported} 份，未变化 {importResult.unchanged} 份，移除 {importResult.removed} 份，跳过 {importResult.skipped_count} 份。来源提交 {importResult.commit_sha.slice(0, 8)}。</p>{importResult.discovered === 0 && <p>默认分支未找到支持的项目文档，可以在下方手动上传。</p>}{importResult.skipped_count > 0 && <details><summary>查看跳过原因（最多显示 20 条）</summary>{importResult.skipped.map(item => <p key={item.path}>{item.path}：{item.reason}</p>)}<p>每次最多导入 200 份、总计 10 MiB，单文件最多 1 MiB。可手动上传其余资料。</p></details>}</div>}
    </section>
    <section className="status-grid" aria-label="索引状态">
      <Status label="索引代" value={status?.active_generation ? `${status.active_generation.chunk_count} 块` : "未建立"} detail={status?.active_generation ? `激活于 ${status.active_generation.activated_at?.slice(0, 19).replace("T", " ")}` : "导入或上传文档后自动构建"} good={!!status?.active_generation} />
      <Status label="待处理变更" value={String(status?.pending_changes ?? 0)} detail={status?.building ? "正在构建新索引代" : "文档变更会触发重建"} good={(status?.pending_changes ?? 0) === 0} />
      <Status label="向量通道" value={status?.active_generation?.embedded ? "已启用" : "未启用"} detail={status?.active_generation?.degraded_reason === "not_configured" ? "未配置嵌入/向量服务，BM25 单路可用" : status?.active_generation?.degraded_reason || "配置后自动写入"} good={!!status?.active_generation?.embedded} />
    </section>
    {status?.latest_failure && <div className="error-box">最近一次索引失败：{status.latest_failure.code} · {status.latest_failure.message}（旧索引代仍然可用）</div>}
    {status?.index_job?.status === "failed" && <div role="alert" className="error-box">索引任务失败：{status.index_job.last_error?.message || "请点击重建索引重试"}</div>}
    <section className="panel">
      <div className="panel-heading"><div><span className="section-number">02</span><h2>文档与手动上传</h2></div><span className="tag">UTF-8 文本 · ≤5 MiB</span></div>
      <form className="repo-form" onSubmit={upload}>
        <label htmlFor="doc-file">选择文档
          <input id="doc-file" ref={fileRef} type="file" accept=".md,.markdown,.mdx,.rst,.txt" />
        </label>
        <button className="button primary" disabled={busy === "upload"}>{busy === "upload" ? "上传中…" : "上传并索引"}</button>
      </form>
      {documents.length === 0
        ? <p className="empty">还没有文档。可从 GitHub 导入，或上传项目文档。</p>
        : <ul className="history">
          {documents.map((document) => (
            <li key={document.id}>
              <div>
                <strong>{document.title || document.source_key}</strong>
                <small>{document.source_key} · v{document.version} · {(document.byte_size / 1024).toFixed(1)} KiB · {document.state === "active" ? "生效中" : "已删除（待重建清理）"}</small>
                <small>{document.source_key.startsWith("git:") ? <a className="link-button" target="_blank" rel="noreferrer" href={`https://github.com/${repoName}/blob/${document.source_revision}/${document.source_key.slice(4).split("/").map(encodeURIComponent).join("/")}`}>查看 GitHub 原文 · {document.source_revision?.slice(0, 8)}</a> : document.origin === "generated" ? "来源：生成报告" : "来源：手动上传"}</small>
                {detail[document.id] && <small className="muted">版本 {detail[document.id].versions.length} 个 · 当前块 {detail[document.id].current_chunk_count} 个</small>}
              </div>
              <div className="button-row">
                <button className="link-button" onClick={() => showDetail(document)}>{detail[document.id] ? "收起" : "详情"}</button>
                {document.state === "active" && (
                  <button className="link-button" disabled={busy === `del-${document.id}`} onClick={() => removeDocument(document)}>删除</button>
                )}
              </div>
            </li>
          ))}
        </ul>}
      {nextCursor && <button className="button secondary small" disabled={!!busy} onClick={loadMore}>{busy === "more" ? "加载中…" : "加载更多文档"}</button>}
    </section>
    <section className="panel">
      <div className="panel-heading"><div><span className="section-number">03</span><h2>召回测试</h2></div><span className="tag">输入文档中的关键词，检查能否找到资料</span></div>
      <form className="repo-form" onSubmit={search}>
        <label htmlFor="kb-query">查询
          <input id="kb-query" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="例如：写操作的权限要求" />
        </label>
        <button className="button primary" disabled={busy === "search" || !query.trim()}>{busy === "search" ? "检索中…" : "检索"}</button>
      </form>
      {hits !== null && <div className="run-result">
        {hits.length === 0 ? <p className="empty">{(status?.document_count ?? 0) === 0 ? "当前知识库为空，请先导入或上传文档。" : processing ? "文档正在处理中，请等索引完成后重试。" : !status?.active_generation ? "文档尚未建立索引，请点击重建索引后重试。" : "未找到与查询词匹配的段落，可尝试项目名、功能名或文档中的关键词。"}</p> : <ul className="finding-list">
          {hits.map((hit) => (
            <li key={hit.chunk_id} className="finding severity-info">
              <span className="severity-badge">{hit.score !== null ? hit.score.toFixed(3) : "-"}</span>
              <div>
                <p><b>{hit.document}</b>{hit.heading_path.length ? ` · ${hit.heading_path.join(" / ")}` : ""}</p>
                <p className="muted">行 {hit.lines[0]}-{hit.lines[1]} · chunk {hit.chunk_id.slice(0, 8)}</p>
                <pre className="evidence-excerpt">{hit.text}</pre>
              </div>
            </li>
          ))}
        </ul>}
        {trace && <div className="kb-trace">
          <p className="form-note">索引代 {trace.generation_id?.slice(0, 8) || "无"} · 融合候选 {trace.fused_candidates} · 过滤淘汰 {trace.dropped_by_filtering.length}{trace.reranked ? " · 已重排" : ""}</p>
          <div className="count-chips">
            {Object.entries(trace.channels).map(([name, channel]) => (
              <span key={name} className="chip">
                {name === "bm25" ? "BM25" : name === "rerank" ? "Rerank" : "向量"}：{channel.enabled ? `${channel.count} 条` : `关闭（${channel.reason}）`}
              </span>
            ))}
            {trace.degraded.map((flag) => <span key={flag} className="chip chip-warn">{degradedNames[flag] || flag}</span>)}
          </div>
        </div>}
      </div>}
    </section>
  </WorkspaceShell>;
}

function Status({ label, value, detail, good }: { label: string; value: string; detail: string; good: boolean }) {
  return <article className="status-card"><p>{label}</p><strong><span className={`status-dot ${good ? "" : "waiting"}`} />{value}</strong><small>{detail}</small></article>;
}
