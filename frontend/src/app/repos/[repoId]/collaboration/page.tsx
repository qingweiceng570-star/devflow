"use client";

import {useCallback, useEffect, useState} from "react";
import {useParams, useRouter} from "next/navigation";
import {api, ApiError} from "@/lib/api/client";
import WorkspaceShell from "@/components/workspace-shell";
import EvidenceChip from "@/components/evidence-chip";
import {TargetPicker, CIForPR} from "@/components/target-picker";
import MemorySourcePicker from "@/components/memory-source-picker";

type Row = {id:string; state:string; created_at:string; payload:Record<string, unknown>; version:number; payload_hash:string; action:string; expires_at:string; preconditions:Record<string, unknown>};
type Memory = {id:string; state:string; payload:{key:string; content:string; evidence_ids:string[]; expires_at:string; paths:string[]; commit_sha?:string}};
type Report = {id:string; run_id:string; document_id:string|null; period_start:string; period_end:string; timezone:string; content:{summary:string; details:{counts:Record<string,number>; evidence_id:string}; limitations:string[]}};
type Extension = {id:string; name:string; kind:string; version:number; state:string; schema_hash:string; content:Record<string,unknown>};
type Data = {actions:Row[]; memory_candidates:Memory[]; memories:Memory[]; reports:Report[]; audit:{id:string; event:string; created_at:string; entity_id:string; detail:Record<string,unknown>}[]};
const empty:Data = {actions:[],memory_candidates:[],memories:[],reports:[],audit:[]};
const sampleSkill = {description:"查询项目知识并保留证据",input_schema:{type:"object",properties:{query:{type:"string"}},required:["query"],additionalProperties:false},allowed_tools:["search_knowledge"],steps:[{id:"search",kind:"tool",tool:"search_knowledge",arguments:{query:{$from:"input.query"}}}],output_schema:{type:"object"},max_steps:3};

