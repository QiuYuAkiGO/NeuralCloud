"""Offline, four-model reader for the original replay plus an APIyi extension."""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import hashlib
import html
import json
from pathlib import Path
import random
import re
import statistics
import sys

from tools.production_acceptance.frozen_model_comparison import digest, save
from tools.production_acceptance.frozen_model_comparison_report import percentile

PROVIDERS = ['deepseek', 'qwen', 'sonnet', 'kimi']
NAMES = {'deepseek': 'DeepSeek V4 Flash', 'qwen': 'Qwen3.8-27B 本地',
         'sonnet': 'API易 Sonnet 4.5', 'kimi': 'Kimi K3 官方中国区'}


def statistics_for(rows):
    seconds = [r['seconds'] for r in rows if r.get('http_status') == 200 and 'seconds' in r]
    body = [r for r in rows if r['kind'] == 'reply' and r.get('http_status') == 200]
    brief = [r for r in body if r['detail'] == 'brief']
    return {'n': len(rows), 'http_200': len(seconds),
            'median_seconds': round(statistics.median(seconds), 4) if seconds else None,
            'sample_p95_seconds': round(percentile(seconds, .95), 4) if seconds else None,
            'visible_bodies': sum(bool(r['display_text'].strip()) for r in body),
            'brief_n': len(brief), 'brief_over_60': sum(r['characters'] > 60 for r in brief),
            'brief_over_2_sentences': sum(r['sentences'] > 2 for r in brief),
            'goal_passed': sum(r.get('evaluation', {}).get('passed', False) for r in rows if r['kind'] == 'goal'),
            'median_first_content_seconds': statistics.median([r['first_content_seconds'] for r in rows
                if r.get('http_status') == 200 and r.get('first_content_seconds') is not None])
                if any(r.get('http_status') == 200 and r.get('first_content_seconds') is not None for r in rows) else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    bundle = json.loads((root / 'requests.json').read_text(encoding='utf-8'))
    baseline = json.loads((root / 'report.json').read_text(encoding='utf-8'))
    extension_name = 'hosted-report.json' if (root / 'hosted-report.json').exists() else 'apiyi-report.json'
    extension = json.loads((root / extension_name).read_text(encoding='utf-8'))
    legacy = json.loads((root / 'apiyi-report.json').read_text(encoding='utf-8'))
    diagnostic_names = [name for name in ['sonnet-connectivity-diagnostic.json',
        'sonnet-backup-connectivity-diagnostic.json'] if (root / name).exists()]
    diagnostics = [json.loads((root / name).read_text(encoding='utf-8')) for name in diagnostic_names]
    assert baseline['bundle_sha256'] == extension['bundle_sha256'] == digest(bundle)
    sys.path.insert(0, str(root / 'frozen'))
    from harness.pipeline.continuation import split_protocol_segments
    lookup = {}
    for original in baseline['rows'] + extension['rows']:
        row = deepcopy(original)
        key = tuple(row[k] for k in ['arm', 'kind', 'id', 'repeat'])
        case = next(c for c in bundle['cases'] if tuple(c[k] for k in ['arm', 'kind', 'id', 'repeat']) == key)
        assert case['messages_sha256'] == row['messages_sha256']
        row.update(detail=case.get('detail'), user_input=case.get('user_input'))
        segments, _ = split_protocol_segments(row.get('text', '')) if row['kind'] != 'goal' else ([], False)
        row['display_text'] = '\n\n'.join(segments)
        row['characters'] = len(re.sub(r'\s', '', row['display_text']))
        row['sentences'] = len([s for s in re.split(r'[。！？!?]+', row['display_text']) if s.strip()])
        lookup[(row['provider'], *key)] = row  # Latest explicit attempt; old failures retained in raw ledger.
    groups = defaultdict(list)
    for row in lookup.values():
        groups[(row['provider'], row['arm'], row['kind'])].append(row)
    reply_keys = [{key[1:] for key, r in lookup.items()
                   if key[0] == provider and r['kind'] == 'reply' and r.get('http_status') == 200}
                  for provider in PROVIDERS]
    common = set.intersection(*reply_keys)
    available = [(p, keys) for p, keys in zip(PROVIDERS, reply_keys, strict=True) if keys]
    available_common = set.intersection(*(keys for _, keys in available)) if available else set()
    analysis = {'bundle_sha256': digest(bundle), 'completed': extension['completed'],
                'budget_cny': extension['budget_cny'], 'prior_cost_upper_cny': extension['prior_cost_upper_cny'],
                'total_cost_upper_cny': extension['estimated_total_cost_upper_cny'],
                'new_attempts': len(extension['rows']), 'four_model_common_reply_slots': len(common),
                'all_new_api_attempts': len(extension['rows']) + (len(legacy['rows']) if extension_name != 'apiyi-report.json' else 0)
                                       + len(diagnostics),
                'sonnet_successful_calls': sum(r.get('http_status') == 200 for r in extension['rows'] if r['provider'] == 'sonnet'),
                'available_reply_models': [p for p, _ in available],
                'available_models_common_reply_slots': len(available_common),
                'groups': {':'.join(key): statistics_for(rows) for key, rows in sorted(groups.items())},
                'matched_reply_groups': {f'{p}:{a}': statistics_for([r for key, r in lookup.items()
                    if key[0] == p and key[1:] in common and r['arm'] == a])
                    for p in PROVIDERS for a in ['before', 'after']},
                'source_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                  for name in dict.fromkeys(['report.json', 'apiyi-report.json', extension_name,
                                                             'requests.json', *diagnostic_names])},
                'analyzer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    save(root / 'four-model-analysis.json', analysis)
    save(root / 'four-model-normalized.json', list(lookup.values()))
    lines = ['# NeuralCloud 四模型冻结对照', '',
        f"总预算 {extension['budget_cny']} 元；当前费用及未释放预留上限 {analysis['total_cost_upper_cny']} 元。",
        '费用不是账户实扣值；失败且无 usage 的请求仍占完整预留。', '',
        '沿用相同冻结人设、两版正文提示词及 8 场景 × 2 次生成；另有 SSE 和 goal。',
        'Qwen/DeepSeek 使用已完成的原始结果，新增模型在之后测量，网络和缓存条件未统一。',
        'Sonnet 保留 temperature 0.7、省略互斥的 top_p、关闭思考。K3 必须思考，effort=low，'
        '固定 temperature=1/top_p=.95，4096 上限包含思考；其他模型正文上限 768。'
        'Sonnet 原生接口没有匹配的 json_object 开关，goal 使用同样的文本规则与冻结校验器。',
        'K3 的思考仅计字符数和费用，不作为回复正文或自动判错；goal 仍检查 25 秒期限。',
        '60 字符与 2 句为描述性观察线，不代表总质量或新提示词硬性限制。', '',
        f"四模型共同成功的正文槽位：{len(common)} / 32；当前可用模型 {', '.join(p for p, _ in available)} "
        f"共同成功 {len(available_common)} / 32。下表耗时仅计算 HTTP 200；接口失败不是质量样本。", '',
        '| 模型 | 版本 | 已测 / HTTP200 | 可见正文 | 完整耗时中位数 | 普通 >60 字 / >2 句 |',
        '|---|---|---:|---:|---:|---:|']
    for provider in PROVIDERS:
        for arm in ['before', 'after']:
            s = analysis['groups'].get(f'{provider}:{arm}:reply', statistics_for([]))
            timing = f"{s['median_seconds']:.3f}s" if s['median_seconds'] is not None else '尚无有效结果'
            lines.append(f"| {NAMES[provider]} | {arm} | {s['n']} / {s['http_200']} | {s['visible_bodies']} | "
                         f"{timing} | {s['brief_over_60']} / {s['brief_over_2_sentences']}（n={s['brief_n']}） |")
    names = sorted({c['id'] for c in bundle['cases'] if c['kind'] == 'reply'})
    cards, mapping = [], {}
    rng = random.Random(10520264)
    for name in names:
        order = list(PROVIDERS)
        rng.shuffle(order)
        mapping[name] = dict(zip('ABCD', order, strict=True))
        for repeat in [1, 2]:
            case = next(c for c in bundle['cases'] if c['id'] == name and c['kind'] == 'reply' and c['repeat'] == repeat)
            lines.extend(['', f'## {name} · 第 {repeat} 次', '', case['user_input'], ''])
            arms = []
            for arm in ['before', 'after']:
                columns = []
                for label, provider in zip('ABCD', order, strict=True):
                    row = lookup.get((provider, arm, 'reply', name, repeat))
                    success = bool(row and row.get('http_status') == 200)
                    text = (row['display_text'] or '（没有可见正文）') if success else '（接口未成功，暂无可比较正文）' if row else '（尚未调用）'
                    metric = (f"{row['seconds']:.3f} 秒 · {row['characters']} 字符 · {row.get('finish_reason')}"
                              if success else f"HTTP {row.get('http_status', '未知')}" if row else '未测')
                    columns.append(f'<article><h3>模型 {label}<small class="identity">{html.escape(NAMES[provider])}</small></h3>'
                        f'<div class="reply">{html.escape(text)}</div><div class="metrics">{html.escape(metric)}</div></article>')
                    lines.extend([f'**{NAMES[provider]} / {arm} / {metric}**', '',
                                  '\n'.join('> ' + part for part in text.splitlines()), ''])
                arms.append(f'<h2>{"修改前" if arm == "before" else "修改后"}提示词</h2><div class="columns">{"".join(columns)}</div>')
            cards.append(f'<section class="card" data-case="{html.escape(name)}" data-repeat="{repeat}">'
                f'<header>{html.escape(name)} · 第 {repeat} 次</header><p class="input">{html.escape(case["user_input"])}</p>{"".join(arms)}</section>')
    lines.extend(['', '## 独立的 goal 与 SSE 结果', ''])
    for key, s in analysis['groups'].items():
        if not key.endswith(':reply'):
            lines.extend([f'**{key}**', '', json.dumps(s, ensure_ascii=False), ''])
    lines.extend(['## 接入失败记录', ''])
    for row in (legacy['rows'] if extension_name != 'apiyi-report.json' else []) + extension['rows']:
        if row.get('http_status') != 200:
            lines.extend([f"- {row['slot']}，第 {row['attempt']} 次，HTTP {row.get('http_status')}："
                          + json.dumps(row.get('provider_error', {}), ensure_ascii=False)])
    for diagnostic in diagnostics:
        lines.extend(['', '**最小健康请求（不进入质量比较）**：'
            + json.dumps({k: diagnostic.get(k) for k in ['endpoint', 'http_status', 'cost_upper_cny', 'provider_error']}, ensure_ascii=False)])
    (root / 'four-model-comparison.md').write_text('\n'.join(lines), encoding='utf-8')
    save(root / 'four-model-blind-map.json', mapping)
    options = ''.join(f'<option value="{html.escape(n)}">{html.escape(n)}</option>' for n in names)
    markup = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NeuralCloud 四模型冻结对照</title><style>
