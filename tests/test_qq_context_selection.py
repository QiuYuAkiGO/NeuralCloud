"""Whole-record model budgeting, scoped provenance and old-failure semantics."""

from copy import deepcopy
from datetime import timedelta
import json
import sqlite3

import pytest

from harness.qq import decision
from harness.qq.context_budget import project_group_context, select_model_context
from harness.qq.memory import QQGroupContext
from harness.qq.telemetry import QQTurnTrace
from tests.test_qq_decision import NOW, add_member, isolated_static_parser_cache as isolated_static_parser_cache, setup as setup
from tests.test_qq_decision_context_budget import _gate_payload, _persona_payload


def _payload(store):
    return {"group_context": store.group_context(group_id="10001").model_dump(mode="json"),
            "request": "保留完整原文", "effective_query": "当前主题", "new_observation_record_ids": [],
            "verified_self_aliases": [], "delivery_receipts": [], "segment_delivery_receipts": [], "prior_decisions": []}


def _select(payload, *, required=(), handled=(), maximum=48_000, reserve=0):
    return select_model_context(payload, required_observation_ids=set(required), handled_observation_ids=set(handled),
                                max_characters=maximum, assessment_reserve=reserve)


def _observations(payload):
    return [item for member in payload["group_context"]["members"] for item in member["observations"]]


def test_projection_preserves_every_semantic_field_and_does_not_mutate_original(setup):
    store, _, _, _, _ = setup
    payload = _payload(store)
    payload["verified_self_aliases"] = [{"alias": "Misty", "group_id": "10001", "self_member_id": "29999",
                                          "verification": "qq_ui_verified", "evidence_refs": ["test-evidence://alias"]}]
    before = deepcopy(payload)
    actual = _select(payload)
    assert actual.fits and payload == before
    for original, projected in zip(before["group_context"]["members"], actual.payload["group_context"]["members"], strict=True):
        for old, new in zip(original["observations"], projected["observations"], strict=True):
            assert new == {key: value for key, value in old.items() if key not in {"evidence", "attribution_evidence"}}
            assert new["message_id"] is None and new["claims"] == old["claims"]
        for old, new in zip(original["profiles"], projected["profiles"], strict=True):
            assert new == {key: value for key, value in old.items() if key not in {"group_evidence", "member_evidence"}}
    assert actual.payload["verified_self_aliases"] == [{key: value for key, value in before["verified_self_aliases"][0].items() if key != "evidence_refs"}]
    assert actual.payload["model_context_selection"]["proof_refs_stored_outside_model"] is True
    assert actual.characters == len(decision._canonical(actual.payload))


@pytest.mark.parametrize("corruption", ["missing", "foreign_member", "foreign_group"])
def test_observation_cannot_enter_model_without_exact_original_profile(setup, corruption):
    payload = _payload(setup[0])
    member = payload["group_context"]["members"][0]
    if corruption == "missing":
        member["profiles"] = []
    else:
        member["profiles"][0]["member_id" if corruption == "foreign_member" else "group_id"] = "99999"
    with pytest.raises(ValueError, match="scope|profile"):
        _select(payload)


def test_budget_counts_omission_metadata_and_reserves_worst_escaped_assessment(setup):
    payload = _payload(setup[0])
    required = {item["record_id"] for item in _observations(payload)}
    full = _select(payload, required=required)
    reserved = decision._assessment_reserve()
    gate = decision._GateDecision(
        action="speak", reason="\x00" * 512, addressing="ambient",
        addressing_source_record_id="\x00" * 160, addressing_supporting_quote="\x00" * 1000,
        reply_style="conversational", reply_style_reason="\x00" * 256,
    )
    assert len(decision._canonical({**full.payload, "assessment": gate.model_dump()})) <= full.characters + reserved
    assert full.characters == len(decision._canonical(full.payload))
    boundary = _select(payload, required=required, maximum=full.characters + reserved, reserve=reserved)
    assert boundary.fits
    too_small = _select(payload, required=required, maximum=full.characters + reserved - 1, reserve=reserved)
    assert not too_small.fits and too_small.observation_ids == boundary.observation_ids
    assert too_small.payload["request"] == payload["request"]


