"""Extend an immutable Qwen/DeepSeek replay with APIyi Sonnet 4.5 and Kimi K3.

No delivery, memory writes, automatic retries, or production configuration changes.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path

import httpx

from tools.production_acceptance.frozen_model_comparison import canonical, digest, dispatch, evaluate, save

MODELS = {'sonnet': 'claude-sonnet-4-5-20250929', 'kimi': 'kimi-k3'}
ENDPOINT = 'https://api.apiyi.com/v1/chat/completions'


def payload_for(case, provider):
    payload = {'model': MODELS[provider], 'messages': deepcopy(case['messages']),
               'stream': case['kind'] == 'stream'}
    if provider == 'sonnet':
        payload.update(max_tokens=case['max_tokens'], temperature=case['temperature'],
                       thinking={'type': 'disabled'})
        # Sonnet 4.5 accepts either temperature or top_p, not both.
    else:
        payload.update(max_completion_tokens=4096, reasoning_effort='low')
        # K3 always thinks and fixes sampling. Cap includes reasoning and visible output.
    if case.get('response_format'):
        payload['response_format'] = deepcopy(case['response_format'])
    if payload['stream']:
        payload['stream_options'] = {'include_usage': True}
    return payload


def fee_upper(input_tokens, output_tokens, provider):
    if any(not isinstance(n, int) or n < 0 for n in (input_tokens, output_tokens)):
        raise ValueError('invalid_usage')
    # APIyi list prices $3/$15 per M, fixed CNY7/USD, no discounts assumed.
    # Conservatively allow cache-write surcharge over ALL input, no cache-hit credits.
    input_rate = Decimal('6.75') if provider == 'kimi' else Decimal('3.75')
    amount = (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * 15) * 7 / 1_000_000
    return amount.quantize(Decimal('0.01'), rounding=ROUND_CEILING)


def reservation_for(payload, provider):
    bound = 2 * len(canonical(payload).encode('utf-8')) + 4096
    return fee_upper(bound, payload.get('max_tokens', payload.get('max_completion_tokens')), provider)


def verified_bundle(root):
    bundle = json.loads((root / 'requests.json').read_text(encoding='utf-8'))
    original = json.loads((root / 'report.json').read_text(encoding='utf-8'))
    assert digest(bundle) == original['bundle_sha256'], 'original_bundle_changed'
    manifest = json.loads((root / 'freeze-manifest.json').read_text(encoding='utf-8'))
    for name, sha in manifest['frozen_source_sha256'].items():
        assert hashlib.sha256((root / 'frozen' / name).read_bytes()).hexdigest() == sha, 'frozen_source_changed'
    for case in bundle['cases']:
        assert digest(case['messages']) == case['messages_sha256'], 'frozen_messages_changed'
    return bundle, original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--budget-cny', type=Decimal, required=True)
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('--resume-denied-with-new-key', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    bundle, original = verified_bundle(root)
    key = args.key_file.read_text(encoding='utf-8-sig').strip()
    assert key and not any(c.isspace() for c in key), 'key_file_must_contain_one_token'
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    cases = sorted(bundle['cases'], key=lambda c: (
        c['repeat'], {'reply': 0, 'stream': 1, 'goal': 2}[c['kind']],
        0 if c['id'] == 'praise' else 1, c['id'], c['arm']))
    plan = {'bundle_sha256': digest(bundle), 'models': MODELS, 'endpoint': ENDPOINT,
            'budget_cny': str(args.budget_cny), 'prior_cost_upper_cny': original['estimated_peak_cost_upper_cny'],
            'rows': [{'provider': p, 'case': c, 'payload': payload_for(c, p)} for c in cases for p in MODELS],
            'pricing_sources': ['https://docs.apiyi.com/model-registry.json', 'https://api.apiyi.com/api/pricing',
                                'https://docs.apiyi.com/pricing'],
            'controls_note': 'Exact frozen messages. Sonnet: temperature 0.7, top_p omitted, thinking disabled; '
                'K3: fixed temperature 1/top_p .95, effort low, total output cap 4096 includes thought; '
                'other models reply cap 768. K3 thought is not exposed as reply text. No automatic retries.',
            'cost_note': 'List rates, CNY7/USD; rounded up per call; includes conservative cache-write allowance; '
                         'not attributed account balance or claimed actual billed amount.'}
    plan_file, report_file = root / 'apiyi-plan.json', root / 'apiyi-report.json'
    if plan_file.exists():
        assert digest(json.loads(plan_file.read_text(encoding='utf-8'))) == digest(plan), 'plan_changed'
    else:
        save(plan_file, plan)
    report = json.loads(report_file.read_text(encoding='utf-8')) if report_file.exists() else {
        'started_at': datetime.now(timezone.utc).isoformat(), 'completed': False, 'plan_sha256': digest(plan),
        'bundle_sha256': digest(bundle), 'budget_cny': str(args.budget_cny), 'models': MODELS,
        'prior_cost_upper_cny': original['estimated_peak_cost_upper_cny'], 'rows': [], 'preflights': []}
    assert report['plan_sha256'] == digest(plan), 'resume_plan_changed'
    blocked = set()
    errors = []

    def capture_error(response):
        if response.status_code != 200:
            response.read()
            try:
                error = response.json().get('error') or {}
                errors.append({k: str(error.get(k, '')).replace(key, '[REDACTED]')[:500]
                               for k in ['code', 'type', 'message']})
            except (ValueError, AttributeError):
                errors.append({'code': 'non_json_error'})

    with httpx.Client(headers={'Authorization': 'Bearer ' + key}, timeout=60,
                      transport=httpx.HTTPTransport(retries=0), trust_env=False, follow_redirects=False,
                      event_hooks={'response': [capture_error]}) as client:
        response = client.get('https://api.apiyi.com/v1/models')
        ids = [v['id'] for v in response.json().get('data', []) if isinstance(v, dict) and 'id' in v]
        report['preflights'].append({'at': datetime.now(timezone.utc).isoformat(), 'key_fingerprint': fingerprint,
                                     'http_status': response.status_code, 'model_ids': ids,
                                     'requested_models_listed': {p: m in ids for p, m in MODELS.items()},
                                     'generation_calls': 0})
        save(report_file, report)
        for item in plan['rows']:
            provider, case, payload = item['provider'], item['case'], item['payload']
            slot = f"{provider}:{case['arm']}:{case['kind']}:{case['id']}:{case['repeat']}"
            previous = [r for r in report['rows'] if r['slot'] == slot]
            if previous:
                last = previous[-1]
                # Only explicit new-key resumes of definite access failures; never retry unknown requests.
                if not (args.resume_denied_with_new_key and last.get('http_status') in {401, 403, 404, 503}
                        and last['key_fingerprint'] != fingerprint and last.get('dispatch_state') == 'response_received'):
                    continue
            if provider in blocked:
                continue
            if args.probe_only and any(r['provider'] == provider for r in report['rows']):
                continue
            reserve = reservation_for(payload, provider)
            spent = Decimal(report['prior_cost_upper_cny']) + sum(Decimal(r['cost_upper_cny']) for r in report['rows'])
            if spent + reserve > args.budget_cny:
                report['stop_reason'] = 'next_request_exceeds_remaining_budget'
                blocked.add(provider)
                continue
            row = {'slot': slot, 'provider': provider, 'id': case['id'], 'arm': case['arm'],
                   'kind': case['kind'], 'repeat': case['repeat'], 'messages_sha256': case['messages_sha256'],
                   'payload_sha256': digest(payload), 'payload': payload, 'key_fingerprint': fingerprint,
                   'attempt': len(previous) + 1, 'dispatch_state': 'reserved_unknown',
                   'reserved_cny': str(reserve), 'cost_upper_cny': str(reserve)}
            report['rows'].append(row)
            save(report_file, report)
            try:
                result = dispatch(client, ENDPOINT, payload)
                row.update(result, dispatch_state='response_received')
                if result.get('http_status') == 200:
                    thought = result['reasoning_characters']
                    row['evaluation'] = evaluate(case, result['text'], result['seconds'], result['finish_reason'],
                                                 0 if provider == 'kimi' else thought, root / 'frozen')
                    row['thinking_expected'] = provider == 'kimi'
                    usage = result.get('usage') or {}
                    if all(isinstance(usage.get(k), int) for k in ['prompt_tokens', 'completion_tokens']):
                        actual = fee_upper(usage['prompt_tokens'], usage['completion_tokens'], provider)
                        if actual > reserve:
                            raise RuntimeError('pricing_reservation_underestimated')
                        row['cost_upper_cny'] = str(actual)
                else:
                    row['provider_error'] = errors[-1] if errors else {}
                    blocked.add(provider)
                    report['stop_reason'] = 'provider_access_failure'
                    # Retain full reservation when no authoritative billed usage is returned.
            except Exception as exc:
                row['error_type'] = type(exc).__name__
                row['dispatch_state'] = 'uncertain_or_validation_failure'
                blocked.add(provider)
                report['stop_reason'] = 'uncertain_request_no_retry'
            report['estimated_total_cost_upper_cny'] = str(Decimal(report['prior_cost_upper_cny'])
                + sum(Decimal(r['cost_upper_cny']) for r in report['rows']))
            save(report_file, report)
            print(canonical({k: row.get(k) for k in ['slot', 'attempt', 'http_status', 'seconds',
                'finish_reason', 'reasoning_characters', 'dispatch_state', 'cost_upper_cny', 'error_type']}), flush=True)
    successful_slots = {r['slot'] for r in report['rows'] if r.get('http_status') == 200}
    report['completed'] = len(successful_slots) == len(plan['rows'])
    report['ended_at'] = datetime.now(timezone.utc).isoformat()
    report['estimated_total_cost_upper_cny'] = str(Decimal(report['prior_cost_upper_cny'])
        + sum(Decimal(r['cost_upper_cny']) for r in report['rows']))
    report['runner_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    verified_bundle(root)
    save(report_file, report)


if __name__ == '__main__':
    main()
