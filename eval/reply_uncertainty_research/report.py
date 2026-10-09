"""Build a portable interactive report; embeds only selected, synthetic-case evidence."""

import argparse
import json
from pathlib import Path

from .common import read

PAPERS = [
    {
        "title": "Faithfully Express Intrinsic Uncertainty · EMNLP 2024",
        "url": "https://aclanthology.org/2024.emnlp-main.443/",
        "finding": "按断言比较语言的确定程度与模型内在置信，过度保留和保留不足都计为失准。",
        "use": "同时保留必要限定与不必要保留的对照，避免只统计“不敢认死”出现次数。",
        "limit": "本实验核对外部证据支持关系，没有复现论文的多次采样置信估计，也不输出校准概率。",
    },
    {
        "title": "Linguistic Calibration · ICML 2024",
        "url": "https://proceedings.mlr.press/v235/band24a.html",
        "finding": "从读者随后能否作出校准判断来定义长文本的语言校准，论文方法包含 SFT 与强化学习。",
        "use": "检查读者会不会误以为角色已确认、全文已读过、共同往事真的发生；语气流畅本身不算成功。",
        "limit": "借用评估视角；本轮没有训练或替换模型，也未复现读者概率预测实验。",
    },
    {
        "title": "KnowNo · CoRL 2023",
        "url": "https://robot-help.github.io/",
        "finding": "用校准后的行动候选集合决定何时向人求助，并把完成任务与减少不必要求助一并考虑。",
        "use": "把不确定性转化成回答、局部限定、求证或澄清的选择；同一份证据换一个问题，合适的行为应改变。",
        "limit": "Misty 没有建立该论文所需的校准集和选项似然；本试验没有共形预测保证。",
    },
    {
        "title": "FActScore · EMNLP 2023",
        "url": "https://aclanthology.org/2023.emnlp-main.741/",
        "finding": "将长文本拆成原子事实，逐一检查能否得到可靠来源支持。",
        "use": "把画面、角色、换装、OCR 和执行状态分开核对；比较紧凑表结构与逐条显式来源记录。",
        "limit": "使用的是项目已有逐断言校验器与自建对照，并非 FActScore 官方实现或原始基准。",
    },
    {
        "title": "Structured Uncertainty guided Clarification · ACL Findings 2026",
        "url": "https://aclanthology.org/2026.findings-acl.2028/",
        "finding": "区分用户需求未明确与模型预测不确定，在工具参数空间中衡量澄清的价值与重复提问成本。",
        "use": "作为后续设计依据：缺口只有会改变当前答案或行动时才值得披露或追问。",
        "limit": "论文针对结构化工具任务；不能把其覆盖率增益或理论结论直接搬到中文人格聊天。该文在本轮冻结之后补充阅读。",
    },
    {
        "title": "Rethinking Pragmatics · EMNLP 2024",
        "url": "https://aclanthology.org/2024.emnlp-main.1258/",
        "finding": "直接评价自由生成的回复比选择题更能反映语用表现，并研究了偏好优化。",
        "use": "保留整条原始回复及当前问题，复核是否接住意思；不能用结构化规划正确代替正文验收。",
        "limit": "模型训练暂不进入当前方案。缺少独立人工偏好标签时，不宣称模型更自然或人格更好。",
    },
]

TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Misty 表达内化：论文与实验</title>
<style>
:root{color-scheme:light dark;--bg:light-dark(#fafaf8,#191b1c);--text:light-dark(#222b2b,#e6e9e7);--muted:light-dark(#5c6968,#a1aca9);--line:light-dark(#d8dedb,#414945);--accent:light-dark(#17665b,#80c9b8);--surface:light-dark(#f0f3f0,#232928);--warning:light-dark(#825218,#ecc286)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.75 system-ui,'Microsoft YaHei',sans-serif}main{max-width:1250px;margin:auto;padding:32px 28px 70px}h1{font-size:24px;line-height:1.4;margin:4px 0 14px}h2{font-size:19px;margin:28px 0 12px}h3{font-size:16px;margin:0 0 8px}p{margin:8px 0 14px}a{color:var(--accent)}small,.muted{color:var(--muted);font-size:13px}.lead{max-width:920px;font-size:17px}.notice{background:var(--surface);padding:15px 18px;border-left:3px solid var(--accent);margin:18px 0}nav{display:flex;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--line);padding:14px 0;position:sticky;top:0;background:var(--bg);z-index:2}button,select{font:inherit;background:var(--bg);color:var(--text);border:1px solid var(--line);padding:7px 12px;border-radius:4px}button{cursor:pointer}button[aria-selected=true]{background:var(--accent);color:var(--bg);border-color:var(--accent)}section[hidden]{display:none}.controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:20px 0}select{max-width:100%}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.answer{padding:18px;background:var(--surface);border:1px solid var(--line)}.reply{font-size:16px;white-space:pre-wrap;overflow-wrap:anywhere;margin:14px 0}.status{color:var(--muted);font-size:13px}.warn{color:var(--warning)}details{margin:12px 0}summary{cursor:pointer;color:var(--accent)}pre{font:12px/1.6 ui-monospace,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:440px;overflow:auto;padding:12px;background:var(--surface)}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}.tablewrap{overflow-x:auto}ol,ul{padding-left:24px}li{margin:8px 0}.paper{padding:18px 0;border-bottom:1px solid var(--line)}.paper p{margin:5px 0}.metric{font-variant-numeric:tabular-nums}.topline{display:flex;gap:28px;flex-wrap:wrap;margin:20px 0}.topline span{font-size:21px;color:var(--accent);display:block}.finding{max-width:1000px}footer{margin-top:32px;border-top:1px solid var(--line);padding-top:16px}button:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}@media(max-width:760px){main{padding:20px 14px}.grid{grid-template-columns:1fr}nav{position:static}th,td{padding:8px}}
</style>
<main>
<small>NeuralCloud / Misty · 隔离开发实验 · 2026-10-09</small>
<h1>让不确定性影响当前回答的选择</h1>
<p class="lead">建议以当前问题决定说什么，并补齐宿主执行证据。仅清理正文交接尚不能证明更优；自然度评测器未通过自身对照，不能据此给出提升百分比。</p>
<div class="topline"><div><span>128</span>四组正文试验</div><div><span>96</span>固定事实校验</div><div><span>16</span>失败记录补充对照</div><div><span>0</span>真实 QQ 投递</div></div>
<nav aria-label="报告视图">
<button data-tab="findings" aria-selected="true">结论与论文</button><button data-tab="factorial" aria-selected="false">四组原始回复</button><button data-tab="critic" aria-selected="false">事实校验</button><button data-tab="receipts" aria-selected="false">失败记录对照</button><button data-tab="limits" aria-selected="false">评测边界</button>
</nav>
<section id="findings">
<h2>本轮得到什么</h2><div id="findings-body" class="finding"></div>
<h2>直接相关的反例</h2>
<p>原始回复逐条复核中，“只看蓝白配色”这一题，清理交接及两项结合的 4 条回复都追加了模糊错误码；现有方案与只展开证据的 4 条都没有追加。这是特定用例的复核，不是全样本自然度评分。</p>
<p>补充实验里，用户只问图片是否收到，正文“收到啦”却被校验器判为遗漏识图失败说明；重写后又因失败事实没有证据行而无法完成。这表明规则在校验端也会重新变成正文义务，不能只改规划字段。</p>
<p class="muted">其余问题也保留在报告中：有些工具回复忽略已有摘要；情绪场景出现把“这不是你的错”接成“不怪你”的原意倒置。以上复核由本次实现助手完成，非独立人工盲评。</p>
<h2>更合适的下一版设计</h2>
<ol>
<li><strong>先让证据链完整。</strong>把已收到、已选择、读取成功、读取失败作为宿主执行记录，绑定原消息与检查点。成功内容和失败状态分别提供依据，规划器的理由不能充当事实来源。</li>
<li><strong>让缺口服从当前交流目标。</strong>已有规划调用先确定用户此刻要解决什么，再判断“补上这个缺口会不会改变当前答案”。配色依赖色彩与画面，身份问题才依赖身份。未知信息保留在证据层。</li>
<li><strong>正文接收有限的交流任务。</strong>保留原始证据、必要披露、人格与情绪，仅传入有用的回应目标。应单独消融路由指令、规划 JSON 和表达提示；本轮整体删除方案把多个因素一起改变，无法定位单项贡献。</li>
<li><strong>校验每个实际断言。</strong>优先试验逐条显式来源，保留原文与来源归属。回答相关性回到用户原话核对，不能把规划器的内部约束重新当成正文义务。既拒绝编造成功，也拒绝把部分失败扩大成“什么都没得到”。</li>
<li><strong>修好评价再谈发布。</strong>建立语义边界清楚的独立标注集，把“是否直接回应”“是否事实正确”“是否多余披露”拆开。下一轮使用未参与提示迭代的题与独立人工盲评。</li>
</ol>
<div class="notice">实现优先级：失败事实与来源可读性 → 独立评测集 → 更窄的正文交接消融。暂不采用禁词、删句、统一增强自信，也不改变现有模型或人格。当前实验适配器没有导入线上运行链路。</div>
<h2>论文如何转化为可检验的改动</h2><div id="papers"></div>
</section>
<section id="factorial" hidden>
<h2>同一输入，四种处理</h2><p>control 为此前 v6 研究候选。handoff 清理正文继承的控制指令和规划 JSON；cards 将校验证据从紧凑表展开成逐条带来源的记录；combined 同时应用两项。各组沿用同一模型、人格、参数和调用流程。图片、网页结果为冻结夹具，工具选择仍由真实模型完成。</p>
<div id="factorial-metrics"></div>
<div class="controls"><label for="scenario">当前问题</label><select id="scenario"></select><label for="repeat">重复</label><select id="repeat"><option value="1">第 1 次</option><option value="2">第 2 次</option></select></div>
<p id="question"></p><details><summary>展开原始问题、证据及预设核对标准</summary><pre id="input"></pre></details><div id="answers" class="grid"></div>
</section>
<section id="critic" hidden>
<h2>校验器是否理解来源支持关系</h2><p>24 条固定候选各重复两次：12 条应通过、12 条应拒绝。只改变证据的呈现方式，原文、行号、归属和事实状态不变。此处“判对”相对预先写定的题目标签。</p><div id="critic-metrics"></div>
<div class="controls"><label for="critic-case">事实用例</label><select id="critic-case"></select></div><p id="critic-candidate"></p><div id="critic-details" class="grid"></div>
<p class="muted">两次重复不是两个独立场景；48/48 不能证明未来零漏报。当前改进集中在同一道“部分失败不等于全无结果”用例。</p>
</section>
<section id="receipts" hidden>
<h2>把宿主失败记录交给校验器</h2><p>补充实验只比较此前 v6 与增加宿主失败记录。没有展开证据卡片，也没有清理正文交接。4 个场景各重复两次，含 2 个失败场景与 2 个正常识图对照。视觉返回由夹具固定模拟；增加的是宿主执行结果的可引用来源。普通成功样本不会增加失败记录。</p><div id="receipts-metrics"></div>
<div class="controls"><label for="receipt-case">场景</label><select id="receipt-case"></select><label for="receipt-repeat">重复</label><select id="receipt-repeat"><option value="1">第 1 次</option><option value="2">第 2 次</option></select></div><p id="receipt-question"></p><div id="receipt-answers" class="grid"></div>
<p class="muted">这是观察到故障后追加的开发实验。结果支持继续验证证据缺口，不能代替独立测试、恢复幂等验证或上线观察。</p>
</section>
<section id="limits" hidden>
<h2>没有把评测失败当成质量提升</h2><div id="screen"></div>
<details><summary>展开评分器的全部固定对照结果</summary><pre id="screen-raw"></pre></details>
<h2>本轮结论的适用范围</h2><ul>
<li>使用此前冻结的 v6 研究源码，不是后来并发更新的线上版本。没有发布、重启服务或真实 QQ 投递。</li>
<li>全部正文来自真实模型；识图和网页的工具输出固定。未验证真实视觉端点、持续多轮聊天或生产会话表现。</li>
<li>每题只有两次重复，属于开发探索。没有独立人工匿名偏好评测，不能宣布达到原方案的 70% 自然度门槛。</li>
<li>表中“可发送”是链路完成状态。失败稿、降级正文和未评分记录均保留；未用删掉失败样本的方式改善结果。</li>
<li>延迟包含实验并发和校验重试，仅作描述。不能据此宣称正常路径 P95 或调用次数发布门槛通过。</li>
<li>清理正文交接同时改变了系统提示和规划可见范围。需要进一步拆分，不能把效果都归因于某个字段。</li>
</ul><details><summary>冻结信息与校验摘要</summary><pre id="provenance"></pre></details>
</section>
<footer><small>所有展示数据嵌入本文件；不请求外部服务。论文链接指向原始出版来源。原始调用记录和冻结脚本保存在项目 data/uncertainty-research-20261009/live-evidence-v1 中。</small></footer>
</main>
<script id="evidence" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('evidence').textContent),$=id=>document.getElementById(id);
const labels={control:'现有方案',handoff:'清理正文交接',cards:'展开校验证据',combined:'两项结合'};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const json=x=>JSON.stringify(x,null,2),num=x=>x==null?'—':x.toFixed(2);
function table(headers,rows){return '<div class="tablewrap"><table><thead><tr>'+headers.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>'}
function metrics(data,names){return table(['处理方式','可发送 / 总试验','生成失败或缺失','平均模型请求数','P95 延迟（秒）'],data.manifest.arms.map(a=>[a,data.metrics[a]]).map(([a,m])=>[names[a],m.ready+' / '+m.expected,m.expected-m.ready,num(m.mean_requests),num(m.p95_ms_all_recorded/1000)]))+'<p class="muted">分母包含失败记录；P95 使用所有已完成记录做线性分位数插值。指标反映链路运行情况，不是自然度或事实质量评分。</p>'}
const m=D.factorial.metrics,c=D.critic.metrics,r=D.receipts.metrics;
$('findings-body').innerHTML='<p><strong>逐条显式来源有一个明确的局部收益。</strong>固定事实校验由 '+c.control.correct+'/48 判对变为 '+c.cards.correct+'/48；原格式两次放过“摘要已拿到却说什么也没有”的候选，展开来源后两次均拒绝。</p><p><strong>大幅清理正文交接暂不采用。</strong>现有方案可发送 '+m.control.ready+'/32，清理交接 '+m.handoff.ready+'/32，两项结合 '+m.combined.ready+'/32。部分失败涉及工具执行证据缺口；这些数字不能用来断言哪组正文更自然。</p><p><strong>补齐宿主失败记录值得优先验证。</strong>补充对照可发送数由 '+r.control.ready+'/8 变为 '+r.cards.ready+'/8。其中失败场景为 2/4 → 4/4，正常对照为 3/4 → 4/4。正常对照没有新增失败记录，其差异说明生成仍有随机波动，不能把总增益全归因于该改动。</p><p><strong>自然度提升仍未证实。</strong>评分器把两条预设的多余保留当成合格表达，未达到冻结的逐项准确率门槛，因此没有对 128 条正文发布自动质量排名。</p>';
$('papers').innerHTML=D.papers.map(p=>'<article class="paper"><h3><a href="'+esc(p.url)+'" target="_blank" rel="noreferrer">'+esc(p.title)+'</a></h3><p>'+esc(p.finding)+'</p><p><strong>如何使用：</strong>'+esc(p.use)+'</p><p class="muted">'+esc(p.limit)+'</p></article>').join('');
$('factorial-metrics').innerHTML=metrics(D.factorial,labels);$('receipts-metrics').innerHTML=metrics(D.receipts,{control:'现有方案',cards:'增加真实失败记录'});
function options(id,data){const seen=new Set;$(id).innerHTML=data.rows.filter(r=>!seen.has(r.scenario_id)&&seen.add(r.scenario_id)).map(r=>'<option value="'+esc(r.scenario_id)+'">'+esc(r.case.turns[0].messages[0])+'</option>').join('')}
function answer(row,label){return '<article class="answer"><h3>'+esc(label)+'</h3><div class="status '+(row.status==='ok'?'':'warn')+'">'+(row.status==='ok'?'生成完成 · 模拟投递':'未形成可发送正文 · '+esc(row.error_type||row.status))+' · '+esc(row.model_requests??'—')+' 次模型请求</div><div class="reply">'+esc(row.status==='ok'?row.reply:'此试验未完成。降级文本不计作合格回复。')+'</div><details><summary>查看原始草稿与校验反馈</summary><pre>'+esc(json({drafts:row.drafts,critics:row.critics,final:row.reply,status:row.status,error:row.error}))+'</pre></details></article>'}
function renderFactorial(){const rows=D.factorial.rows.filter(r=>r.scenario_id===$('scenario').value&&r.repeat===Number($('repeat').value));$('question').textContent=rows[0].case.turns[0].messages[0];$('input').textContent=json(rows[0].case);$('answers').innerHTML=rows.map(r=>answer(r,labels[r.arm])).join('')}
function renderReceipts(){const rows=D.receipts.rows.filter(r=>r.scenario_id===$('receipt-case').value&&r.repeat===Number($('receipt-repeat').value));$('receipt-question').textContent=rows[0].case.turns[0].messages[0];$('receipt-answers').innerHTML=rows.map(r=>answer(r,r.arm==='control'?'现有方案':'增加真实失败记录')).join('')}
options('scenario',D.factorial);options('receipt-case',D.receipts);['scenario','repeat'].forEach(id=>$(id).addEventListener('change',renderFactorial));['receipt-case','receipt-repeat'].forEach(id=>$(id).addEventListener('change',renderReceipts));renderFactorial();renderReceipts();
$('critic-metrics').innerHTML=table(['处理方式','判对 / 总数','误拒绝 / 应通过','漏拒绝 / 应拒绝','未评分'],["control","cards"].map(arm=>[arm,c[arm]]).map(([arm,m])=>[labels[arm],m.correct+' / '+m.expected,m.false_rejects+' / '+m.positive_n,m.false_accepts+' / '+m.negative_n,m.unscored]));
const seen=new Set;$('critic-case').innerHTML=D.critic.rows.filter(r=>!seen.has(r.case.id)&&seen.add(r.case.id)).map(r=>'<option>'+esc(r.case.id)+'</option>').join('');$('critic-case').value='partial_erased';
function renderCritic(){const rows=D.critic.rows.filter(r=>r.case.id===$('critic-case').value);$('critic-candidate').textContent='候选：'+rows[0].case.candidate+'　预设标签：'+(rows[0].case.expected_pass?'应通过':'应拒绝');$('critic-details').innerHTML=rows.map(r=>'<article class="answer"><h3>'+labels[r.arm]+' · 第 '+r.repeat+' 次</h3><p>'+esc(r.validation?.verdict||r.status)+' · '+(r.correct?'判对':'与标签不符')+'</p><details><summary>证据与完整反馈</summary><pre>'+esc(json({evidence:r.case.evidence,validation:r.validation}))+'</pre></details></article>').join('')};$('critic-case').addEventListener('change',renderCritic);renderCritic();
const screenNames={factual_violation:'事实越界',intent_preserved:'原意保持',missing_necessary_boundary:'必要限定遗漏',unnecessary_boundary:'不必要保留',useful_response:'可用回应'};
$('screen').innerHTML='<p>第一版评分脚本因接口参数不兼容产生 '+D.screening.failed_v1_unscored+' 条未评分记录，未发出模型请求。修正接口后重新执行 12 条固定对照；冻结门槛为每项准确率至少 90%，本次未通过。</p>'+table(['评分维度','判对 / 固定对照'],Object.entries(D.screening.field_accuracy).map(([k,v])=>[screenNames[k],v.correct+' / '+v.n]))+'<p class="muted">“可用回应”的标注也存在歧义：预设标签将直接作答与事实正确分开，而评分器把虚构答案视作不可用。这需要修订定义并在新题上验证，不能改标签后宣称旧评测通过。</p>';
$('screen-raw').textContent=json(D.screening.controls);$('provenance').textContent=json({factorial_manifest_sha256:D.factorial.manifest_sha256,receipt_manifest_sha256:D.receipts.manifest_sha256,same_inputs_across_arms:D.factorial.same_input_hash_across_arms,created_at:D.factorial.manifest.created_at,source:D.factorial.manifest.sources.control.root,limitations:D.limitations});
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('nav button').forEach(x=>x.setAttribute('aria-selected',String(x===b)));document.querySelectorAll('main>section').forEach(s=>s.hidden=s.id!==b.dataset.tab)}));
</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = read(args.summary)
    data["papers"] = PAPERS
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(TEMPLATE.replace("__DATA__", payload), encoding="utf-8")
    print(str(args.output.resolve()))


if __name__ == "__main__":
    main()
