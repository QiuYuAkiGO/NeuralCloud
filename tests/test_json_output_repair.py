import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from harness.config import Settings
from harness.llm.client import OpenAICompatClient, parse_json_object
from harness.llm.deadline import model_deadline
from harness.llm.json_output import JSONOutputError, decode_model_json

SCHEMA = {"type": "object", "properties": {"action": {"enum": ["wait", "speak"]}},
          "required": ["action"], "additionalProperties": False}


@pytest.fixture
def cfg(tmp_path):
    return Settings(_env_file=None, trace_dir=tmp_path, json_repair_enabled=True)


@pytest.mark.parametrize("text,expected", [
    ('说明：```json\n{"action":"wait"}\n```\n结束。', {"action": "wait"}),
    ('\ufeff{"action":"wait",}', {"action": "wait"}),
    ("{action:'wait'}", {"action": "wait"}),
    ('{"action":"wait"', {"action": "wait"}),
    ('{"action":"wait}', {"action": "wait"}),
    ('{"action":"wait" "extra":1}', {"action": "wait"}),
    ('{"action":"speak"} 更正：{"action":"wait"}', {"action": "wait"}),
    ('{"action":"speak","action":"wait"}', {"action": "wait"}),
    ("{action:'speak',action:'wait',}", {"action": "wait"}),
    ('{"action":"wait","extra":{"a":1}}', {"action": "wait"}),
])
def test_repairs(text, expected, cfg):
    result = decode_model_json(text, schema=SCHEMA, settings=cfg)
    assert result.value == expected
    assert json.loads(result.text) == expected
    assert result.actions
    records = list((cfg.trace_dir / 'json-repair').glob('*.json'))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding='utf-8'))['original'] == text


@pytest.mark.parametrize('text', ['', 'nothing here', '[]', '{"action":"other"}', '{}',
    '{"action":3}', '{"action":"wait"} {}', '{"action":NaN}', '{"action":Infinity}',
    '{action:NaN,}', '{"action":1e999}', '{"action":}', '['*65+']'*65,
    '{}'*33, 'x'*(1024*1024+1)], ids=lambda text: str(len(text)) + '-' + text[:20])
def test_rejections(text, cfg):
    with pytest.raises(JSONOutputError):
        decode_model_json(text, schema=SCHEMA, settings=cfg)


def test_strings_and_arrays_are_not_root_candidates(cfg):
    text = json.dumps({'s': '中文 " {} [] // 尾巴', 'items': [{'x': 1}, {'x': 2}]}, ensure_ascii=False)
    result = decode_model_json(text, settings=cfg)
    assert result.text == text and result.actions == {}
    assert not (cfg.trace_dir / 'json-repair').exists()


@pytest.mark.parametrize('schema,expected', [
    (None, None), ({'type': ['string', 'null']}, None), ({'type': 'string'}, ''),
])
def test_missing_value_not_missing_key(cfg, schema, expected):
    s = {'type': 'object', 'properties': {'x': schema}, 'required': ['x']} if schema else None
    assert decode_model_json('{"x":}', schema=s, settings=cfg).value == {'x': expected}
    if s:
        with pytest.raises(JSONOutputError):
            decode_model_json('{}', schema=s, settings=cfg)


def test_schema_refs_unions_patterns_free_keys(cfg):
    schema = {'type': 'object', 'properties': {
        'items': {'type': 'array', 'items': {'$ref': '#/$defs/item'}},
        'free': {'type': 'object'},
        'choice': {'anyOf': [{'type': 'null'}, {'$ref': '#/$defs/item'}]}},
        '$defs': {'item': {'type': 'object', 'properties': {'x': {'type': 'integer'}},
                          'patternProperties': {'^tag_': {'type': 'string'}}, 'additionalProperties': False}},
        'additionalProperties': False}
    value = {'items': [{'x': 1, 'extra': 2, 'tag_a': 'ok'}], 'free': {'arbitrary': 3},
             'choice': {'x': 2, 'extra': 4}, 'extra': 5}
    result = decode_model_json(json.dumps(value), schema=schema, settings=cfg)
    assert result.value == {'items': [{'x': 1, 'tag_a': 'ok'}], 'free': {'arbitrary': 3}, 'choice': {'x': 2}}
    assert result.actions['extra_fields'] == 3
    assert 'extra' in value


def test_ambiguous_union_does_not_choose_arbitrary_branch(cfg):
    schema = {'anyOf': [{'type': 'object', 'properties': {k: {}}, 'required': [k], 'additionalProperties': False}
                        for k in ['a', 'b']]}
    with pytest.raises(JSONOutputError, match='ambiguous'):
        decode_model_json('{"a":1,"b":2}', schema=schema, settings=cfg)


def test_allof_missing_value_uses_all_constraints(cfg):
    schema = {'allOf': [{'properties': {'x': {'type': ['null', 'string']}}},
                        {'properties': {'x': {'enum': ['']}}}]}
    assert decode_model_json('{"x":}', schema=schema, settings=cfg).value == {'x': ''}


