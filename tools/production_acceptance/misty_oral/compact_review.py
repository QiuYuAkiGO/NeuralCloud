"""Build a small review subset without changing the full experiment or its votes."""
import argparse
import collections
import hashlib
import json
from pathlib import Path

from review import clean_context

SCENES = ['real-030', 'real-009', 'real-073', 'real-020',
          'synthetic-00', 'synthetic-01', 'synthetic-02', 'synthetic-03',
          'synthetic-04', 'synthetic-07', 'synthetic-08', 'synthetic-10']
RULE = '通常优先简短；长度接近时优先自然；有特别的表达（如吐槽、文学感）时，可以优先于长度。无需为了简短牺牲明确请求的完整回答。'


def build(root, source):
    root, source = Path(root), Path(source)
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    imported = json.loads(raw.decode('utf-8-sig'))
    scores = {s['pair_id']: s for s in imported['scores']}
    pairs = json.loads((root / 'blind-review.json').read_text(encoding='utf8'))
    key = json.loads((root / 'blind-key.json').read_text(encoding='utf8'))
    selected = {}
    counts = collections.Counter()
    # Reuse existing reviewed pairs regardless of their preferred side.
    for cid in SCENES:
        matches = [p for p in pairs if p['case_id'] == cid and p['pair_id'] in scores]
        if matches:
            selected[cid] = matches[0]
            counts[key[matches[0]['pair_id']]['candidate_group']] += 1
    for cid in SCENES:
        if cid not in selected:
            matches = [p for p in pairs if p['case_id'] == cid]
            if not matches:
                raise ValueError(f'Missing scenario: {cid}')
            pair = min(matches, key=lambda p: (counts[key[p['pair_id']]['candidate_group']], key[p['pair_id']]['candidate_group']))
            selected[cid] = pair
            counts[key[pair['pair_id']]['candidate_group']] += 1
    view = [{**selected[cid], 'context': clean_context(selected[cid]['context'])} for cid in SCENES]
    manifest = {
        'source_sha256': digest, 'rating_rule': RULE,
        'selection': 'One pair per distinct selected scenario; reuse rated pairs, then balance groups. Always original repetition 0. No selection by response or preference outcome.',
        'limitation': 'Exploratory preference subset, not the full three-repetition evaluation; cannot establish formal promotion eligibility.',
        'pairs': [{'pair_id': p['pair_id'], 'case_id': p['case_id'], 'trial_index': 0} for p in view],
        'imported_rows': len(scores),
        'completed_selected': sum(bool(scores.get(p['pair_id'], {}).get('trial_preferences', [None])[0]) for p in view),
    }
    def js(value):
        return json.dumps(value, ensure_ascii=False).replace('<', '\\u003c')
    template = Path(__file__).with_name('review.html').read_text(encoding='utf8')
    template = template.replace('每个案例有三次对照，不需要打分或写评语。', '精简为12个不同场景，每个场景只看一次对照。已有选择已导入。')
    template = template.replace('<header>', '<p>' + RULE + '</p><header>')
    template = template.replace('<option value="12">先看12对</option><option value="all">全部配对</option>', '<option value="all">精简12个场景</option>')
    template = template.replace('/*DATA*/[]', js(view))
    template = template.replace("'misty-blind-click-v2:'", "'misty-compact-v1:'")
    template = template.replace('let scores={};let index=0;', 'let scores=' + js(scores) + ';let index=0;\nconst subset=' + js(manifest) + ';')
    template = template.replace("scores=JSON.parse(localStorage.getItem(storageKey)||'{}')", "Object.assign(scores,JSON.parse(localStorage.getItem(storageKey)||'{}'))")
    template = template.replace('i<p.trials.length', 'i<1')
    template = template.replace("scores[p.pair_id]?.preference).length", "scores[p.pair_id]?.trial_preferences?.[0]).length")
    template = template.replace("version:2,mode:'preference_only',scores:Object.values(scores)", "version:2,mode:'preference_only',review_subset:subset,compact_choices:pairs.map(p=>({pair_id:p.pair_id,trial_index:0,preference:scores[p.pair_id]?.trial_preferences?.[0]||null})),scores:Object.values(scores)")
    template = template.replace("a.download='misty-human-review.json'", "a.download='misty-human-review-compact.json'")
    template = template.replace(';show();\n</script>', ";index=Math.max(0,pairs.findIndex(p=>!scores[p.pair_id]?.trial_preferences?.[0]));show();\n</script>")
    (root / f'human-review-import-{digest[:12]}.json').write_bytes(raw)
    (root / 'compact-review-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf8')
    (root / 'compact-review.html').write_text(template, encoding='utf8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root')
    parser.add_argument('source')
    args = parser.parse_args()
    print(json.dumps(build(args.root, args.source), ensure_ascii=False, indent=2))
