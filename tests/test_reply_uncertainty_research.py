import json
from copy import deepcopy

from eval.reply_uncertainty_research.adapters import expand_projection, handoff
from eval.reply_uncertainty_research.critic_cases import cases
from eval.reply_uncertainty_research.scenarios import scenarios
from harness.core.inference import MemoryEvidenceContext, MemoryEvidenceEntry
from harness.validator.evidence_projection import project_evidence


def test_cards_preserve_complete_evidence_and_attribution():
    evidence = MemoryEvidenceContext(
        audience="here",
        current_speaker_id="alice",
        entries=[
            MemoryEvidenceEntry(
                source_id="shared",
                text="第一条\n完整文本",
                audience="here",
                source_channel="web_search_snippet",
                source_conversation_id="there",
                status="provisional",
                observed_at="2026-10-09T12:00:00+08:00",
            ),
            MemoryEvidenceEntry(
                source_id="shared",
                text="另一位群友说的话",
                audience="here",
                source_channel="qq_group",
                source_conversation_id="here",
                speaker_id="bob",
                status="user_claim",
            ),
        ],
    )
    projected, mapping = project_evidence(evidence)
    before = deepcopy(projected)
    cards = expand_projection(projected)
    assert projected == before
    assert cards["records"][0]["text"] == evidence.entries[0].text
    assert cards["records"][0]["observed_at"] == "2026-10-09T12:00:00+08:00"
    assert cards["records"][0]["status"] == "provisional"
    assert cards["records"][1]["speaker_id"] != cards["current_speaker_id"]
    assert cards["same_source_rows"] == [[0, 1]]
    assert "alice" not in json.dumps(cards) and "bob" not in json.dumps(cards)
    assert mapping["original_evidence"]["entries"][0]["source_id"] == "shared"


def test_handoff_preserves_raw_sources_and_does_not_touch_candidate_sentences():
    source = {"role": "user", "content": '{"observation":{"uncertainties":["身份未知"],"ocr":"未识别"}}'}
    kwargs = {
        "prefix_messages": [
            {"role": "system", "content": "PERSONA+controller"},
            source,
            {"role": "assistant", "content": '{"action":"speak","reason":"保留身份限制"}'},
        ],
        "payload": json.dumps(
            {
                "assessment": {
                    "reply_plan": {
                        "must_address": "保留身份限制",
                        "intent": "answer",
                        "understanding": {"summary": "问配色"},
                    }
                },
                "web_context": {"failures": [{"code": "timeout"}]},
            }
        ),
        "response_expression": object(),
        "memory_evidence_entries": ["original"],
    }
    before = deepcopy(kwargs["prefix_messages"])
    result = handoff(kwargs, "PERSONA")
    assert kwargs["prefix_messages"] == before
    assert result["prefix_messages"][1] == source
    assert len(result["prefix_messages"]) == 2
    assert "controller" not in result["prefix_messages"][0]["content"]
    payload = json.loads(result["payload"])
    assert payload["web_context"] == {"failures": [{"code": "timeout"}]}
    assert "assessment" not in payload and "must_address" not in payload["response_target"]
    assert result["response_expression"] is kwargs["response_expression"]
    assert result["memory_evidence_entries"] is kwargs["memory_evidence_entries"]


def test_question_pairs_hold_evidence_constant_and_keep_both_sides_of_risk():
    dataset = scenarios()
    assert len(dataset) == 16
    for i in range(0, len(dataset), 2):
        a, b = dataset[i : i + 2]
        assert a["pair"] == b["pair"]
        for key in ("image", "web"):
            assert a["turns"][0].get(key) == b["turns"][0].get(key)
        assert a.get("history") == b.get("history") and a.get("guidance") == b.get("guidance")
        assert a["turns"][0]["messages"] != b["turns"][0]["messages"]
    gold = cases()
    assert len(gold) == 24 and sum(c["expected_pass"] for c in gold) == 12


def test_failure_receipt_cannot_cross_scope_or_fabricate_a_failure():
    from eval.reply_uncertainty_research.receipts import failure_entries

    receipt = {
        "group_id": "10001",
        "sources": [{"observation_id": "obs1", "evidence_ref": "image1"}],
        "result": {"status": "unavailable", "error": "vision_timeout"},
    }
    entries = failure_entries([receipt], group_id="10001", presented_ids={"obs1"})
    assert len(entries) == 1 and entries[0].text == "vision_timeout"
    assert entries[0].status == "retained_fact" and entries[0].source_refs == ["obs1", "image1"]
    assert not failure_entries([receipt], group_id="20002", presented_ids={"obs1"})
    assert not failure_entries([receipt], group_id="10001", presented_ids=set())
    assert not failure_entries([receipt | {"result": {"status": "ok"}}], group_id="10001", presented_ids={"obs1"})
    assert not failure_entries([], group_id="10001", presented_ids={"obs1"})
