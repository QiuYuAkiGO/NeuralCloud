"""Native protocol preserves frozen text; billing and SSE parsing stay auditable."""
from decimal import Decimal
import json

import httpx

from tools.production_acceptance import frozen_hosted_comparison as hosted


def test_native_system_lift_preserves_exact_text_and_message_order():
    messages = [{'role': 'system', 'content': '冻结人设与规则\n保持原文'},
                {'role': 'user', 'content': '起因？'}, {'role': 'assistant', 'content': '嗯'},
                {'role': 'user', 'content': '继续'}]
    case = {'messages': messages, 'kind': 'goal', 'max_tokens': 4096, 'temperature': .7,
            'response_format': {'type': 'json_object'}}
    payload = hosted.hosted_payload(case, 'sonnet')
    assert [{'role': 'system', 'content': payload['system']}, *payload['messages']] == messages
    assert 'response_format' not in payload
    assert messages[0]['role'] == 'system'  # Input was not mutated.


def test_china_tariff_includes_cache_write_plus_miss_input():
    assert hosted.fee(1_000_000, 0, 'kimi') == Decimal('40.00')
    assert hosted.fee(0, 1_000_000, 'kimi') == Decimal('100.00')
    assert hosted.fee(1_000_000, 0, 'sonnet') == Decimal('26.25')


def test_cache_hit_write_and_reasoning_are_not_double_counted():
    usage = {'prompt_tokens': 1000, 'completion_tokens': 100,
             'prompt_tokens_details': {'cached_tokens': 500, 'cache_write_tokens': 200},
             'completion_tokens_details': {'reasoning_tokens': 80}}
    assert hosted.usage_fee(usage, 'kimi') == Decimal('.03')
    assert hosted.usage_fee({'prompt_tokens': 1000, 'completion_tokens': 100}, 'kimi') == Decimal('.05')


def test_native_usage_distinguishes_cache_read_write_and_uncached_input():
    usage = {'prompt_tokens': 1000, 'completion_tokens': 100, 'input_tokens': 100,
             'output_tokens': 100, 'cache_read_input_tokens': 500, 'cache_creation_input_tokens': 400}
    assert hosted.usage_fee(usage, 'sonnet') == Decimal('.03')


def test_only_explicit_no_channel_rejection_releases_reservation():
    rows = [{'slot': 'kimi', 'cost_upper_cny': '1.78', 'http_status': 503,
             'provider_error': {'message': 'groups have no available channels for model kimi-k3'}},
            {'slot': 'sonnet', 'cost_upper_cny': '.84', 'http_status': 500,
             'provider_error': {'message': 'Internal server error'}}]
    total, adjustments = hosted.reconcile_probe({'rows': rows, 'prior_cost_upper_cny': '.41'})
    assert total == Decimal('1.25')
    assert adjustments[0]['retained_upper_cny'] == '0'
    assert Decimal(adjustments[1]['retained_upper_cny']) == Decimal('.84')


def test_native_sse_merges_usage_and_ignores_reasoning_as_body():
    events = [
        {'type': 'message_start', 'message': {'model': 'claude-sonnet-4-5-20250929', 'id': 'test',
                                            'usage': {'input_tokens': 123, 'output_tokens': 1}}},
        {'type': 'content_block_start', 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'delta': {'type': 'thinking_delta', 'thinking': 'hidden'}},
        {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '嗯。'}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 9}},
        {'type': 'message_stop'}]
    body = '\n\n'.join('data: ' + json.dumps(e, ensure_ascii=False) for e in events)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))) as client:
        result = hosted.native_dispatch(client, {'stream': True})
    assert result['text'] == '嗯。'
    assert result['reasoning_characters'] == 6
    assert result['finish_reason'] == 'stop'
    assert result['usage']['prompt_tokens'] == 123
    assert result['usage']['completion_tokens'] == 9
    assert result['first_content_seconds'] is not None
