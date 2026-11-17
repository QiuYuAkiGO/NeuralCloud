"""Validate every Kimi judgment against its sealed input and compute descriptive metrics."""
from __future__ import annotations

import argparse
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
from statistics import mean, median

from prepare_judgments import DIMENSIONS, FLAGS, atomic_json

WEIGHTS = dict(zip(DIMENSIONS, [.25, .20, .25, .15, .15]))
PRIMARY = {'confirmed_delivered', 'confirmed_partial'}


def _artifact(directory: Path, name: str) -> Path:
    assert isinstance(name, str) and Path(name).name == name and name.endswith('.json'), 'invalid artifact name'
    return directory / name


def _call_evidence(job: Path, result: dict, *, kind: str) -> dict:
    response = result.get('response', {})
    return {k: result.get(k) for k in ('job', 'status', 'started_at', 'completed_at', 'attempt', 'request_id')} | {
        'job': job.name, 'kind': kind, 'model': response.get('model'), 'usage': response.get('usage', {}),
        'response_id': response.get('id'), 'job_sha256': hashlib.sha256(job.read_bytes()).hexdigest()}


def _validated_content(job: Path, result: dict) -> dict:
    response = result.get('response', {})
    assert result.get('status') == 200, 'HTTP error'
    assert response.get('model') == 'kimi-k3', 'unexpected response model'
    assert result.get('request_sha256', result.get('job_sha256')) == hashlib.sha256(job.read_bytes()).hexdigest(), 'job changed'
    choices = response.get('choices')
    assert isinstance(choices, list) and len(choices) == 1, 'unexpected choices'
    assert choices[0]['finish_reason'] == 'stop', 'incomplete finish'
    return json.loads(choices[0]['message']['content'])


def _apply_quote_repairs(root: Path, known: dict, ratings: dict, calls: list) -> list[dict]:
    manifest_path = root / 'quote_repairs.json'
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert manifest.get('schema_version') == 'misty-quote-repairs-v1', 'invalid quote repair manifest'
    names = manifest.get('repair_jobs')
    assert isinstance(names, list) and len(set(names)) == len(names), 'duplicate or invalid quote repair jobs'
    audit = []
    repaired = set()
    for name in names:
        job = _artifact(root / 'jobs', name)
        payload = json.loads(job.read_text(encoding='utf-8'))
        expected_rows = json.loads(payload['messages'][-1]['content'].split('修复以下全部引文：\n', 1)[1])
        expected = {row['id']: row for row in expected_rows}
        assert len(expected) == len(expected_rows), 'duplicate quote repair input id'
        for identifier, row in expected.items():
            assert identifier in ratings and identifier not in repaired, f'unknown or multiply repaired id: {identifier}'
            assert row['source_job'] == ratings[identifier]['judge_job'], f'quote source job mismatch: {identifier}'
            assert row['reply'] == known[identifier]['evaluation']['reply'], f'quote repair reply changed: {identifier}'
            assert row['raw_quote'] == ratings[identifier]['quote'], f'quote repair raw quote changed: {identifier}'
        result = json.loads(_artifact(root / 'results', name).read_text(encoding='utf-8'))
        calls.append(_call_evidence(job, result, kind='quote_repair'))
        parsed = _validated_content(job, result)
        assert isinstance(parsed, dict) and set(parsed) == {'repairs'}, 'invalid quote repair response'
        items = parsed['repairs']
        counts = Counter(item['id'] for item in items)
        assert set(counts) == set(expected) and all(n == 1 for n in counts.values()), 'quote repair id coverage mismatch'
        for item in items:
            identifier = item['id']
            assert set(item) == {'id', 'source_job', 'quote'}, f'quote repair fields mismatch: {identifier}'
            assert item['source_job'] == expected[identifier]['source_job'], f'quote repair source mismatch: {identifier}'
            quote = item['quote']
            assert isinstance(quote, str) and 0 < len(quote) <= 80, f'invalid quote repair length: {identifier}'
            assert quote in expected[identifier]['reply'], f'nonliteral repaired quote: {identifier}'
            original = ratings[identifier]
            ratings[identifier] = original | {'raw_quote': original['quote'], 'quote': quote, 'quote_repair_job': name}
            repaired.add(identifier)
            audit.append({'id': identifier, 'source_job': item['source_job'], 'repair_job': name,
                          'raw_quote': original['quote'], 'verified_quote': quote,
                          'scores_and_reason_unchanged': True})
    return audit


