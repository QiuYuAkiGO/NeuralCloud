"use strict";

const RevisionView = (() => {
  const labels = {pending:"等待处理",retry:"等待重试",running:"正在修订",applied:"已生效",dry_run:"预览完成",failed:"处理失败",blocked:"等待处理",tombstoned:"已撤销",rolled_back:"已撤销",invalidated:"等待重新判断",rejected:"未通过校验"};
  const dimensions = {closeness:"亲近",trust:"信任",attachment:"依恋",recent_tension:"近期紧张"};
  const array = value => Array.isArray(value) ? value : [];
  const word = value => value == null ? "" : typeof value === "string" ? value : JSON.stringify(value);
  function node(tag, text, className) { const element=document.createElement(tag); if(text!==undefined) element.textContent=word(text); if(className) element.className=className; return element; }
  function details(title, children) { const item=node("details"); item.append(node("summary",title),...children); return item; }
  function runCard(run, onSelect) {
    const card=node("article",undefined,"card"), heading=node("div",undefined,"card-heading");
    heading.append(node("h3",run.day||"修订"),node("span",labels[run.status]||run.status,"tag")); card.append(heading);
    card.append(node("p",`${run.source_count||0} 条来源 · ${run.dry_run?"预览":"自动修订"}`,"meta"));
    if(run.last_error) card.append(node("p",run.last_error,"meta"));
    const button=node("button","查看结论与依据","quiet");button.type="button";button.addEventListener("click",()=>onSelect(run.run_id));card.append(button);return card;
  }
  function conclusion(title, rows) {
    const group=node("section"); group.append(node("h3",`${title} · ${rows.length}`));
    if(!rows.length) { group.append(node("p","本次没有新增变化。","meta"));return group; }
    const list=node("div",undefined,"revision-list");
    rows.forEach(row=>{const item=node("div",undefined,"revision-item");item.append(node("p",row.statement||row.summary));
      if(row.target||row.subject) item.append(node("p",row.target||row.subject,"meta"));
      const refs=[...array(row.evidence_ids),...array(row.counterevidence_ids)];
      if(refs.length)item.append(details("查看引用",refs.map(ref=>node("p",ref,"meta"))));list.append(item);});group.append(list);return group;
  }
  function result(data, onAction) {
    const view=node("div"), proposal=data.proposal||{};
    const gaps=array(data.coverage_gaps);if(gaps.length)view.append(node("p",`记录覆盖提示：${gaps.join("；")}`,"notice"));
    view.append(conclusion("印象",array(proposal.impressions)),conclusion("记忆",array(proposal.memories)),conclusion("关系判断",array(proposal.relationships)));
    const relationDiff=(data.diff||{}).relationships;
    if(relationDiff){const old=new Map(array(relationDiff.before).map(row=>[row.target,row]));
      for(const next of array(relationDiff.after)){const previous=old.get(next.target)||{};const group=node("section");group.append(node("h3",`${next.target} · 关系变化`));let changed=false;
        for(const [key,label] of Object.entries(dimensions)){if(previous[key]===next[key])continue;changed=true;const row=node("div",undefined,"dimension-change");row.append(node("span",label),node("strong",`${typeof previous[key]==="number"?previous[key].toFixed(3):"—"} → ${Number(next[key]).toFixed(3)}`));group.append(row);}if(changed)view.append(group);}}
    view.append(details(`清洗记录 · ${array(proposal.cleaned).length}`,array(proposal.cleaned).map(row=>node("p",`${{keep:"保留",merge:"合并",ignore:"忽略"}[row.disposition]||row.disposition}：${row.summary||row.reason}`,"revision-evidence"))));
    view.append(details(`原始来源 · ${array(data.evidence).length}`,array(data.evidence).map(row=>{const item=node("div",undefined,"revision-item");item.append(node("p",`${row.author_principal||"来源"} · ${row.occurred_at||""}`,"meta"),node("p",row.text,"revision-evidence"),node("p",row.source_id,"meta"));return item;})));
    const actions=node("div",undefined,"actions");
    for(const [op,title,allowed] of [["retry","重试本次修订",["failed","blocked","retry","rejected"].includes(data.status)],["rollback","撤销本次修订",data.status==="applied"]]){
      if(!allowed)continue;const button=node("button",title,"quiet");button.type="button";button.addEventListener("click",()=>onAction(op,data.run_id));actions.append(button);}
    view.append(actions);return view;
  }
  return {node,runCard,result};
})();

