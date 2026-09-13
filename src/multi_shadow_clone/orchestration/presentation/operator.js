"use strict";
const $ = (id) => document.getElementById(id);
const csrf = document.querySelector('meta[name="multi-shadow-clone-csrf"]').content;
let snapshot = null, refreshedAt = 0, busy = false, connected = false;
let formSubmitting = false;
const roleOpen = new Set();
const resultOpen = new Set();
const names = {running:"実行中",accepted:"検査合格",audit_pending:"監査待ち",pending:"待機",needs_revision:"修正待ち",completed:"完了",stopped:"停止受付済み",unknown:"結果・生存が不明",quarantined:"隔離",paused_budget:"回数上限",paused_deadline:"期限上限",blocked:"保留",blocked_preflight:"利用条件の確認待ち",blocked_provider:"実行を確定できません",blocked_validation:"検査で不合格",blocked_contract_changed:"契約が変わりました",blocked_artifact_changed:"成果が変わりました",erased:"消去済み",exhausted:"修正回数上限",no_progress:"進展なし"};
function element(tag, text, className) { const el = document.createElement(tag); if(text!==undefined) el.textContent=text; if(className) el.className=className; return el; }
function badge(state) { return element("span",names[state]||state,"badge "+(["running","unknown"].includes(state)?state:"")); }
function offline() {
  connected=false;
  $("weekly-usage").textContent="画面の接続が切れています。最新の利用率は不明です";
  $("connection").textContent="接続を確認できません"; $("connection").className="error"; $("live").textContent="—"; $("unknown").textContent="—";
  for(const el of document.querySelectorAll(".running")) el.classList.remove("running");
  for(const el of document.querySelectorAll(".activity")) el.textContent="生存を確認できません";
  for(const el of document.querySelectorAll("[data-action]")) el.disabled=true;
  $("save-request").disabled=true;
}
function roles() {
  if(!snapshot) return;
  const query=$("search").value.trim().toLocaleLowerCase();
  const selected=snapshot.roles.filter(r=>(r.id+" "+r.name+" "+r.group+" "+r.trigger).toLocaleLowerCase().includes(query));
  $("role-list").replaceChildren();
  for(const role of selected) {
    const row=element("details",undefined,"role "+(connected?role.activity:"unknown")), summary=element("summary");
    row.open=roleOpen.has(role.id); summary.dataset.focus="role-"+role.id;
    summary.addEventListener("click",()=>{if(row.open) roleOpen.delete(role.id); else roleOpen.add(role.id);});
    summary.append(element("span",!connected?"生存を確認できません":role.activity==="running"?"● 生存通知あり":role.activity==="unknown"?"◐ 結果・生存が不明":"○ 待機","activity"),element("h3",role.id+" · "+role.name),element("p",role.group+" / "+(role.lifecycle==="candidate"?"能力評価前の候補":role.lifecycle)));
    row.append(summary,element("p","呼ぶとき："+role.trigger),element("p","返すもの："+role.output),element("p","判断の姿勢："+role.behavior));
    $("role-list").append(row);
  }
  $("role-count").textContent=selected.length+"役を表示 / 全"+snapshot.roles.length+"役";
}
function modelLabel(job,role) {
  const contract=job.provider.roles?.[role]||job.provider;
  return contract.model ? contract.model+" / "+contract.effort : "モデル情報なし";
}
function jobGraph(job) {
  const ns="http://www.w3.org/2000/svg", wrap=element("div",undefined,"job-graph");
  const svg=document.createElementNS(ns,"svg");svg.setAttribute("role","img");svg.setAttribute("aria-label","担当の依存関係。矢印の先は前の担当の成果を待ちます");
  const depth=Object.fromEntries(job.nodes.map(n=>[n.id,0]));
  for(let pass=0;pass<job.nodes.length;pass++) for(const node of job.nodes) depth[node.id]=Math.max(0,...node.dependencies.map(id=>(depth[id]||0)+1));
  const rows={},pos={};let maxRows=1,maxDepth=0;
  for(const node of job.nodes) {const d=depth[node.id],r=rows[d]||0;rows[d]=r+1;pos[node.id]=[20+d*270,20+r*135];maxRows=Math.max(maxRows,r+1);maxDepth=Math.max(maxDepth,d);}
  svg.setAttribute("viewBox",`0 0 ${maxDepth*270+280} ${maxRows*135+20}`);
  svg.setAttribute("width",maxDepth*270+280);svg.setAttribute("height",maxRows*135+20);
  function shape(tag,attrs,text) {const el=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,v);if(text!==undefined)el.textContent=text;svg.append(el);return el;}
  for(const node of job.nodes) for(const id of node.dependencies) {
    if(!pos[id]) continue;const [x,y]=pos[id],[endX,endY]=pos[node.id];
    shape("path",{d:`M ${x+235} ${y+45} C ${x+255} ${y+45},${endX-20} ${endY+45},${endX-5} ${endY+45}`,fill:"none",stroke:"#6b8377","stroke-width":2});
    shape("path",{d:`M ${endX-12} ${endY+39} L ${endX-4} ${endY+45} L ${endX-12} ${endY+51}`,fill:"none",stroke:"#6b8377","stroke-width":2});
  }
  for(const node of job.nodes) {const [x,y]=pos[node.id],active=job.active.find(a=>a.node_id===node.id);
    const label=active?(active.activity==="running"?"実行中": "結果・生存が不明"):(names[node.state]||node.state);
    shape("rect",{x,y,width:235,height:105,rx:8,fill:active?.activity==="running"?"#d5f5df":"#eef1ee",stroke:"#799487"});
    shape("text",{x:x+12,y:y+24,"font-size":14,fill:"#18362b"},node.role_id+" · "+node.id);
    shape("text",{x:x+12,y:y+47,"font-size":12,fill:"#365346"},label);
    shape("text",{x:x+12,y:y+69,"font-size":11,fill:"#365346"},modelLabel(job,node.role_id));
    shape("text",{x:x+12,y:y+91,"font-size":11,fill:"#365346"},node.audit_role?"監査："+node.audit_role:"独立監査の担当なし");
  }
  wrap.append(svg);return wrap;
}
function jobs() {
  const list=$("job-list"); list.replaceChildren();
  if(!snapshot.jobs.length) list.append(element("p","まだ保存した仕事はありません。上の入力欄から依頼を保存してください。","empty"));
  for(const job of snapshot.jobs) {
    const article=element("article",undefined,"job"), head=element("div",undefined,"job-head"), title=element("div");
    title.append(element("h3",job.objective),element("p",job.id,"job-id"));
    const stateBadge=badge(job.state);
    if(job.state==="running") {stateBadge.classList.remove("running"); stateBadge.textContent=job.active.length?"処理中・担当の状態は下へ":"実行待ち";}
    if(job.workspace_retirement) stateBadge.textContent="退役・再開不可";
    else if(!job.acceptance.compatible) stateBadge.textContent="過去の記録・再利用不可";
    head.append(title,stateBadge);
    article.append(head,element("p",`送信 ${job.turns} / ${job.limits.max_turns} · ${job.provider.kind}`));
    if(job.next_step) article.append(element("p",job.next_step,"history-note"));
    if(!job.acceptance.compatible && !job.workspace_retirement) article.append(element("p",job.erased?"この仕事の本文は消去されています。":"入力・役・検査プログラムなどが現在と異なります。再利用する場合は、新しい依頼として保存・検査します。","history-note"));
    const nodes=element("ul",undefined,"nodes");
    for(const node of job.nodes) {
      const active=job.active.find(a=>a.node_id===node.id&&a.activity==="running");
      const row=element("li",undefined,"node "+(active?"running":""));
      row.append(element("strong",node.id+" / "+node.role_id),element("small",names[node.state]||node.state),element("small",modelLabel(job,node.role_id)),element("small",node.dependencies.length?"先に待つ："+node.dependencies.join("、"):"前の担当への依存なし")); nodes.append(row);
    }
    if(job.execution_settings) article.append(element("p","この仕事の設定版："+job.execution_settings.revision));
    article.append(jobGraph(job),nodes);
    for(const result of job.results) {
      const key=job.id+"-"+result.node_id, detail=element("details",undefined,"result"), summary=element("summary","検査した成果を読む · "+result.node_id);
      detail.open=resultOpen.has(key); summary.dataset.focus="result-"+key;
      summary.addEventListener("click",()=>{if(detail.open) resultOpen.delete(key); else resultOpen.add(key);});
      detail.append(summary,element("p",result.output.text,"answer"));
      if(result.output.limits.length) detail.append(element("p","適用範囲・限界："+result.output.limits.join(" / ")));
      if(Object.keys(result.output.values).length) detail.append(element("pre",JSON.stringify(result.output.values,null,2)));
      article.append(detail);
    }
    const cleanupNames={archived:"アーカイブ済み",released_unmaterialized:"未永続タスクを解放済み",cleanup_pending:"後片付けを確認できません"};
    if(job.cleanup?.length) {
      const cleanup=element("ul");
      for(const item of job.cleanup) cleanup.append(element("li",(cleanupNames[item.state]||"後片付けの状態が不明")+" · "+item.thread_id));
      article.append(element("p","後片付け（仕事の成果とは別の状態）"),cleanup);
    }
    if(job.workspaces?.length) {
      const labels={retiring:"退役処理中（再開不可）",retired:"退役済み"};
      article.append(element("p","作業場所："+job.workspaces.join("、")+" · "+(labels[job.workspace_retirement?.state]||"利用権は退役時に解放")));
    }
    const actions=element("div",undefined,"actions");
    for(const [operation,label] of [["run","実行"],["stop","停止"],["resume","停止を解除"],["reconcile","不明な結果を照合"],["cleanup","後片付けだけ再試行"],["retire-workspace","作業場所を退役（再開不可）"]]) {
      const button=element("button",label,operation==="stop"?"danger":""); button.type="button"; button.dataset.action=operation;
      button.dataset.focus=job.id+"-"+operation;
      button.disabled=!connected||job.erased||(operation==="retire-workspace"&&(!job.can_retire_workspace||snapshot.driver?.state==="driving"))||(operation==="stop"&&!!job.workspace_retirement)||(job.execution_available===false&&operation!=="stop")||(operation==="cleanup"&&(!job.can_cleanup||snapshot.driver?.state==="driving"))||(["run","resume"].includes(operation)&&!job.acceptance.compatible)||(operation==="resume"&&!job.can_resume)||(operation==="run"&&(job.stopped||job.state!=="running"||job.active.length>0||snapshot.driver?.state==="driving"))||(operation==="reconcile"&&!job.active.some(a=>a.activity!=="running"));
      button.addEventListener("click",()=>act(operation,job.id)); actions.append(button);
    }
    article.append(actions); list.append(article);
  }
}
function deliveries() {
  const delivery=snapshot.delivery, list=$("delivery-list"); list.replaceChildren();
  const phases={running:"稼働中",retired:"停止・退役済み",not_created:"未作成",creating:"作成中",created:"作成済み",starting:"起動中",started:"起動済み",seeding:"初期データ作成中",seeded:"初期データ作成済み",stopping:"停止中",service_stopped:"サービス停止済み",cleaning:"退役の照合中"};
  $("delivery-state").textContent="専用環境："+(phases[delivery.environment_phase]||delivery.environment_phase)+(delivery.stopped?" / 配送の新規受付は停止中":"");
  for(const item of delivery.operations) {
    const row=element("article"); row.append(element("strong",item.state==="confirmed"?"投稿の受領証あり":item.state==="reserved"?"受付済み・まだ送信していません":item.state==="cancelled"?"取消済み・送信していません":"投稿の結果が不明"),element("p",item.reference.run_id+" / "+item.reference.node_id),element("p","送信・再送の開始："+item.submissions+"回"+(item.remote_id?" / 投稿ID："+item.remote_id:"")));
    if(!item.environment_current) row.append(element("p","過去の専用環境の記録です。現在も投稿先が稼働しているという意味ではありません。"));
    list.append(row);
  }
  if(!delivery.operations.length) list.append(element("p","この実行方式には投稿の記録がありません。"));
}
async function refresh() {
  if(busy) return; busy=true;
  try {
    const response=await fetch("/api/state",{cache:"no-store",signal:AbortSignal.timeout(5000)});
    if(!response.ok) throw new Error("connection failed"); snapshot=await response.json(); refreshedAt=Date.now(); connected=true;
    $("connection").textContent="接続中 · "+new Date().toLocaleTimeString("ja-JP"); $("connection").className="";
    $("mode").textContent=snapshot.mode==="codex"?"Codexサブスクリプション / 実行には契約枠を使用":"OFFLINE / 固定応答の操作練習";
    $("live").textContent=snapshot.live_roles; $("unknown").textContent=snapshot.unknown_attempts; $("total").textContent=snapshot.roles.length;
    $("save-request").disabled=formSubmitting;
    const focus=document.activeElement?.dataset.focus;
    const usage=snapshot.usage;
    $("weekly-usage").textContent=usage?.available ? `使用 ${usage.used_percent}% · 残り ${usage.remaining_percent}% · リセット ${usage.reset_at ? new Date(usage.reset_at*1000).toLocaleString("ja-JP") : "不明"} · 取得 ${new Date(usage.observed_at*1000).toLocaleString("ja-JP")}${usage.stale ? "（古い情報）" : ""}` : "週次利用枠をまだ取得できていません";
    jobs(); roles(); deliveries();
    if(focus) [...document.querySelectorAll("[data-focus]")].find(el=>el.dataset.focus===focus)?.focus({preventScroll:true});
    if(snapshot.driver?.state==="failed") $("message").textContent="実行の呼び出しが失敗しました："+snapshot.driver.error+"。仕事の状態を確認してください。";
  } catch(error) { offline(); } finally { busy=false; }
}
async function act(operation,run_id) {
  $("message").textContent="操作を受け付けています…";
  try {
    const response=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json","X-Multi-Shadow-Clone-CSRF":csrf},body:JSON.stringify({operation,run_id}),signal:AbortSignal.timeout(5000)});
    const data=await response.json(); if(!response.ok) throw new Error(data.error||"操作を受け付けられませんでした");
    $("message").textContent=operation==="resume"?"停止を解除しました。開始するときは「実行」を押してください。":data.state==="accepted_for_execution"?"操作を受け付けました。仕事の状態で進捗を確認できます。":"受付結果："+(names[data.state]||data.state)+"。";
  } catch(error) { $("message").textContent=error.message; }
  await refresh();
}
$("request-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); if(!connected||formSubmitting) return;
  formSubmitting=true; $("save-request").disabled=true;
  try {
    const response=await fetch("/api/requests",{method:"POST",headers:{"Content-Type":"application/json","X-Multi-Shadow-Clone-CSRF":csrf},body:JSON.stringify({objective:$("objective").value,source:$("source").value,max_turns:Number($("max-turns").value)}),signal:AbortSignal.timeout(5000)});
    const data=await response.json(); if(!response.ok) throw new Error(data.error||"依頼を保存できませんでした");
    $("request-message").textContent="保存しました。下の仕事一覧から実行できます。ID："+data.id;
  } catch(error) {$("request-message").textContent="保存結果を確認してください："+error.message;}
  finally {formSubmitting=false; $("save-request").disabled=!connected;}
  await refresh();
});
$("search").addEventListener("input",roles); $("refresh").addEventListener("click",refresh);
setInterval(refresh,3000); setInterval(()=>{if(refreshedAt&&Date.now()-refreshedAt>15000) offline();},1000); refresh();