def load_results(root: Path, inputs: list[dict]) -> tuple[list[dict], dict]:
    known = {item['evaluation']['id']: item for item in inputs}
    assert len(known) == len(inputs), 'duplicate sealed input id'
    ratings = {}
    errors = []
    calls = []
    supersession_path = root / 'supersession.json'
    supersession = json.loads(supersession_path.read_text(encoding='utf-8')) if supersession_path.exists() else {
        'schema_version': 'misty-rating-supersession-v1', 'replacements': {}}
    assert supersession.get('schema_version') == 'misty-rating-supersession-v1', 'invalid supersession manifest'
    replacements = supersession.get('replacements')
    assert isinstance(replacements, dict), 'invalid replacement mapping'
    jobs = {job.name: job for job in sorted((root / 'jobs').glob('*-ratings*.json'))}
    for identifier, name in replacements.items():
        assert identifier in known and name in jobs, f'unknown supersession target: {identifier}'
        _artifact(root / 'jobs', name)
    superseded = []
    for job in jobs.values():
        payload = json.loads(job.read_text(encoding='utf-8'))
        # Each job includes a separately persisted list of expected ids in its input text.
        user_text = payload['messages'][-1]['content']
        expected_rows = json.loads(user_text.split('逐条评估以下全部记录：\n', 1)[1])
        expected = {row['id']: row for row in expected_rows}
        assert len(expected) == len(expected_rows), f'duplicate expected id: {job.name}'
        active = {identifier: row for identifier, row in expected.items()
                  if identifier not in replacements or replacements[identifier] == job.name}
        for identifier in set(expected) - set(active):
            superseded.append({'id': identifier, 'original_job': job.name, 'replacement_job': replacements[identifier]})
        for identifier, sealed_row in active.items():
            assert identifier in known and sealed_row == known[identifier]['evaluation'], f'Sealed input mismatch: {identifier}'
        result_file = root / 'results' / job.name
        if not result_file.exists():
            if active:
                errors.append(f'{job.name}: result missing')
            else:
                calls.append({'job': job.name, 'kind': 'superseded_rating', 'status': None,
                              'usage': {}, 'evidence_missing': True})
            continue
        result = json.loads(result_file.read_text(encoding='utf-8'))
        calls.append(_call_evidence(job, result, kind='rating' if active else 'superseded_rating'))
        if not active:
            continue
        try:
            parsed = _validated_content(job, result)
            assert isinstance(parsed, dict) and set(parsed) == {'ratings'}, 'invalid rating response'
            items = parsed['ratings']
            counts = Counter(item['id'] for item in items)
            assert set(counts).issubset(expected) and all(counts[identifier] == 1 for identifier in active), 'id coverage mismatch'
            for item in items:
                if item['id'] not in active:
                    continue
                assert item['id'] in known, 'unknown id'
                assert item['id'] not in ratings, 'duplicate active judgment without explicit supersession'
                assert set(item) == {'id', 'scores', 'intent', 'flags', 'context_sufficient', 'confidence', 'quote', 'reason'}, 'rating fields mismatch'
                assert set(item['scores']) == set(DIMENSIONS), 'score dimension mismatch'
                assert all(v is None or (type(v) is int and 1 <= v <= 5) for v in item['scores'].values()), 'score out of range'
                assert isinstance(item['flags'], list) and all(flag in FLAGS for flag in item['flags']), 'unknown flag'
                assert isinstance(item['context_sufficient'], bool), 'invalid context flag'
                assert item['confidence'] in ('low', 'medium', 'high'), 'invalid confidence'
                assert item['intent'] in ('casual_chat', 'task_answer', 'emotional_support', 'memory_identity', 'boundary_refusal', 'proactive', 'other'), 'invalid intent'
                assert isinstance(item['reason'], str) and item['reason'].strip(), 'missing rationale'
                assert isinstance(item['quote'], str), 'invalid quote type'
                ratings[item['id']] = item | {'judge_job': job.name}
        except (AssertionError, KeyError, ValueError, TypeError) as exc:
            errors.append(f'{job.name}: {exc}')
    if errors:
        atomic_json(root / 'validation_errors.json', errors)
        raise ValueError('\n'.join(errors))
    assert set(ratings) == set(known), 'Not all attributable records have a valid judgment'
    for identifier, name in replacements.items():
        assert ratings[identifier]['judge_job'] == name, f'replacement missing target: {identifier}'
    quote_repairs = _apply_quote_repairs(root, known, ratings, calls)
    for identifier, item in ratings.items():
        assert 0 < len(item['quote']) <= 80 and item['quote'] in known[identifier]['evaluation']['reply'], f'nonliteral quote: {identifier}'
    merged = []
    for identifier, source in known.items():
        item = ratings[identifier]
        values = item['scores']
        total_weight = sum(WEIGHTS[k] for k, v in values.items() if v is not None)
        normalized = 100 * sum(WEIGHTS[k] * (v - 1) / 4 for k, v in values.items() if v is not None) / total_weight if total_weight else None
        merged.append(source['evaluation'] | {'source_id': source['source_id'], 'prompt_version': source['source_record'].get('prompt_version'),
                      'generation_model': source['source_record'].get('model'), 'scope': source['source_record'].get('scope'),
                      'rating': item, 'composite_100': round(normalized, 2) if normalized is not None else None,
                      'composite_dimensions': sum(v is not None for v in values.values())})
    usage = Counter()
    for call in calls:
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cached_tokens'):
            value = call['usage'].get(key)
            if isinstance(value, (int, float)):
                usage[key] += value
    evidence = {'valid_record_count': len(merged), 'calls': calls, 'known_usage': dict(usage),
                'unknown_previous_attempts': sum(max(0, (c.get('attempt') or 1) - 1) for c in calls),
                'supersession': {'replacements': replacements, 'reasons': supersession.get('reasons', {}),
                                 'excluded_original_judgments': superseded},
                'quote_repairs': quote_repairs,
                'audit_methods': {'supersession': 'Only explicitly mapped replacement jobs contribute a superseded id; original call evidence is retained.',
                                  'quote_repairs': 'Only validated Kimi quote-only responses replace display quotations. Original quote, score, and rationale remain auditable; scores and rationale are unchanged.'}}
    return merged, evidence


