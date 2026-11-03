"""Audit compact preference exports separately from the full three-repeat rubric."""
import argparse
import collections
import hashlib
import html
import json
from pathlib import Path


def analyze(root, source):
    root, source = Path(root), Path(source)
    raw = source.read_bytes()
    packet = json.loads(raw.decode('utf-8-sig'))
    pairs = {p['pair_id']: p for p in json.loads((root / 'blind-review.json').read_text(encoding='utf8'))}
    keys = json.loads((root / 'blind-key.json').read_text(encoding='utf8'))
    manifest = json.loads((root / 'compact-review-manifest.json').read_text(encoding='utf8'))
    expected = {(p['pair_id'], p['trial_index']) for p in manifest['pairs']}
    scores = {s['pair_id']: s for s in packet['scores']}
    seen = set()
    rows = []
    for choice in packet['compact_choices']:
        ident, index, pref = choice['pair_id'], choice['trial_index'], choice['preference']
        token = (ident, index)
        if token not in expected or token in seen:
            raise ValueError('Unknown or duplicate compact choice')
        if pref not in ('left', 'right', 'tie', 'unevaluable'):
            raise ValueError('Incomplete compact review')
        if scores[ident]['trial_preferences'][index] != pref:
            raise ValueError('Compact choice disagrees with archived trial preference')
        seen.add(token)
        pair, key = pairs[ident], keys[ident]
        trial = pair['trials'][index]
        flags = []
        for side, reply in trial.items():
            if not reply['messages'] and not reply['wait']:
                flags.append(side + ': no_send_and_no_wait')
            if reply['status'] != 'completed' or reply['missing_evidence']:
                flags.append(side + ': incomplete_or_missing_evidence')
        # Confirmed by inspecting both emitted messages; flag explicitly, not all multi-message replies.
        if pair['case_id'] == 'real-030' and key['candidate_group'] == 'B':
            flags.append(key['candidate_side'] + ': near_duplicate_send_manual_review')
        winner = (key['candidate_group'] if pref == key['candidate_side'] else 'A') if pref in ('left', 'right') else pref
        rows.append(dict(pair_id=ident, case_id=pair['case_id'], trial_index=index,
                         candidate_group=key['candidate_group'], preference=pref,
                         winner=winner, flags=flags, replies=trial))
    if seen != expected:
        raise ValueError('Missing compact choices')
    groups = {}
    for group in 'BCDE':
        subset = [r for r in rows if r['candidate_group'] == group]
        clean = [r for r in subset if not r['flags']]
        groups[group] = {'raw': dict(collections.Counter(r['winner'] for r in subset)),
                         'unflagged': dict(collections.Counter(r['winner'] for r in clean))}
    report = dict(source_sha256=hashlib.sha256(raw).hexdigest(), reviewed_scenarios=len(rows),
                  rating_rule=packet['review_subset']['rating_rule'], groups=groups, rows=rows,
                  formal_winner=None, production_changed=False,
                  limitation='12 unique scenarios, one repetition each, unequal group coverage. Preferences do not certify factual correctness, specificity, persona consistency or regression clearance. Prior votes overlap and must not be added as independent cases.')
    (root / 'human-review-compact-final.json').write_bytes(raw)
    (root / 'human-review-compact-final-analysis.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    esc = html.escape
    parts = ['<!doctype html><html lang="zh"><meta charset="utf-8"><title>Misty 最终精简盲评</title><style>body{max-width:1000px;margin:36px auto;padding:0 24px;font:16px/1.7 system-ui;color:#263538;background:#f5f4f0}section,details{background:white;padding:18px;margin:16px 0}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;font:inherit}.flag{color:#914619}</style><h1>Misty 最终精简盲评</h1>',
             '<p>12 / 12 个不同场景已完成，每场景固定使用第 1 次运行。原始选择：改动组 9 胜，基线 3 胜。两例存在行为异常，排除这两例后为 7 胜、3 负；这是描述性统计，不是正式胜出判定。</p>',
             '<section><h2>你的偏好</h2><p>' + esc(report['rating_rule']) + '</p><p>偏好按钮记录整体选择，并未逐条记录原因；下方风格解释是结合你的说明作出的推断。</p></section>',
             '<h2>逐组结果</h2><table><tr><th>组别</th><th>原始胜 / 负</th><th>无已标记异常的胜 / 负</th></tr>']
    names = {'B':'具体表达规则', 'C':'规则＋示例', 'D':'示例＋提示去重', 'E':'去重＋末尾提醒'}
    for g, counts in groups.items():
        parts.append(f'<tr><td>{g}：{names[g]}</td><td>{counts["raw"].get(g,0)} / {counts["raw"].get("A",0)}</td><td>{counts["unflagged"].get(g,0)} / {counts["unflagged"].get("A",0)}</td></tr>')
    parts.append('</table><section><h2>结论与下一步方向</h2><p>支持“简短、有具体观点、允许偶尔有亮点”的表达方向。暂以 C 的规则与示例作为后续提示草案的参考，不宣称 C 是胜出组。E 只有两例，无法确认末尾提醒的独立增益；D 也不能证明去重更优。保留生产现状。</p><p>鲸鱼风筝的“一片更大的海”表达获选；回忆水饺时，较长的基线“一整段被包得好好的时间”也获选。面条案例里，表达自己的口味胜过顺手提出建议。复杂解释仍选了较长版本，不能把闲聊短句规则推广为所有任务都缩短。</p><p>吐槽和文学性应作为可选表达空间，不宜变成每条回复必须完成的任务。偏好胜出不等于事实正确，尤其较长解释仍需单独检查事实与任务完整性。</p></section><h2>全部案例与异常</h2>')
    for row in rows:
        parts.append('<details><summary>' + esc(f'{row["case_id"]} · A 对 {row["candidate_group"]} · 选择 {row["winner"]}') + '</summary>')
        if row['flags']:
            parts.append('<p class="flag">异常：' + esc('; '.join(row['flags'])) + '。保留原票，不作为表达改善证据。</p>')
        for side, reply in row['replies'].items():
            parts.append('<h3>' + esc(side) + ('（已选）' if row['preference'] == side else '') + '</h3><pre>' + esc('\n\n[下一条发送]\n\n'.join(reply['messages']) or ('[等待]' if reply['wait'] else '[未发送，未等待]')) + '</pre>')
        parts.append('</details>')
    parts.append('<p>范围限制：四组使用不同场景，数量不均；未完成三次运行的完整盲评，也未逐项评估具体性、角色一致性或严重回归。旧评分与本次部分重叠，不叠加为独立样本。报告含受控聊天内容，仅供本地查看。</p></html>')
    (root / 'human-review-compact-final-analysis.html').write_text(''.join(parts), encoding='utf8')
    return {k:v for k,v in report.items() if k != 'rows'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root')
    parser.add_argument('source')
    args = parser.parse_args()
    print(json.dumps(analyze(args.root, args.source), ensure_ascii=False, indent=2))