def test_matched_old_record_survives_before_unrelated_newer_handled_history(setup):
    store, _, _, _, originals = setup
    old_profile, related = add_member(store, "20001", text="长期主题" + "甲" * 1000, capture="relevant", moment=NOW + timedelta(seconds=1))
    _, unrelated = add_member(store, "20001", text="无关闲谈" + "乙" * 1000, capture="irrelevant", moment=NOW + timedelta(seconds=2))
    _, latest = add_member(store, "20001", text="这个主题还记得吗", capture="latest", moment=NOW + timedelta(seconds=3))
    payload = _payload(store)
    retrieval = next(item for item in payload["group_context"]["member_retrieval"] if item["member_id"] == "20001")
    retrieval["history_matches"] = [{"record_id": related.record_id, "reason": "query_substring", "score": 5,
                                    "matched_terms": ["长期主题"], "matched_fields": ["text"]}]
    retrieval["history_record_ids"] = [related.record_id]
    required = {item.record_id for item in [*originals, latest]}
    full = _select(payload, required=required)
    actual = _select(payload, required=required, handled={related.record_id, unrelated.record_id}, maximum=full.characters - 100)
    assert actual.fits and related.record_id in actual.observation_ids and unrelated.record_id not in actual.observation_ids
    assert old_profile.record_id in actual.profile_ids
    by_id = {item["record_id"]: item for item in _observations(actual.payload)}
    assert by_id[related.record_id]["profile_record_id"] == old_profile.record_id
    assert by_id[related.record_id]["text"] == related.text
    assert actual.payload["model_context_selection"]["omitted_observation_record_ids"] == [unrelated.record_id]


def test_latest_actual_partial_segment_is_mandatory_without_unsent_suffix(setup):
    payload = _payload(setup[0])
    payload["delivery_receipts"] = [{"receipt_id": "old", "delivered_at": NOW.isoformat(), "delivered_text": "旧" * 2000}]
    payload["segment_delivery_receipts"] = [{"receipt_id": "latest-partial", "decision_id": "partial-plan",
        "delivered_at": (NOW + timedelta(seconds=1)).isoformat(), "delivered_text": "实际前缀", "segment_index": 0, "segment_count": 3}]
    required = {item["record_id"] for item in _observations(payload)}
    full = _select(payload, required=required)
    actual = _select(payload, required=required, maximum=full.characters - 1000)
    assert actual.fits and actual.delivery_ids == ("latest-partial",)
    assert actual.payload["segment_delivery_receipts"] == payload["segment_delivery_receipts"]
    assert actual.payload["delivery_receipts"] == []


def test_all_new_records_are_mandatory_even_if_caller_also_marks_them_handled(setup):
    payload = _payload(setup[0])
    ids = {item["record_id"] for item in _observations(payload)}
    full = _select(payload, required=ids)
    limited = _select(payload, required=ids, handled=ids, maximum=full.characters - 1)
    assert not limited.fits and set(limited.observation_ids) == ids
    assert limited.payload["model_context_selection"]["omitted_observation_record_ids"] == []
    with pytest.raises(ValueError, match="outside"):
        _select(payload, required={"qq-visual:not-in-this-group"})


def test_final_assessment_reserve_rejects_mandatory_content_before_spending_model_call(setup, monkeypatch):
    monkeypatch.setenv("NC_QQ_INPUT_TOKEN_BUDGET", "24000")
    store, client, builds, kwargs, _ = setup
    add_member(store, "20003", text="必须保留的完整新原文" + "长" * 17_000)
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, trace=trace)
    counters = trace.snapshot()["counters"]
    assert result.outcome == "error_fallback" and result.reason == decision._CONTEXT_BUDGET_REASON
    assert not builds and not client.gate_messages and result.source_record_ids
    assert result.reply == decision.ERROR_FALLBACK_REPLY
    assert len(result.seen_observation_record_ids) == len(result.new_observation_record_ids) == 3
    assert counters["context_final_characters"] < 48_000
    assert counters["context_final_characters"] + counters["context_assessment_reserved_characters"] > 48_000
    assert counters["context_budget_exceeded_final"] == 1


def test_long_proof_only_context_fits_both_models_and_retains_full_private_refs(setup, monkeypatch):
    store, client, _, kwargs, records = setup
    context = store.group_context(group_id="10001")
    raw = context.model_dump(mode="json")
    proofs = [f"test-evidence://PRIVATE_PROOF_{index}/" + "x" * 1900 for index in range(16)]
    for member in raw["members"]:
        for profile in member["profiles"]:
            profile.update(group_evidence=proofs, member_evidence=proofs)
        for observation in member["observations"]:
            observation.update(evidence=proofs, attribution_evidence=proofs)
    expanded = QQGroupContext.model_validate(raw)
    monkeypatch.setattr(decision.QQVisualMemoryStore, "group_context", lambda *args, **kw: expanded)
    aliases = [decision.QQVerifiedSelfAlias(group_id="10001", self_member_id="29999", alias="Misty",
                                           evidence_refs=["test-evidence://PRIVATE_ALIAS"])]
    trace = QQTurnTrace()
    actual = decision.decide_qq_reply(**kwargs, verified_self_aliases=aliases, trace=trace)
    gate, persona = _gate_payload(client), _persona_payload(client)
    assert actual.outcome == "ready" and set(actual.source_record_ids) == {item.record_id for item in records}
    assert set(proofs).issubset(actual.evidence_refs)
    assert trace.snapshot()["counters"]["context_source_characters"] > 48_000
    assert gate["group_context"] == persona["group_context"] == project_group_context(raw)
    assert "PRIVATE_PROOF" not in decision._canonical(gate) + decision._canonical(persona)
    assert "PRIVATE_ALIAS" not in decision._canonical(gate) + decision._canonical(persona)
    assert gate["verified_self_aliases"][0]["alias"] == "Misty"


