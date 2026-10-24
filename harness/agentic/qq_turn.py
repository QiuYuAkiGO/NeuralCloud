"""Native QQ cognition reuses verified sources and the existing delivery ledger."""
from harness.qq.conversation import conversation_audience, official_scope_app


def decide(*, agent, result, settings, observations, context, payload, selection, receipts,
           partial_receipts, owner, new_ids, trigger_id, evidence_refs, preparation, deadline, trace):
    from harness.qq.memory_evidence import build_qq_memory_evidence, current_qq_burst_ids
    from harness.agentic.vision import for_qq_body
    from harness.qq.decision import _prepared_group_segments
    preparation = preparation or {}
    validate = preparation.get('request_validator')
    if validate is not None:
        validate()
    runtime = agent.runtime_state_provider()
    if runtime.get('mode') != 'on' or not runtime.get('running'):
        raise ValueError('native_runtime_state_unavailable')
    resting = runtime.get('rest', {}).get('resting', False)
    wake = preparation.get('native_wake') is True and result.initiated_by == 'message'
    if resting and not wake:
        return result.model_copy(update={'action':'wait', 'reply':None, 'reply_segments':[],
            'decision_source':'no_new_observations', 'outcome':'guard_wait',
            'reason':'休息期间保留消息，等待直接呼唤或自主复查。', 'seen_observation_record_ids':[]})
    entries = build_qq_memory_evidence(payload=selection.payload, trusted_context=context,
        current_observation_ids=current_qq_burst_ids(trusted_context=context,
            new_observation_ids=set(new_ids), trigger_record_id=trigger_id),
        receipts=receipts, partial_receipts=partial_receipts, shared_owner=owner)
    provider = preparation.get('used_sources_provider')
    if provider is not None:
        preparation['used_sources'] = provider(selection.observation_ids)
    instruction = ('当前是 QQ 的一轮交流。你可调用记忆和状态工具，最后决定自然回复或等待。'
        '回应当前实际发言，已经回复的同题没有新增内容时可等待；不要输出内部工具协议或状态数值。')
    if result.initiated_by != 'message':
        instruction = ('这是 Runtime 已授权的自主交流机会，不是新收到的用户发言。'
            '结合已核验的当前会话自行决定是否联系；可以等待，不得捏造有人刚对你说话。')
    if resting:
        instruction += ('你正在休息，本轮是对方直接呼唤后的简短判断，只能使用一次模型调用。'
            '可简短回应、延后或保持休息；若选择结束休息用 submit_action，执行仍以 Runtime 回执为准。')
    vision = None if resting else for_qq_body(settings, observations, group_id=result.group_id,
        self_member_id=result.self_member_id, payload=payload, presented_ids=set(selection.observation_ids),
        current_ids=set(new_ids) | ({trigger_id} if trigger_id else set()))
    final = agent.prepare_qq(audience=conversation_audience(result.group_id), entries=entries,
        instruction=instruction, timeout=min(12, deadline.check()) if resting else deadline.check(),
        vision_tool=vision, request_validator=validate, vision_trace=trace, wake_only=resting, runtime_state=runtime)
    if validate is not None:
        validate()
    trace.add_counter('native_model_calls', final.usage.calls)
    if final.text.strip():
        segments, reply = _prepared_group_segments(final, trace, reply_style='conversational', official=bool(official_scope_app(result.group_id)))
        update = {'action':'speak', 'reply':reply, 'reply_segments':segments, 'outcome':'ready'}
    else:
        update = {'action':'wait', 'reply':None, 'reply_segments':[], 'outcome':'natural_wait'}
    return result.model_copy(update={**update, 'decision_source':'model',
        'reason':'主模型基于当前会话、记忆和自身状态决定。', 'usage':final.usage,
        'agentic_evidence':final.agentic, 'source_record_ids':list(selection.observation_ids),
        'evidence_refs':evidence_refs})
