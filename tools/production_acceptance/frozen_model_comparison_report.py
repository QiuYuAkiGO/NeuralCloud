"""Offline statistics and an anonymous four-arm reader for a frozen replay."""
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


def percentile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root / 'frozen'))
    from harness.pipeline.continuation import split_protocol_segments

    report = json.loads((root / 'report.json').read_text(encoding='utf-8'))
    bundle = json.loads((root / 'requests.json').read_text(encoding='utf-8'))
    cases = {(c['arm'], c['kind'], c['id'], c['repeat']): c for c in bundle['cases']}
    normalized = []
    for original in report['rows']:
        row = deepcopy(original)
        case = cases[(row['arm'], row['kind'], row['id'], row['repeat'])]
        assert row['messages_sha256'] == case['messages_sha256']
        if row['kind'] != 'goal':
            segments, continuation = split_protocol_segments(row.get('text', ''))
            text = '\n\n'.join(segments)
            row.update(display_text=text, display_segments=segments, protocol_continuation=continuation,
                       detail=case['detail'], user_input=case['user_input'])
            characters = len(re.sub(r'\s', '', text))
            sentences = len([p for p in re.split(r'[。！？!?]+', text) if p.strip()])
            row['display_metrics'] = {'characters_without_whitespace': characters, 'punctuation_sentences': sentences,
                'protocol_segments': len(segments), 'over_60_characters': characters > 60,
                'over_2_sentences': sentences > 2,
                'nonempty_visible_body': bool(text.strip()),
                'explicit_missing_record_preface': bool(re.search(r'我这边|我(?:不记得|没有)|没有.*(?:记录|记忆)', text))}
        normalized.append(row)
    groups = defaultdict(list)
    for row in normalized:
        groups[(row['provider'], row['arm'], row['kind'])].append(row)
    summary = {'completed': report['completed'], 'rows': len(normalized),
               'persona_sha256': json.loads((root / 'freeze-manifest.json').read_text(encoding='utf-8'))['persona_sha256'],
               'conservative_cost_upper_cny': report['estimated_peak_cost_upper_cny'],
               'peak_usage_estimate_unrounded_cny': sum(r.get('cost_at_peak_unrounded_cny', 0) for r in normalized),
               'api_calls': {p: sum(r['provider'] == p for r in normalized) for p in ['qwen', 'deepseek']},
               'http_200': sum(r.get('http_status') == 200 for r in normalized),
               'reasoning_leaked': sum(bool(r.get('reasoning_characters')) for r in normalized),
               'source_report_sha256': hashlib.sha256((root / 'report.json').read_bytes()).hexdigest(),
               'analyzer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'groups': {}}
    for (provider, arm, kind), rows in sorted(groups.items()):
        key = f'{provider}:{arm}:{kind}'
        seconds = [r['seconds'] for r in rows if 'seconds' in r]
        entry = {'n': len(rows), 'median_seconds': round(statistics.median(seconds), 4),
                 'sample_p95_seconds': round(percentile(seconds, 0.95), 4),
                 'min_seconds': min(seconds), 'max_seconds': max(seconds)}
        if kind == 'reply':
            brief = [r for r in rows if r['detail'] == 'brief']
            entry.update(visible_bodies=sum(r['display_metrics']['nonempty_visible_body'] for r in rows),
                brief_n=len(brief), brief_over_60=sum(r['display_metrics']['over_60_characters'] for r in brief),
                brief_over_2_sentences=sum(r['display_metrics']['over_2_sentences'] for r in brief),
                median_brief_characters=statistics.median(r['display_metrics']['characters_without_whitespace'] for r in brief))
        elif kind == 'stream':
            first = [r['first_content_seconds'] for r in rows if r.get('first_content_seconds') is not None]
            entry['median_first_content_seconds'] = round(statistics.median(first), 4) if first else None
        else:
            entry['goal_passed'] = sum(r.get('evaluation', {}).get('passed', False) for r in rows)
        summary['groups'][key] = entry
    (root / 'analysis.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (root / 'normalized-results.json').write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    headers = ['模型', '提示词', '样本', '完整耗时中位数', '样本 P95', '普通样例 >60 字', '普通样例 >2 句']
    table = ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |']
    for provider in ['deepseek', 'qwen']:
        for arm in ['before', 'after']:
            s = summary['groups'][f'{provider}:{arm}:reply']
            table.append(f"| {provider} | {arm} | {s['n']} | {s['median_seconds']:.3f}s | {s['sample_p95_seconds']:.3f}s | {s['brief_over_60']}/{s['brief_n']} | {s['brief_over_2_sentences']}/{s['brief_n']} |")
    lines = ['# Qwen 与 DeepSeek 冻结对照', '',
        f"实际 {summary['rows']} 次调用，预算 1 元；保守费用上限 {summary['conservative_cost_upper_cny']} 元。", '',
        '同一份修改前人设、两版冻结正文提示词、8 个合成样例、每格 2 次生成。统一替换修改后请求的人设，排除同步的人设改写。', '',
        '普通样例的 60 字/2 句是共用描述性观察线；修改后提示词允许按内容自然展开，不能把这些计数当作其硬性违约或整体质量分数。', '',
        '正文按冻结的项目协议解析器去除 [END]/[CONTINUE]，原始输出保存在 report.json，未修复或重写模型内容。耗时统计含网络/读取，未清空服务端缓存。', '',
        *table, '', '## 完整回复对照', '']
    names = [c['id'] for c in json.loads((root / 'reference/cases.json').read_text(encoding='utf-8'))]
    lookup = {(r['provider'], r['arm'], r['id'], r['repeat']): r for r in normalized if r['kind'] == 'reply'}
    for name in names:
        for repeat in [1, 2]:
            row = lookup.get(('qwen', 'before', name, repeat))
            if not row:
                continue
            lines.extend([f'### {name} · 第 {repeat} 次', '', row['user_input'], ''])
            for arm in ['before', 'after']:
                for provider in ['deepseek', 'qwen']:
                    row = lookup.get((provider, arm, name, repeat))
                    if row:
                        body = ('\n'.join('> ' + line for line in row['display_text'].splitlines())
                                if row['display_text'] else '（没有可见正文；原始输出保存在 report.json。）')
                        lines.extend([f"**{provider} / {arm} / {row['seconds']:.3f}s**", '',
                                      body, ''])
    (root / 'comparison.md').write_text('\n'.join(lines), encoding='utf-8')

    # No external scripts/fonts or network calls. Anonymous labels remain stable across prompt arms.
    cards = []
    rng = random.Random(1052026)
    mapping = {}
    labels = {'before': '修改前提示词', 'after': '修改后提示词'}
    for name in names:
        order = ['qwen', 'deepseek']
        rng.shuffle(order)
        mapping[name] = {'A': order[0], 'B': order[1]}
        for repeat in [1, 2]:
            base = lookup.get((order[0], 'before', name, repeat))
            if not base:
                continue
            arms = []
            for arm in ['before', 'after']:
                columns = []
                for label, provider in zip(['A', 'B'], order, strict=True):
                    row = lookup.get((provider, arm, name, repeat))
                    if row:
                        body = html.escape(row['display_text']) if row['display_text'] else '（没有可见正文）'
                        columns.append(f'<article><h3>模型 {label}<small class="identity">{provider}</small></h3>'
                            f'<div class="reply">{body}</div>'
                            f'<div class="metrics">{row["seconds"]:.3f} 秒 · {row["display_metrics"]["characters_without_whitespace"]} 字符</div></article>')
                arms.append(f'<h2>{labels[arm]}</h2><div class="columns">{"".join(columns)}</div>')
            cards.append(f'<section class="card" data-case="{html.escape(name)}" data-repeat="{repeat}">'
                f'<header>{html.escape(name)} · 第 {repeat} 次</header><p class="input">{html.escape(base["user_input"])}</p>{"".join(arms)}</section>')
    (root / 'blind-map.json').write_text(json.dumps(mapping, indent=2), encoding='utf-8')
    options = ''.join(f'<option value="{html.escape(name)}">{html.escape(name)}</option>' for name in names)
    markup = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NeuralCloud · 冻结人设模型对照</title><style>
:root{color-scheme:light dark}body{font:16px/1.75 system-ui,sans-serif;background:#101521;color:#e3e8f2;max-width:1100px;margin:36px auto;padding:0 22px}h1{font-size:30px}small{margin-left:12px;color:#9eabc4}nav{position:sticky;top:0;background:#101521;padding:14px 0;display:flex;gap:12px;z-index:1}select,button{background:#202b40;color:#e3e8f2;border:1px solid #46546c;border-radius:8px;padding:9px 14px}.card{background:#182132;border:1px solid #303e55;border-radius:14px;margin:24px 0;padding:22px}.card header{color:#96bdff;font-weight:650}.input{background:#111927;padding:14px;border-radius:8px}.columns{display:grid;grid-template-columns:1fr 1fr;gap:16px}article{background:#202b40;border-radius:10px;padding:16px}h2{font-size:17px;color:#9eabc4}h3{margin:0 0 10px}.reply{white-space:pre-wrap}.metrics{font-size:12px;color:#9eabc4;margin-top:16px}.identity{display:none}.revealed .identity{display:inline}.note{font-size:14px;color:#9eabc4}section[hidden]{display:none}@media(max-width:680px){.columns{grid-template-columns:1fr}}</style>
<h1>冻结人设 · 模型与提示词对照</h1><p>同一份小雾人设，逐项比较两版提示词与两个模型。A/B 标签在同一场景的前后版本中保持一致。</p>
<p class="note">人设已冻结；修改后人设的同期改写不进入本轮。样例均为合成输入，未发送 QQ 或写人物记忆。协议标记已按项目解析器隐藏，原始文本完整保留。此页尚无独立人工评分。</p>
<style>.metrics{display:none}.revealed .metrics{display:block}</style>
<nav><select id="case"><option value="all">全部场景</option>OPTIONS</select><select id="repeat"><option value="all">两次生成</option><option value="1">第 1 次</option><option value="2">第 2 次</option></select><button id="reveal">显示模型身份与耗时</button></nav>
CARDS<script>const c=document.querySelector('#case'),r=document.querySelector('#repeat');function filter(){document.querySelectorAll('.card').forEach(s=>s.hidden=(c.value!=='all'&&s.dataset.case!==c.value)||(r.value!=='all'&&s.dataset.repeat!==r.value))}c.onchange=r.onchange=filter;document.querySelector('#reveal').onclick=e=>{document.body.classList.toggle('revealed');e.target.textContent=document.body.classList.contains('revealed')?'隐藏模型身份与耗时':'显示模型身份与耗时'};</script></html>'''
    (root / 'comparison.html').write_text(markup.replace('OPTIONS', options).replace('CARDS', '\n'.join(cards)), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
