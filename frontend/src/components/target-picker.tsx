"use client";
import {useEffect, useState} from "react";
import {api} from "@/lib/api/client";

export type TargetOptions = {pulls:{number:number;title:string;state:string}[];issues:{number:number;title:string;state:string}[];ci_runs:{run_id:number;attempt:number;name:string;conclusion:string|null}[];coverage:string};
export function TargetPicker({repoId,kind,value,onChange,disabled=false,onAttempt,attempt}:{repoId:string;kind:string;value:string;onChange:(value:string)=>void;disabled?:boolean;onAttempt?:(value:string)=>void;attempt?:string}) {
  const [options,setOptions]=useState<TargetOptions|null>(null);
  const [manual,setManual]=useState(false);
  const [error,setError]=useState("");
  useEffect(()=>{
    let active=true;
    setOptions(null);
    setManual(false);
    setError("");
    api<TargetOptions>(`/repos/${repoId}/analysis-options`).then(data=>{if(active)setOptions(data);}).catch(()=>{if(active){setManual(true);setError("候选暂不可用，可手动填写目标编号。");}});
    return()=>{active=false;};
  },[repoId]);
  const rows=kind==="ci_debug"?options?.ci_runs.map(c=>({value:`${c.run_id}:${c.attempt}`,target:String(c.run_id),label:`${c.name} · ${c.conclusion||"进行中"} · attempt ${c.attempt}`,attempt:c.attempt})): (kind==="action"&&options?[...options.issues,...options.pulls]:kind==="issue_triage"?options?.issues:options?.pulls)?.map(r=>({value:String(r.number),target:String(r.number),label:`#${r.number} ${r.title} · ${r.state}`,attempt:undefined}));
  const selectedValue=kind==="ci_debug"&&value?`${value}:${attempt||"1"}`:value;
  const showManual=manual||!!value&&!!rows&&!rows.some(r=>r.value===selectedValue);
  const label=kind==="ci_debug"?"CI 运行":kind==="issue_triage"?"Issue":kind==="action"?"Issue / PR":"PR";
  return <div className="target-picker"><label>选择{label}<select aria-label={`选择${label}`} disabled={disabled} value={showManual?"manual":selectedValue} onChange={e=>{if(e.target.value==="manual"){setManual(true);return;}setManual(false);const row=rows?.find(r=>r.value===e.target.value);onChange(row?.target||e.target.value);if(row?.attempt)onAttempt?.(String(row.attempt));}}><option value="">请选择已同步的{label}</option>{rows?.map((r,i)=><option key={`${r.value}-${i}`} value={r.value}>{r.label}</option>)}<option value="manual">手动填写其他目标</option></select></label>{showManual&&<label>{label}编号<input required inputMode="numeric" value={value} disabled={disabled} onChange={e=>onChange(e.target.value)}/></label>}<small className="muted">{error||options?.coverage||"正在加载候选…"}</small></div>;
}

export function CIForPR({repoId,pr,onSelect}:{repoId:string;pr:string;onSelect:(run:string,attempt:string)=>void}) {
  const [items,setItems]=useState<{run_id:number;attempt:number;name:string;conclusion:string|null}[]>([]);
  const [hint,setHint]=useState("");
  useEffect(()=>{
    let active=true;
    setItems([]);
    if(!/^\d+$/.test(pr)||Number(pr)<1){setHint("");return;}
    setHint("正在查询此 PR 提交的 CI…");
    const timer=setTimeout(()=>api<{items:typeof items;has_more:boolean}>(`/repos/${repoId}/ci-options?pr_number=${pr}`).then(data=>{if(active){setItems(data.items);setHint(data.items.length?(data.has_more?"仍有未读取的 CI，列表未覆盖全部运行":"已按当前 PR 提交匹配 CI"):"此提交未查到 CI；可先检查权限或继续自动匹配。");}}).catch(e=>{if(active){setItems([]);setHint(e instanceof Error?e.message:"CI 查询失败");}}),300);
    return()=>{active=false;clearTimeout(timer);};
  },[repoId,pr]);
  return <label>同提交 CI<select defaultValue="" onChange={e=>{const item=items.find((_,i)=>String(i)===e.target.value);onSelect(item?String(item.run_id):"",item?String(item.attempt):"1");}} key={pr}><option value="">运行时自动匹配</option>{items.map((item,i)=><option key={`${item.run_id}-${item.attempt}`} value={i}>{item.name} · {item.conclusion||"进行中"} · 第 {item.attempt} 次</option>)}</select><small className="muted">{hint}</small></label>;
}