export default function CollaborationPage() {
  const {repoId} = useParams<{repoId:string}>();
  const router = useRouter();
  const [user,setUser] = useState<{username:string;role:string}|null>(null);
  const [name,setName] = useState("");
  const [data,setData] = useState<Data>(empty);
  const [extensions,setExtensions] = useState<Extension[]>([]);
  const [tab,setTab] = useState("workflows");
  const [showAllReports,setShowAllReports]=useState(false);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState("");
  const [notice,setNotice] = useState("");
  const [pr,setPr] = useState("");
  const [diagnostics,setDiagnostics] = useState<{checks:{capability:string;status:string;message?:string;remedy?:string}[];log_access:string}|null>(null);
  const [ci,setCi] = useState("");
  const [attempt,setAttempt] = useState("1");
  const [plan,setPlan] = useState("");
  const [from,setFrom] = useState(new Date(Date.now()-7*86400000).toISOString().slice(0,16));
  const [to,setTo] = useState(new Date().toISOString().slice(0,16));
  const [action,setAction] = useState("create_issue_comment");
  const [number,setNumber] = useState("");
  const [title,setTitle] = useState("");
  const [body,setBody] = useState("");
  const [labels,setLabels] = useState("");
  const [editing,setEditing] = useState<Row|null>(null);
  const [runId,setRunId] = useState("");
  const [memoryKey,setMemoryKey] = useState("");

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("tab");
    if (requested && ["workflows","actions","memory","extensions","audit"].includes(requested)) {
      setTab(requested);
    }
  }, []);
  const [memoryText,setMemoryText] = useState("");
  const [evidence,setEvidence] = useState("");
  const [paths,setPaths] = useState("");
  const [expires,setExpires] = useState("30");
  const [supersedes,setSupersedes] = useState<string[]>([]);
  const [extensionName,setExtensionName] = useState("knowledge_lookup");
  const [extensionKind,setExtensionKind] = useState("skill");
  const [extensionVersion,setExtensionVersion] = useState("1");
  const [config,setConfig] = useState(JSON.stringify(sampleSkill,null,2));
  const base = `/repos/${repoId}`;
  const refresh = useCallback(async()=>{
    const [info,collaboration,ext] = await Promise.all([api<{repo:{full_name:string}}>(`${base}`),api<Data>(`${base}/collaboration`),api<Extension[]>(`${base}/extensions`)]);
    setName(info.repo.full_name);setData(collaboration);setExtensions(ext);
  },[base]);
  useEffect(()=>{api<{username:string;role:string}>("/auth/me").then(setUser).then(refresh).catch(e=>{if(e instanceof ApiError&&e.status===401)router.replace("/login");else setError(e.message);});},[refresh,router]);
  async function perform(fn:()=>Promise<unknown>,message="操作已完成") {
    setBusy(true);setError("");setNotice("");
    try {await fn();await refresh();setNotice(message);} catch(e) {setError(e instanceof SyntaxError?"JSON 格式不正确，请检查括号、引号和逗号。":e instanceof Error?e.message:"操作失败");} finally {setBusy(false);}
  }
  async function start(kind:string,target:Record<string,unknown>) {
    const conversation = await api<{id:string}>("/conversations",{repo_id:repoId,title:kind==="release_check"?`发布检查 PR #${pr}`:"项目活动报告"});
    await api(`/conversations/${conversation.id}/runs`,{kind,target,message:kind==="release_check"?"请进行发布检查；保留失败、冲突和缺失证据，按固定提交汇总。":"请按指定时间范围生成可核查的项目活动报告。"});
    router.push(`${base}/conversations/${conversation.id}`);
  }
  function edit(row:Row) {
    setEditing(row);setAction(row.action);setNumber(String(row.payload.number||""));setTitle(String(row.payload.title||""));setBody(String(row.payload.body||"").replace(/\n\n<!-- devflow-action:[\s\S]*?-->$/,""));setLabels((row.payload.labels as string[]||[]).join(", "));
    document.getElementById("action-form")?.scrollIntoView({behavior:"smooth"});
  }
  async function saveAction() {
    const payload = {action,...(action!=="create_issue"?{number:Number(number)}:{}),...(action==="create_issue"?{title}:{}),...(["create_issue","create_issue_comment"].includes(action)?{body}:{}),labels:labels.split(",").map(s=>s.trim()).filter(Boolean),...(editing?{expected_version:editing.version}:{})};
    await api(`${base}/actions${editing?`/${editing.id}`:""}`,payload,editing?"PUT":"POST");
    setEditing(null);setBody("");
  }
  function downloadReport(report:Report) {
    const url=URL.createObjectURL(new Blob([JSON.stringify(report.content,null,2)],{type:"application/json"}));
    const link=document.createElement("a");link.href=url;link.download=`report-${report.id}.json`;link.click();URL.revokeObjectURL(url);
  }
  return <WorkspaceShell user={user} active="collaboration" knowledgeHref={`${base}/knowledge`} onLogout={()=>perform(async()=>{await api("/auth/logout",{});router.replace("/login");})} note={{stage:"M5–M7",title:"可核查的协作",text:"工作流保留各节点结论。项目记忆与外部写入均需审阅批准。"}}>
    <header><div><p className="eyebrow">COLLABORATION</p><h1>工作流与审批</h1><a className="link-button" href={base}>← {name||"仓库"}</a></div><button className="button secondary" onClick={()=>perform(refresh,"已刷新")} disabled={busy}>刷新状态</button></header>
    <nav className="platform-tabs" aria-label="协作模块">{[["workflows","协作与报告"],["actions","操作草稿"],["memory","项目记忆"],["extensions","扩展"],["audit","审计记录"]].map(([key,label])=><button key={key} className={`button ${tab===key?"primary":"secondary"}`} onClick={()=>setTab(key)}>{label}</button>)}</nav>
    {error&&<p role="alert" className="error-box">{error}</p>}{notice&&<p role="status" className="hint">{notice}</p>}
    {tab==="workflows"&&<>
      <section className="panel platform-form"><h2>发布检查</h2><p className="muted">PR 与 CI 按固定提交协作分析；未提供 CI 或发布策略时会显示证据不足。</p>
        <form onSubmit={e=>{e.preventDefault();perform(()=>start("release_check",{pr_number:Number(pr),...(ci?{ci_run_id:Number(ci),attempt:Number(attempt)}:{}),...(plan.trim()?{tasks:JSON.parse(plan)}:{})}));}}>
          <TargetPicker repoId={repoId} kind="pr_review" value={pr} onChange={v=>{setPr(v);setCi("");setAttempt("1");}} disabled={busy}/>
          <CIForPR repoId={repoId} pr={pr} onSelect={(run,n)=>{setCi(run);setAttempt(n);}}/>
          <details><summary>手动指定 CI（高级）</summary><label>CI run ID（可选）<input type="number" min="1" value={ci} onChange={e=>setCi(e.target.value)}/></label><label>CI attempt<input type="number" min="1" value={attempt} onChange={e=>setAttempt(e.target.value)}/></label></details>
          <button type="button" className="button secondary" disabled={busy} onClick={()=>perform(async()=>setDiagnostics(await api(`${base}/github-diagnostics`,{})),"读取能力检查完成")}>检查 GitHub 读取权限</button>
          {diagnostics&&<div role="status">{diagnostics.checks.map(c=><p key={c.capability}>{c.capability}：{c.status==="available"?"可读取":`${c.message} ${c.remedy}`}</p>)}<p>{diagnostics.log_access}</p></div>}
          <details><summary>自定义计划（最多 8 个任务）</summary><p>可选 Agent：pr_review、ci_debug、issue_triage、knowledge_qa。依赖用 depends_on 指向任务 key。</p><textarea rows={6} value={plan} onChange={e=>setPlan(e.target.value)} placeholder={'[{"key":"review","agent":"pr_review","target":{"pr_number":16},"depends_on":[],"required":true}]'}/></details>
          <button className="button primary" disabled={busy}>开始发布检查</button>
        </form>
      </section>
      <section className="panel platform-form"><h2>项目活动报告</h2><p className="muted">输入 UTC 时间，按事件发生时间统计；范围为起点包含、终点不包含。报告只统计已同步活动。</p><form onSubmit={e=>{e.preventDefault();perform(()=>start("weekly_report",{period_start:`${from}:00Z`,period_end:`${to}:00Z`,timezone:"Asia/Shanghai"}));}}><label>开始（UTC）<input type="datetime-local" required value={from} onChange={e=>setFrom(e.target.value)}/></label><label>结束（UTC）<input type="datetime-local" required value={to} onChange={e=>setTo(e.target.value)}/></label><button className="button primary" disabled={busy}>生成报告</button></form>
        {data.reports.slice(0,showAllReports?undefined:1).map(report=><article className="platform-item" key={report.id}><h3>{new Date(report.period_start).toLocaleDateString()} — {new Date(report.period_end).toLocaleDateString()}</h3><p>{report.content.summary}</p>{Object.entries(report.content.details.counts).map(([key,count])=><p key={key}>{key}：{count}</p>)}<EvidenceChip id={report.content.details.evidence_id}/><div className="platform-tabs"><button className="button secondary" onClick={()=>downloadReport(report)}>下载报告</button><button className="button secondary" disabled={busy||!!report.document_id} onClick={()=>perform(()=>api(`${base}/reports/${report.id}/publish-knowledge`,{}),"报告已作为生成内容加入知识库")}>{report.document_id?"已加入知识库":"批准加入知识库"}</button></div></article>)}
        {data.reports.length>1&&<button className="link-button" onClick={()=>setShowAllReports(!showAllReports)}>{showAllReports?"收起历史报告":`查看历史报告（${data.reports.length-1}）`}</button>}
      </section>
    </>}
    {tab==="actions"&&<>
      <section id="action-form" className="panel platform-form"><h2>{editing?`修订草稿 v${editing.version}`:"准备操作草稿"}</h2><p className="muted">创建草稿会读取远端前置状态。审批与执行是两个独立步骤；只有服务端启用的动作和仓库可以执行。</p><form onSubmit={e=>{e.preventDefault();perform(saveAction,"草稿已保存，请审阅完整正文与目标");}}>
        <label>操作<select value={action} onChange={e=>setAction(e.target.value)}><option value="create_issue_comment">发表评论</option><option value="add_labels">添加已有标签</option><option value="create_issue">创建 Issue</option><option value="close_issue">关闭 Issue</option></select></label>
        {action!=="create_issue"&&<TargetPicker repoId={repoId} kind={action==="close_issue"?"issue_triage":"action"} value={number} onChange={setNumber} disabled={busy}/>} {action==="create_issue"&&<label>标题<input required maxLength={256} value={title} onChange={e=>setTitle(e.target.value)}/></label>}
        {["create_issue","create_issue_comment"].includes(action)&&<label>完整正文<textarea aria-label="完整正文" required rows={6} maxLength={10000} value={body} onChange={e=>setBody(e.target.value)}/></label>}
        {["create_issue","add_labels"].includes(action)&&<label>已有标签（逗号分隔）<input value={labels} onChange={e=>setLabels(e.target.value)}/></label>}
        {action==="close_issue"&&<p>仅可关闭当前为 open 的 Issue，原因默认 completed；不关闭 PR。</p>}
        <button className="button primary" disabled={busy}>保存待审批草稿</button>{editing&&<button type="button" className="button secondary" onClick={()=>setEditing(null)}>取消修订</button>}
      </form></section>
      {data.actions.map(row=><article className="panel platform-item" key={row.id}><h2>{row.action} · {name} {row.payload.number?`#${row.payload.number}`:""}</h2><p>状态：<b>{row.state}</b> · v{row.version} · 有效期至 {new Date(row.expires_at).toLocaleString()}</p><p>{String(row.payload.title||"")}</p><pre className="msg-content">{String(row.payload.body||"")}</pre>{!!(row.payload.labels as string[])?.length&&<p>标签：{(row.payload.labels as string[]).join(", ")}</p>}<details><summary>版本指纹与远端前置条件</summary><code>{row.payload_hash}</code><pre>{JSON.stringify(row.preconditions,null,2)}</pre></details>
        <div className="platform-tabs">{row.state==="pending"&&<button className="button primary" disabled={busy} onClick={()=>perform(()=>api(`${base}/actions/${row.id}/approve`,{version:row.version,payload_hash:row.payload_hash}),"已批准此版本；尚未发送")}>批准上述完整内容</button>}{row.state==="approved"&&<button className="button primary" disabled={busy} onClick={()=>perform(()=>api(`${base}/actions/${row.id}/execute`,{}),"执行请求已排队，请刷新核查结果")}>执行已批准操作</button>}{["pending","approved"].includes(row.state)&&<><button className="button secondary" disabled={busy} onClick={()=>edit(row)}>修订并清除批准</button><button className="button secondary" disabled={busy} onClick={()=>perform(()=>api(`${base}/actions/${row.id}/revoke`,{}))}>{row.state==="pending"?"拒绝":"撤销"}</button></>}{row.state==="unknown"&&<button className="button secondary" disabled={busy} onClick={()=>perform(()=>api(`${base}/actions/${row.id}/reconcile`,{}),"已核查；未能确认时仍保留 unknown，不重发")}>只核查远端结果</button>}</div>
      </article>)}
    </>}
    {tab==="memory"&&<>
      <section className="panel platform-form"><h2>提交项目经验候选</h2><form onSubmit={e=>{e.preventDefault();perform(()=>api(`${base}/memories/candidates`,{run_id:runId,key:memoryKey,content:memoryText,evidence_ids:evidence.split(/[\s,]+/).filter(Boolean),paths:paths.split(",").map(s=>s.trim()).filter(Boolean),expires_at:new Date(Date.now()+Number(expires)*86400000).toISOString()}));}}>
        <MemorySourcePicker repoId={repoId} runId={runId} onRun={setRunId} onEvidence={setEvidence}/><label>经验键（用于冲突检测）<input required maxLength={120} value={memoryKey} onChange={e=>setMemoryKey(e.target.value)}/></label><label>经验内容<textarea required maxLength={3000} rows={4} value={memoryText} onChange={e=>setMemoryText(e.target.value)}/></label><label>适用路径（可选，逗号分隔，如 src/*）<input value={paths} onChange={e=>setPaths(e.target.value)}/></label><label>有效天数<input type="number" min="1" max="365" required value={expires} onChange={e=>setExpires(e.target.value)}/></label><button className="button primary" disabled={busy||!runId||!evidence}>提交候选</button>
      </form></section>
      {data.memory_candidates.map(row=><article className="panel platform-item" key={row.id}><h3>{row.payload.key} · {row.state}</h3><p>{row.payload.content}</p><p>适用路径：{row.payload.paths.join(", ")||"全仓库"} · 到期：{new Date(row.payload.expires_at).toLocaleString()}</p>{row.payload.evidence_ids.map(id=><EvidenceChip key={id} id={id}/>)}{row.state==="pending"&&<><p>同键现有记忆（批准替代时勾选）：</p>{data.memories.filter(m=>m.state==="active"&&m.payload.key===row.payload.key).map(m=><label key={m.id}><input type="checkbox" checked={supersedes.includes(m.id)} onChange={e=>setSupersedes(e.target.checked?[...supersedes,m.id]:supersedes.filter(id=>id!==m.id))}/>{m.payload.content}</label>)}<div className="platform-tabs"><button className="button primary" disabled={busy} onClick={()=>perform(()=>api(`${base}/memories/candidates/${row.id}/decision`,{decision:"approved",supersedes:supersedes.filter(id=>data.memories.some(m=>m.id===id&&m.payload.key===row.payload.key))}))}>批准经验</button><button className="button secondary" disabled={busy} onClick={()=>perform(()=>api(`${base}/memories/candidates/${row.id}/decision`,{decision:"rejected"}))}>拒绝</button></div></>}</article>)}
      <section className="panel"><h2>已审批记忆</h2>{data.memories.map(row=><article className="platform-item" key={row.id}><h3>{row.payload.key} · {row.state}</h3><p>{row.payload.content}</p>{row.state==="active"&&<button className="button secondary" disabled={busy} onClick={()=>perform(()=>api(`${base}/memories/${row.id}/revoke`,{}))}>撤销经验</button>}</article>)}</section>
    </>}
    {tab==="extensions"&&<>
      <section className="panel platform-form"><h2>安装仓库扩展</h2><p>仅仓库管理员可安装。MCP 地址需先配置在服务端白名单，且只允许已审查的只读工具。新版本替代旧版本，运行中的任务保留已选版本。</p><form onSubmit={e=>{e.preventDefault();perform(()=>api(`${base}/extensions`,{name:extensionName,kind:extensionKind,version:Number(extensionVersion),content:JSON.parse(config)}));}}><label>名称<input required pattern="[a-z][a-z0-9_]{0,19}" value={extensionName} onChange={e=>setExtensionName(e.target.value)}/></label><label>版本<input type="number" min="1" required value={extensionVersion} onChange={e=>setExtensionVersion(e.target.value)}/></label><label>扩展类型<select value={extensionKind} onChange={e=>{setExtensionKind(e.target.value);setConfig(JSON.stringify(e.target.value==="skill"?sampleSkill:{endpoint:"https://your-server.example/mcp",allowed_tools:["search"],repo_argument:"repository",reviewed_read_only:true},null,2));}}><option value="skill">声明式 Skill</option><option value="mcp">只读 MCP 服务</option></select></label><label>扩展配置（JSON）<textarea rows={14} required value={config} onChange={e=>setConfig(e.target.value)}/></label><button className="button primary" disabled={busy}>校验并安装此版本</button></form></section>
      {extensions.map(row=><article className="panel platform-item" key={row.id}><h3>{row.name} v{row.version} · {row.kind} · {row.state}</h3><details><summary>查看固定配置</summary><pre className="msg-content">{JSON.stringify(row.content,null,2)}</pre><code>{row.schema_hash}</code></details>{row.state!=="disabled"&&<button className="button secondary" disabled={busy} onClick={()=>perform(()=>api(`${base}/extensions/${row.id}/disable`,{}))}>停用此版本</button>}</article>)}
    </>}
    {tab==="audit"&&<section className="panel"><h2>最近 100 条审计记录</h2>{data.audit.map(row=><details className="platform-item" key={row.id}><summary>{new Date(row.created_at).toLocaleString()} · {row.event}</summary><p>对象：{row.entity_id}</p><pre className="msg-content">{JSON.stringify(row.detail,null,2)}</pre></details>)}{!data.audit.length&&<p className="empty">尚无审计事件。</p>}</section>}
  </WorkspaceShell>;
}
