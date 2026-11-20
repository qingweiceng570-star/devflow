"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api/client";
import { useBackgroundRefresh } from "@/components/use-background-refresh";

type Job = { status: string; last_error: { message: string } | null };
type Readiness = { document_count: number; pending_changes: number; building: boolean; active_generation: { chunk_count: number } | null; import_job: Job | null; index_job: Job | null };
const active = (job: Job | null | undefined) => !!job && ["queued", "leased", "retry_wait"].includes(job.status);

export default function KnowledgeReadiness({ repoId }: { repoId: string }) {
  const [state, setState] = useState<Readiness | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const read = useCallback(async (signal: AbortSignal) => {
    const data = await api<Readiness>(`/repos/${repoId}/knowledge/status`, undefined, undefined, signal);
    signal.throwIfAborted();
    setState(data);
  }, [repoId]);
  const background = useBackgroundRefresh(read);
  useEffect(() => { void background.refreshNow(); }, [background.refreshNow]);
  const processing = active(state?.import_job) || active(state?.index_job) || !!state?.building;
  useEffect(() => {
    if (!processing || background.error) return;
    const timer = setTimeout(() => void background.refreshNow(), 2000);
    return () => clearTimeout(timer);
  }, [processing, state, background.error, background.refreshNow]);

  async function importDocuments() {
    setSubmitting(true); setError("");
    try {
      await api(`/repos/${repoId}/knowledge/import`, {});
      await background.refreshNow();
    } catch (err) { setError(err instanceof Error ? err.message : "导入请求失败"); }
    finally { setSubmitting(false); }
  }

  return <div className="form-note" aria-label="知识库准备状态">
    <p role="status">{!state ? "正在检查项目资料…" : active(state.import_job) ? "正在从 GitHub 导入项目文档，完成后自动建立索引。" : processing ? "项目文档正在建立索引，请稍后提问。" : !state.document_count ? "当前知识库为空，先导入 README 和项目文档后，才能回答有资料依据的问题。" : !state.active_generation?.chunk_count || state.pending_changes ? "项目文档尚未完成索引，请到知识库检查处理状态。" : `知识库已就绪：${state.document_count} 份文档，${state.active_generation.chunk_count} 个可检索段落。`}</p>
    <a className="link-button" href={`/repos/${repoId}/knowledge`}>管理项目知识库</a>
    {state?.document_count === 0 && !processing && <button type="button" className="link-button" disabled={submitting} onClick={importDocuments}>{submitting ? "提交中…" : "导入 GitHub 仓库文档"}</button>}
    {(error || background.error) && <p role="alert">{error || background.error} <button type="button" className="link-button" onClick={() => void background.refreshNow()}>重试加载</button></p>}
    {state?.import_job?.status === "failed" && <p role="alert">文档导入失败：{state.import_job.last_error?.message}，可到知识库重新导入。</p>}
  </div>;
}
