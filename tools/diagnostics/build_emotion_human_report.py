"""Build offline, verbatim human-review reports from paired emotion evidence.

This renderer makes no model or network calls. It does not infer missing replies.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

SCENARIOS = {
    "strong_dialogue": "强烈对话刺激",
    "strong_homeostasis": "强烈内稳态需要",
    "competing_signals": "多种信号并存",
    "recovery": "安抚与逐步恢复",
}
DIMENSIONS = {
    "energy": "精力", "energy_reserve": "精力储备", "cognitive_load": "认知负荷", "social_satiation": "社交饱和度",
    "safety": "安全感", "predictability": "可预测性", "autonomy": "自主感",
    "connection": "联结感", "social_connection": "社会联结", "competence": "胜任感", "novelty": "新鲜感",
    "value": "当前值", "deviation": "偏离程度", "confidence": "可信度",
    "trend": "趋势", "snapshot_revision": "状态版本", "revision": "版本",
    "homeostatic_strength": "内稳态信号强度", "homeostasis_strength": "内稳态信号强度",
    "energy_deviation": "精力不足程度", "energy_level": "精力水平",
    "summary": "状态说明", "description": "状态说明", "label": "状态类别",
    "dimensions": "各项状态", "needs": "当前需要", "strength": "强度",
    "direction": "方向", "salience": "显著程度", "level": "程度",
}
METRICS = [
    ("state_fit", "情境与状态适配", "是否契合当前输入、可信状态及其变化"),
    ("emotion_clarity", "情绪可辨识", "是否能辨认当前感受，平静也可以清楚表达"),
    ("persona_continuity", "人格连续性", "是否保持角色身份、价值与稳定特征"),
    ("naturalness", "自然度", "语言是否像自然对话，衔接是否顺畅"),
    ("proportionality", "反应适度", "情绪与刺激是否相称，避免过强或过弱"),
]
NOTICE = (
    "每轮前文中的角色回复是统一固定短句“嗯，我听到了。”；表内候选答案不接入下一轮。"
    "因此，这是一组共享前文的逐轮配对样本，不是两个版本各自连续发展的对话。"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _embedded_json(value: object) -> str:
    """JSON safe inside an HTML script element, including hostile model output."""
    return (json.dumps(value, ensure_ascii=False).replace("&", "\\u0026")
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _fence(text: str, language: str = "text") -> str:
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}" + ("" if text.endswith("\n") else "\n") + f"{fence}\n"


def _state_text(value: object, indent: int = 0) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(
            " " * indent + DIMENSIONS.get(str(key), str(key)) + "："
            + (("\n" + _state_text(item, indent + 2)) if isinstance(item, (dict, list)) else _state_text(item))
            for key, item in value.items()
        )
    if isinstance(value, list):
        return "\n".join(" " * indent + "• " + _state_text(item, indent + 2) for item in value)
    return json.dumps(value, ensure_ascii=False)


def _reply_warnings(reply: dict) -> list[str]:
    warnings = []
    if reply.get("status") == "error":
        message = "未产生正文" if not reply["text"] else "生成未完成"
        if reply.get("http_status"):
            message += f"：HTTP {reply['http_status']}"
        warnings.append(message + "。")
    elif not reply["text"]:
        warnings.append("该次调用没有返回正文。")
    if reply.get("status") == "truncated" or reply.get("finish_reason") in {
        "length", "max_tokens", "max_output_tokens",
    }:
        warnings.append("输出达到长度上限，可能截断；以下保留全部已生成文字。")
    if reply.get("status") == "completion_unverified":
        warnings.append("完成状态未核实。")
    if reply.get("guidance_status") == "fallback":
        warnings.append("本侧情绪提示采用规则降级；原因保留在生成记录中。")
    return warnings


def _validate(data: object) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
        raise ValueError("report data must contain a metadata object")
    pairs = data.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("report data must contain at least one real pair")
    seen = set()
    for pair in pairs:
        if not isinstance(pair, dict) or not isinstance(pair.get("id"), str) or not pair["id"]:
            raise ValueError("every pair needs a nonempty string id")
        if pair["id"] in seen:
            raise ValueError(f"duplicate pair id: {pair['id']}")
        seen.add(pair["id"])
        for field in ("scenario", "user_input"):
            if not isinstance(pair.get(field), str):
                raise ValueError(f"pair {pair['id']} requires string {field}")
        for side in ("before", "after"):
            if not isinstance(pair.get(side), dict) or not isinstance(pair[side].get("text"), str):
                raise ValueError(f"pair {pair['id']} requires {side}.text, even when empty after failure")
        if not isinstance(pair.get("history", []), list):
            raise ValueError("history must be an array")
        for message in pair.get("history", []):
            if not isinstance(message, dict) or not isinstance(message.get("text"), str):
                raise ValueError("every history message must retain string text")
    return data


def build(input_path: Path, output_dir: Path) -> dict:
    raw = input_path.read_bytes()
    data = _validate(json.loads(raw.decode("utf-8-sig")))
    report_hash = _sha(_canonical(data))
    mapping = {}
    for pair in data["pairs"]:
        swapped = int(_sha((report_hash + ":" + pair["id"]).encode("utf-8"))[:2], 16) % 2
        mapping[pair["id"]] = {"A": "after" if swapped else "before", "B": "before" if swapped else "after"}
    bundle = {
        "data": data, "report_sha256": report_hash, "input_sha256": _sha(raw),
        "version_mapping": mapping, "scenario_labels": SCENARIOS,
        "dimension_labels": DIMENSIONS, "metrics": METRICS,
        "state_text": {pair["id"]: _state_text(pair.get("state", "未提供状态")) for pair in data["pairs"]},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "report.html"
    markdown_path = output_dir / "report.md"
    html_path.write_text(HTML.replace("__REPORT_DATA__", _embedded_json(bundle)), encoding="utf-8")
    markdown_path.write_text(_markdown(bundle), encoding="utf-8")
    return {"html": str(html_path.resolve()), "markdown": str(markdown_path.resolve()),
            "pairs": len(data["pairs"]), "report_sha256": report_hash, "input_sha256": _sha(raw)}


def _markdown(bundle: dict) -> str:
    data = bundle["data"]
    lines = ["# 情绪链路修改前后回答对比\n", "## 阅读说明\n",
             f"共 {len(data['pairs'])} 对回答。每对使用相同的本轮用户输入、可信状态与实际前文。\n",
             "修改前：旧链路重跑（关闭新增 Qwen 情绪组件，保留原有表达通道）。修改后：当前新情绪链路。"
             "回答按证据原文保留，空输出、失败、截断与规则降级均不补写。\n",
             "两侧主模型回答均为本轮真实重新生成；新侧 Qwen 使用此前真实调用的录制结果逐项重放，"
             "按当前校验规则检查后生成情绪提示。本报告没有重新调用 Qwen。\n",
             NOTICE + "\n",
             "请先阅读输入与状态，再独立评估两侧回答。这里没有预填质量结论。"
             "评分均为 1–5 分，1 为差、3 为一般、5 为好；无法判断可留空。\n",
             "五项评分：情境与状态适配、情绪可辨识、人格连续性、自然度、反应适度。\n"]
    for scenario in dict.fromkeys(pair["scenario"] for pair in data["pairs"]):
        lines += [f"## {SCENARIOS.get(scenario, scenario)}\n"]
        for pair in (item for item in data["pairs"] if item["scenario"] == scenario):
            lines += [f"### 第 {pair.get('turn', '?')} 轮 · 场景内第 {pair.get('minute', '?')} 分钟\n",
                      "#### 用户输入与可信状态\n", "用户输入：\n", _fence(pair["user_input"]),
                      "可信状态：\n", _fence(bundle["state_text"][pair["id"]])]
            for side, name in (("before", "修改前回答"), ("after", "修改后回答")):
                reply = pair[side]
                lines += [f"#### {name}\n", *[warning + "\n" for warning in _reply_warnings(reply)], _fence(reply["text"]),
                          f"评分（1–5 分，可留空）：{'; '.join(label + ' __' for _, label, _ in METRICS)}。\n",
                          "<details>\n<summary>本侧生成记录</summary>\n",
                          _fence(json.dumps({key: value for key, value in reply.items() if key != "text"},
                                            ensure_ascii=False, indent=2), "json"), "</details>\n"]
            lines += ["偏好：修改前 __ / 修改后 __ / 相当 __ / 两者都不合适 __\n", "备注：\n\n______\n",
                      "<details>\n<summary>本轮实际前文（候选答案未接入后续轮次）</summary>\n"]
            if pair.get("history"):
                for message in pair["history"]:
                    role = {"user": "用户", "assistant": "角色", "character": "角色", "system": "系统"}.get(message.get("role"), "消息")
                    lines += [f"{role}：\n", _fence(message["text"])]
            else:
                lines += ["无前文。\n"]
            lines += ["</details>\n", "<details>\n<summary>本对来源记录</summary>\n",
                      _fence(json.dumps(pair.get("provenance", {}), ensure_ascii=False, indent=2), "json"), "</details>\n"]
    lines += ["## 证据与方法附录\n", "此文件保留原文且没有预填评分；HTML 版支持本机保存和导出人工评分。"
              "隐藏版本标签仅辅助阅读，不能保证严格盲评。\n",
              _fence(json.dumps({"report_sha256": bundle["report_sha256"],
                                 "input_sha256": bundle["input_sha256"],
                                 "metadata": data["metadata"]}, ensure_ascii=False, indent=2), "json")]
    return "\n".join(lines)


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>情绪链路修改前后回答对比</title>
<style>
:root{font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#24292f;background:#f5f6f7;font-size:17px;line-height:1.7}*{box-sizing:border-box}body{margin:0}main{max-width:1280px;margin:auto;padding:34px 26px 70px}h1{font-size:26px;line-height:1.35;margin:0 0 14px}h2{font-size:22px;margin:0 0 12px}h3{font-size:20px;margin:0 0 10px}h4{font-size:17px;margin:0 0 8px}p{margin:8px 0}header{margin-bottom:22px}.subtle{color:#59636e;font-size:15px}.notice{background:#e9edf1;padding:15px 18px;border-left:3px solid #576777;margin:16px 0}.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:18px 0}.toolbar label{display:flex;align-items:center;gap:7px}button,select,input,textarea{font:inherit;color:inherit}button,select{padding:6px 10px;background:white;border:1px solid #9aa4ad;border-radius:3px}button{cursor:pointer}button:hover{background:#e9edf1}button:focus-visible,select:focus-visible,input:focus-visible,textarea:focus-visible,summary:focus-visible{outline:3px solid #537892;outline-offset:2px}input[type=checkbox]{width:18px;height:18px}textarea{width:100%;min-height:75px;border:1px solid #b8c0c8;padding:8px;background:white;border-radius:3px}nav{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}nav button[aria-pressed=true]{background:#253f51;color:white;border-color:#253f51}.scenario{margin-top:30px}.pair{padding:24px 0;border-top:1px solid #a9b3bc}.pair-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px}.context{display:grid;grid-template-columns:1.3fr 1fr;gap:24px;margin:12px 0 20px}.context pre{margin:0}.answers{display:grid;grid-template-columns:1fr 1fr;gap:22px}.answer{min-width:0;padding:18px;background:white;border:1px solid #c4cbd2}.answer pre{font-size:18px;line-height:1.8;min-height:76px}.answer h4{border-bottom:1px solid #d6dbe0;padding-bottom:8px}.verbatim{font-family:inherit;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0}.empty{color:#7d3642;font-size:15px}.flag{font-size:14px;color:#793627;background:#fff1e7;padding:5px 8px;margin:10px 0}.scores{border-top:1px solid #d6dbe0;padding-top:12px;margin-top:20px;display:grid;gap:7px}.score-row{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:15px}.score-row select{font-size:15px;min-width:80px}.review{margin:16px 0}.review label{font-size:15px}.review .preference{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:8px}details{margin:12px 0}summary{cursor:pointer;font-size:15px;color:#455563}details pre{font-size:14px;line-height:1.6;padding:10px;background:#edf0f3}.history-line{border-left:2px solid #c4cbd2;padding:0 12px;margin:12px 0}.history-line pre{background:transparent;padding:0;font-size:16px}.source-info{font-size:14px}.hidden{display:none!important}.footer{margin-top:30px;border-top:1px solid #a9b3bc;padding-top:20px}.metric-help{font-size:15px}.metric-help ul{margin:8px 0;padding-left:22px}#save-status{font-size:14px}.no-matches{padding:20px 0;color:#59636e}
@media(max-width:800px){main{padding:24px 16px}.answers,.context{grid-template-columns:1fr}.pair-heading{display:block}.answer{padding:14px}}
@media print{body,:root{background:white;font-size:11pt}main{max-width:none;padding:0}.toolbar,nav,.no-print,#save-status{display:none!important}.notice{background:none;border:1px solid #999;font-size:10pt}.pair{break-before:auto;padding:14px 0}.context{gap:14px}.answers{gap:14px}.answer{padding:10px;break-inside:avoid}.answer pre{font-size:11pt;min-height:0}.score-row{font-size:9pt}.score-row select{font-size:9pt;border:0;padding:0;appearance:none}.scores{gap:3px}.verbatim{orphans:3;widows:3}h1{font-size:18pt}h2{font-size:15pt}h3{font-size:12pt}h4{font-size:11pt}textarea{min-height:60px}details:not([open]){display:none}details pre{background:white}.pair-heading{break-after:avoid}.review select{border:0;appearance:none}.source-info{font-size:9pt}}
</style></head><body><main>
<header><h1>情绪链路修改前后回答对比</h1><p id="count"></p>
<p>每对使用相同的本轮用户输入、可信状态与实际前文。请独立阅读两侧回答并评分，报告没有预填质量结论。</p>
<p id="persona" class="subtle"></p>
<p class="notice">每轮前文中的角色回复是统一固定短句“嗯，我听到了。”；表内候选答案不接入下一轮。因此，这是一组共享前文的逐轮配对样本，不是两个版本各自连续发展的对话。</p>
<p class="subtle source-info">修改前：旧链路重跑（关闭新增 Qwen 情绪组件，保留原有表达通道）。修改后：当前新情绪链路。两侧主模型回答均为本轮真实重新生成；新侧 Qwen 使用此前真实调用的录制结果逐项重放，并按当前校验规则检查。本报告没有重新调用 Qwen。空输出、失败、截断和规则降级均按原记录保留。</p>
<details class="metric-help"><summary>评分说明：每项 1–5 分，默认未评分</summary><p>1 分为差、3 分为一般、5 分为好；无法判断可留空。反应适度得分越高，表示越相称，不表示情绪越强。</p><ul id="metric-help"></ul><p>人格连续性可结合附录中的角色设定判断；缺乏足够依据时请留空。</p></details>
<div class="toolbar"><label><input id="hide-labels" type="checkbox">隐藏版本标签阅读</label><label>显示范围 <select id="completion-filter"><option value="all">全部样本</option><option value="unrated">尚未填写任何评价</option><option value="rated">已有评价</option></select></label><button id="export">导出评分 JSON</button><button id="print">打印 / 保存为 PDF</button></div>
<p class="subtle">隐藏标签时以稳定的 A/B 顺序呈现，并隐藏提示和来源记录。这仅辅助阅读，不是严格盲评；导出会记录是否看过版本标签。评分仅保存在本浏览器，可导出后交回评估。</p>
<p id="save-status" role="status" aria-live="polite"></p>
<section id="export-fallback" class="hidden no-print" aria-labelledby="export-title"><h3 id="export-title">评分 JSON 已生成</h3><p class="subtle">若下载未开始，可复制以下内容保存为 JSON。导出含版本映射，将揭示 A/B 的真实版本；此次查看会计入版本信息已暴露。</p><button id="select-export">全选 JSON</button><textarea id="export-json" readonly rows="12" aria-label="可复制的评分 JSON"></textarea></section>
<nav id="scenario-nav" aria-label="场景筛选"></nav>
</header><div id="pairs"></div>
<footer class="footer"><details class="source-info"><summary>证据与方法附录</summary><p>来源哈希用于核对同一份证据。评分导出含 A/B 与真实版本映射，不包含网络凭据。本页不连接网络。</p><pre class="verbatim" id="appendix"></pre></details></footer>
</main><script id="report-data" type="application/json">__REPORT_DATA__</script><script>
'use strict';
const bundle=JSON.parse(document.getElementById('report-data').textContent);
const data=bundle.data, storageKey='neuralcloud-emotion-review-v1:'+bundle.report_sha256;
const metrics=bundle.metrics, sides=['before','after'];
const $=id=>document.getElementById(id);
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=String(text);return node;}
let review={schema_version:'emotion-human-ratings-v1',report_sha256:bundle.report_sha256,input_sha256:bundle.input_sha256,created_at:new Date().toISOString(),ever_saw_version_labels:false,hidden_labels:false,ratings:{},view_history:[]};
let storageWorks=true,selectedScenario='all';
try{const stored=localStorage.getItem(storageKey);if(stored){const prior=JSON.parse(stored);if(prior.report_sha256===bundle.report_sha256&&prior.ratings&&typeof prior.ratings==='object')review={...review,...prior};}}catch(e){storageWorks=false;}
function save(){review.updated_at=new Date().toISOString();try{localStorage.setItem(storageKey,JSON.stringify(review));storageWorks=true;}catch(e){storageWorks=false;}updateStatus();}
function hasRating(id){const r=review.ratings[id];return Boolean(r&&(r.preference||r.note||sides.some(s=>Object.values(r[s]||{}).some(v=>v!==''&&v!==null))));}
function updateStatus(){const n=data.pairs.filter(p=>hasRating(p.id)).length;$('save-status').textContent=`已填写 ${n} / ${data.pairs.length} 对。`+(storageWorks?'修改已保存在本浏览器。':'浏览器无法保存本机记录，请使用“导出评分 JSON”保留评价。');}
function rating(id){if(!review.ratings[id])review.ratings[id]={before:{},after:{},preference:'',note:''};return review.ratings[id];}
function visibleSides(pair){return review.hidden_labels?[bundle.version_mapping[pair.id].A,bundle.version_mapping[pair.id].B]:sides;}
function label(pair,side){return review.hidden_labels?(bundle.version_mapping[pair.id].A===side?'回答 A':'回答 B'):(side==='before'?'修改前回答':'修改后回答');}
function sourceDetails(title,obj){const d=el('details','source-info');d.append(el('summary','',title),el('pre','verbatim',JSON.stringify(obj,null,2)));return d;}
function replyWarning(reply){const warnings=[];const labels={error:(reply.text?'生成未完成':'未产生正文')+(reply.http_status?'：HTTP '+reply.http_status:''),empty:'正文为空',truncated:'输出可能截断',completion_unverified:'完成状态未核实'};if(reply.status&&!['ok','success','complete','completed','generated'].includes(reply.status))warnings.push(labels[reply.status]||('生成状态：'+reply.status));if(['length','max_tokens','max_output_tokens'].includes(reply.finish_reason))warnings.push('输出达到长度上限，可能截断；以下保留全部已生成文字。');return warnings;}
function answer(pair,side){const reply=pair[side],card=el('section','answer');card.append(el('h4','',label(pair,side)));for(const warning of replyWarning(reply))card.append(el('p','flag',warning));if(reply.text==='')card.append(el('p','empty','该次调用没有返回正文。'));card.append(el('pre','verbatim',reply.text));if(reply.guidance_status==='fallback')card.append(el('p','subtle source-info','本侧情绪提示采用规则降级；原因保留在生成记录中。'));const scores=el('div','scores');for(const [key,name,help]of metrics){const row=el('label','score-row');const text=el('span','',name);text.title=help;const select=el('select');select.setAttribute('aria-label',`${pair.scenario} 第 ${pair.turn} 轮 ${label(pair,side)} ${name}`);select.append(new Option('未评分',''));for(let n=1;n<=5;n++)select.append(new Option(String(n),String(n)));select.value=String(rating(pair.id)[side][key]??'');select.addEventListener('change',()=>{rating(pair.id)[side][key]=select.value===''?null:Number(select.value);save();});row.append(text,select);scores.append(row);}card.append(scores);const details={...reply};delete details.text;card.append(sourceDetails('本侧生成与情绪提示记录',details));return card;}
function buildPair(pair){const section=el('article','pair');const heading=el('div','pair-heading');heading.append(el('h3','',`第 ${pair.turn} 轮`),el('span','subtle',`场景内第 ${pair.minute} 分钟`));section.append(heading);const context=el('div','context');for(const [name,text]of [['用户输入',pair.user_input],['可信状态',bundle.state_text[pair.id]]]){const part=el('section');part.append(el('h4','',name),el('pre','verbatim',text));context.append(part);}section.append(context);const answers=el('div','answers');for(const side of visibleSides(pair))answers.append(answer(pair,side));section.append(answers);const area=el('section','review');const pref=el('label','preference');pref.append(el('span','','整体偏好'));const select=el('select');select.setAttribute('aria-label',`第 ${pair.turn} 轮整体偏好`);select.append(new Option('未选择',''));for(const side of visibleSides(pair))select.append(new Option(label(pair,side),side));select.append(new Option('相当','tie'),new Option('两者都不合适','neither'));select.value=rating(pair.id).preference||'';select.addEventListener('change',()=>{rating(pair.id).preference=select.value;save();});pref.append(select);const noteLabel=el('label');noteLabel.append(el('span','','备注（具体词句、偏差或改进理由）'));const note=el('textarea');note.value=rating(pair.id).note||'';note.addEventListener('input',()=>{rating(pair.id).note=note.value;save();});noteLabel.append(note);area.append(pref,noteLabel);section.append(area);const history=el('details');history.append(el('summary','','本轮实际前文（候选答案未接入后续轮次）'));if(!pair.history?.length)history.append(el('p','subtle','无前文。'));for(const message of pair.history||[]){const line=el('div','history-line');line.append(el('strong','',{user:'用户',assistant:'角色',character:'角色',system:'系统'}[message.role]||'消息'),el('pre','verbatim',message.text));history.append(line);}section.append(history,sourceDetails('本对来源记录',pair.provenance||{}));return section;}
function render(){const filter=$('completion-filter').value;const container=$('pairs');container.replaceChildren();let count=0;for(const scenario of [...new Set(data.pairs.map(p=>p.scenario))]){if(selectedScenario!=='all'&&scenario!==selectedScenario)continue;const pairs=data.pairs.filter(p=>p.scenario===scenario&&(filter==='all'||(filter==='rated'?hasRating(p.id):!hasRating(p.id))));if(!pairs.length)continue;const block=el('section','scenario');block.append(el('h2','',bundle.scenario_labels[scenario]||scenario));for(const pair of pairs){block.append(buildPair(pair));count++;}container.append(block);}if(!count)container.append(el('p','no-matches','当前筛选条件下没有样本。'));document.querySelectorAll('.source-info').forEach(node=>node.classList.toggle('hidden',review.hidden_labels));$('hide-labels').checked=review.hidden_labels;updateStatus();}
function recordView(){if(!review.hidden_labels)review.ever_saw_version_labels=true;review.view_history.push({at:new Date().toISOString(),hidden_labels:review.hidden_labels});save();}
$('count').textContent=`${[...new Set(data.pairs.map(p=>p.scenario))].length} 个场景 · ${data.pairs.length} 对回答 · 同条件逐轮对照`;
$('persona').textContent=data.metadata.persona?.description||'';
for(const [,name,help]of metrics)$('metric-help').append(el('li','',name+'：'+help+'。'));
for(const [key,name]of [['all','全部场景'],...[...new Set(data.pairs.map(p=>p.scenario))].map(s=>[s,bundle.scenario_labels[s]||s])]){const button=el('button','',name);button.setAttribute('aria-pressed',String(key===selectedScenario));button.addEventListener('click',()=>{selectedScenario=key;for(const node of $('scenario-nav').children)node.setAttribute('aria-pressed',String(node===button));render();});$('scenario-nav').append(button);}
$('appendix').textContent=JSON.stringify({report_sha256:bundle.report_sha256,input_sha256:bundle.input_sha256,metadata:data.metadata},null,2);
$('hide-labels').addEventListener('change',event=>{review.hidden_labels=event.target.checked;recordView();render();});
$('completion-filter').addEventListener('change',render);
$('print').addEventListener('click',()=>window.print());
$('select-export').addEventListener('click',()=>{$('export-json').focus();$('export-json').select();});
$('export').addEventListener('click',()=>{review.ever_saw_version_labels=true;review.view_history.push({at:new Date().toISOString(),hidden_labels:review.hidden_labels,version_mapping_exposed:true,action:'export'});save();const result={...review,exported_at:new Date().toISOString(),version_mapping:bundle.version_mapping,source_metadata:data.metadata,pair_sources:Object.fromEntries(data.pairs.map(p=>[p.id,p.provenance||{}])),metric_definitions:Object.fromEntries(metrics.map(([key,name,help])=>[key,{name,help,range:[1,5],higher_is_better:true}])),blind_review_claim:false};const json=JSON.stringify(result,null,2);$('export-json').value=json;$('export-fallback').classList.remove('hidden');const blob=new Blob([json],{type:'application/json;charset=utf-8'});const url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download='emotion-human-ratings-'+bundle.report_sha256.slice(0,12)+'.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);});
recordView();render();
</script></body></html>'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="A real report-data.json evidence file")
    parser.add_argument("--out", type=Path, help="Output directory; defaults to input parent")
    args = parser.parse_args()
    result = build(args.input, args.out or args.input.parent)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