body{font:16px/1.7 system-ui,sans-serif;background:#101521;color:#e3e8f2;max-width:1500px;margin:30px auto;padding:0 20px}nav{position:sticky;top:0;background:#101521;padding:14px 0;display:flex;gap:12px;flex-wrap:wrap;z-index:1}select,button{background:#202b40;color:#e3e8f2;border:1px solid #46546c;border-radius:8px;padding:9px 14px}.card{background:#182132;border:1px solid #303e55;border-radius:14px;margin:24px 0;padding:22px}.card header{color:#96bdff;font-weight:650}.input{background:#111927;padding:14px;border-radius:8px}.columns{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}article{background:#202b40;border-radius:10px;padding:16px;overflow-wrap:anywhere}h2{font-size:17px;color:#9eabc4}h3{margin:0 0 10px;font-size:17px}.reply{white-space:pre-wrap}.metrics,small{font-size:12px;color:#9eabc4}.metrics{margin-top:16px}.identity,.metrics{display:none}.revealed .identity,.revealed .metrics{display:block}.note{font-size:14px;color:#9eabc4}section[hidden]{display:none}@media(max-width:1100px){.columns{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:650px){.columns{grid-template-columns:1fr}}
</style><h1>冻结人设 · 四模型与两版提示词</h1><p>Qwen3.8、DeepSeek、API易 Sonnet 4.5、Kimi K3 官方中国区。A/B/C/D 标签在每个场景的两版提示词与两次生成间保持一致。</p>
<p class="note">同一份冻结旧人格；同期人格改写未纳入。K3 必须思考、采样固定且输出预算包含思考；Sonnet 省略 top_p。日期、网关与缓存条件不同，耗时仅是本批观察。协议标记已隐藏，原始结果保留，没有独立人工评分。</p>
<p class="note">BUDGET_NOTE</p><nav><select id="case"><option value="all">全部场景</option>OPTIONS</select><select id="repeat"><option value="all">两次生成</option><option value="1">第 1 次</option><option value="2">第 2 次</option></select><button id="reveal">显示模型身份与耗时</button></nav>
CARDS<script>const c=document.querySelector('#case'),r=document.querySelector('#repeat');function filter(){document.querySelectorAll('.card').forEach(s=>s.hidden=(c.value!=='all'&&s.dataset.case!==c.value)||(r.value!=='all'&&s.dataset.repeat!==r.value))}c.onchange=r.onchange=filter;document.querySelector('#reveal').onclick=e=>{document.body.classList.toggle('revealed');e.target.textContent=document.body.classList.contains('revealed')?'隐藏模型身份与耗时':'显示模型身份与耗时'};</script></html>'''
    note = f"总预算 {analysis['budget_cny']} 元；费用及未释放预留上限 {analysis['total_cost_upper_cny']} 元（不是实扣值）。四模型共同成功正文槽位 {len(common)}/32。"
    (root / 'four-model-comparison.html').write_text(markup.replace('BUDGET_NOTE', html.escape(note))
        .replace('OPTIONS', options).replace('CARDS', '\n'.join(cards)), encoding='utf-8')
    print(json.dumps(analysis, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