@pytest.mark.parametrize("proof_field", ["group_evidence", "member_evidence", "evidence", "attribution_evidence", "alias"])
def test_original_proof_change_invalidates_full_fingerprint_without_model_uri(setup, monkeypatch, proof_field):
    store, client, _, kwargs, _ = setup
    context = store.group_context(group_id="10001", window_mode="chronological_append")
    aliases = [decision.QQVerifiedSelfAlias(group_id="10001", self_member_id="29999", alias="Misty", evidence_refs=["test-evidence://alias-before"])]
    before = decision.decide_qq_reply(**kwargs, request="明确的当前请求", verified_self_aliases=aliases)
    first = _gate_payload(client)
    raw = context.model_dump(mode="json")
    if proof_field == "alias":
        aliases = [aliases[0].model_copy(update={"evidence_refs": ["test-evidence://alias-after"]})]
    else:
        target = raw["members"][0]["profiles" if proof_field in {"group_evidence", "member_evidence"} else "observations"][0]
        target[proof_field].append("test-evidence://full-proof-change")
    monkeypatch.setattr(decision.QQVisualMemoryStore, "group_context", lambda *args, **kw: QQGroupContext.model_validate(raw))
    after = decision.decide_qq_reply(**kwargs, request="明确的当前请求", verified_self_aliases=aliases)
    assert after.input_hash != before.input_hash and after.decision_id != before.decision_id and not after.replayed
    assert _gate_payload(client)["group_context"] == first["group_context"]
    assert _gate_payload(client)["verified_self_aliases"] == first["verified_self_aliases"]


def test_failed_old_observations_stay_seen_but_new_input_is_the_only_new_candidate(setup, monkeypatch):
    monkeypatch.setenv("NC_QQ_INPUT_TOKEN_BUDGET", "24000")
    store, client, builds, kwargs, _ = setup
    for index in range(8):
        add_member(store, "20001", text=f"旧资料{index}" + "原" * 3000, capture=f"old-{index}", moment=NOW + timedelta(seconds=index + 1))
    first = decision.decide_qq_reply(**kwargs)
    assert first.outcome == "error_fallback" and first.source_record_ids and not builds
    assert decision.decide_qq_reply(**kwargs).replayed
    _, latest = add_member(store, "20001", text="Misty，当前这个新问题呢？", capture="new-now", moment=NOW + timedelta(seconds=20))
    second = decision.decide_qq_reply(**kwargs, trigger_member_id="20001", query="原")
    gate = _gate_payload(client)
    included = {item["record_id"] for item in _observations(gate)}
    omitted = set(gate["model_context_selection"]["omitted_observation_record_ids"])
    assert second.outcome == "ready" and second.new_observation_record_ids == [latest.record_id]
    assert set(second.source_record_ids) == included and omitted and not (omitted & included)
    assert latest.record_id in included
    for member in store.group_context(group_id="10001").members:
        for item in member.observations:
            if item.record_id in included:
                assert set(item.evidence + item.attribution_evidence).issubset(second.evidence_refs)
            elif item.record_id in omitted:
                assert not set(item.evidence + item.attribution_evidence) & set(second.evidence_refs)
    with pytest.raises(decision.QQDraftError, match="considered"):
        decision.record_qq_delivery(memory_db=store.path, group_id="10001", self_member_id="29999", decision_id=second.decision_id,
            delivered_text=second.reply, source_record_ids=[next(iter(omitted))], evidence_refs=["test-evidence://not-actually-sent"], verification="qq_ui_verified")
    with sqlite3.connect(store.path) as connection:
        original = decision.QQReplyDecision.model_validate_json(connection.execute(
            "SELECT record_json FROM qq_reply_decisions WHERE decision_id=?", (first.decision_id,)).fetchone()[0])
    assert original == first