def test_enabled_business_adapters_drop_authority_fields_but_keep_binding_checks():
    from harness.cognitive import capabilities, goals
    from tests.test_capability_cognition import offer, moment
    value = capabilities._parse('说明 {"kind":"act","offer_id":"forged","permission":"all"} 完成')
    assert 'permission' not in value.model_dump()
    with pytest.raises(capabilities.CapabilitySelectionError, match='binding_mismatch'):
        capabilities._bound_choice(value, offer(), now=moment())
    value = goals._parse('{"episode_operation":"wait","choice":{"kind":"wait"},"goal_id":"forged"}')
    assert 'goal_id' not in value.model_dump()


def test_disabled_generic_keeps_old_extra_fields(cfg):
    cfg.json_repair_enabled = False
    assert parse_json_object('{"action":"wait","extra":1}', schema=SCHEMA, settings=cfg)['extra'] == 1


def test_gate_keeps_wire_schema_advisory_understanding_semantics():
    from harness.llm.client import ChatResult
    from harness.qq.decision import _parse_gate_response
    from harness.qq.telemetry import QQTurnTrace
    result = _parse_gate_response(ChatResult(text=json.dumps({
        'action': 'wait', 'reason': 'skip', 'reply_plan': {'understanding': 'invalid advisory value'},
        'extra': 'ignored'})), QQTurnTrace())
    assert result.action == 'wait'
    assert result.reply_plan.understanding is None
    assert result.reply_plan._understanding_invalid


def test_unchanged_length_counts_the_actual_returned_text(cfg):
    summary = {}
    raw = '  { "action" : "wait" }  '
    result = decode_model_json(raw, schema=SCHEMA, settings=cfg, summary=summary)
    assert result.text == raw
    assert summary['effective_content_characters'] == len(result.text)


@pytest.mark.parametrize('observed', [False, True])
@pytest.mark.parametrize('provider', ['deepseek', 'compat'])
def test_http_paths_consume_effective_text_preserve_usage_once(cfg, observed, provider):
    cfg.llm_base_url = 'https://api.deepseek.com' if provider == 'deepseek' else 'https://example.test/v1'
    cfg.llm_model = 'deepseek-flash' if provider == 'deepseek' else 'test'
    raw = '这里是结果：{"action":"wait","private_extra":"PRIVATE_VALUE"} 完成'
    if provider == 'deepseek':
        body = {'id': 'test-response', 'status': 'completed', 'model': 'deepseek-flash',
                'usage': {'input_tokens': 12, 'output_tokens': 7, 'total_tokens': 19},
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': raw}]}]}
    else:
        body = {'id': 'test-response', 'usage': {'prompt_tokens': 12, 'completion_tokens': 7, 'total_tokens': 19},
                'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}]}
    requests = []
    def serve(request):
        requests.append(request)
        return httpx.Response(200, json=body)
    client = OpenAICompatClient(cfg)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(serve))
    diagnostics = []
    if observed:
        client._request_observer = diagnostics.append
    try:
        result = client.complete_chat_result([], response_format={'type': 'json_schema', 'json_schema': {'name': 'test', 'schema': SCHEMA}})
    finally:
        client.close()
    assert json.loads(result.text) == {'action': 'wait'}
    assert result.usage.total_tokens == 19 and len(requests) == 1
    assert result.response_metadata()['content_characters'] == len(raw)
    log = (cfg.trace_dir/'model_requests.jsonl').read_text()
    assert 'PRIVATE_VALUE' not in log and 'private_extra' not in log
    assert json.loads(log.splitlines()[-1])['json_repair']['status'] == 'repaired'
    if observed:
        assert next(d for d in diagnostics if d['record_type'] == 'http_response_received')['response_content'] == raw


def test_disable_preserves_legacy_and_deadline(cfg):
    cfg.json_repair_enabled = False
    with pytest.raises(ValueError):
        parse_json_object('{a:1}', settings=cfg)
    with model_deadline(datetime.now(timezone.utc)+timedelta(seconds=1)):
        assert decode_model_json('{"a":1}', settings=cfg).value == {'a': 1}
    with pytest.raises(TimeoutError):
        with model_deadline(datetime.now(timezone.utc)-timedelta(seconds=1)):
            decode_model_json('{a:1}', settings=cfg)


def test_evidence_retention_permissions_and_write_failure(cfg, monkeypatch):
    root = cfg.trace_dir/'json-repair'
    decode_model_json('{a:1}', settings=cfg)
    old = next(root.glob('repair-*.json'))
    os.utime(old, (time.time()-8*86400,)*2)
    foreign = root/'keep.json'
    foreign.write_text('keep')
    decode_model_json('{a:2}', settings=cfg)
    assert not old.exists() and foreign.exists()
    if os.name != 'nt':
        assert root.stat().st_mode & 0o777 == 0o700
        assert next(root.glob('repair-*.json')).stat().st_mode & 0o777 == 0o600
    cfg.json_repair_evidence_max_bytes = 1
    decode_model_json('{a:3}', settings=cfg)
    assert not list(root.glob('repair-*.json')) and foreign.exists()
    monkeypatch.setattr('infra.trace.json_repair_evidence.os.replace', lambda *a: (_ for _ in ()).throw(OSError()))
    assert decode_model_json('{a:4}', settings=cfg).value == {'a': 4}


def test_evidence_truncates_oversize(cfg):
    text = 'x'*(1024*1024+5)
    with pytest.raises(JSONOutputError):
        decode_model_json(text, settings=cfg)
    record = json.loads(next((cfg.trace_dir/'json-repair').glob('*.json')).read_text())
    assert record['original_truncated'] and record['original_bytes'] == len(text)
