"""Offline checks for paid replay controls, reservations, and retry boundaries."""
from copy import deepcopy
from decimal import Decimal
import json
import sys

import pytest

from tools.production_acceptance import frozen_apiyi_comparison as runner


def case(arm='before'):
    messages = [{'role': 'system', 'content': '冻结人格'}, {'role': 'user', 'content': '谢谢，你听懂了。'}]
    return {'id': 'praise', 'arm': arm, 'repeat': 1, 'kind': 'reply', 'messages': messages,
            'messages_sha256': runner.digest(messages), 'temperature': .7, 'max_tokens': 768}


def test_k3_fixed_sampling_and_thought_cap_preserve_messages():
    original = case()
    payload = runner.payload_for(original, 'kimi')
    assert payload['messages'] == original['messages']
    assert payload['max_completion_tokens'] == 4096
    assert payload['reasoning_effort'] == 'low'
    assert not {'temperature', 'top_p', 'thinking', 'max_tokens'} & payload.keys()
    payload['messages'][0]['content'] = 'changed'
    assert original['messages'][0]['content'] == '冻结人格'


def test_sonnet_avoids_incompatible_sampling_combination():
    payload = runner.payload_for(case(), 'sonnet')
    assert payload['temperature'] == .7 and 'top_p' not in payload
    assert payload['thinking'] == {'type': 'disabled'}
    assert payload['max_tokens'] == 768


def test_budget_includes_full_output_and_cache_write_margin():
    payload = runner.payload_for(case(), 'kimi')
    assert runner.reservation_for(payload, 'kimi') >= runner.fee_upper(1000, 4096, 'kimi')
    assert runner.fee_upper(1_000_000, 0, 'kimi') == Decimal('47.25')
    assert runner.fee_upper(0, 1_000_000, 'sonnet') == Decimal('105.00')
    assert runner.fee_upper(1, 1, 'kimi') == Decimal('.01')
    with pytest.raises(ValueError):
        runner.fee_upper(-1, 0, 'kimi')


def setup_run(monkeypatch, tmp_path, *, budget='10'):
    bundle = {'cases': [case(), case('after')]}
    monkeypatch.setattr(runner, 'verified_bundle', lambda _: (deepcopy(bundle), {'estimated_peak_cost_upper_cny': '.41'}))
    key = tmp_path / 'key.txt'
    key.write_text('test-token-one', encoding='utf-8')
    argv = ['runner', '--root', str(tmp_path), '--key-file', str(key), '--budget-cny', budget]
    monkeypatch.setattr(sys, 'argv', argv)

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, url):
            class Response:
                status_code = 200

                def json(self):
                    return {'data': []}
            return Response()

    monkeypatch.setattr(runner.httpx, 'Client', Client)
    return argv, key


def test_failed_provider_stops_and_same_key_never_retries(monkeypatch, tmp_path):
    setup_run(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(runner, 'dispatch', lambda *a: calls.append(a[2]['model']) or {'http_status': 503})
    runner.main()
    runner.main()
    assert calls == list(runner.MODELS.values())
    report = json.loads((tmp_path / 'apiyi-report.json').read_text(encoding='utf-8'))
    assert len(report['rows']) == 2 and not report['completed']
    assert Decimal(report['estimated_total_cost_upper_cny']) <= 10
    assert all(r['cost_upper_cny'] == r['reserved_cny'] for r in report['rows'])


def test_budget_gate_happens_before_dispatch(monkeypatch, tmp_path):
    setup_run(monkeypatch, tmp_path, budget='.41')
    monkeypatch.setattr(runner, 'dispatch', lambda *a: pytest.fail('paid request exceeded budget'))
    runner.main()
    report = json.loads((tmp_path / 'apiyi-report.json').read_text(encoding='utf-8'))
    assert report['rows'] == []
    assert report['stop_reason'] == 'next_request_exceeds_remaining_budget'


def test_explicit_new_key_resume_retries_only_definite_responses(monkeypatch, tmp_path):
    argv, key = setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, 'dispatch', lambda *a: {'http_status': 503})
    runner.main()
    key.write_text('test-token-two', encoding='utf-8')
    argv.append('--resume-denied-with-new-key')
    monkeypatch.setattr(runner, 'dispatch', lambda *a: {'http_status': 200, 'text': '嗯。', 'seconds': .2,
        'finish_reason': 'stop', 'reasoning_characters': 1, 'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})
    monkeypatch.setattr(runner, 'evaluate', lambda *a: {'usable': True})
    runner.main()
    report = json.loads((tmp_path / 'apiyi-report.json').read_text(encoding='utf-8'))
    assert report['completed'] and len(report['rows']) == 6
    assert len([r for r in report['rows'] if r['attempt'] == 2]) == 2
