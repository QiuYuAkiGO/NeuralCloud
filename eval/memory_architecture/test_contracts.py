import json

from .adapters import bounded_hy_context
from .fixtures import build_fixture, fixture_digest
from .gateway import stored_response
from .runner import ARMS, authorized_history, blind_input


def test_fixture_is_deterministic_and_query_blind():
    fixture = build_fixture()
    assert fixture_digest(fixture) == fixture_digest(build_fixture())
    assert len(fixture["cases"]) == 12
    assert sum(len(c["probes"]) for c in fixture["cases"]) == 36
    assert sum(len(b["messages"]) for c in fixture["cases"] for b in c["batches"]) == 432
    assert all("expected" not in json.dumps(b) and "probes" not in b for c in fixture["cases"] for b in c["batches"])


def test_scope_controls_have_no_private_history():
    case = next(c for c in build_fixture()["cases"] if c["id"] == "audience_scope")
    public = authorized_history(case, "group")
    assert public and "PRIVATE_MOSS_7319" not in json.dumps(public, ensure_ascii=False)
    assert "PRIVATE_MOSS_7319" in json.dumps(authorized_history(case, "private"), ensure_ascii=False)


def test_judge_order_reversal_and_no_arm_labels():
    case = build_fixture()["cases"][0]
    q = case["probes"][0]
    answers = [{"probe": q["id"], "arm": a, "text": f"匿名正文{i}"} for i, a in enumerate(ARMS)]
    forward, body = blind_input(case, q, answers)
    reverse, reverse_body = blind_input(case, q, answers, reverse=True)
    assert list(forward.values()) == list(reversed(list(reverse.values())))
    assert set(body["candidate_answers"].values()) == set(reverse_body["candidate_answers"].values())
    assert not any(arm in json.dumps(body) for arm in ARMS)


def test_memory_budget_preserves_whole_records():
    raw = {"memories": {"normal": [{"content": "甲" * 300}, {"content": "乙" * 300}]}}
    context, dropped = bounded_hy_context(raw, 450)
    assert len(context) <= 450 and dropped == 1
    assert json.loads(context)["memories"]["normal"] == [raw["memories"]["normal"][0]]


def test_logs_omit_reasoning_and_embedding_arrays_without_mutation():
    raw = {"choices": [{"message": {"content": "answer", "reasoning_content": "hidden"}}],
           "data": [{"embedding": [0.1, 0.2]}]}
    saved = stored_response(raw)
    assert "reasoning_content" not in saved["choices"][0]["message"]
    assert "embedding" not in saved["data"][0]
    assert saved["data"][0]["embedding_dimensions"] == 2
    assert raw["data"][0]["embedding"] == [0.1, 0.2]
