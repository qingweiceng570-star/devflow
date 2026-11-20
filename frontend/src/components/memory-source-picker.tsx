"use client";
import {useEffect, useState} from "react";
import {api} from "@/lib/api/client";
type SourceRun={id:string;kind:string;target:Record<string,unknown>;created_at:string};
type SourceEvidence={id:string;source_type:string;excerpt:string;locator:Record<string,unknown>};
export default function MemorySourcePicker({repoId,runId,onRun,onEvidence}:{repoId:string;runId:string;onRun:(id:string)=>void;onEvidence:(ids:string)=>void}) {
  const [runs,setRuns]=useState<SourceRun[]>([]);
  const [evidence,setEvidence]=useState<SourceEvidence[]>([]);
  const [selected,setSelected]=useState<string[]>([]);
  const [error,setError]=useState("");
  useEffect(()=>{let active=true;api<{items:SourceRun[]}>(`/repos/${repoId}/runs?status=completed&limit=100`).then(d=>{if(active)setRuns(d.items);}).catch(e=>{if(active)setError(e.message);});return()=>{active=false;};},[repoId]);
  useEffect(()=>{let active=true;setEvidence([]);setSelected([]);onEvidence("");if(runId)api<{items:SourceEvidence[]}>(`/runs/${runId}/evidence`).then(d=>{if(active){setEvidence(d.items);setError("");}}).catch(e=>{if(active)setError(e.message);});return()=>{active=false;};},[runId,onEvidence]);
  return <div className="memory-source-picker"><label>来源分析<select aria-label="来源分析" required value={runId} onChange={e=>onRun(e.target.value)}><option value="">选择已完成的分析</option>{runs.map(r=><option key={r.id} value={r.id}>{r.kind} · {String(r.target.pr_number||r.target.issue_number||r.target.ci_run_id||"项目")} · {new Date(r.created_at).toLocaleString()}</option>)}</select></label>{error&&<p role="alert">{error}</p>}{runId&&<fieldset><legend>选择支持这条经验的证据</legend>{evidence.map(e=><label className="evidence-option" key={e.id}><input type="checkbox" checked={selected.includes(e.id)} onChange={event=>{const next=event.target.checked?[...selected,e.id]:selected.filter(id=>id!==e.id);setSelected(next);onEvidence(next.join(","));}}/><span>{e.source_type} · {String(e.locator.path||e.locator.number||e.locator.job_id||"")}<small>{e.excerpt.slice(0,180)}</small></span></label>)}{!evidence.length&&<p className="muted">该分析没有可选证据，无法作为经验来源。</p>}</fieldset>}</div>;
}
