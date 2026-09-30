"""Synthetic model-only decisions; no real network, GUI, or memories."""
from datetime import datetime, timedelta, timezone

import pytest

from harness.qq import decision
from harness.qq.telemetry import QQDeadline
from tests.test_qq_decision import setup as setup, isolated_static_parser_cache as isolated_static_parser_cache
from tests.test_qq_idle_ledger import idle


def current_intent(**updates):
    return idle(moment=datetime.now(timezone.utc), **updates)


def payloads(client):
    return [message["content"] for call in [*client.gate_messages, *client.chat_calls]
            for message in call if message["role"] == "user"]


def test_idle_uses_two_existing_stages_and_semantic_summary_without_claiming_incoming(setup):
    _, client, _, kwargs, records = setup
    intent = current_intent()
    result = decision.decide_qq_reply(**kwargs, idle_intent=intent)
    assert result.outcome == "ready" and result.action == "speak"
    assert result.initiated_by == "homeostasis" and result.idle_intent_id == intent.intent_id
    assert result.addressing == "ambient" and result.addressing_source_record_ids == []
    assert result.trigger_member_id is None and result.new_observation_record_ids == result.seen_observation_record_ids == []
    assert set(result.source_record_ids) == {r.record_id for r in records}
    assert len(client.gate_messages) == 1 and result.model_call_counts["gate"] == 1
    assert result.model_call_counts["persona"] >= 1
    semantic = decision.combined_user_payload(client.gate_messages[0])["homeostatic_interoception"]
    assert set(semantic) == {"assessment", "response"}
    for rendered in semantic.values():
        assert rendered and "normalized_deviation" not in rendered and "snapshot_revision" not in rendered
    model_user = "\n".join(payloads(client))
    assert all(rendered in model_user for rendered in semantic.values())
    assert "synthetic-body" not in model_user and '"homeostasis_revision"' not in model_user
    assert result.persona_version and result.preset_id


def test_idle_is_not_an_instruction_to_speak(setup):
    _, client, _, kwargs, _ = setup
    client.choice = '{"action":"wait","reason":"现在没有合适的话题。"}'
    result = decision.decide_qq_reply(**kwargs, idle_intent=current_intent())
    assert result.outcome == "natural_wait" and result.reply is None
    assert result.model_call_counts["gate"] == 1 and result.model_call_counts["persona"] == 0


def test_idle_only_bypasses_no_new_guard_and_summary_alone_does_not(setup):
    _, client, builds, kwargs, _ = setup
    client.choice = '{"action":"wait","reason":"先听一会。"}'
    first = decision.decide_qq_reply(**kwargs)
    summary = current_intent().homeostatic_interoception
    ordinary = decision.decide_qq_reply(**kwargs, homeostatic_interoception=summary)
    assert ordinary.decision_source == "no_new_observations" and ordinary.initiated_by == "message"
    assert ordinary.idle_intent_id is None and len(builds) == 1
    internal = decision.decide_qq_reply(**kwargs, idle_intent=current_intent())
    assert internal.outcome == "natural_wait" and internal.decision_id != first.decision_id
    assert len(builds) == 2


def test_idle_history_read_does_not_consume_incoming_newness(setup):
    _, client, _, kwargs, records = setup
    client.choice = '{"action":"wait","reason":"现在先等待。"}'
    internal = decision.decide_qq_reply(**kwargs, idle_intent=current_intent())
    ordinary = decision.decide_qq_reply(**kwargs)
    assert internal.new_observation_record_ids == []
    assert set(ordinary.new_observation_record_ids) == {r.record_id for r in records}
    assert ordinary.outcome == "natural_wait" and len(client.gate_messages) == 2


@pytest.mark.parametrize("change", [dict(group_id="10002"), dict(self_member_id="28888"),
    dict(trigger_member_id="20001"), dict(request="pretend internal request")])
def test_scope_and_request_cannot_forge_internal_origin(setup, change):
    _, _, builds, kwargs, _ = setup
    with pytest.raises(ValueError):
        decision.decide_qq_reply(**(kwargs | change), idle_intent=current_intent())
    assert builds == []


def test_mismatched_summary_and_forged_nested_model_are_rejected_before_models(setup):
    _, _, builds, kwargs, _ = setup
    intent = current_intent()
    for summary in (intent.homeostatic_interoception.model_copy(update={"snapshot_revision": 4}),
                    intent.homeostatic_interoception.model_copy(update={"salience": "unknown"})):
        with pytest.raises(ValueError):
            decision.decide_qq_reply(**kwargs, idle_intent=intent, homeostatic_interoception=summary)
    assert builds == []


