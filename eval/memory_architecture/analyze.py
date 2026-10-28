"""Reproducible pilot statistics and a portable, locally rendered result viewer."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import html
import json
from pathlib import Path
import random
import statistics

from .gateway import canonical, save
from .runner import ARMS

LABELS = {"neuralcloud": "NeuralCloud evidence-v2", "hy_pro": "Hy-Memory pro",
          "full_history": "完整授权历史（诊断）", "no_memory": "无记忆（诊断）"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def bootstrap_case_difference(rows, repetitions=10000):
    groups = defaultdict(list)
    for row in rows:
        if row["scores"]["neuralcloud"]["pass"] is not None and row["scores"]["hy_pro"]["pass"] is not None:
            groups[row["case"]].append(int(row["scores"]["neuralcloud"]["pass"]) - int(row["scores"]["hy_pro"]["pass"]))
    names = sorted(groups)
    if not names:
        return None
    rng = random.Random(20261019)
    estimates = []
    for _ in range(repetitions):
        selected = [value for name in rng.choices(names, k=len(names)) for value in groups[name]]
        estimates.append(statistics.mean(selected))
    estimates.sort()
    return {"estimate": statistics.mean(v for values in groups.values() for v in values),
            "low": estimates[int(repetitions * .025)], "high": estimates[int(repetitions * .975)],
            "cluster_count": len(names), "replicates": repetitions, "method": "paired_history_cluster_percentile_bootstrap"}


def analyze(root):
    fixture = read(root / "fixture.json")
    manifest = read(root / "manifest.json")
    rows, writes, call_totals, category = [], defaultdict(list), defaultdict(Counter), defaultdict(lambda: defaultdict(list))
    for case in fixture["cases"]:
        result = read(root / "cases" / case["id"] / "result.json")
        for arm, state in result["arms"].items():
            writes[arm].extend(state.get("writes", []))
        for p in case["probes"]:
            judgments = []
            for suffix in ("forward", "reverse"):
                path = root / "judgments" / f"{case['id']}-{p['id']}-{suffix}.json"
                if path.exists():
                    judgments.append(read(path))
            row = {"case": case["id"], "category": case["category"], "probe": p["id"], "question": p["question"],
                   "expected": p["expected"], "audience": p["audience"], "scores": {}, "answers": {},
                   "retrievals": {}, "rubric_conflict": any(j.get("result", {}).get("rubric_conflict") for j in judgments)}
            for arm in ARMS:
                values = []
                for j in judgments:
                    if j.get("status") == "ok":
                        label = next(k for k, v in j["aliases"].items() if v == arm)
                        values.append(j["result"]["answers"][label])
                complete = len(values) == 2
                score = {"pass": all(v["score"] == 2 and not v["fabricated"] for v in values) if complete else None,
                         "mean_score": statistics.mean(v["score"] for v in values) if complete else None,
                         "agreement": values[0]["score"] == values[1]["score"] if complete else None,
                         "judgments": values}
                row["scores"][arm] = score
                row["answers"][arm] = next(a for a in result["answers"] if a["arm"] == arm and a["probe"] == p["id"])
                if arm in result["arms"]:
                    row["retrievals"][arm] = result["arms"][arm].get("retrievals", {}).get(p["id"], {})
                category[case["category"]][arm].append(score)
            rows.append(row)
    for path in sorted((root / "calls").glob("*.json")):
        event = read(path)
        arm = event.get("arm") or "unattributed"
        usage = event.get("response", {}).get("usage") or {}
        call_totals[arm]["calls"] += 1
        call_totals[arm]["failed_calls"] += event.get("status") != 200
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            call_totals[arm][key] += usage.get(key, 0)
        call_totals[arm]["seconds_sum"] += event.get("seconds", 0)
    metrics = {}
    for arm in ARMS:
        scored = [r["scores"][arm] for r in rows if r["scores"][arm]["pass"] is not None]
        metrics[arm] = {"label": LABELS[arm], "n": len(scored), "passed": sum(s["pass"] for s in scored),
            "rate": statistics.mean(s["pass"] for s in scored) if scored else None,
            "mean_score": statistics.mean(s["mean_score"] for s in scored) if scored else None,
            "judge_agreement": statistics.mean(s["agreement"] for s in scored) if scored else None,
            "generation_errors": sum(r["answers"][arm]["status"] != "ok" for r in rows),
            "write_errors": sum(w["status"] != "ok" for w in writes[arm]),
            "write_batches": len(writes[arm]),
            "rejected_groups": sum((w.get("result", {}).get("receipt") or {}).get("rejected_count", 0) for w in writes[arm]),
            "model_usage": dict(call_totals[arm])}
    comparison = Counter()
    for row in rows:
        a, b = row["scores"]["neuralcloud"]["pass"], row["scores"]["hy_pro"]["pass"]
        if a is not None and b is not None:
            comparison["both_pass" if a and b else "neuralcloud_only" if a else "hy_only" if b else "both_fail"] += 1
    result = {"manifest": manifest, "completion": read(root / "completion.json") if (root / "completion.json").exists() else {},
              "metrics": metrics, "paired": dict(comparison), "difference": bootstrap_case_difference(rows),
              "rubric_conflicts": sum(r["rubric_conflict"] for r in rows), "rows": rows,
              "judge_usage": dict(call_totals["blinded"]), "run_dir": str(root)}
    save(root / "analysis.json", result)
    return result


def render(result, destination):
    def e(text):
        return html.escape(str(text))
    primary = result["metrics"]
    cards = []
    for arm in ARMS:
        m = primary[arm]
        rate = f"{m['rate']:.1%}" if m["rate"] is not None else "未完成评分"
        cards.append(f'<div><div class="muted">{e(LABELS[arm])}</div><div class="value">{rate}</div><div>{m["passed"]} / {m["n"]} 题双评审均通过</div></div>')
    diff = result["difference"]
    diff_text = (f'NeuralCloud − Hy-Memory：{diff["estimate"] * 100:+.1f} 个百分点；按历史分组的配对 bootstrap 95% 区间 '
                 f'[{diff["low"] * 100:+.1f}, {diff["high"] * 100:+.1f}]。'
                 f'只有 {diff["cluster_count"]} 段独立历史，区间仅反映此测试集的抽样变化。') if diff else '评分尚未完整，不能计算配对差异。'
    summary_rows = []
    for arm in ARMS:
        m = primary[arm]
        agreement = f'{m["judge_agreement"]:.1%}' if m["judge_agreement"] is not None else '—'
        mean = f'{m["mean_score"]:.2f}' if m["mean_score"] is not None else '—'
        usage = m['model_usage']
        summary_rows.append(f'<tr><td>{e(LABELS[arm])}</td><td>{mean} / 2</td><td>{agreement}</td><td>{m["generation_errors"]}</td><td>{m["write_errors"]} / {m["write_batches"]}</td><td>{m["rejected_groups"]}</td><td>{usage.get("calls", 0)}</td><td>{usage.get("total_tokens", 0):,}</td></tr>')
    categories = sorted({r['category'] for r in result['rows']})
    options = ''.join(f'<option value="{e(c)}">{e(c)}</option>' for c in categories)
    data = canonical({"labels": LABELS, "arms": list(ARMS), "rows": result["rows"]}).replace('<', '\\u003c')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>记忆架构最终回答质量对照</title>
<style>:root{color-scheme:light dark;--bg:#f7f8f6;--paper:#fff;--text:#1e302b;--muted:#5b6b64;--line:#d4dfd8;--accent:#246352;--soft:#e9f1ec}*{box-sizing:border-box}body{background:var(--bg);color:var(--text);font:15px/1.75 system-ui,"Microsoft YaHei",sans-serif;margin:0}main{max-width:1320px;padding:28px;margin:auto}h1{font-size:24px;line-height:1.4;margin:5px 0 12px}h2{font-size:20px;margin:28px 0 12px}h3{font-size:16px}p{margin:8px 0 15px}.muted,small{color:var(--muted)}.lead{padding:15px 20px;background:var(--soft);border-left:4px solid var(--accent)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:25px;margin:25px 0}.value{font-size:24px;font-weight:650;color:var(--accent)}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top;min-width:80px}th{background:var(--soft)}section{border-bottom:1px solid var(--line);padding-bottom:24px}.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:16px 0}select,input,button{font:inherit;background:var(--paper);color:var(--text);border:1px solid var(--line);padding:6px 10px}details{border-bottom:1px solid var(--line);padding:14px 0}summary{cursor:pointer;font-weight:600}.answers{display:grid;grid-template-columns:1fr 1fr;gap:20px}.answer{background:var(--paper);padding:15px;border:1px solid var(--line)}.answer h3{margin-top:0}.score{color:var(--accent);font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 ui-monospace,Consolas,monospace}a{color:var(--accent)}code{overflow-wrap:anywhere;font-size:12px}.requirements{font-size:13px;padding:9px 13px;background:var(--soft)}@media(prefers-color-scheme:dark){:root{--bg:#13221e;--paper:#192c25;--text:#e4eee7;--muted:#adbbb2;--line:#3b5044;--accent:#8bc6a9;--soft:#243d30}}@media(max-width:760px){main{padding:18px}.metrics{grid-template-columns:1fr 1fr;gap:18px}.answers{grid-template-columns:1fr}}</style>
<main><header><small>NEURALCLOUD · LIVE MEMORY ARCHITECTURE PILOT · 2026-10-19</small><h1>两套记忆架构，能让最终回答答对多少</h1>
<p>固定同一个写入与回答模型，在中文合成历史上比较记忆写入、维护和检索后的最终回答。Hy-Memory pro 使用发布源码；NeuralCloud 使用现有 production / evidence-v2 子系统。</p></header>
<div class="lead"><strong>本轮是架构对照，未使用 Hy-Memory-30B-A3B 专用写入权重。</strong> 结果仅适用于本次模型、配置和测试集。没有运行原生记忆接口、完整角色运行时或数月在线持续对话。</div>
<section><h2>最终回答完全正确率</h2><div class="metrics">__CARDS__</div><p>__DIFF__</p><p class="muted">通过条件：同一题的两次独立模型评分均为 2/2，且均未标记具体事实编造。每次评审看到匿名答案，第二次反转顺序。完整历史与无记忆为诊断组，不能与两套主架构作等预算性能比较。</p></section>
<section><h2>质量、错误与消耗</h2><div class="scroll"><table><thead><tr><th>条件</th><th>平均评分</th><th>双评审分数一致率</th><th>回答技术失败</th><th>写入技术失败 / 批次</th><th>提案拒绝组数</th><th>模型请求数</th><th>总 token</th></tr></thead><tbody>__SUMMARY__</tbody></table></div><p class="muted">请求数包含各条件的写入、回答以及 Hy 的 embedding 请求。总 token 按提供方返回 usage 汇总，包含 embedding token，不是费用金额。评审单独消耗 __JUDGECALLS__ 次请求、__JUDGETOKENS__ token。并发、缓存和不同写入步骤影响耗时，本轮不作延迟排名。</p><p>配对结果：两者都通过 __BOTH__ 题；仅 NeuralCloud 通过 __NCONLY__ 题；仅 Hy-Memory 通过 __HYONLY__ 题；两者都未通过 __FAIL__ 题。评审报告的评分要求冲突：__CONFLICTS__ 题，保留在明细中。</p></section>
<section><h2>逐题查看回答、评分与实际检索内容</h2><div class="controls"><label>类别 <select id="category"><option value="">全部类别</option>__OPTIONS__</select></label><label>显示 <select id="outcome"><option value="all">全部结果</option><option value="different">两架构结果不同</option><option value="failure">任一架构未通过</option><option value="disagreement">评审分歧</option></select></label><label>搜索 <input id="search" placeholder="问题或案例名称"></label><span id="count" class="muted"></span></div><div id="cases"></div></section>
<section><h2>怎样理解这次实验</h2><ul><li>12 段独立历史、432 条消息、36 个问题；每段历史分三批写入，问题与答案要求未交给 writer。查询模拟跨会话，仅给检索记忆和当前问题。</li><li>覆盖更正、有效时间、任务续接、转述、共同经历、语义回忆、多人归属、未知信息、精确编号、跨片段整合、受众隔离和晚到历史。每类只有三题，不能当作领域基准。</li><li>两组写入与回答使用官方 deepseek-flash，关闭思考。Hy 使用 Chroma、bge-m3、pro 模式；没有启用 ultra / System 2。NeuralCloud 使用现有提案 schema、证据校验和 SQL recall。两组写入模型输出上限同为 4096 token。</li><li>回答记忆最多 6000 个 Unicode 字符，按完整记录保留；这不等于相同 token 数。NeuralCloud 与 Hy 保留各自记录表示，格式开销属于本次架构配置。</li><li>Hy 以故事与受众分区隔离；NeuralCloud 使用原有 visibility。受众政策由评测适配器提供，不代表 Hy 原生实现了相同权限合同。</li><li>完整历史是能力诊断：它也答错时，应先检查问题、评分或回答模型。无记忆用于观察猜测和弃答水平。所有中间记录、异常、使用量和原始回答都已保存。</li><li>仅一轮写入、一个生成模型与一个自动评审模型。反转顺序不等于独立的人类验证。12 段历史的置信区间不能覆盖数据分布选择、模型变化或长期状态漂移。</li></ul></section>
<footer><h2>复现依据</h2><p>NeuralCloud：<code>__NCHEAD__</code><br>Hy-Memory：<code>__HYHEAD__</code><br>测试集 SHA-256：<code>__FIXTURE__</code></p><p>完整运行目录：<code>__RUNDIR__</code>。其中 manifest.json 保存代码哈希与依赖版本；calls/ 保存脱敏传输记录；cases/ 保存写入、检索和回答；judgments/ 保存匿名顺序与原始评审；analysis.json 保存统计结果。</p><p class="muted">本报告可离线打开，不请求外部脚本或服务。研究前的接线测试保留为独立 smoke 目录，不计入这些分数。</p></footer></main>
<script id="data" type="application/json">__DATA__</script><script>const d=JSON.parse(document.getElementById('data').textContent);const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));function render(){const cat=document.getElementById('category').value,kind=document.getElementById('outcome').value,q=document.getElementById('search').value.toLowerCase();let rows=d.rows.filter(r=>(!cat||r.category===cat)&&(!q||(r.question+r.case).toLowerCase().includes(q))&&(kind==='all'||kind==='different'&&r.scores.neuralcloud.pass!==r.scores.hy_pro.pass||kind==='failure'&&(!r.scores.neuralcloud.pass||!r.scores.hy_pro.pass)||kind==='disagreement'&&d.arms.some(a=>r.scores[a].agreement===false)));document.getElementById('count').textContent=`${rows.length} / ${d.rows.length} 题`;document.getElementById('cases').innerHTML=rows.map(r=>`<details><summary>${esc(r.category)} · ${esc(r.question)} <span class="score">NC ${r.scores.neuralcloud.pass?'通过':'未通过'} / Hy ${r.scores.hy_pro.pass?'通过':'未通过'}</span></summary><p class="requirements">评分要求：${esc(r.expected)}${r.rubric_conflict?' · 评审标记了要求冲突':''}</p><div class="answers">${d.arms.map(a=>`<div class="answer"><h3>${esc(d.labels[a])} <span class="score">${esc(r.scores[a].mean_score)} / 2</span></h3><p>${esc(r.answers[a].text||'[未生成回答]')}</p><small>${r.scores[a].judgments.map((j,i)=>`${i+1}：${j.score}/2 · ${esc(j.reason)}`).join('<br>')}</small>${r.retrievals[a]?`<details><summary>实际检索上下文 · ${r.retrievals[a].characters??0} 字符</summary><pre>${esc(r.retrievals[a].context||JSON.stringify(r.retrievals[a],null,2))}</pre></details>`:''}</div>`).join('')}</div></details>`).join('')}['category','outcome','search'].forEach(id=>document.getElementById(id).addEventListener(id==='search'?'input':'change',render));render();</script></html>'''
    replacements = {'CARDS': ''.join(cards), 'DIFF': e(diff_text), 'SUMMARY': ''.join(summary_rows), 'OPTIONS': options,
        'DATA': data, 'JUDGECALLS': str(result['judge_usage'].get('calls', 0)), 'JUDGETOKENS': f"{result['judge_usage'].get('total_tokens', 0):,}",
        'BOTH': str(result['paired'].get('both_pass', 0)), 'NCONLY': str(result['paired'].get('neuralcloud_only', 0)),
        'HYONLY': str(result['paired'].get('hy_only', 0)), 'FAIL': str(result['paired'].get('both_fail', 0)),
        'CONFLICTS': str(result['rubric_conflicts']), 'NCHEAD': e(result['manifest']['neuralcloud_head']),
        'HYHEAD': e(result['manifest']['hy_commit']), 'FIXTURE': e(result['manifest']['fixture_sha256']), 'RUNDIR': e(result['run_dir'])}
    for key, value in replacements.items():
        page = page.replace('__' + key + '__', value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(page, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--html', type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.run_dir.resolve())
    render(result, args.html.resolve())
    print(canonical({k: result[k] for k in ('metrics', 'paired', 'difference', 'rubric_conflicts')}))


if __name__ == '__main__':
    main()