def test_trigger_latest_is_pinned_in_history_only_retrieval_without_scope_expansion(setup):
    store, client, _, kwargs, _ = setup
    _, latest = add_member(store, "20001", text="最新完整问题", capture="newest", moment=NOW + timedelta(seconds=10))
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20001", query="UNTRUSTED_A",
                                      recent_limit_per_member=0, history_limit_per_member=1)
    assert result.outcome == "ready" and latest.record_id in result.source_record_ids
    assert any(item["text"] == latest.text for item in _observations(_gate_payload(client)))
    scoped = decision.decide_qq_reply(**kwargs, member_ids=["20002"], trigger_member_id="20001", request="仍不能越过范围")
    assert scoped.decision_source == "unknown_trigger" and scoped.participants == ["20002"]
    assert latest.record_id not in scoped.seen_observation_record_ids


def test_old_version_failed_cache_stays_immutable_and_does_not_become_new_send(setup, monkeypatch):
    store, _, builds, kwargs, records = setup
    monkeypatch.setattr(decision, "PROMPT_VERSION", "qq-group-turn-taking-v9")
    builds_before = len(builds)
    old = decision.decide_qq_reply(**kwargs)
    raw = old.model_dump(mode="json")
    raw.update(action="wait", reply=None, reply_segments=[], outcome="model_invalid", decision_source="model_invalid",
               error_type="model_invalid", prompt_version="qq-group-turn-taking-v9", reason=decision._CONTEXT_BUDGET_REASON)
    raw.pop("seen_observation_record_ids")
    raw["source_record_ids"] = [item.record_id for item in records]  # Legacy pre-model provenance.
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE qq_reply_decisions SET record_json=? WHERE decision_id=?", (json.dumps(raw), old.decision_id))
    calls_before = len(builds)
    monkeypatch.setattr(decision, "PROMPT_VERSION", "qq-group-turn-taking-v10")
    result = decision.decide_qq_reply(**kwargs)
    assert result.decision_source == "no_new_observations" and result.source_record_ids == []
    assert result.new_observation_record_ids == [] and len(builds) == calls_before == builds_before + 1
    with sqlite3.connect(store.path) as connection:
        assert json.loads(connection.execute("SELECT record_json FROM qq_reply_decisions WHERE decision_id=?", (old.decision_id,)).fetchone()[0]) == raw


def test_closed_blocks_are_dropped_oldest_first_without_reordering(setup):
    store, _, _, _, _ = setup
    for index in range(12):
        add_member(store, "20001", text=f"封闭块资料{index}" + "原" * 40, capture=f"block-{index}",
                   moment=NOW + timedelta(seconds=index + 1))
    payload = _payload(store)
    ordered = sorted(_observations(payload), key=lambda item: (item["observed_at"], item["stored_at"], item["record_id"]))
    payload["conversation_window"] = {
        "mode": "chronological_append", "block_size": 4,
        "closed_observation_ids": [item["record_id"] for item in ordered[:8]],
        "open_observation_ids": [item["record_id"] for item in ordered[8:]],
    }
    required = {ordered[-1]["record_id"]}
    handled = {item["record_id"] for item in ordered[:-1]}
    full = _select(payload, required=required, handled=handled)
    actual = _select(payload, required=required, handled=handled, maximum=full.characters - 80)
    omitted = actual.payload["model_context_selection"]["omitted_observation_record_ids"]
    assert omitted and len(omitted) % 4 == 0
    assert omitted == [item["record_id"] for item in ordered[:len(omitted)]]
    assert ordered[-1]["record_id"] in actual.observation_ids
    retained = {item["record_id"] for member in actual.payload["group_context"]["members"]
                for item in member["observations"]}
    assert retained == {item["record_id"] for item in ordered if item["record_id"] not in set(omitted)}


def test_required_open_block_is_not_deleted_with_oldest_closed_blocks(setup):
    store, _, _, _, _ = setup
    for index in range(8):
        add_member(store, "20001", text=f"窗口资料{index}", capture=f"keep-{index}",
                   moment=NOW + timedelta(seconds=index + 1))
    payload = _payload(store)
    ordered = sorted(_observations(payload), key=lambda item: (item["observed_at"], item["stored_at"], item["record_id"]))
    payload["conversation_window"] = {
        "mode": "chronological_append", "block_size": 4,
        "closed_observation_ids": [item["record_id"] for item in ordered[:4]],
        "open_observation_ids": [item["record_id"] for item in ordered[4:]],
    }
    required = {ordered[-1]["record_id"]}
    handled = {item["record_id"] for item in ordered[:4]}
    full = _select(payload, required=required, handled=handled)
    actual = _select(payload, required=required, handled=handled, maximum=max(200, full.characters - 400))
    assert ordered[-1]["record_id"] in actual.observation_ids
    assert set(payload["conversation_window"]["open_observation_ids"]).issubset(actual.observation_ids)