def describe(rows: list[dict]) -> dict:
    scores = {}
    for dimension in DIMENSIONS:
        values = [row['rating']['scores'][dimension] for row in rows if row['rating']['scores'][dimension] is not None]
        scores[dimension] = {'mean': round(mean(values), 3) if values else None,
                             'n': len(values), 'missing': len(rows) - len(values)}
    composites = [row['composite_100'] for row in rows if row['composite_dimensions'] == 5]
    flags = Counter(flag for row in rows for flag in set(row['rating']['flags']))
    lengths = [len(row['reply']) for row in rows]
    return {'n': len(rows), 'scores': scores, 'complete_case_composite_100': round(mean(composites), 2) if composites else None,
            'complete_case_n': len(composites), 'flag_counts': dict(flags),
            'intent_counts': dict(Counter(row['rating']['intent'] for row in rows)),
            'context_sufficient_n': sum(row['rating']['context_sufficient'] for row in rows),
            'low_confidence_n': sum(row['rating']['confidence'] == 'low' for row in rows),
            'naturalness_le_2': sum(row['rating']['scores']['conversational_naturalness'] is not None
                                  and row['rating']['scores']['conversational_naturalness'] <= 2 for row in rows),
            'non_template_le_2': sum(row['rating']['scores']['non_template'] is not None
                                  and row['rating']['scores']['non_template'] <= 2 for row in rows),
            'reply_characters': {'median': median(lengths) if lengths else None, 'mean': round(mean(lengths), 1) if lengths else None},
            'literal_indicators': {'contains_ellipsis': sum('…' in row['reply'] or '...' in row['reply'] for row in rows),
                                   'contains_question_mark': sum('？' in row['reply'] or '?' in row['reply'] for row in rows),
                                   'starts_hesitation': sum(bool(re.match(r'^[\s“"「]*(嗯|唔|呃|那个|啊|诶)[…\.，, ]', row['reply'])) for row in rows)}}