let modelSettings=null;
function modelEfforts(selected) {
  $("model-effort").replaceChildren();
  for(const effort of modelSettings.catalog[$("model-choice").value]||[]) {
    const option=element("option",effort); option.value=effort; $("model-effort").append(option);
  }
  if(selected) $("model-effort").value=selected;
}
function selectRoleModel() {
  if(!modelSettings) return;
  const role=$("model-role").value, value=modelSettings.value;
  const choice=value.overrides[role]||value.default;
  $("model-choice").value=choice.model; modelEfforts(choice.effort);
  $("inherit-model").disabled=!role;
}
async function loadModels() {
  try {
    const response=await fetch("/api/settings",{signal:AbortSignal.timeout(5000)});
    const data=await response.json();
    if(!response.ok||!data.available) throw new Error("この実行方式では設定を取得できません");
    modelSettings={...data,value:data.value||{default:{model:"gpt-5.6-luna",effort:"low"},overrides:{}}};
    $("model-role").replaceChildren(element("option","共通設定")); $("model-role").firstChild.value="";
    for(const role of snapshot?.roles||[]) {const option=element("option",role.id+" · "+role.name);option.value=role.id;$("model-role").append(option);}
    $("model-choice").replaceChildren();
    for(const model of Object.keys(data.catalog)) {const option=element("option",model);option.value=model;$("model-choice").append(option);}
    selectRoleModel(); $("save-models").disabled=false;
    $("model-message").textContent="保存版："+data.revision+"。設定は読込済みです。";
  } catch(error) {$("model-message").textContent=error.message;$("save-models").disabled=true;}
}
async function saveModels(inherit=false) {
  if(!modelSettings) return;
  $("save-models").disabled=true; $("inherit-model").disabled=true;
  const value=JSON.parse(JSON.stringify(modelSettings.value)), role=$("model-role").value;
  const choice={model:$("model-choice").value,effort:$("model-effort").value};
  if(role) {if(inherit) delete value.overrides[role]; else value.overrides[role]=choice;} else value.default=choice;
  try {
    const response=await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json","X-Multi-Shadow-Clone-CSRF":csrf},body:JSON.stringify({value,expected_revision:modelSettings.revision}),signal:AbortSignal.timeout(5000)});
    const data=await response.json();if(!response.ok) throw new Error(data.error||"保存できませんでした");
    await loadModels();$("model-message").textContent="設定版"+data.revision+"を保存しました。次に作る仕事へ適用します。既存の仕事の設定は変わりません。";
  } catch(error) {$("model-message").textContent=error.message+"。再読込して確認してください。";}
}
$("load-models").addEventListener("click",loadModels);
$("model-role").addEventListener("change",selectRoleModel);
$("model-choice").addEventListener("change",()=>modelEfforts());
$("model-form").addEventListener("submit",event=>{event.preventDefault();saveModels();});
$("inherit-model").addEventListener("click",()=>saveModels(true));

$("refresh-usage").addEventListener("click",async()=>{
  $("refresh-usage").disabled=true;
  try {
    const response=await fetch("/api/usage",{method:"POST",headers:{"Content-Type":"application/json","X-Multi-Shadow-Clone-CSRF":csrf},body:"{}",signal:AbortSignal.timeout(30000)});
    const data=await response.json();if(!response.ok) throw new Error(data.error||"利用枠を取得できません");
    await refresh();
  } catch(error) {$("weekly-usage").textContent=error.message;}
  finally {$("refresh-usage").disabled=false;}
});
