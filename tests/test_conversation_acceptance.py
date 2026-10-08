"""Interpretation maturity and source admission are independent of social stance."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from harness.core.conversation_policy import CONVERSATION_ACCEPTANCE_RULES
from harness.core.inference import MemoryEvidenceContext
from harness.memory.evidence_status import derived_evidence_status, interpretation_status
from harness.qq import decision
from harness.qq.draft import _GROUP_DRAFT_RULES
from harness.qq.memory_evidence import build_qq_memory_evidence
from harness.validator.semantic import SEMANTIC_RULES
from tests.test_qq_brief_reply import _payload
from tests.test_qq_decision import setup as setup


@pytest.mark.parametrize("states,counters,expected", [
    ((None,), False, "provisional"), (("support_only",), False, "provisional"),
    (("stable",), False, "stable_interpretation"), (("stable_interpretation",), False, "stable_interpretation"),
    (("stable", "disputed"), False, "disputed"), (("stable", "contested"), False, "disputed"),
    (("provisional",), True, "disputed"), (("stable",), True, "stable_interpretation"),
])
def test_explicit_maturity_and_active_dispute_take_precedence(states, counters, expected):
    assert interpretation_status(*states, has_counterevidence=counters) == expected


@pytest.mark.parametrize("authority", ["misty_tentative_interpretation", "daily_evidence_bound_interpretation",
                                      "source_anchored_interpretation"])
def test_numeric_confidence_does_not_promote_interpretations(authority):
    assert derived_evidence_status({"authority": authority, "confidence": 1.0}) == "provisional"
    assert derived_evidence_status({"authority": authority, "confidence": 1.0, "status": "support_only"}) == "provisional"
    assert derived_evidence_status({"authority": authority, "confidence": 0.15, "status": "stable"}) == "stable_interpretation"


@pytest.mark.parametrize("status,expected", [("provisional", "provisional"), ("stable", "stable_interpretation"),
                                           ("disputed", "disputed")])
def test_qq_projection_preserves_authority_scope_counter_and_checkpoint_roundtrip(setup, status, expected):
    context, payload = _payload(setup)
    original = {"record_id": "impression-test", "current_impression": "通常愿意解释，也有一次拒绝。",
        "kind": "person", "status": status, "confidence": 0.99, "authority": "misty_tentative_interpretation",
        "source_channel": "qq_group", "source_conversation_id": "qq:group:10001",
        "supporting_episode_ids": ["support-test"],
        "counterevidence": [] if status == "provisional" else [{"episode_id": "counter-test", "statement": "那次拒绝了解释。"}]}
    trusted = SimpleNamespace(context={}, derived_memory={"entries": [deepcopy(original)]})
    payload["derived_memory"] = deepcopy(trusted.derived_memory)
    entries = build_qq_memory_evidence(payload=payload, trusted_context=context,
        current_observation_ids=set(), receipts=[], partial_receipts=[], shared_owner=trusted)
    derived = [entry for entry in entries if entry.kind == "derived_memory"]
    assert derived[0].status == expected and derived[0].source_refs == ["support-test"]
    assert derived[0].source_conversation_id == original["source_conversation_id"]
    if status != "provisional":
        assert derived[1].status == "disputed" and derived[1].text == "那次拒绝了解释。"
    assert trusted.derived_memory["entries"][0] == original
    evidence = MemoryEvidenceContext(audience="qq:group:10001", entries=entries)
    assert MemoryEvidenceContext.model_validate_json(evidence.model_dump_json()) == evidence
    payload["derived_memory"]["entries"][0]["status"] = "forged"
    with pytest.raises(ValueError, match="projection mismatch"):
        build_qq_memory_evidence(payload=payload, trusted_context=context,
            current_observation_ids=set(), receipts=[], partial_receipts=[], shared_owner=trusted)


def test_all_planning_generation_and_review_stages_share_one_static_policy():
    for rules in [decision._RULES, decision._PRIVATE_RULES, _GROUP_DRAFT_RULES,
                  decision._PRIVATE_DRAFT_RULES, SEMANTIC_RULES]:
        assert rules.count(CONVERSATION_ACCEPTANCE_RULES) == 1
    assert decision.QQReplyPlan().necessary_boundary is None


def test_stable_interpretation_does_not_change_ordinary_episode_authority():
    assert derived_evidence_status({"kind": "episode", "authority": "source_attributed_experience"}) == "retained_fact"
