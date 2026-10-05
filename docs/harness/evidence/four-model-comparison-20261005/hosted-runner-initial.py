"""Frozen replay using APIyi native Sonnet and the user's official China Kimi key."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import time

import httpx

from tools.production_acceptance.frozen_apiyi_comparison import MODELS, payload_for, verified_bundle
from tools.production_acceptance.frozen_model_comparison import canonical, digest, dispatch, evaluate, save

URLS = {'sonnet': 'https://api.apiyi.com/v1/messages',
        'kimi': 'https://api.moonshot.cn/v1/chat/completions'}


def hosted_payload(case, provider):
    payload = payload_for(case, provider)
    if provider == 'sonnet':
        assert case['messages'][0]['role'] == 'system'
        assert all(m['role'] in {'user', 'assistant'} for m in case['messages'][1:])
        payload['system'] = payload['messages'].pop(0)['content']
        payload.pop('stream_options', None)
        payload.pop('response_format', None)  # Sonnet 4.5 has no matching json_object switch.
    return payload


def fee(input_tokens, output_tokens, provider):
    if any(not isinstance(n, int) or n < 0 for n in (input_tokens, output_tokens)):
        raise ValueError('invalid_usage')
    # Kimi China: CNY20 miss + CNY20 5min cache write, CNY100 output per M.
    # Sonnet: $3 input, with 1.25 cache write margin; $15 output; fixed CNY7/USD.
    input_rate, output_rate = (Decimal(40), Decimal(100)) if provider == 'kimi' else (Decimal('26.25'), Decimal(105))
    return ((input_tokens * input_rate + output_tokens * output_rate) / 1_000_000).quantize(
        Decimal('.01'), rounding=ROUND_CEILING)


def reserve(payload, provider):
    return fee(2 * len(canonical(payload).encode('utf-8')) + 4096,
               payload.get('max_tokens', payload.get('max_completion_tokens')), provider)


def native_dispatch(client, payload):
    started = time.perf_counter()
    text, thought, first, finish, model, request_id, usage = '', 0, None, None, None, None, {}
    if not payload['stream']:
        response = client.post(URLS['sonnet'], json=payload)
        body = response.json()
        if response.status_code != 200:
            return {'http_status': response.status_code, 'seconds': time.perf_counter() - started,
                    'provider_error': body.get('error') or {}, 'usage': body.get('usage') or {}}
        text = ''.join(b.get('text', '') for b in body.get('content', []) if b.get('type') == 'text')
        thought = sum(len(b.get('thinking', '')) for b in body.get('content', []) if b.get('type') == 'thinking')
        usage, finish, model, request_id = body.get('usage') or {}, body.get('stop_reason'), body.get('model'), body.get('id')
    else:
        with client.stream('POST', URLS['sonnet'], json=payload) as response:
            if response.status_code != 200:
                response.read()
                return {'http_status': response.status_code, 'seconds': time.perf_counter() - started,
                        'provider_error': response.json().get('error') or {}}
            for line in response.iter_lines():
                if not line.startswith('data: '):
                    continue
                event = json.loads(line[6:])
                kind = event.get('type')
                if kind == 'error':
                    raise RuntimeError('native_stream_error')
                if kind == 'message_start':
                    msg = event['message']
                    usage.update(msg.get('usage') or {})
                    model, request_id = msg.get('model'), msg.get('id')
                elif kind == 'message_delta':
                    usage.update(event.get('usage') or {})
                    finish = (event.get('delta') or {}).get('stop_reason') or finish
                elif kind in {'content_block_delta', 'content_block_start'}:
                    delta = event.get('delta') or event.get('content_block') or {}
                    piece = delta.get('text') or ''
                    if piece and first is None:
                        first = time.perf_counter() - started
                    text += piece
                    thought += len(delta.get('thinking') or '')
    normalized_usage = deepcopy(usage)
    if isinstance(usage.get('input_tokens'), int) and isinstance(usage.get('output_tokens'), int):
        normalized_usage.update(prompt_tokens=usage['input_tokens'] + usage.get('cache_read_input_tokens', 0)
            + usage.get('cache_creation_input_tokens', 0), completion_tokens=usage['output_tokens'])
    return {'http_status': 200, 'text': text, 'reasoning_characters': thought,
            'seconds': round(time.perf_counter() - started, 4),
            'first_content_seconds': round(first, 4) if first is not None else None,
            'finish_reason': 'stop' if finish in {'end_turn', 'stop_sequence'} else finish,
            'native_stop_reason': finish, 'usage': normalized_usage, 'response_model': model, 'request_id': request_id}


def reconcile_probe(probe):
    adjustments = []
    for row in probe['rows']:
        amount = Decimal(row['cost_upper_cny'])
        message = row.get('provider_error', {}).get('message', '')
        if (row.get('http_status') == 503 and 'have no available channels for model kimi-k3' in message
                and not row.get('usage') and not row.get('text')):
            amount = Decimal(0)  # Gateway explicitly rejected routing before model inference.
        adjustments.append({'slot': row['slot'], 'previous_reserved_cny': row['cost_upper_cny'],
                            'retained_upper_cny': str(amount), 'basis': 'explicit pre-inference no-channel rejection'
                            if amount == 0 else 'unknown billing; keep full reservation'})
    return Decimal(probe['prior_cost_upper_cny']) + sum(Decimal(a['retained_upper_cny']) for a in adjustments), adjustments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--sonnet-key', type=Path, required=True)
    parser.add_argument('--kimi-key', type=Path, required=True)
    parser.add_argument('--budget-cny', type=Decimal, required=True)
    parser.add_argument('--probe-only', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    bundle, _ = verified_bundle(root)
    legacy = json.loads((root / 'apiyi-report.json').read_text(encoding='utf-8'))
    prior, adjustments = reconcile_probe(legacy)
    keys = {p: path.read_text(encoding='utf-8-sig').strip() for p, path in [('sonnet', args.sonnet_key), ('kimi', args.kimi_key)]}
    assert all(k and not any(c.isspace() for c in k) for k in keys.values())
    fingerprints = {p: hashlib.sha256(k.encode()).hexdigest() for p, k in keys.items()}
    cases = sorted(bundle['cases'], key=lambda c: (c['repeat'], {'reply': 0, 'stream': 1, 'goal': 2}[c['kind']],
        0 if c['id'] == 'praise' else 1, c['id'], c['arm']))
    plan = {'bundle_sha256': digest(bundle), 'models': MODELS, 'urls': URLS, 'budget_cny': str(args.budget_cny),
            'prior_cost_upper_cny': str(prior), 'probe_reconciliation': adjustments,
            'rows': [{'provider': p, 'case': c, 'payload': hosted_payload(c, p)} for c in cases for p in MODELS],
            'pricing': {'sonnet': {'currency': 'USD', 'input': 3, 'output': 15, 'cny_per_usd': 7},
                        'kimi': {'currency': 'CNY', 'input_miss': 20, 'input_hit': 2,
                                 'cache_write_5min': 20, 'cache_write_1h': 40, 'output': 100}},
            'sources': ['https://docs.apiyi.com/model-registry.json', 'https://docs.apiyi.com/pricing',
                        'https://platform.kimi.com/docs/pricing/chat', 'https://platform.kimi.ai/docs/guide/kimi-k3-quickstart'],
            'limitations': 'Identical message text/order. Sonnet initial system lifted into native system field, '
                'temperature=.7/top_p omitted, disabled thinking, no json_object switch. K3 official China, '
                'effort low, fixed temperature=1/top_p=.95, total cap4096 includes thought (others reply768). '
                'Gateway model identity not independently verified; no routing substitutes or automatic retries.'}
    plan_file, report_file = root / 'hosted-plan.json', root / 'hosted-report.json'
    if plan_file.exists():
        assert digest(json.loads(plan_file.read_text(encoding='utf-8'))) == digest(plan), 'plan_changed'
    else:
        save(plan_file, plan)
    report = json.loads(report_file.read_text(encoding='utf-8')) if report_file.exists() else {
        'started_at': datetime.now(timezone.utc).isoformat(), 'completed': False, 'bundle_sha256': digest(bundle),
        'plan_sha256': digest(plan), 'budget_cny': str(args.budget_cny), 'models': MODELS, 'urls': URLS,
        'prior_cost_upper_cny': str(prior), 'probe_reconciliation': adjustments, 'rows': [], 'preflights': []}
    assert report['plan_sha256'] == digest(plan)
    blocked = {r['provider'] for r in report['rows'] if r.get('http_status') != 200
               or r.get('dispatch_state') != 'response_received'}
    with ExitStack() as stack:
        clients = {}
        for provider, key in keys.items():
            headers = {'Authorization': 'Bearer ' + key}
            if provider == 'sonnet':
                headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
            clients[provider] = stack.enter_context(httpx.Client(headers=headers, timeout=60,
                transport=httpx.HTTPTransport(retries=0), trust_env=False, follow_redirects=False))
        for item in plan['rows']:
            p, case, payload = item['provider'], item['case'], item['payload']
            slot = f"{p}:{case['arm']}:{case['kind']}:{case['id']}:{case['repeat']}"
            if p in blocked or any(r['slot'] == slot for r in report['rows']):
                continue
            if args.probe_only and any(r['provider'] == p for r in report['rows']):
                continue
            reservation = reserve(payload, p)
            spent = prior + sum(Decimal(r['cost_upper_cny']) for r in report['rows'])
            if spent + reservation > args.budget_cny:
                blocked.add(p)
                report['stop_reason'] = 'next_request_exceeds_remaining_budget'
                continue
            row = {'slot': slot, 'provider': p, 'id': case['id'], 'arm': case['arm'], 'kind': case['kind'],
                   'repeat': case['repeat'], 'messages_sha256': case['messages_sha256'], 'payload': payload,
                   'payload_sha256': digest(payload), 'key_fingerprint': fingerprints[p], 'attempt': 1,
                   'endpoint': URLS[p], 'dispatch_state': 'reserved_unknown', 'reserved_cny': str(reservation),
                   'cost_upper_cny': str(reservation)}
            report['rows'].append(row)
            save(report_file, report)
            try:
                result = native_dispatch(clients[p], payload) if p == 'sonnet' else dispatch(clients[p], URLS[p], payload)
                if result.get('provider_error'):
                    result['provider_error'] = json.loads(json.dumps(result['provider_error']).replace(keys[p], '[REDACTED]'))
                row.update(result, dispatch_state='response_received')
                if result.get('http_status') == 200:
                    row['evaluation'] = evaluate(case, result['text'], result['seconds'], result['finish_reason'],
                                                 0 if p == 'kimi' else result['reasoning_characters'], root / 'frozen')
                    row['thinking_expected'] = p == 'kimi'
                    usage = result.get('usage') or {}
                    if all(isinstance(usage.get(k), int) for k in ['prompt_tokens', 'completion_tokens']):
                        actual = fee(usage['prompt_tokens'], usage['completion_tokens'], p)
                        if actual > reservation:
                            raise RuntimeError('reservation_underestimated')
                        row['cost_upper_cny'] = str(actual)
                else:
                    blocked.add(p)
                    report['stop_reason'] = 'provider_access_failure'
            except Exception as exc:
                row.update(error_type=type(exc).__name__, dispatch_state='uncertain_or_validation_failure')
                blocked.add(p)
                report['stop_reason'] = 'uncertain_request_no_retry'
            report['estimated_total_cost_upper_cny'] = str(prior + sum(Decimal(r['cost_upper_cny']) for r in report['rows']))
            save(report_file, report)
            print(canonical({k: row.get(k) for k in ['slot', 'http_status', 'seconds', 'finish_reason',
                'reasoning_characters', 'cost_upper_cny', 'error_type']}), flush=True)
    report['completed'] = len(report['rows']) == len(plan['rows']) and all(r.get('http_status') == 200 for r in report['rows'])
    report['ended_at'] = datetime.now(timezone.utc).isoformat()
    report['estimated_total_cost_upper_cny'] = str(prior + sum(Decimal(r['cost_upper_cny']) for r in report['rows']))
    report['runner_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    verified_bundle(root)
    save(report_file, report)


if __name__ == '__main__':
    main()