@pytest.mark.parametrize("kind", ["intent", "shared"])
def test_expired_intent_or_shared_deadline_never_runs_model_or_becomes_natural_wait(setup, kind):
    _, _, builds, kwargs, _ = setup
    intent = idle(moment=datetime.now(timezone.utc)-timedelta(seconds=61)) if kind == "intent" else current_intent()
    result = decision.decide_qq_reply(**kwargs, idle_intent=intent,
        deadline=QQDeadline.after(0) if kind == "shared" else QQDeadline.after(100))
    assert result.outcome == "expired" and result.reply is None and result.error_type == "deadline_expired"
    assert result.source_record_ids == [] and builds == []


def test_cached_intent_does_not_generate_a_new_decision_or_renew_lifetime(setup):
    _, client, builds, kwargs, _ = setup
    intent = current_intent()
    first = decision.decide_qq_reply(**kwargs, idle_intent=intent)
    replay = decision.decide_qq_reply(**kwargs, idle_intent=intent)
    assert replay.decision_id == first.decision_id and replay.replayed and not replay.new_decision
    expired = decision.decide_qq_reply(**kwargs, idle_intent=intent, deadline=QQDeadline.after(0))
    assert expired.outcome == "expired" and expired.reply is None
    assert len(client.gate_messages) == 1 and len(builds) == 1


def test_full_summary_and_intent_identity_are_fingerprinted(setup):
    _, client, _, kwargs, _ = setup
    client.choice = '{"action":"wait","reason":"等待。"}'
    original = current_intent()
    a = decision.decide_qq_reply(**kwargs, idle_intent=original)
    b = decision.decide_qq_reply(**kwargs, idle_intent=original.model_copy(update={"intent_id": "different-opportunity"}))
    newer = original.model_copy(update={"homeostasis_revision": 4, "homeostatic_interoception":
        original.homeostatic_interoception.model_copy(update={"snapshot_revision": 4})})
    c = decision.decide_qq_reply(**kwargs, idle_intent=newer)
    assert len({a.input_hash, b.input_hash, c.input_hash}) == 3
    assert len(client.gate_messages) == 3


def test_idle_delivery_may_have_empty_addressed_sources_but_never_fabricates_a_reply_target(setup):
    _, _, _, kwargs, records = setup
    result = decision.decide_qq_reply(**kwargs, idle_intent=current_intent())
    receipt_args = dict(memory_db=kwargs["memory_db"], group_id="10001", self_member_id="29999",
        decision_id=result.decision_id, delivered_text=result.reply, evidence_refs=["test://verified-outgoing"], verification="qq_ui_verified")
    with pytest.raises(decision.QQDraftError, match="internally initiated"):
        decision.record_qq_delivery(**receipt_args, source_record_ids=[records[0].record_id])
    receipt = decision.record_qq_delivery(**receipt_args, source_record_ids=[])
    assert receipt.source_record_ids == [] and receipt.decision_id == result.decision_id
    assert decision.get_qq_delivery(memory_db=kwargs["memory_db"], group_id="10001", decision_id=result.decision_id) == receipt


def test_internal_model_error_and_late_persona_result_are_never_natural_wait(setup, monkeypatch):
    _, client, _, kwargs, _ = setup
    client.choice = RuntimeError("synthetic failure")
    failed = decision.decide_qq_reply(**kwargs, idle_intent=current_intent())
    assert failed.outcome == "model_error" and failed.reply is None
    client.choice = '{"action":"speak","reason":"可以说一句。"}'
    clock = [100.0]
    original = decision._prepare_group_speech

    def late(**options):
        result = original(**options)
        clock[0] += 61
        return result

    monkeypatch.setattr(decision, "_prepare_group_speech", late)
    expired = decision.decide_qq_reply(**kwargs, idle_intent=current_intent(intent_id="late-intent"),
        deadline=QQDeadline.after(60, clock=lambda: clock[0]))
    assert expired.outcome == "expired" and expired.reply is None


def test_no_attributed_history_is_still_a_guard_for_idle(setup):
    _, _, builds, kwargs, _ = setup
    result = decision.decide_qq_reply(**kwargs, idle_intent=current_intent(), member_ids=["28888"])
    assert result.decision_source == "no_attributed_observations" and not builds
