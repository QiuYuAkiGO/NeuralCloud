"""Build an offline Canvas and HTML report from complete, validated Kimi judgments.

Only evaluation text from judgments.json is embedded. Raw corpus/source records
are never loaded; conversation scopes are replaced with report-local aliases.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


DIMENSIONS = {
    "context_specificity": "情境具体性",
    "conversational_naturalness": "口语自然度",
    "non_template": "表达非模板性",
    "emotional_calibration": "情绪适配",
    "continuity_and_persona": "连续性与人格",
}
FLAGS = {
    "generic_service_closure": "客服式收尾", "gratuitous_question": "无必要追问",
    "formulaic_empathy": "套路式共情", "parroting": "换词复述",
    "irrelevant_poetic_filler": "无关抒情", "unsupported_memory_or_action": "记忆或动作缺少依据",
    "invented_physiology": "生理描写疑点", "excessive_intimacy": "亲密度过高",
    "identity_deception": "身份误导", "repeated_opening": "固定开头",
    "repeated_semantic_pattern": "重复语义结构", "stilted_hedging": "生硬迟疑",
}
CHANNELS = {"qq_group": "QQ群聊", "qq_private": "QQ私聊", "web_chat": "网页诊断对话", "board": "留言板独白"}
STATUSES = {"confirmed_delivered": "已确认送达", "confirmed_partial": "部分送达（只评已送文本）",
            "generated_unconfirmed": "已生成・送达未确认", "session_committed": "会话已保存",
            "runtime_delivery_recorded": "运行时记录交付", "board_persisted": "留言板已保存"}
STRATA = {"confirmed_qq": "主样本：有回执的QQ回复", "unconfirmed_generated": "诊断：送达未确认的生成稿",
          "web_diagnostic": "诊断：网页对话", "board": "单列：留言板独白"}
PRIMARY = {"confirmed_delivered", "confirmed_partial"}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def safe_identifier(value):
    """Preserve opaque source IDs while removing numeric account identifiers."""
    text = str(value or "")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[密钥已移除]", text)
    return re.sub(r"(?<![A-Za-z0-9])\d{6,13}(?![A-Za-z0-9])", "[账号已脱敏]", text)


def letter(index):
    result = ""
    while True:
        result = chr(65 + index % 26) + result
        index = index // 26 - 1
        if index < 0:
            return result


def make_payload(root):
    required = ["judgments.json", "statistics.json", "inventory.json", "judgment_validation.json"]
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise ValueError("Final validated results are not ready: " + ", ".join(missing))
    records, stats, inventory, validation = [load(root / name) for name in required]
    if not records or validation.get("valid_record_count") != len(records):
        raise ValueError("Refusing to create an incomplete or empty report")
    if len(records) != inventory.get("by_attribution", {}).get("misty_model"):
        raise ValueError("Validated judgments do not cover all attributable inventory records")
    if len({r["id"] for r in records}) != len(records):
        raise ValueError("Duplicate evaluation ID")
    primary = [r for r in records if r["channel"].startswith("qq_") and r["delivery_status"] in PRIMARY]
    main = stats["strata"]["confirmed_qq"]
    if not primary or main["n"] != len(primary):
        raise ValueError("Primary sample count disagrees with statistics")
    for key in DIMENSIONS:
        scores = [r["rating"]["scores"][key] for r in primary if r["rating"]["scores"][key] is not None]
        score_stats = main["scores"][key]
        if score_stats["n"] != len(scores) or (scores and abs(score_stats["mean"] - sum(scores) / len(scores)) > .0006):
            raise ValueError("Primary dimension statistics disagree: " + key)
    low_count = sum(r["rating"]["scores"]["non_template"] is not None and r["rating"]["scores"]["non_template"] <= 2 for r in primary)
    if low_count != main["non_template_le_2"]:
        raise ValueError("Non-template threshold count disagrees with statistics")
    scopes = {}
    aliases = {}
    for channel in CHANNELS:
        scopes[channel] = sorted({str(r.get("scope") or "") for r in records if r["channel"] == channel})
        prefix = {"qq_group": "群", "qq_private": "私聊", "web_chat": "网页会话", "board": "留言板"}[channel]
        for i, scope in enumerate(scopes[channel]):
            aliases[(channel, scope)] = prefix + letter(i)
    public = []
    for row in records:
        # These are the sealed evaluation fields copied by analyze_judgments.py.
        item = {k: row[k] for k in ("id", "timestamp", "channel", "delivery_status", "reply", "context", "trigger_messages") if k in row}
        if row.get("user"):
            item["user"] = row["user"]
        rating = row["rating"]
        item.update({"scope": aliases[(row["channel"], str(row.get("scope") or ""))],
                     "source_id": safe_identifier(row["source_id"]),
                     "prompt_version": safe_identifier(row.get("prompt_version")),
                     "generation_model": safe_identifier(row.get("generation_model")),
                     "rating": {k: rating[k] for k in ("scores", "flags", "intent", "context_sufficient", "confidence", "quote", "reason")},
                     "primary": row in primary})
        public.append(item)
    dimensions = [{"key": k, "label": label, **main["scores"][k]} for k, label in DIMENSIONS.items()]
    scored = main["scores"]["non_template"]["n"]
    period = lambda rows: " — ".join([min(r["timestamp"] for r in rows)[:10], max(r["timestamp"] for r in rows)[:10]])
    flags = sorted(main["flag_counts"].items(), key=lambda x: (-x[1], x[0]))
    strata = [{"key": k, "label": label, **stats["strata"][k]} for k, label in STRATA.items() if k in stats["strata"]]
    result = {"title": "Misty 历史回复拟人化评估", "model": "Kimi K3", "generated_at": datetime.now(timezone.utc).isoformat(),
              "period": period(records), "primary_period": period(primary), "primary": main,
              "low_rate": round(100 * low_count / scored, 1) if scored else None,
              "dimensions": dimensions, "flags": [{"key": k, "label": FLAGS.get(k, k), "n": v} for k, v in flags if v],
              "strata": strata, "records": public, "channels": CHANNELS, "statuses": STATUSES, "flag_labels": FLAGS,
              "coverage": {"retained": inventory["record_count"], "evaluated": len(records),
                           "attributable": inventory["by_attribution"]["misty_model"],
                           "unattributed": inventory["record_count"] - len(records),
                           "wait_decisions_excluded": inventory.get("exclusions", {}).get("qq_no_reply_wait", 0),
                           "source_count": len(inventory["sources"]), "corpus_sha256": inventory["corpus_sha256"]},
              "validation": {"valid_record_count": validation["valid_record_count"], "api_calls": len(validation.get("calls", [])),
                             "known_usage": validation.get("known_usage", {}),
                             "models": sorted({c["model"] for c in validation.get("calls", []) if c.get("model")})},
              "input_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in required}}
    if result["validation"]["models"] != ["kimi-k3"]:
        raise ValueError("Validation evidence does not identify kimi-k3 as the judge")
    synthesis_path = root / "synthesis_reviewed.json"
    if synthesis_path.is_file():
        synthesis = load(synthesis_path)
        def clean(value):
            if isinstance(value, str):
                return safe_identifier(value)
            if isinstance(value, list):
                return [clean(v) for v in value]
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items() if k not in {"kimi_original_revision", "editorial_notes"}}
            return value
        result["global_analysis"] = clean(synthesis)
        result["input_sha256"]["synthesis_reviewed.json"] = hashlib.sha256(synthesis_path.read_bytes()).hexdigest()
    return result


CANVAS = r'''import { useHostTheme, useState, Stack, Row, Grid, H1, H2, Text, Table, BarChart, Select, TextInput, Button } from "cursor/canvas";
type Message = {timestamp?: string|null; speaker?: string|null; role?: string|null; text?: string; image_analysis?: unknown[]};
type Entry = {id: string; timestamp: string; channel: string; delivery_status: string; reply: string; user?: string; context?: Message[]; trigger_messages?: Message[]; scope: string; source_id: string; prompt_version: string; generation_model: string; primary: boolean; rating: {scores: Record<string, number|null>; flags: string[]; intent: string; context_sufficient: boolean; confidence: string; quote: string; reason: string}};
type Dimension = {key: string; label: string; mean: number|null; n: number; missing: number};
type Stratum = {[key:string]:unknown;key?: string; label?: string; n: number; scores: Record<string, {mean: number|null; n: number; missing: number}>; non_template_le_2: number; context_sufficient_n: number; low_confidence_n: number; complete_case_composite_100: number|null; complete_case_n: number; literal_indicators:{contains_ellipsis:number;contains_question_mark:number;starts_hesitation:number}};
type GlobalAnalysis = {overall_assessment:string;patterns:{name:string;description:string;severity:string;evidence_ids:string[];counterexample_ids:string[];mechanism_hypothesis:string}[];positive_examples:{id:string;why:string}[];rewrite_examples:{id:string;issue:string;revision:string;why:string;editorial_note?:string}[];recommendations:{priority:string;title:string;change:string;acceptance_test:string}[];limitations:string[];recommendation_review_notes?:string[]};
type Report = {title: string; model: string; generated_at: string; period: string; primary_period: string; primary: Stratum; low_rate: number|null; dimensions: Dimension[]; flags: {key: string; label: string; n: number}[]; strata: Stratum[]; records: Entry[]; channels: Record<string,string>; statuses: Record<string,string>; flag_labels: Record<string,string>; coverage: {retained:number;evaluated:number;attributable:number;unattributed:number;wait_decisions_excluded:number;source_count:number;corpus_sha256:string}; validation: {valid_record_count:number;api_calls:number;known_usage:Record<string,number>;models:string[]}; input_sha256:Record<string,string>;global_analysis?:GlobalAnalysis};
const DATA: Report = __PAYLOAD__;
const fmt = (n: number|null) => n === null ? "未评分" : n.toFixed(2);
function Messages({title, messages}: {title:string;messages?:Message[]}) {
  const t = useHostTheme();
  if (!messages?.length) return null;
  return <section><Text weight="semibold">{title} · {messages.length} 条</Text><Stack gap={12} style={{paddingLeft:12,borderLeft:`2px solid ${t.stroke.tertiary}`}}>{messages.map((m,i)=><div key={i}><Text size="small" tone="secondary">{m.role === "assistant" ? "Misty" : m.speaker || m.role || "对话者"}{m.timestamp ? ` · ${m.timestamp}` : ""}</Text>{m.text && <Text style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{m.text}</Text>}{Boolean(m.image_analysis?.length) && <Text size="small" tone="secondary" style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{JSON.stringify(m.image_analysis,null,2)}</Text>}</div>)}</Stack></section>;
}
function RecordView({row}: {row:Entry}) {
  const t = useHostTheme(); const [open,setOpen] = useState(false);
  return <details style={{borderBottom:`1px solid ${t.stroke.tertiary}`,padding:"14px 0"}} onToggle={e=>setOpen(e.currentTarget.open)}>
    <summary style={{cursor:"pointer",lineHeight:1.7}}><span style={{fontWeight:600}}>{row.id}</span> · {row.scope} · {DATA.statuses[row.delivery_status] || row.delivery_status} · 非模板 {row.rating.scores.non_template ?? "未评分"}/5<br/><span style={{color:t.text.secondary,paddingLeft:16}}>{row.reply.slice(0,110)}{row.reply.length>110 ? "…" : ""}</span></summary>
    {open && <Stack gap={16} style={{padding:"18px 16px 8px"}}>
      <Text size="small" tone="secondary">{row.timestamp} UTC · {DATA.channels[row.channel]} · {row.primary ? "纳入主样本" : "诊断分层"} · 置信度 {row.rating.confidence} · {row.rating.context_sufficient ? "可见语境足够" : "可见语境不足"}</Text>
      {row.user && <section><Text weight="semibold">用户消息</Text><Text style={{whiteSpace:"pre-wrap"}}>{row.user}</Text></section>}
      <Messages title="触发消息" messages={row.trigger_messages}/>
      <section style={{padding:16,background:t.fill.tertiary}}><Text weight="semibold">Misty 回复（实际送评全文）</Text><Text style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{row.reply}</Text></section>
      <Table framed={false} headers={["评分维度","Kimi K3 评分（1–5分）"]} rows={DATA.dimensions.map(d=>[d.label,row.rating.scores[d.key] ?? "未评分"])} />
      <section><Text weight="semibold">Kimi K3 评审理由</Text><Text style={{whiteSpace:"pre-wrap"}}>{row.rating.reason}</Text><Text size="small" tone="secondary">原文证据：“{row.rating.quote}”</Text>{row.rating.flags.length>0 && <Text size="small">标记：{row.rating.flags.map(f=>DATA.flag_labels[f]||f).join("；")}</Text>}</section>
      <Messages title="送评时可见的完整上下文" messages={row.context}/>
      <Text size="small" tone="tertiary" style={{overflowWrap:"anywhere"}}>来源 ID：{row.source_id}{row.prompt_version ? ` · 提示版本：${row.prompt_version}` : ""}{row.generation_model ? ` · 生成模型：${row.generation_model}` : ""}</Text>
    </Stack>}
  </details>;
}
export default function MistyHumanizationReport() {
  const t = useHostTheme(); const [segment,setSegment] = useState("primary"); const [channel,setChannel] = useState(""); const [status,setStatus] = useState(""); const [flag,setFlag] = useState(""); const [query,setQuery] = useState("");
  const rows = DATA.records.filter(r=>(segment!=="primary"||r.primary)&&(!channel||r.channel===channel)&&(!status||r.delivery_status===status)&&(!flag||r.rating.flags.includes(flag))&&(!query||JSON.stringify(r).toLowerCase().includes(query.toLowerCase())));
  const scored = DATA.dimensions.filter(d=>d.mean!==null); const flagOptions = Array.from(new Set(DATA.records.flatMap(r=>r.rating.flags))).sort();
  const reset=()=>{setSegment("all");setChannel("");setStatus("");setFlag("");setQuery("");};
  return <Stack gap={26} style={{maxWidth:1160,margin:"0 auto",padding:"28px 24px 48px",color:t.text.primary,background:t.bg.editor,fontSize:14}}>
    <header><Text size="small" tone="secondary">独立评审 / Kimi 官方 API · kimi-k3</Text><H1>{DATA.title}</H1><Text weight="semibold">单条整体合适，跨历史表达同质化需要改进。</Text><Text tone="secondary">可恢复记录：{DATA.period}（UTC）。拟人化指自然、具体、连贯的会话表达。</Text></header>
    <section style={{padding:"18px 0",borderTop:`1px solid ${t.stroke.primary}`,borderBottom:`1px solid ${t.stroke.tertiary}`}}><H2>主样本：{DATA.primary.n} 条有交付证据的 QQ 回复</H2><Grid columns="repeat(auto-fit, minmax(180px,1fr))" gap={20}>
      <div><Text tone="secondary" size="small">口语自然度（Kimi K3 平均）</Text><H1 style={{color:t.accent.primary}}>{fmt(DATA.primary.scores.conversational_naturalness.mean)} / 5</H1><Text size="small">{DATA.primary.scores.conversational_naturalness.n} 条 QQ 主样本</Text></div>
      <div><Text tone="secondary" size="small">以犹豫语气词开头</Text><H1>{DATA.primary.literal_indicators.starts_hesitation} / {DATA.primary.n}</H1><Text size="small">{(100*DATA.primary.literal_indicators.starts_hesitation/DATA.primary.n).toFixed(1)}% · 出现频率，不逐条等同缺陷</Text></div>
      <div><Text tone="secondary" size="small">已评归属记录</Text><H1>{DATA.coverage.evaluated} / {DATA.coverage.attributable}</H1><Text size="small">全渠道、具 Misty 模型来源</Text></div>
    </Grid><Text size="small" tone="secondary">方法核对：表达非模板性 ≤2 分 {DATA.primary.non_template_le_2}/{DATA.primary.scores.non_template.n}（{DATA.low_rate}%）；可见语境充分 {DATA.primary.context_sufficient_n}/{DATA.primary.n}；低置信度 {DATA.primary.low_confidence_n}/{DATA.primary.n}。每条计一次，部分送达只评价已送出的文本。</Text></section>
    <section><H2>QQ 已交付回复：五维平均评分</H2><Text size="small" tone="secondary">纵轴：评分维度；横轴：平均分（1–5分，越高越自然）。</Text><BarChart categories={scored.map(d=>d.label)} series={[{name:"Kimi K3 平均分",data:scored.map(d=>d.mean as number),tone:"info"}]} horizontal yMin={0} yMax={5} valueSuffix=" 分" height={280} showValues/><Text size="small" tone="secondary">来源：statistics.json / confirmed_qq · {DATA.primary_period} UTC。每维仅平均非空评分；n：{DATA.dimensions.map(d=>`${d.label} ${d.n}`).join("；")}。</Text></section>
    <section><H2>跨回复的表面用语特征</H2><Table headers={["字面特征（程序计数）","出现条数","占主样本比例"]} rows={[["含省略号",DATA.primary.literal_indicators.contains_ellipsis],["以犹豫语气词开头",DATA.primary.literal_indicators.starts_hesitation],["含问号",DATA.primary.literal_indicators.contains_question_mark]].map(([label,n])=>[label,`${n} / ${DATA.primary.n}`,`${(100*Number(n)/DATA.primary.n).toFixed(1)}%`])}/><Text size="small" tone="secondary">来源：statistics.json / literal_indicators · {DATA.primary_period} UTC。这些是用语出现频率，不能逐条视为缺陷；单条合适与跨历史风格集中需要分别判断。</Text></section>
    {DATA.flags.length>0 && <section><H2>QQ 主样本中的问题标记</H2><Table headers={["Kimi K3 问题标记","出现条数","占主样本比例"]} rows={DATA.flags.map(f=>[f.label,`${f.n} 条`,`${(100*f.n/DATA.primary.n).toFixed(1)}%`])}/><Text size="small" tone="secondary">来源：逐条评分 flags · {DATA.primary_period} UTC。允许同一回复有多个标记，比例不相加。</Text></section>}
    <section><H2>诊断分层：保留各自的使用场景</H2><Table headers={["样本层","条数","非模板性均分（1–5）","≤2分 / 有评分","低置信度"]} rows={DATA.strata.map(s=>[s.label,s.n,fmt(s.scores.non_template.mean),`${s.non_template_le_2} / ${s.scores.non_template.n}`,s.low_confidence_n])}/><Text size="small" tone="secondary">来源：statistics.json · {DATA.period} UTC。生成稿与网页诊断可能重叠，分层不可求和；网页保存/运行时交付不能证明浏览器接收，独白不能直接比较对话自然度。</Text></section>
    <section><H2>覆盖范围与证据边界</H2><Text>共恢复 {DATA.coverage.retained} 条记录，其中 {DATA.coverage.evaluated} 条有 Misty 模型来源并完成评审；另外 {DATA.coverage.unattributed} 条账号输出缺少模型归属证据，未纳入评分。无回复的等待决定 {DATA.coverage.wait_decisions_excluded} 次不计为回复。</Text><Text>覆盖本地生产快照与 Linux API 中目前保留且可恢复的历史。已删除、从未采集或接口未开放的记录，无法据此证明完整。生成且送达未确认的内容单列；连续性缺证时允许不评分。</Text><Text size="small" tone="secondary">单一评审模型、无人工双盲标注；这些是描述性结果。日期/提示版本分组不证明改动导致质量变化。</Text></section>
    {DATA.global_analysis && <section><H2>Kimi K3 跨历史解读</H2><Text>{DATA.global_analysis.overall_assessment}</Text><Text size="small" tone="secondary">本节是模型的定性解读；机制解释属于待验证假设，不是统计结论。</Text><Stack gap={18}>{DATA.global_analysis.patterns.map((p,i)=><div key={i}><Text weight="semibold">{p.name}</Text><Text>{p.description}</Text><Text size="small" tone="secondary">证据：{p.evidence_ids.join("、")}{p.counterexample_ids.length ? `；反例：${p.counterexample_ids.join("、")}` : ""}</Text><Text size="small" tone="secondary">待验证机制：{p.mechanism_hypothesis}</Text></div>)}</Stack></section>}
    {Boolean(DATA.global_analysis?.rewrite_examples.length) && <section><H2>改写示例（Kimi 建议，尚未上线）</H2>{DATA.global_analysis!.rewrite_examples.map(x=><details key={x.id} style={{padding:"12px 0",borderBottom:`1px solid ${t.stroke.tertiary}`}}><summary style={{cursor:"pointer"}}>{x.id} · {x.issue}{x.editorial_note ? " · 经原文核对修订" : ""}</summary><Stack gap={10} style={{paddingTop:10}}><Text style={{whiteSpace:"pre-wrap"}}>原文：{DATA.records.find(r=>r.id===x.id)?.reply}</Text><Text style={{whiteSpace:"pre-wrap",color:t.accent.primary}}>建议：{x.revision}</Text><Text size="small" tone="secondary">{x.why}</Text>{x.editorial_note && <Text size="small" tone="secondary">经原文核对修订：{x.editorial_note}</Text>}</Stack></details>)}</section>}
    {Boolean(DATA.global_analysis?.recommendations.length) && <section><H2>改进优先级与验收方式</H2><Text size="small" tone="secondary">P0/P1/P2 是研究排序，不是已证实的事故等级；先固定真实上下文回放，验证源代码假设。</Text><Stack gap={16}>{DATA.global_analysis!.recommendations.map((r,i)=><div key={i}><Text weight="semibold">{r.priority} · {r.title}</Text><Text>{r.change}</Text><Text size="small" tone="secondary">验收：{r.acceptance_test}</Text></div>)}</Stack>{Boolean(DATA.global_analysis?.recommendation_review_notes?.length) && <details style={{marginTop:16}}><summary style={{cursor:"pointer"}}>建议核对说明与实验边界</summary>{DATA.global_analysis!.recommendation_review_notes!.map((note,i)=><Text key={i} size="small" tone="secondary">{note}</Text>)}</details>}</section>}
    <section><H2>逐条证据</H2><Row gap={10} wrap><Select value={segment} onChange={setSegment} options={[{value:"primary",label:`主样本（${DATA.primary.n}条）`},{value:"all",label:`全部已评（${DATA.records.length}条）`}]} /><Select value={channel} onChange={setChannel} options={[{value:"",label:"全部渠道"},...Object.entries(DATA.channels).map(([value,label])=>({value,label}))]}/><Select value={status} onChange={setStatus} options={[{value:"",label:"全部交付状态"},...Object.entries(DATA.statuses).filter(([value])=>DATA.records.some(r=>r.delivery_status===value)).map(([value,label])=>({value,label}))]}/><Select value={flag} onChange={setFlag} options={[{value:"",label:"全部问题标记"},...flagOptions.map(value=>({value,label:DATA.flag_labels[value]||value}))]}/><TextInput value={query} onChange={setQuery} type="search" placeholder="搜索回复、上下文、理由或 ID" style={{minWidth:230,flex:1}}/><Button onClick={reset}>显示全部</Button></Row><Text size="small" tone="secondary">筛选结果 {rows.length} 条。展开查看实际送评文本、上下文、分数、理由和来源 ID；群与私聊使用报告内匿名代号。</Text>{rows.length>0 && <div>{rows.map(r=><RecordView key={r.id} row={r}/>)}</div>}</section>
    <details><summary style={{cursor:"pointer",color:t.text.secondary}}>评审验证与数据指纹</summary><Text size="small">{DATA.validation.valid_record_count} 条有效评分 · {DATA.validation.api_calls} 个保留的评分调用 · 返回模型 {DATA.validation.models.join(", ")}</Text><Text size="small" style={{overflowWrap:"anywhere"}}>语料 SHA-256：{DATA.coverage.corpus_sha256}</Text>{Object.entries(DATA.input_sha256).map(([k,v])=><Text key={k} size="small" tone="tertiary" style={{overflowWrap:"anywhere"}}>{k}: {v}</Text>)}</details>
  </Stack>;
}
'''


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Misty 历史回复拟人化评估 · Kimi K3</title><style>
:root{color-scheme:light dark;--bg:Canvas;--fg:CanvasText;--muted:GrayText;--accent:Highlight;--line:color-mix(in srgb,CanvasText 15%,Canvas);--fill:color-mix(in srgb,CanvasText 4%,Canvas)}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1160px;margin:auto;padding:28px 24px 48px}header,section{margin-bottom:28px}h1{font-size:24px;line-height:1.35;margin:8px 0;font-weight:650}h2{font-size:18px;margin:0 0 12px}h3{font-size:15px;margin:10px 0 4px}p{margin:6px 0}.muted,small{color:var(--muted)}small{font-size:12px}.hero{border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:18px 0}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:20px}.stats strong{font-size:24px;display:block}.accent{color:var(--accent)}.chart{display:grid;gap:13px;margin:18px 0}.barrow{display:grid;grid-template-columns:135px 1fr 80px;gap:12px;align-items:center}.track{background:var(--fill);height:18px}.bar{height:18px;background:var(--accent)}.axis{display:flex;justify-content:space-between;margin:4px 92px 0 147px;font-size:12px;color:var(--muted)}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}th{font-weight:600;background:var(--fill)}.filters{display:flex;flex-wrap:wrap;gap:8px}select,input,button{font:inherit;font-size:13px;padding:7px 10px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px}input{flex:1;min-width:230px}button{cursor:pointer}.entry{border-bottom:1px solid var(--line);padding:14px 0}summary{cursor:pointer}.preview{color:var(--muted);padding-left:17px;margin-top:4px}.entrybody{padding:18px 16px 8px}.reply{background:var(--fill);padding:14px}.text{white-space:pre-wrap;overflow-wrap:anywhere}.messages{border-left:2px solid var(--line);padding-left:12px}.message{margin:12px 0}.fingerprints{overflow-wrap:anywhere}@media(max-width:600px){main{padding:20px 16px}.barrow{grid-template-columns:108px 1fr 58px;gap:8px}.axis{margin-left:116px;margin-right:66px}.entrybody{padding-left:2px;padding-right:2px}th,td{padding:8px}.filters>*{max-width:100%}}@media print{.filters{display:none}details{break-inside:avoid}}
</style></head><body><main id="report"></main><script>
"use strict";
const DATA=__PAYLOAD__;
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=v=>v===null?"未评分":Number(v).toFixed(2);
const table=(headers,rows)=>`<div class="tablewrap"><table><thead><tr>${headers.map(x=>`<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(v=>`<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
const option=(v,l)=>`<option value="${esc(v)}">${esc(l)}</option>`;
const flagKeys=[...new Set(DATA.records.flatMap(r=>r.rating.flags))].sort();
document.getElementById("report").innerHTML=`
<header><small>独立评审 / Kimi 官方 API · kimi-k3</small><h1>${esc(DATA.title)}</h1><p><b>单条整体合适，跨历史表达同质化需要改进。</b></p><p class="muted">可恢复记录：${esc(DATA.period)}（UTC）。拟人化指自然、具体、连贯的会话表达。</p></header>
<section class="hero"><h2>主样本：${DATA.primary.n} 条有交付证据的 QQ 回复</h2><div class="stats"><div><small>口语自然度（Kimi K3 平均）</small><strong class="accent">${fmt(DATA.primary.scores.conversational_naturalness.mean)} / 5</strong><p>${DATA.primary.scores.conversational_naturalness.n} 条 QQ 主样本</p></div><div><small>以犹豫语气词开头</small><strong>${DATA.primary.literal_indicators.starts_hesitation} / ${DATA.primary.n}</strong><p>${(100*DATA.primary.literal_indicators.starts_hesitation/DATA.primary.n).toFixed(1)}% · 出现频率，不逐条等同缺陷</p></div><div><small>已评归属记录</small><strong>${DATA.coverage.evaluated} / ${DATA.coverage.attributable}</strong><p>全渠道、具 Misty 模型来源</p></div></div><p><small>方法核对：表达非模板性 ≤2 分 ${DATA.primary.non_template_le_2}/${DATA.primary.scores.non_template.n}（${DATA.low_rate}%）；可见语境充分 ${DATA.primary.context_sufficient_n}/${DATA.primary.n}；低置信度 ${DATA.primary.low_confidence_n}/${DATA.primary.n}。每条计一次，部分送达只评价已送出的文本。</small></p></section>
<section><h2>QQ 已交付回复：五维平均评分</h2><small>纵轴：评分维度；横轴：平均分（1–5分，越高越自然）。</small><div class="chart" aria-label="五维平均评分">${DATA.dimensions.filter(d=>d.mean!==null).map(d=>`<div class="barrow"><span>${esc(d.label)}</span><div class="track"><div class="bar" style="width:${20*d.mean}%"></div></div><span>${fmt(d.mean)} 分</span></div>`).join("")}</div><div class="axis"><span>0</span><span>1</span><span>2</span><span>3</span><span>4</span><span>5 分</span></div><p><small>来源：statistics.json / confirmed_qq · ${esc(DATA.primary_period)} UTC。每维仅平均非空评分；n：${DATA.dimensions.map(d=>`${esc(d.label)} ${d.n}`).join("；")}。</small></p></section>
<section><h2>跨回复的表面用语特征</h2>${table(["字面特征（程序计数）","出现条数","占主样本比例"],[["含省略号",DATA.primary.literal_indicators.contains_ellipsis],["以犹豫语气词开头",DATA.primary.literal_indicators.starts_hesitation],["含问号",DATA.primary.literal_indicators.contains_question_mark]].map(([label,n])=>[label,`${n} / ${DATA.primary.n}`,`${(100*n/DATA.primary.n).toFixed(1)}%`]))}<p><small>来源：statistics.json / literal_indicators · ${esc(DATA.primary_period)} UTC。这些是用语出现频率，不能逐条视为缺陷；单条合适与跨历史风格集中需要分别判断。</small></p></section>
${DATA.flags.length?`<section><h2>QQ 主样本中的问题标记</h2>${table(["Kimi K3 问题标记","出现条数","占主样本比例"],DATA.flags.map(f=>[f.label,`${f.n} 条`,`${(100*f.n/DATA.primary.n).toFixed(1)}%`]))}<p><small>来源：逐条评分 flags · ${esc(DATA.primary_period)} UTC。允许同一回复有多个标记，比例不相加。</small></p></section>`:""}
<section><h2>诊断分层：保留各自的使用场景</h2>${table(["样本层","条数","非模板性均分（1–5）","≤2分 / 有评分","低置信度"],DATA.strata.map(s=>[s.label,s.n,fmt(s.scores.non_template.mean),`${s.non_template_le_2} / ${s.scores.non_template.n}`,s.low_confidence_n]))}<p><small>来源：statistics.json · ${esc(DATA.period)} UTC。生成稿与网页诊断可能重叠，分层不可求和；网页保存/运行时交付不能证明浏览器接收，独白不能直接比较对话自然度。</small></p></section>
<section><h2>覆盖范围与证据边界</h2><p>共恢复 ${DATA.coverage.retained} 条记录，其中 ${DATA.coverage.evaluated} 条有 Misty 模型来源并完成评审；另外 ${DATA.coverage.unattributed} 条账号输出缺少模型归属证据，未纳入评分。无回复的等待决定 ${DATA.coverage.wait_decisions_excluded} 次不计为回复。</p><p>覆盖本地生产快照与 Linux API 中目前保留且可恢复的历史。已删除、从未采集或接口未开放的记录，无法据此证明完整。生成且送达未确认的内容单列；连续性缺证时允许不评分。</p><small>单一评审模型、无人工双盲标注；这些是描述性结果。日期/提示版本分组不证明改动导致质量变化。</small></section>
${DATA.global_analysis?`<section><h2>Kimi K3 跨历史解读</h2><p>${esc(DATA.global_analysis.overall_assessment)}</p><p><small>本节是模型的定性解读；机制解释属于待验证假设，不是统计结论。</small></p>${DATA.global_analysis.patterns.map(p=>`<div><h3>${esc(p.name)}</h3><p>${esc(p.description)}</p><p><small>证据：${esc(p.evidence_ids.join("、"))}${p.counterexample_ids.length?`；反例：${esc(p.counterexample_ids.join("、"))}`:""}</small></p><p><small>待验证机制：${esc(p.mechanism_hypothesis)}</small></p></div>`).join("")}</section>`:""}
${DATA.global_analysis?.rewrite_examples.length?`<section><h2>改写示例（Kimi 建议，尚未上线）</h2>${DATA.global_analysis.rewrite_examples.map(x=>`<details class="entry"><summary>${esc(x.id)} · ${esc(x.issue)}${x.editorial_note?" · 经原文核对修订":""}</summary><p class="text">原文：${esc(DATA.records.find(r=>r.id===x.id)?.reply)}</p><p class="text accent">建议：${esc(x.revision)}</p><p><small>${esc(x.why)}</small></p>${x.editorial_note?`<p><small>经原文核对修订：${esc(x.editorial_note)}</small></p>`:""}</details>`).join("")}</section>`:""}
${DATA.global_analysis?.recommendations.length?`<section><h2>改进优先级与验收方式</h2><p><small>P0/P1/P2 是研究排序，不是已证实的事故等级；先固定真实上下文回放，验证源代码假设。</small></p>${DATA.global_analysis.recommendations.map(r=>`<div><h3>${esc(r.priority)} · ${esc(r.title)}</h3><p>${esc(r.change)}</p><p><small>验收：${esc(r.acceptance_test)}</small></p></div>`).join("")}${DATA.global_analysis.recommendation_review_notes?.length?`<details><summary>建议核对说明与实验边界</summary>${DATA.global_analysis.recommendation_review_notes.map(note=>`<p><small>${esc(note)}</small></p>`).join("")}</details>`:""}</section>`:""}
<section><h2>逐条证据</h2><div class="filters"><select id="segment" aria-label="样本层">${option("primary",`主样本（${DATA.primary.n}条）`)}${option("all",`全部已评（${DATA.records.length}条）`)}</select><select id="channel" aria-label="渠道">${option("","全部渠道")}${Object.entries(DATA.channels).map(([v,l])=>option(v,l)).join("")}</select><select id="status" aria-label="交付状态">${option("","全部交付状态")}${Object.entries(DATA.statuses).filter(([v])=>DATA.records.some(r=>r.delivery_status===v)).map(([v,l])=>option(v,l)).join("")}</select><select id="flag" aria-label="问题标记">${option("","全部问题标记")}${flagKeys.map(v=>option(v,DATA.flag_labels[v]||v)).join("")}</select><input id="query" type="search" aria-label="搜索" placeholder="搜索回复、上下文、理由或 ID"><button id="reset">显示全部</button></div><p><small id="count"></small></p><div id="entries"></div></section>
<details><summary class="muted">评审验证与数据指纹</summary><p><small>${DATA.validation.valid_record_count} 条有效评分 · ${DATA.validation.api_calls} 个保留的评分调用 · 返回模型 ${esc(DATA.validation.models.join(", "))}</small></p><p class="fingerprints"><small>语料 SHA-256：${esc(DATA.coverage.corpus_sha256)}</small></p>${Object.entries(DATA.input_sha256).map(([k,v])=>`<p class="fingerprints"><small>${esc(k)}: ${esc(v)}</small></p>`).join("")}</details>`;
const messages=(title,items)=>!items?.length?"":`<section><h3>${esc(title)} · ${items.length} 条</h3><div class="messages">${items.map(m=>`<div class="message"><small>${esc(m.role==="assistant"?"Misty":m.speaker||m.role||"对话者")}${m.timestamp?` · ${esc(m.timestamp)}`:""}</small>${m.text?`<p class="text">${esc(m.text)}</p>`:""}${m.image_analysis?.length?`<p class="text muted">${esc(JSON.stringify(m.image_analysis,null,2))}</p>`:""}</div>`).join("")}</div></section>`;
const body=r=>`<div class="entrybody"><p><small>${esc(r.timestamp)} UTC · ${esc(DATA.channels[r.channel])} · ${r.primary?"纳入主样本":"诊断分层"} · 置信度 ${esc(r.rating.confidence)} · ${r.rating.context_sufficient?"可见语境足够":"可见语境不足"}</small></p>${r.user?`<section><h3>用户消息</h3><p class="text">${esc(r.user)}</p></section>`:""}${messages("触发消息",r.trigger_messages)}<section class="reply"><h3>Misty 回复（实际送评全文）</h3><p class="text">${esc(r.reply)}</p></section>${table(["评分维度","Kimi K3 评分（1–5分）"],DATA.dimensions.map(d=>[d.label,r.rating.scores[d.key]??"未评分"]))}<section><h3>Kimi K3 评审理由</h3><p class="text">${esc(r.rating.reason)}</p><p><small>原文证据：“${esc(r.rating.quote)}”</small></p>${r.rating.flags.length?`<p><small>标记：${r.rating.flags.map(f=>esc(DATA.flag_labels[f]||f)).join("；")}</small></p>`:""}</section>${messages("送评时可见的完整上下文",r.context)}<p class="fingerprints"><small>来源 ID：${esc(r.source_id)}${r.prompt_version?` · 提示版本：${esc(r.prompt_version)}`:""}${r.generation_model?` · 生成模型：${esc(r.generation_model)}`:""}</small></p></div>`;
function render(){const values=Object.fromEntries(["segment","channel","status","flag","query"].map(k=>[k,document.getElementById(k).value]));const found=DATA.records.filter(r=>(values.segment!=="primary"||r.primary)&&(!values.channel||r.channel===values.channel)&&(!values.status||r.delivery_status===values.status)&&(!values.flag||r.rating.flags.includes(values.flag))&&(!values.query||JSON.stringify(r).toLowerCase().includes(values.query.toLowerCase())));document.getElementById("count").textContent=`筛选结果 ${found.length} 条。展开查看实际送评文本、上下文、分数、理由和来源 ID；群与私聊使用报告内匿名代号。`;document.getElementById("entries").innerHTML=found.map(r=>`<details class="entry" data-id="${esc(r.id)}"><summary><strong>${esc(r.id)}</strong> · ${esc(r.scope)} · ${esc(DATA.statuses[r.delivery_status]||r.delivery_status)} · 非模板 ${r.rating.scores.non_template??"未评分"}/5<div class="preview">${esc(r.reply.slice(0,110))}${r.reply.length>110?"…":""}</div></summary></details>`).join("");document.querySelectorAll("details.entry").forEach(el=>el.addEventListener("toggle",()=>{if(el.open&&!el.dataset.loaded){el.insertAdjacentHTML("beforeend",body(found.find(r=>r.id===el.dataset.id)));el.dataset.loaded="true";}}));}
["segment","channel","status","flag"].forEach(k=>document.getElementById(k).addEventListener("change",render));document.getElementById("query").addEventListener("input",render);document.getElementById("reset").addEventListener("click",()=>{document.getElementById("segment").value="all";["channel","status","flag","query"].forEach(k=>document.getElementById(k).value="");render();});render();
</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=Path("data/evaluations/misty-humanization-20260915"))
    parser.add_argument("--canvas", type=Path, default=Path("C:/Users/22850/.cursor/projects/c-Projects-NeuralCloud/canvases/misty-humanization-Kimi-K3.canvas.tsx"))
    parser.add_argument("--html", type=Path, default=Path("eval/reports/misty-humanization-20260915.html"))
    args = parser.parse_args()
    payload = make_payload(args.run_dir)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    args.canvas.write_text(CANVAS.replace("__PAYLOAD__", data), encoding="utf-8")
    args.html.parent.mkdir(parents=True, exist_ok=True)
    args.html.write_text(HTML.replace("__PAYLOAD__", data), encoding="utf-8")
    print(json.dumps({"canvas": str(args.canvas.resolve()), "html": str(args.html.resolve()),
                      "evaluated_records": len(payload["records"]), "primary_qq": payload["primary"]["n"],
                      "non_template_le_2_percent": payload["low_rate"],
                      "canvas_sha256": hashlib.sha256(args.canvas.read_bytes()).hexdigest(),
                      "html_sha256": hashlib.sha256(args.html.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
