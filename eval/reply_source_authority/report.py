"""Portable research report, with exact candidates and no unvalidated preference scores."""

import argparse
import json
from pathlib import Path

from eval.qq_brief.common import read

PAPERS = [
    {"title": "Sufficient Context · ICLR 2025", "url": "https://arxiv.org/abs/2411.06037",
     "finding": "是否有足够信息是当前问题与上下文的关系；上下文不完整与模型没有用好现有信息是不同错误。",
     "experiment": "同一张图可以足够评价配色，却不足以确认角色；检验当前问题与原始证据的直接呈现。",
     "limit": "借用任务相关的充分性概念，没有复现论文的选择性生成算法；中文人格对话不能直接套用其结果。"},
    {"title": "CRITIC · ICLR 2024", "url": "https://arxiv.org/abs/2305.11738",
     "finding": "通过工具取得外部反馈，再据此检查和改进回答，并做了去除工具反馈的消融。",
     "experiment": "把真实执行失败保留为宿主证据，规划中的‘已核实’或‘无法识别’不承担事实证明作用。",
     "limit": "Misty 本轮沿用现有调用；没有复现 CRITIC 的多轮工具纠错，也不假定增加重写必然有效。"},
    {"title": "Large Language Models Cannot Self-Correct Reasoning Yet · ICLR 2024", "url": "https://arxiv.org/abs/2310.01798",
     "finding": "在其推理实验中，缺少外部反馈的自纠错可能降低表现；使用正确答案标签或不等预算会影响结论。",
     "experiment": "固定候选与证据，只改变输入边界；金标不进入模型；失败、重试与调用数都保留。",
     "limit": "该论文不证明所有自我检查都无效，也不能替代对 Misty 当前模型的实测。"},
    {"title": "Faithfully Express Intrinsic Uncertainty · EMNLP 2024", "url": "https://aclanthology.org/2024.emnlp-main.443/",
     "finding": "同时考察语言中的过度确定与过度保留，避免把少说免责声明等同于表达正确。",
     "experiment": "对照中同时放入自然省略、必要限定、拒绝回答、编造事实和真正漏答。",
     "limit": "本轮没有估计模型内部置信度或得到校准概率，评价依据是冻结证据和预设任务。"},
]

TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Misty：从表达规则到上下文协议</title>
<style>
:root{color-scheme:light dark;--bg:light-dark(#fafaf8,#191b1c);--text:light-dark(#242c2b,#e5e9e7);--muted:light-dark(#5f6b68,#a4aea9);--line:light-dark(#d8dfda,#414a46);--accent:light-dark(#17685d,#83cbbd);--surface:light-dark(#edf2ee,#242b27);--warn:light-dark(#81531f,#dfb985)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.75 system-ui,'Microsoft YaHei',sans-serif}main{max-width:1280px;margin:auto;padding:30px 28px 60px}h1{font-size:24px;line-height:1.4;margin:6px 0 16px}h2{font-size:19px;margin:28px 0 12px}h3{font-size:16px;margin:18px 0 8px}p{margin:8px 0 14px}a{color:var(--accent)}small,.muted{color:var(--muted);font-size:13px}.lead{max-width:1000px;font-size:17px}.note{background:var(--surface);padding:15px 18px;border-left:3px solid var(--accent);margin:20px 0}nav{display:flex;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--line);padding:12px 0;position:sticky;top:0;background:var(--bg);z-index:2}button,select{font:inherit;background:var(--bg);color:var(--text);border:1px solid var(--line);padding:7px 12px;border-radius:4px;max-width:100%}button{cursor:pointer}button[aria-selected=true]{background:var(--accent);color:var(--bg)}button:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}section[hidden]{display:none}.controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:20px 0}.twocol{display:grid;grid-template-columns:1fr 1fr;gap:22px}.sample{border-top:2px solid var(--line);padding:14px 0}.reply{white-space:pre-wrap;overflow-wrap:anywhere;font-size:16px}.warning{color:var(--warn)}details{margin:12px 0}summary{cursor:pointer;color:var(--accent)}pre{font:12px/1.6 ui-monospace,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;padding:12px;background:var(--surface)}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}.tablewrap{overflow-x:auto}.numbers{display:flex;gap:30px;flex-wrap:wrap;margin:22px 0}.numbers b{display:block;font-size:23px;font-weight:500;color:var(--accent)}.flow{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:20px 0}.flow div{padding:14px;border-top:2px solid var(--accent);background:var(--surface)}.flow b{display:block}ul,ol{padding-left:24px}li{margin:9px 0}.paper{padding:18px 0;border-bottom:1px solid var(--line)}.paper p{margin:5px 0}.tag{font-size:12px;color:var(--muted)}footer{border-top:1px solid var(--line);margin-top:32px;padding-top:16px}@media(max-width:760px){main{padding:20px 14px}.twocol,.flow{grid-template-columns:1fr}nav{position:static}th,td{padding:8px}}
</style>
<main>
<small>NeuralCloud / Misty · 第二轮机制研究 · 2026-10-09</small>
<h1>原话决定任务，证据约束断言，规划保留为建议</h1>
<p class="lead">优化需要进入上下文传递与校验接口。把“必答内容”和“内部约束”分成字段仍不足以隔离影响：校验器会受错误规划干扰；直接删掉规划又会丢失部分任务提示作用。</p>
<div id="numbers" class="numbers"></div>
<nav aria-label="研究内容"><button data-panel="findings" aria-selected="true">结论与改造范围</button><button data-panel="critic" aria-selected="false">机制对照</button><button data-panel="body" aria-selected="false">正文回放</button><button data-panel="papers" aria-selected="false">论文与方法边界</button></nav>
<section id="findings">
<h2>目前可以确认的机制</h2>
<ol>
<li><b>错误规划能改变相同回复的处理结果。</b>三组主试验固定用户原话、证据和候选文本，只切换规划。原候选有14/48对出现最终放行/拦截翻转；移除规划后输入完全相同的48对仍有1对翻转，提示服务端推理也有残余波动。</li>
<li><b>单句补条件没有解决传播问题。</b>将“图片收到但识图失败就说明失败”改成“失败阻止当前内容回答时说明”后，错误规划条件下正常回复被拦截12/24，原候选为11/24。这个结果只否定此次具体修补，不代表所有提示词改进无效。</li>
<li><b>移除规划不能直接成为上线方案。</b>正常回复在两种规划条件下均未被拦截，但“图里写了什么→收到啦”及部分原意、信息利用错误被漏检。需要提供更清楚的原始任务输入，并保持事实检查。</li>
<li><b>报告无效与判断错误必须分开。</b>原候选27/96次最终返回“校验未完成”，均出现任务引用行不满足宿主约束。错误回复被保护性拦截不算校验成功。引用规则与模型报告约定也需要一起修订。</li>
</ol>
<div class="note"><b>建议的下一版：</b>正文保留有用的内容规划，校验直接使用原话与证据，并修正补充证据的引用约定。直接删除正文规划在本轮仍有退步。现有独立开关继续关闭，离线原型和生产启用保持明确区分。</div>
<h2>已实现的更大范围原型</h2><p id="replayFinding"></p><p>原型继续要求任务触发行确实来自当前会话的用户原话、至少引用一个触发行、全部补充证据属于已准入的同一受众、引用行与候选摘录真实存在；事实断言的支持检查保持原样。只修改了隔离副本中的一个宿主引用条件，没有修改任何候选正文或原始模型报告。</p>
<h2>可以扩大到哪些模块</h2>
<div class="flow" aria-label="建议的信息流"><div><b>1. 原始任务视图</b>当前原话、触发行、说话者和回复对象；保存纠正与指代来源。</div><div><b>2. 证据记录</b>画面、OCR、角色、换装、记忆归属和工具执行各自绑定来源。</div><div><b>3. 回应选择</b>选择回答、观点、局部限定、澄清或求证；只使用完成当前交流所需的判断。</div><div><b>4. 分项校验</b>事实、原意、表达分别报告；事实问题与明确漏答可触发现有重写，风格诊断不单独重写。</div></div>
<div class="tablewrap"><table><thead><tr><th>改造对象</th><th>具体变更</th><th>当前证据与状态</th></tr></thead><tbody>
<tr><td>规划与任务接口</td><td>由宿主构建原始任务视图；规划中的摘要、必须说明项不成为校验器的权威输入。保留原计划用于审计。</td><td>已实现离线输入投影及原话显式呈现对照；不能据此直接删除生产规划。</td></tr>
<tr><td>证据与执行记录</td><td>为收到、识别成功、识别失败保留不同记录；每项事实保留原置信与出处，失败只影响其对应操作。</td><td>前轮失败记录试验提供支持；仍需覆盖私聊、预算裁剪、重放、跨会话隔离。</td></tr>
<tr><td>正文输入</td><td>保留人格、全部政策和原始来源；继续用规划约束回应重点，减少无关边界项进入规划。</td><td>64次回放不支持直接移除正文规划：移除组出现1次空正文，配色样本也更常提无关OCR。</td></tr>
<tr><td>校验报告约定</td><td>任务必须有用户原话锚点；已准入的工具/记忆记录可作为补充证据，不能替代锚点。</td><td>已完成隔离宿主原型与384份原报告双版本重放；越范围、缺触发、伪造摘录等负例继续拒绝。</td></tr>
<tr><td>情绪与记忆</td><td>独立保存状态与强度；状态只影响表达和立场，不改写发言主语。记忆承接保留说话者与讨论前提。</td><td>原意反转用例仍难检出，需单独的原话/指代对照；本轮不改情绪计算。</td></tr>
<tr><td>评测与版本</td><td>记录原稿、改稿、判定、未评分原因；按独立会话汇总。接口版本进入缓存与恢复绑定，保留原证据。</td><td>试验输入、代码、HTTP记录均冻结并校验哈希；独立盲评、真实图片链路及生产P95仍未完成。</td></tr>
</tbody></table></div>
<h2>实施顺序与停止条件</h2>
<p>先统一任务与证据输入协议，修正校验报告引用约定；再用保持事实相同的对照优化信息选择。单独补充说话者、被指对象与因果/否定关系的对照，解决“不是你的错”被改写成“你别自责”的原意错误；现有校验在两次重复中仍漏检此问题。对新视图先采用 observe，通过留出集后才考虑 enforce。</p>
<p>下一轮仍需冻结24组、每组三轮、重复两次的完整对照，补真实图片调用与恢复重放，并预置最近八轮送达历史。必要限定遗漏、事实越界或主语反转出现新增回归时，不启用候选。自然度与偏好由匿名人工成对评审；当前自动评测器未过自身对照，不用来选择优胜方案。</p>
<p>更大范围的偏好训练可以作为后续候选，但训练对必须先保证事实边界等价，并获得可靠偏好标签。现有裁判会漏检原意问题，直接把它的通过/不通过转为训练标签会固化错误。</p>
</section>
<section id="critic" hidden>
<h2>固定候选：规划是否干扰判定</h2>
<p>24条候选包含12条预设可接受回复、12条事实或任务错误；每条两种规划、重复两次、三种输入配置，共288次。标签由实施者预先编写，不是独立人工金标。原始证据含失败记录，所有配置相同。</p>
<div id="criticMetrics" class="tablewrap"></div>
<p class="muted">“有效判定正确”排除校验未完成，但分母保留全部预定次数；“正常回复被拦截”包含保护性拦截。P95为本批校验请求路径的描述值，不能代替完整生产延迟。模型、证据与旧v6候选固定；线上已另行加入JSON修复。</p>
<h3>后续对照：直接呈现原话</h3><div id="followupMetrics" class="tablewrap"></div><p id="followupNote"></p>
<h3>相同首份报告：宿主引用规则重放</h3><div id="replayMetrics" class="tablewrap"></div><p class="muted">384份真实模型首份报告，在原宿主与隔离原型各执行一次，共768次本地校验；没有新增模型调用，不含模型重试结果。与上表“包含原重试”的运行结果不是同一统计口径。</p>
<div class="controls"><label>试验 <select id="study"><option value="mechanism">主试验 · 288次</option><option value="followup">原话呈现 · 96次</option></select></label><label>候选 <select id="case"></select></label><label>重复 <select id="repeat"></select></label></div>
<div id="caseView"></div><div id="criticViews"></div>
</section>
<section id="body" hidden>
<h2>同一检查点的正文回放</h2><p>每组共享同一份已捕获规划、原话、证据、人格与情绪。两组均使用移除规划的校验输入；只有正文是否收到规划说明不同。失败场景使用前轮保存了宿主失败记录的检查点。没有重新选择工具或真实投递。</p>
<div id="bodyMetrics" class="tablewrap"></div><p class="warning">校验放行只说明这次流程通过，不等于事实无误、回答有用或更自然；不提供未经独立评审的提升百分比。</p>
<p>实施者复核的具体例子：配色场景移除规划后，两次都追问或说明模糊错误码，保留规划两次均未提；网页场景保留规划两次都忽略了已有营业时间摘要，移除规划后回答了时间，但把来源说成“公告上写”。这些表现说明内容规划有帮助也会带偏，不能用整体删减替代信息选择。这是定性复核，未做独立盲评。</p>
<div class="controls"><label>检查点 <select id="bodyCase"></select></label></div><div id="bodyView"></div>
</section>
<section id="papers" hidden><h2>把论文结论变成可区分的试验</h2><div id="paperList"></div>
<h2>本轮不能支持的结论</h2><ul><li>不能把人工构造的错误规划比例当成线上故障率。</li><li>重复试验和同一场景正反例相关，不能把全部调用当作独立样本计算显著性。</li><li>后续原话呈现是看到主试验不足后追加的开发对照，需要新的留出集确认。</li><li>图像与网页结果来自冻结夹具；没有测真实视觉模型识别质量。</li><li>没有新增正常路径调用这一点来自原型结构；生产调用数和延迟门槛仍需端到端实测。</li></ul>
<details><summary>冻结清单与本地审计摘要</summary><pre id="audit"></pre></details></section>
<footer>原始结果与失败均保存在本地研究目录；当前页面为自包含HTML，可保存后独立打开。<a href="http://127.0.0.1:57881/">查看上一轮研究</a></footer>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);const el=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={control:'原候选',conditional_example:'仅补示例条件',source_only:'移除规划',source_explicit:'移除规划＋原话显式呈现',keep_plan:'正文保留规划'};
const details=(title,v)=>`<details><summary>${esc(title)}</summary><pre>${esc(JSON.stringify(v,null,2))}</pre></details>`;
const table=(headers,rows)=>`<table><thead><tr>${headers.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(x=>`<td>${esc(x)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
el('numbers').innerHTML=[['288','固定候选主试验'],['96','原话呈现后续对照'],['64','正文回放'],['0','真实QQ投递']].map(([n,t])=>`<div><b>${n}</b>${t}</div>`).join('');
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button').forEach(x=>x.setAttribute('aria-selected',x===b));document.querySelectorAll('main>section').forEach(s=>s.hidden=s.id!==b.dataset.panel)});
function metricTable(study){const rows=[];for(const [arm,m] of Object.entries(study.metrics)){for(const state of ['faithful','drifted']){const x=m[state];rows.push([labels[arm],state==='faithful'?'合理规划':'偏移规划',`${x.correct}/${x.expected}`,`${x.blocked_good}/${x.good_expected}`,`${x.accepted_bad}/${x.bad_expected}`,x.unscored,x.requests,Math.round(x.p95_ms)+' ms'])}}return table(['输入配置','规划条件','有效判定正确/预定','正常回复被拦截','错误回复被放行','校验未完成','模型请求数','P95'],rows)}
el('criticMetrics').innerHTML=metricTable(D.mechanism);el('followupMetrics').innerHTML=metricTable(D.followup);
el('followupNote').textContent=D.followup_note;
const replayRows=[];for(const [name,replay] of Object.entries(D.replays)){for(const [study,arms] of [['mechanism',['control','source_only']],['followup',['source_explicit']]]){for(const arm of arms){const rows=replay.rows.filter(r=>r.study===study&&r.arm===arm);replayRows.push([name==='original'?'原宿主':'同受众补充证据原型',study==='mechanism'?'主试验':'原话呈现',labels[arm],`${rows.filter(r=>r.correct).length}/${rows.length}`,rows.filter(r=>r.status!=='ok').length,rows.filter(r=>!r.expected_pass&&r.validation?.verdict==='pass').length])}}}el('replayMetrics').innerHTML=table(['宿主规则','模型报告来源','输入配置','有效判定正确/预定','校验未完成','错误回复放行'],replayRows);
el('replayFinding').textContent='在显式呈现原话的48份相同首份报告上，有效判定正确从43/48变为46/48，校验未完成从3次变为0次；仍有2次情绪场景的原意错误被放行。这个结果支持修正宿主引用约定，尚不满足发布门槛。';
function resetCases(){const study=D[el('study').value];el('case').innerHTML=study.cases.map(c=>`<option value="${esc(c.id)}">${esc(c.id)} · ${c.expected_pass?'预设可接受':'预设有问题'}</option>`).join('');el('repeat').innerHTML=Array.from({length:study.manifest.repeats},(_,i)=>`<option>${i+1}</option>`).join('');renderCritic()}
function renderCritic(){const study=D[el('study').value],c=study.cases.find(x=>x.id===el('case').value),repeat=Number(el('repeat').value);el('caseView').innerHTML=`<h3>原话</h3><p class="reply">${esc(c.question)}</p><h3>固定候选回复</h3><p class="reply">${esc(c.candidate)}</p>${details('相同原始证据',c.evidence)}`;el('criticViews').innerHTML=study.manifest.arms.map(arm=>`<h3>${labels[arm]}</h3><div class="twocol">${['faithful','drifted'].map(state=>{const r=study.rows.find(r=>r.case_id===c.id&&r.repeat===repeat&&r.arm===arm&&r.plan===state);return `<div class="sample"><small>${state==='faithful'?'合理规划':'偏移规划'}：${esc(c[state+'_plan'])}</small><p>${r.critic_available?'有效判定：':'校验未完成：'}${esc(r.validation?.verdict??r.status)} · ${esc(r.model_requests)}次请求</p>${details('判定与修改要求',r.validation??r)}${details('模型原始校验报告',r.reports)}</div>`}).join('')}</div>`).join('')}
el('study').onchange=resetCases;el('case').onchange=renderCritic;el('repeat').onchange=renderCritic;resetCases();
el('bodyMetrics').innerHTML=table(['正文配置','校验放行/预定','被拒/异常/缺失','模型请求数','平均请求/回放','P95'],Object.entries(D.body.metrics).map(([arm,m])=>[labels[arm],`${m.statuses.ok??0}/${m.expected}`,JSON.stringify(Object.fromEntries(Object.entries(m.statuses).filter(([k])=>k!=='ok'))),m.requests,m.mean_requests.toFixed(2),Math.round(m.p95_ms)+' ms']));
el('bodyCase').innerHTML=D.body.manifest.cases.map(key=>`<option>${esc(key)}</option>`).join('');
function renderBody(){const rows=D.body.rows.filter(r=>r.key===el('bodyCase').value);el('bodyView').innerHTML=`<p class="reply">${esc(JSON.stringify(rows[0].input.messages))}</p><div class="twocol">${rows.map(r=>`<div class="sample"><h3>${labels[r.arm]}</h3><small>状态 ${esc(r.status)} · ${esc(r.model_requests)}次模型请求</small><h3>初稿</h3><div class="reply">${esc(r.drafts?.[0]??'未取得初稿')}</div><h3>最终正文</h3><div class="reply">${esc(r.final?.text??'未取得最终正文')}</div>${details('原计划与校验结果',{plan:r.plan,validation:r.final?.validation,drafts:r.drafts,error_type:r.error_type})}</div>`).join('')}</div>`}
el('bodyCase').onchange=renderBody;renderBody();
el('paperList').innerHTML=D.papers.map(p=>`<article class="paper"><h3><a href="${esc(p.url)}" target="_blank" rel="noreferrer">${esc(p.title)}</a></h3><p>${esc(p.finding)}</p><p><b>本轮试验：</b>${esc(p.experiment)}</p><p class="muted">${esc(p.limit)}</p></article>`).join('');
el('audit').textContent=JSON.stringify({manifest_hashes:{mechanism:D.mechanism.manifest_sha256,followup:D.followup.manifest_sha256,body:D.body.manifest_sha256},same_fixed_inputs:D.mechanism.fixed_inputs_match,same_captures:D.body.same_captures,main_plan_pair_metrics:Object.fromEntries(Object.entries(D.mechanism.metrics).map(([k,v])=>[k,Object.fromEntries(Object.entries(v.pairs).filter(([key])=>key!=='rows'))])),verification:D.verification},null,2);
</script></html>'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--summary", type=Path, required=True)
    p.add_argument("--verification", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    data = read(args.summary)
    data["papers"] = PAPERS
    data["verification"] = read(args.verification)
    data["replays"] = {name: read(args.summary.parent / filename) for name, filename in
                       (("original", "original-replay.json"), ("scoped", "scoped-replay.json"))}
    data["followup_note"] = "此追加试验只改变原始触发文本的呈现位置：把相同行号及原话放进任务输入，不加入人工正确答案、不调用新的模型。两种规划状态在输入投影后相同，差异用于观察模型残余波动；这不是独立留出集。"
    encoded = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    args.output.write_text(TEMPLATE.replace("__DATA__", encoded), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