def statistics(rows: list[dict]) -> dict:
    primary = [row for row in rows if row['delivery_status'] in PRIMARY and row['channel'].startswith('qq_')]
    segments = {'confirmed_qq': primary,
                'unconfirmed_generated': [row for row in rows if row['delivery_status'] == 'generated_unconfirmed'],
                'web_diagnostic': [row for row in rows if row['channel'] == 'web_chat'],
                'board': [row for row in rows if row['channel'] == 'board']}
    for channel in sorted({row['channel'] for row in primary}):
        segments[channel] = [row for row in primary if row['channel'] == channel]
    for day in sorted({row['timestamp'][:10] for row in primary}):
        segments['day:' + day] = [row for row in primary if row['timestamp'][:10] == day]
    for version in sorted({row.get('prompt_version') or 'unknown' for row in primary}):
        segments['prompt:' + version] = [row for row in primary if (row.get('prompt_version') or 'unknown') == version]
    near = []
    for index, left in enumerate(primary):
        a = re.sub(r'\W+', '', left['reply'])
        for right in primary[index + 1:]:
            b = re.sub(r'\W+', '', right['reply'])
            ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
            if ratio >= .55 and min(len(a), len(b)) >= 12:
                near.append({'ids': [left['id'], right['id']], 'similarity': round(ratio, 3)})
    phrases = ['我在', '慢慢', '没关系', '不好意思', '不知道', '我不记得', '如果你愿意', '要不要', '这个词', '只是', '不过']
    return {'strata': {key: describe(value) for key, value in segments.items() if value},
            'repeated_phrases': [{'phrase': phrase, 'n': sum(phrase in row['reply'] for row in primary),
                                 'ids': [row['id'] for row in primary if phrase in row['reply']]} for phrase in phrases],
            'near_duplicate_pairs': sorted(near, key=lambda x: -x['similarity']),
            'methods': {'primary': 'One attributed QQ decision with receipt evidence, including delivered text of partial sends.',
                        'dates': 'UTC dates; observational strata, not controlled before/after comparisons.',
                        'composite': '100 * sum(weight * (score-1)/4); aggregate only complete 5-dimension cases.',
                        'weights': WEIGHTS, 'similarity': 'SequenceMatcher on text after removing non-word characters, ratio>=0.55, minimum12chars; heuristic, not semantic template rate.',
                        'phrase_counts': 'Predeclared exploratory literal phrase incidence; not proof that each occurrence is a quality failure.'}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    args = parser.parse_args()
    root = args.run_dir
    inputs = json.loads((root / 'evaluation_inputs.json').read_text(encoding='utf-8'))
    merged, evidence = load_results(root, inputs)
    atomic_json(root / 'judgments.json', merged)
    atomic_json(root / 'judgment_validation.json', evidence)
    atomic_json(root / 'statistics.json', statistics(merged))
    print(json.dumps({'validated': len(merged), 'usage': evidence['known_usage'],
                      'confirmed_qq': statistics(merged)['strata'].get('confirmed_qq')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
