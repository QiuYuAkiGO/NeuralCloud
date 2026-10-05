"""Budgeted replay of immutable before/after QQ requests on two models.

Only synthetic generation and validation; no Runtime, tools or delivery writes.
Credentials are read at dispatch and never serialized into the bundle or report.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import random
import sys
import time

import httpx


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def save(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for attempt in range(20):
        try:
            temp.replace(path)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def peak_cost(input_tokens, output_tokens, cached=0):
    """CNY at peak pricing; rounded upwards to a cent for each request."""
    cost = (Decimal(input_tokens - cached) * 2 + Decimal(cached) * Decimal('0.04')
            + Decimal(output_tokens) * 8) / 1_000_000
    return cost.quantize(Decimal('0.01'), rounding=ROUND_CEILING)


def reserve_cost(payload):
    # UTF-8 byte fallback bounds text tokens; double bytes plus 4096 allow protocol overhead.
    bound = 2 * len(canonical(payload).encode('utf-8')) + 4096
    return peak_cost(bound, payload['max_tokens'])


def prepare_bundle(root):
    sys.path.insert(0, str(root / 'frozen'))
    from harness.cognitive import goals
    from harness.cognitive.prompts import persona_prefix
    from harness.contracts.goals import GoalDecisionRequest
    from harness.core.models import PersonaCore
    from tests.test_capability_cognition import FakeLLM, host
    from tests.test_goal_cognition import goal_command

    before = json.loads((root / 'reference/baseline.json').read_text(encoding='utf-8'))
    after = json.loads((root / 'reference/candidate.json').read_text(encoding='utf-8'))
    assert before['completed'] and after['completed']
    assert before['conditions']['cases_sha256'] == after['conditions']['cases_sha256']
    frozen_prefix = before['rows'][0]['messages'][0]['content']
    persona = PersonaCore.model_validate(json.loads((root / 'persona.json').read_text(encoding='utf-8')))
    assert persona_prefix(persona) == frozen_prefix
    candidate_persona = PersonaCore.model_validate(json.loads(
        (root / 'reference/candidate/presets/builtin/misty.json').read_text(encoding='utf-8-sig'))['persona'])
    after_prefix = persona_prefix(candidate_persona)
    cases = []
    for arm, reference, original_prefix in [('before', before, frozen_prefix), ('after', after, after_prefix)]:
        for row in reference['rows']:
            messages = deepcopy(row['messages'])
            assert messages[0]['role'] == 'system' and messages[0]['content'] == original_prefix
            messages[0]['content'] = frozen_prefix
            cases.append({'id': row['id'], 'arm': arm, 'repeat': row['repeat'], 'kind': 'reply',
                          'user_input': row['input'], 'detail': row['detail'], 'messages': messages,
                          'messages_sha256': digest(messages), 'temperature': 0.7, 'max_tokens': 768})
        # A small separate SSE sample; main quality comparison remains the project's nonstream path.
        for name in ['choice', 'expanded']:
            base = next(c for c in cases if c['arm'] == arm and c['id'] == name and c['repeat'] == 1)
            cases.append({**deepcopy(base), 'kind': 'stream', 'repeat': 1})
    background = '\n'.join(f'合成备忘{i}：晚上整理书桌、喝茶和休息；这些背景资料未记录本轮动作结果。' for i in range(40))
    for name in ['no_offers', 'start', 'unfinished', 'verified']:
        request = goal_command(None if name == 'start' else [], active=name in {'unfinished', 'verified'}, user=True)
        for offer in request.payload['offers']:
            offer['valid_until'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        context = request.payload['goal_context']
        context['requested_goal']['statement'] = '通过留言板写下一条简短的休息安排，收到实际保存回执才算完成。'
        context['appraisal_evidence_refs'] = []
        if name in {'unfinished', 'verified'}:
            context['active_episode']['steps'] = [{'status': 'planned' if name == 'unfinished' else 'executed',
                'statement': '保存一条休息安排', 'completion_criteria': ['留言板保存成功并收到回执']}]
            context['active_episode']['completion_criteria'] = ['留言板保存成功并收到回执']
            context['outcomes'] = ([{'status': 'error', 'evidence_refs': []}] if name == 'unfinished' else [{
                'status': 'completed', 'evidence_refs': ['receipt:1'], 'step_index': 0,
                'receipt': {'source': 'board_storage', 'saved': True, 'readback_verified': True,
                            'text': '今晚喝茶后整理书桌，然后早点休息。'},
                'summary': '留言板已保存休息安排并返回独立回执 receipt:1，回读内容一致。'}])
        fake = FakeLLM({'episode_operation': 'wait', 'choice': {'kind': 'wait'}})
        owner = host(fake)
        owner.chat.memory.get_persona = lambda: persona
        from harness.core.inference import ContextSegment
        owner.shared.local_context = lambda event: [ContextSegment(kind='memory', text=background, source_ids=['fact:1'])]
        asyncio.run(goals.prepare(owner, request))
        messages = deepcopy(fake.calls[0][0])
        assert messages[0]['content'].startswith(frozen_prefix)
        validated = GoalDecisionRequest.model_validate(request.payload)
        cases.append({'id': name, 'arm': 'goal', 'repeat': 1, 'kind': 'goal', 'messages': messages,
                      'messages_sha256': digest(messages), 'temperature': 0.7, 'max_tokens': 4096,
                      'request': validated.model_dump(mode='json'), 'response_format': {'type': 'json_object'}})
    # Stable randomized case order, with provider order alternating within each pair.
    random.Random(1052026).shuffle(cases)
    bundle = {'created_at': datetime.now(timezone.utc).isoformat(), 'synthetic': True,
              'persona_prefix_sha256': digest(frozen_prefix), 'persona_json_sha256': digest(persona.model_dump(mode='json')),
              'persona_source': 'pre-edit baseline snapshot', 'cases': cases,
              'scope': '2 models × 2 QQ prompt versions × 8 cases × 2 repeats; 8 SSE calls and 8 goal calls separately',
              'controls': {'same_persona': True, 'same_messages_per_pair': True, 'temperature': 0.7, 'top_p': 0.8,
                           'thinking': 'disabled', 'max_reply_tokens': 768, 'max_goal_tokens': 4096,
                           'automatic_retries': 0, 'candidate_persona_edits_excluded': True,
                           'goal_json_mode': 'same json_object on both; not the DeepSeek Responses schema route'},
              'pricing': {'source': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
                          'currency': 'CNY', 'peak_per_million': {'input_miss': 2, 'input_hit': 0.04, 'output': 8},
                          'account_balance_changes_not_attributed': 'Other sessions also use this API key'}}
    save(root / 'requests.json', bundle)
    return bundle


def evaluate(case, text, elapsed, finish, reasoning, frozen):
    sys.path.insert(0, str(frozen))
    from harness.qq.reply_context import style_diagnostics
    if case['kind'] != 'goal':
        return {'usable': bool(text.strip()) and finish == 'stop' and not reasoning,
                'style': style_diagnostics(text, detail_level=case['detail'], segments=len(text.split('\n\n'))),
                'quality_requires_review': True}
    from harness.cognitive import goals
    from harness.cognitive import capabilities
    from harness.contracts.goals import GoalDecisionRequest
    try:
        output = goals._parse(text)
        request = GoalDecisionRequest.model_validate(case['request'])
        goals._validate_episode(output, request)
        if output.choice.offer_id is not None:
            offer = next(o for o in request.offers if o.offer_id == output.choice.offer_id)
            capabilities._bound_choice(output.choice, offer, now=request.deadline_at - timedelta(seconds=60))
        operation = output.episode_operation
        semantic = (operation == 'wait' if case['id'] == 'no_offers' else
                    operation == 'start' and output.choice.kind == 'act' if case['id'] == 'start' else
                    operation != 'complete' if case['id'] == 'unfinished' else operation == 'complete')
        return {'schema_valid': True, 'semantic_passed': semantic, 'under_goal_deadline': elapsed < 25,
                'passed': semantic and elapsed < 25 and finish == 'stop' and not reasoning,
                'decision': output.model_dump(mode='json')}
    except Exception as exc:
        return {'schema_valid': False, 'passed': False, 'error_code': str(exc)[:180]}


def dispatch(client, url, payload):
    started = time.perf_counter()
    first, usage, response_model, reasoning, text, finish = None, {}, None, '', '', None
    if not payload['stream']:
        response = client.post(url, json=payload)
        body = response.json()
        if response.status_code != 200:
            return {'http_status': response.status_code, 'error_code': (body.get('error') or {}).get('code'),
                    'seconds': time.perf_counter() - started, 'usage': body.get('usage') or {}}
        choice = body['choices'][0]
        message = choice['message']
        text = message.get('content') or ''
        reasoning = message.get('reasoning_content') or message.get('reasoning') or ''
        usage, response_model, finish = body.get('usage') or {}, body.get('model'), choice.get('finish_reason')
        timings = body.get('timings')
        request_id = body.get('id')
    else:
        timings, request_id = None, None
        with client.stream('POST', url, json=payload) as response:
            if response.status_code != 200:
                response.read()
                return {'http_status': response.status_code, 'seconds': time.perf_counter() - started}
            for line in response.iter_lines():
                if not line.startswith('data: ') or line == 'data: [DONE]':
                    continue
                event = json.loads(line[6:])
                usage = event.get('usage') or usage
                response_model = event.get('model') or response_model
                timings = event.get('timings') or timings
                request_id = event.get('id') or request_id
                for choice in event.get('choices', []):
                    delta = choice.get('delta', {})
                    piece = delta.get('content') or ''
                    if piece and first is None:
                        first = time.perf_counter() - started
                    text += piece
                    reasoning += delta.get('reasoning_content') or delta.get('reasoning') or ''
                    finish = choice.get('finish_reason') or finish
    return {'http_status': 200, 'seconds': round(time.perf_counter() - started, 4),
            'first_content_seconds': round(first, 4) if first is not None else None,
            'text': text, 'reasoning_characters': len(reasoning), 'finish_reason': finish,
            'usage': usage, 'response_model': response_model, 'request_id': request_id, 'timings': timings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--local-key', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    bundle_file = root / 'requests.json'
    bundle = json.loads(bundle_file.read_text(encoding='utf-8')) if bundle_file.exists() else prepare_bundle(root)
    manifest = json.loads((root / 'freeze-manifest.json').read_text(encoding='utf-8'))
    for name, sha in manifest['frozen_source_sha256'].items():
        assert hashlib.sha256((root / 'frozen' / name).read_bytes()).hexdigest() == sha, 'frozen_source_changed'
    if args.prepare_only:
        print(json.dumps({'bundle_sha256': digest(bundle), 'cases_per_model': len(bundle['cases']),
                          'maximum_single_reservation_cny': str(max(reserve_cost(c) for c in bundle['cases']))}))
        return
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    if config['base_url'].rstrip('/') not in {'https://api.deepseek.com', 'https://api.deepseek.com/v1'}:
        raise ValueError('official_deepseek_endpoint_required')
    if config['model'] not in {'deepseek-flash', 'deepseek-v4-flash'}:
        raise ValueError('frozen_flash_pricing_only')
    clients = {
        'deepseek': httpx.Client(headers={'Authorization': 'Bearer ' + config['api_key']}, timeout=45, trust_env=False),
        'qwen': httpx.Client(headers={'Authorization': 'Bearer ' + args.local_key.read_text(encoding='utf-8').strip()},
                             timeout=45, trust_env=False),
    }
    urls = {'deepseek': config['base_url'].rstrip('/') + '/chat/completions',
            'qwen': 'http://127.0.0.1:8080/v1/chat/completions'}
    models = {'deepseek': config['model'], 'qwen': 'qwen3.8-27b'}
    del config
    report_file = root / 'report.json'
    report = (json.loads(report_file.read_text(encoding='utf-8')) if report_file.exists() else {
        'started_at': datetime.now(timezone.utc).isoformat(), 'completed': False,
        'bundle_sha256': digest(bundle), 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'budget_cny': '1.00', 'estimated_peak_cost_upper_cny': '0.00', 'rows': [], 'models': models})
    assert report['bundle_sha256'] == digest(bundle) and report['models'] == models
    assert report['runner_sha256'] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'resume_runner_changed'
    budget = Decimal(report['budget_cny'])
    blocked = False
    try:
        for index, case in enumerate(bundle['cases']):
            order = ['qwen', 'deepseek'] if index % 2 == 0 else ['deepseek', 'qwen']
            for provider in order:
                slot = f"{provider}:{case['arm']}:{case['kind']}:{case['id']}:{case['repeat']}"
                if any(row['slot'] == slot for row in report['rows']):
                    continue  # Unknown dispatched slots are never silently repeated.
                if provider == 'deepseek' and blocked:
                    continue
                payload = {'model': models[provider], 'messages': case['messages'], 'temperature': case['temperature'],
                           'top_p': 0.8, 'max_tokens': case['max_tokens'], 'stream': case['kind'] == 'stream',
                           'frequency_penalty': 0, 'presence_penalty': 0}
                if case.get('response_format'):
                    payload['response_format'] = case['response_format']
                if payload['stream']:
                    payload['stream_options'] = {'include_usage': True}
                if provider == 'deepseek':
                    payload['thinking'] = {'type': 'disabled'}
                else:
                    payload.update(top_k=0, min_p=0, repeat_penalty=1,
                                   chat_template_kwargs={'enable_thinking': False})
                reservation = reserve_cost(payload) if provider == 'deepseek' else Decimal(0)
                spent = Decimal(report['estimated_peak_cost_upper_cny'])
                if spent + reservation > budget:
                    report['stop_reason'] = 'next_request_exceeds_remaining_budget'
                    blocked = True
                    save(report_file, report)
                    continue
                row = {'slot': slot, 'provider': provider, 'id': case['id'], 'arm': case['arm'],
                       'kind': case['kind'], 'repeat': case['repeat'], 'messages_sha256': case['messages_sha256'],
                       'payload_sha256': digest(payload), 'dispatch_state': 'reserved_unknown',
                       'reserved_cny': str(reservation), 'cost_upper_cny': str(reservation)}
                report['rows'].append(row)
                report['estimated_peak_cost_upper_cny'] = str(spent + reservation)
                save(report_file, report)  # Persist the paid slot before dispatch.
                try:
                    result = dispatch(clients[provider], urls[provider], payload)
                    row.update(result, dispatch_state='response_received')
                    if result.get('http_status') == 200:
                        row['evaluation'] = evaluate(case, result['text'], result['seconds'], result['finish_reason'],
                                                     result['reasoning_characters'], root / 'frozen')
                    usage = result.get('usage') or {}
                    if provider == 'deepseek' and all(isinstance(usage.get(k), int) for k in ['prompt_tokens', 'completion_tokens']):
                        cached = usage.get('prompt_cache_hit_tokens', (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0)) or 0
                        if (usage['prompt_tokens'] < 0 or usage['completion_tokens'] < 0
                                or not isinstance(cached, int) or not 0 <= cached <= usage['prompt_tokens']):
                            raise ValueError('invalid_usage_keep_full_reservation')
                        actual = peak_cost(usage['prompt_tokens'], usage['completion_tokens'], cached)
                        if actual > reservation:
                            raise RuntimeError('pricing_reservation_underestimated')
                        row['cost_upper_cny'] = str(actual)
                        row['cost_at_peak_unrounded_cny'] = float((Decimal(usage['prompt_tokens'] - cached) * 2
                            + Decimal(cached) * Decimal('0.04') + Decimal(usage['completion_tokens']) * 8) / 1_000_000)
                        report['estimated_peak_cost_upper_cny'] = str(spent + actual)
                    if result.get('http_status') in {401, 402, 403, 429} and provider == 'deepseek':
                        blocked = True
                        report['stop_reason'] = 'provider_access_or_rate_blocked'
                except Exception as exc:
                    row['error_type'] = type(exc).__name__
                    row['dispatch_state'] = 'uncertain_or_validation_failure'
                save(report_file, report)
                print(canonical({k: row.get(k) for k in ['slot', 'http_status', 'seconds', 'first_content_seconds',
                                                        'dispatch_state', 'cost_upper_cny', 'error_type']}), flush=True)
        report['completed'] = len(report['rows']) == 2 * len(bundle['cases'])
        assert digest(json.loads(bundle_file.read_text(encoding='utf-8'))) == report['bundle_sha256'], 'bundle_changed'
        report['ended_at'] = datetime.now(timezone.utc).isoformat()
    finally:
        for client in clients.values():
            client.close()
        save(report_file, report)


if __name__ == '__main__':
    main()