if(typeof module!=="undefined")module.exports=RevisionView;
if(typeof document!=="undefined"&&document.getElementById("config-form")){
  const byId=id=>document.getElementById(id); let configLoaded=false, selected=null, busy=false;
  async function api(path,body){const response=await fetch(`/api/daily-revision/${path}`,{method:body===undefined?"GET":"POST",headers:{"Content-Type":"application/json"},...(body===undefined?{}:{body:JSON.stringify(body)})});const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==="string"?data.detail:"操作未完成");return data;}
  function notice(text){byId("notice").textContent=text;}
  async function select(id){selected=id;const data=await api(`diff?run_id=${encodeURIComponent(id)}`);byId("selected").textContent=data.day||"";byId("detail").replaceChildren(RevisionView.result(data,action));}
  async function refresh(){try{const state=await api("status");const config=state.config||{};
    byId("connection").textContent=state.processing?"正在后台修订，对话仍可继续。":state.maintenance_paused?"维护已暂停。":state.configured?config.enabled?"每日自动修订已启用。":"模型可用，可手动预览或修订。":"尚未配置可用模型密钥。";
    const usage=((state.queue||{}).usage||[])[0];byId("usage").textContent=usage?`${usage.day} · ${usage.requests} 次模型请求 · ${usage.charged_tokens} tokens`:"暂无模型调用";
    byId("key-state").textContent=config.api_key_configured?"已配置可用密钥。":"密钥尚不可用。";
    for(const id of ["run","dry-run"])byId(id).disabled=busy||!state.configured||state.maintenance_paused;
    if(!configLoaded){byId("enabled").checked=!!config.enabled;for(const [id,key] of [["base-url","base_url"],["model","model"],["schedule","schedule_time"],["timezone","timezone"],["backfill","backfill_days"],["reasoning","reasoning_effort"]])byId(id).value=config[key]??"";configLoaded=true;}
    const runs=state.history||[];byId("run-count").textContent=`最近 ${runs.length} 次`;byId("history").replaceChildren(...(runs.length?runs.map(run=>RevisionView.runCard(run,id=>select(id).catch(error=>notice(error.message)))):[RevisionView.node("p","还没有修订记录。","empty")]));
    if(selected)await select(selected);
  }catch(error){byId("connection").textContent="暂时无法读取状态。";notice(error.message);}}
  async function action(operation,runId){if(busy)return;busy=true;try{const payload=runId?{run_id:runId}:{day:byId("day").value};const response=await api(operation,payload);notice(operation==="rollback"?"本次修订已撤销，后续有效经历已保留。":response.run_ids?.length===0?"该日期没有可处理的来源。":"任务已加入后台队列。");await refresh();}catch(error){notice(error.message);}finally{busy=false;}}
  const yesterday=new Date(Date.now()-86400000);byId("day").value=new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit"}).format(yesterday);
  byId("refresh").addEventListener("click",refresh);byId("run").addEventListener("click",()=>action("run"));byId("dry-run").addEventListener("click",()=>action("dry_run"));
  byId("config-form").addEventListener("submit",async event=>{event.preventDefault();const payload={enabled:byId("enabled").checked,base_url:byId("base-url").value,model:byId("model").value,schedule_time:byId("schedule").value,timezone:byId("timezone").value,backfill_days:Number(byId("backfill").value),reasoning_effort:byId("reasoning").value};if(byId("key-file").value.trim())payload.api_key_file=byId("key-file").value.trim();try{await api("configure",payload);byId("key-file").value="";configLoaded=false;notice("配置已保存。");await refresh();}catch(error){notice(error.message);}});
  refresh();setInterval(()=>{if(!document.hidden&&!busy)refresh();},15000);
}
