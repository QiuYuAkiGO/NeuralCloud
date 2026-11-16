import json
import pytest
from fastapi.testclient import TestClient
from neuralcloud.api import create_app
from neuralcloud.contracts import (
    SourceEvent,
    SourceEventV2,
    ChannelContext,
    RecallSubject,
    RecallConstraints,
    Correction,
    canonical,
    digest,
)
from neuralcloud.evidence_index import EvidenceIndex
from neuralcloud.evidence_index import account, author_of, is_primary
from neuralcloud.retrieval import Retriever, IdentityIndex
from neuralcloud.workflow import MemoryWorkflow


def add(store, key, content, sender="2512627824", **kw):
    return store.ingest(
        SourceEvent(
            character_id="misty-test",
            source_instance="onebot:765681548",
            event_id=key,
            session_id="s",
            conversation_id="group:g",
            occurred_at=10,
            content=content,
            participants=["qq:member:" + sender],
            **kw,
        )
    )


@pytest.fixture
def setup(store, tmp_path):
    idx = EvidenceIndex(store.path, tmp_path / "evidence.db", writable=True, self_accounts=["qq:765681548"])
    identity = IdentityIndex(tmp_path / "identity.db")
    identity.seed([], store)
    workflow = MemoryWorkflow(Retriever(store, identity), idx)
    yield workflow, idx
    idx.close()
    identity.close()


def test_exact_account_name_ignores_current_speaker_and_topic_threshold(store, setup):
    flow, idx = setup
    fact = add(
        store,
        "profile",
        json.dumps({"display_name_source": "qq_profile", "observed_display_name": "送风格"}, ensure_ascii=False),
    )
    me = add(store, "me", "我叫秋语", "2285027817")
    for i in range(60):
        add(store, str(i), "@2512627824")
    while idx.sync_batch():
        pass
    result = flow.recall("2512627824 叫什么名字", subject=RecallSubject(platform="qq", account_id="2285027817"))
    assert result["assessment"]["state"] == "DIRECT"
    assert result["items"][0]["record_id"] == fact["record_id"]
    assert me["record_id"] not in [r["record_id"] for r in result["items"]]


def test_nonempty_irrelevant_results_are_not_an_answer(store, setup):
    flow, idx = setup
    add(store, "mention", "@2512627824")
    idx.sync_batch()
    r = flow.recall("2512627824叫什么名字")
    assert r["items"]
    assert r["assessment"]["state"] == "RELATED"
    assert not r["assessment"]["sufficient"]
    assert r["diagnostics"]["rounds"] <= 3


def test_revision_invalidates_index_before_worker_catches_up(store, setup):
    flow, idx = setup
    saved = add(store, "name", "我叫送风格")
    idx.sync_batch()
    assert flow.recall("2512627824")["assessment"]["state"] == "DIRECT"
    store.correct(
        Correction(
            character_id="misty-test",
            source_instance="fix",
            event_id="1",
            record_id=saved["record_id"],
            expected_revision=1,
            reason="wrong",
            retract=True,
        )
    )
    assert not flow.recall("2512627824")["assessment"]["sufficient"]
    assert not idx.lookup(["qq:2512627824"])


def test_quoted_and_conversation_constraints_survive_fallback(store, setup):
    flow, idx = setup
    add(store, "name", "我叫送风格")
    idx.sync_batch()
    for constraints, query in [
        (RecallConstraints(conversation_id="group:other"), "2512627824叫啥"),
        (None, '2512627824 "完全不相关" 叫什么'),
    ]:
        r = flow.recall(query, constraints=constraints)
        assert not r["items"]
        assert not r["assessment"]["sufficient"]


def test_qqbot_account_does_not_merge_numeric_qq(store, setup):
    flow, idx = setup
    add(store, "name", "我叫送风格")
    idx.sync_batch()
    r = flow.recall("我叫什么", subject=RecallSubject(platform="qqbot", account_id="2512627824"))
    assert not r["assessment"]["sufficient"]


def test_nested_legacy_qqbot_identifiers_keep_platform_and_role():
    assert account("qq:member:qqbot:1905863573:user:ABC123") == "qqbot:1905863573:user:ABC123"
    assert account("qq:member:qqbot:1905863573:bot:1905863573") == "qqbot:1905863573:bot:1905863573"
    assert account("qq:member:12345") == "qq:12345"
    p = {"participants": ["qq:member:qqbot:1905863573:bot:1905863573"], "kind": "observation"}
    assert author_of(p).startswith("qqbot:")
    assert not is_primary(p)


def test_self_expression_and_quoted_negative_names_not_promoted(store, setup):
    flow, idx = setup
    add(store, "self", "我叫送风格", kind="self_expression")
    add(store, "quote", "他说“我叫送风格”")
    add(store, "negative", "我不是送风格")
    idx.sync_batch()
    assert not idx.lookup(["qq:2512627824"])


def test_v1_digest_stable_and_v2_transport_roundtrips(store, setup):
    v1 = SourceEvent(
        character_id="misty-test", source_instance="qq", event_id="v1", session_id="s", occurred_at=1, content="hello"
    )
    before = digest(v1)
    saved = store.ingest(v1)
    assert "channel" not in json.loads(canonical(v1))
    assert store.ingest(SourceEvent.model_validate_json(canonical(v1))) == saved
    assert digest(v1) == before
    c = ChannelContext(
        platform="qq",
        account_id="765681548",
        message_id="m",
        sender_id="2512627824",
        conversation_id="group:g",
        mentions=["2285027817"],
        reply_to="previous",
        nickname="送风格",
    )
    v2 = SourceEventV2(**{**v1.model_dump(), "schema_version": 2, "event_id": "v2"}, channel=c)
    client = TestClient(create_app(store, "x" * 32, retriever=setup[0]))
    response = client.post("/v1/ingest", json=v2.model_dump(), headers={"Authorization": "Bearer " + "x" * 32})
    assert response.status_code == 200
    record = client.get("/v1/records/" + response.json()["record_id"], headers={"Authorization": "Bearer " + "x" * 32})
    assert record.status_code == 200
    assert record.json()["source"]["channel"]["reply_to"] == "previous"


def test_model_inference_cannot_activate_from_self_confidence(store, setup):
    flow, idx = setup
    saved = add(store, "fact", "我喜欢摄影")
    idx.sync_batch()
    f = {
        "entity": "qq:2512627824",
        "predicate": "preference",
        "value": "可能偏好户外活动",
        "basis": "inferred",
        "confidence": 0.99,
        "verified": True,
        "references": [{"record_id": saved["record_id"], "revision": 1, "start": 0, "end": 5}],
    }
    idx.add_model_facts(saved["record_id"], 1, [f], activation=True)
    assert not idx.lookup(["qq:2512627824"], ["preference"])
    idx.add_model_facts(
        saved["record_id"],
        1,
        [f],
        activation=True,
        inference_policy={"preference": {"precision": 0.97, "blind_count": 40, "threshold": 0.98}},
    )
    result = flow.recall("我有什么偏好", subject=RecallSubject(platform="qq", account_id="2512627824"))
    assert result["assessment"]["state"] == "INFERRED"


def test_source_sender_is_not_replaced_by_claimed_target(store, setup):
    flow, idx = setup
    saved = add(store, "claim", "听说秋语喜欢摄影")
    idx.sync_batch()
    f = {
        "entity": "qq:2285027817",
        "predicate": "likes",
        "value": "摄影",
        "basis": "direct",
        "references": [{"record_id": saved["record_id"], "revision": 1, "start": 0, "end": 8}],
    }
    with pytest.raises(ValueError, match="ENTITY_NOT_GROUNDED"):
        idx.add_model_facts(saved["record_id"], 1, [f])


def test_reply_links_are_exact_and_missing_reply_is_not_fabricated(store, setup):
    flow, idx = setup
    for key, reply in [("a", None), ("b", "a"), ("c", "missing")]:
        store.ingest(
            SourceEventV2(
                character_id="misty-test",
                source_instance="qq",
                event_id=key,
                session_id="s",
                conversation_id="g",
                occurred_at=10,
                content="测试",
                channel=ChannelContext(
                    platform="qq",
                    account_id="765681548",
                    message_id=key,
                    sender_id="2512627824",
                    conversation_id="g",
                    reply_to=reply,
                ),
            )
        )
    idx.sync_batch()
    docs = {r["message_key"]: r["record_id"] for r in idx.db.execute("select * from documents")}
    a = next(v for k, v in docs.items() if json.loads(k)[-1] == "a")
    b = next(v for k, v in docs.items() if json.loads(k)[-1] == "b")
    c = next(v for k, v in docs.items() if json.loads(k)[-1] == "c")
    assert (a, "reply") in idx.related([b])
    assert not any(kind == "reply" for rid, kind in idx.related([c]))


def test_workflow_contract_serialization_and_health(store, setup):
    flow, idx = setup
    add(store, "profile", "我叫送风格")
    idx.sync_batch()
    client = TestClient(create_app(store, "x" * 32, retriever=flow))
    headers = {"Authorization": "Bearer " + "x" * 32}
    assert client.get("/v1/health", headers=headers).json()["workflow"]["workflow_version"] == "memory-workflow-v1"
    r = client.post("/v1/recall", headers=headers, json={"query": "2512627824叫啥"})
    assert r.status_code == 200, r.text
    assert r.json()["assessment"]["state"] == "DIRECT"


def test_parallel_source_revalidation_uses_one_connection_owner(store, setup):
    from concurrent.futures import ThreadPoolExecutor

    flow, idx = setup
    add(store, "name", "我叫送风格")
    idx.sync_batch()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: flow.recall(f"2512627824叫什么名字 {i}"), range(120)))
    assert all(r["assessment"]["sufficient"] for r in results)


def test_same_alias_does_not_merge_people_or_claim_sufficiency(store, setup):
    flow, idx = setup
    add(store, "a", "我叫小林", "12345")
    add(store, "b", "我叫小林", "67890")
    idx.sync_batch()
    result = flow.recall("小林叫什么名字")
    assert result["assessment"]["state"] == "CONFLICT"
    assert not result["assessment"]["sufficient"]


def test_relation_must_answer_the_asked_value(store, setup):
    flow, idx = setup
    saved = add(store, "a", "我喜欢篮球", "12345")
    idx.sync_batch()
    idx.add_model_facts(
        saved["record_id"],
        1,
        [
            {
                "entity": "qq:12345",
                "predicate": "likes",
                "value": "篮球",
                "basis": "direct",
                "confidence": 1,
                "verified": True,
                "references": [{"record_id": saved["record_id"], "revision": 1, "start": 0, "end": 5}],
            }
        ],
        activation=True,
    )
    result = flow.recall("12345喜欢摄影吗", targets=[RecallSubject(platform="qq", account_id="12345")])
    assert not result["assessment"]["sufficient"]
    assert (
        flow.recall("12345喜欢什么", targets=[RecallSubject(platform="qq", account_id="12345")])["assessment"]["state"]
        == "DIRECT"
    )


def test_cancel_and_service_failure_are_not_normal_empty_results(store, setup):
    import threading

    flow, idx = setup
    cancel = threading.Event()
    cancel.set()
    result = flow.recall("不存在的资料", cancel=cancel)
    assert result["assessment"]["state"] == "INCOMPLETE"
    assert result["assessment"]["reason"] == "cancelled"
    flow.base.hybrid = True
    result = flow.recall("不存在的资料")
    assert result["assessment"]["state"] == "INCOMPLETE"


def test_cross_message_dependency_is_rejected_before_index_refresh(store, setup):
    flow, idx = setup
    first = add(store, "one", "我喜欢摄影", "12345")
    second = add(store, "two", "我也喜欢摄影", "12345")
    idx.sync_batch()
    idx.add_model_facts(
        second["record_id"],
        1,
        [
            {
                "entity": "qq:12345",
                "predicate": "likes",
                "value": "摄影",
                "basis": "direct",
                "confidence": 1,
                "verified": True,
                "references": [
                    {"record_id": first["record_id"], "revision": 1, "start": 0, "end": 5},
                    {"record_id": second["record_id"], "revision": 1, "start": 0, "end": 6},
                ],
            }
        ],
        activation=True,
    )
    assert idx.lookup(["qq:12345"], ["likes"])
    store.correct(
        Correction(
            character_id="misty-test",
            source_instance="fix",
            event_id="retract",
            record_id=first["record_id"],
            expected_revision=1,
            reason="withdrawn",
            retract=True,
        )
    )
    assert not idx.lookup(["qq:12345"], ["likes"])
    idx.sync_batch()
    assert (
        idx.db.execute("SELECT status FROM jobs WHERE record_id=? AND revision=1", (second["record_id"],)).fetchone()[0]
        == "PENDING"
    )


def test_name_observation_does_not_establish_real_full_name(store, setup):
    flow, idx = setup
    add(store, "profile", json.dumps({"display_name_source": "qq_profile", "observed_display_name": "小林"}), "12345")
    idx.sync_batch()
    assert not flow.recall("12345的真实姓名")["assessment"]["sufficient"]


def test_verified_uncalibrated_model_fact_is_only_a_related_clue(store, setup):
    flow, idx = setup
    saved = add(store, "liked", "我喜欢摄影", "12345")
    idx.sync_batch()
    idx.add_model_facts(
        saved["record_id"],
        1,
        [
            {
                "entity": "qq:12345",
                "predicate": "likes",
                "value": "摄影",
                "basis": "direct",
                "confidence": 1,
                "verified": True,
                "references": [{"record_id": saved["record_id"], "revision": 1, "start": 0, "end": 5}],
            }
        ],
    )
    r = flow.recall("12345喜欢什么", targets=[RecallSubject(platform="qq", account_id="12345")])
    assert r["items"][0]["evidence"]["eligible"] is False
    assert r["assessment"]["state"] == "RELATED"


def test_explicit_calendar_dates_and_quotes_have_distinct_constraints():
    from neuralcloud.workflow import dated_constraints
    from datetime import datetime, timezone, timedelta

    expected = datetime(2026, 10, 27, tzinfo=timezone(timedelta(hours=8))).timestamp()
    c = dated_constraints("回忆2026年10月27日聊的事", {})
    assert c["since"] == expected and c["until"] < expected + 86400
    assert dated_constraints("谁说了“2026-10-27”", {}) == {}


def test_numeric_target_in_relation_does_not_fall_back_to_current_speaker(store, setup):
    flow, idx = setup
    speaker = RecallSubject(platform="qq", account_id="67890")
    assert flow.plan("12345喜欢什么", speaker, "auto", [])[1] == ["qq:12345"]
    from neuralcloud.storage import Refusal

    with pytest.raises(Refusal, match="WORKFLOW_REQUIRED_FOR_CONSTRAINTS"):
        flow.recall("摄影", mode="legacy", targets=[speaker])


def test_known_robot_roles_persist_for_readonly_index(store, tmp_path):
    path = tmp_path / "roles.db"
    add(store, "robot", "我叫机器人", "12345")
    writer = EvidenceIndex(store.path, path, writable=True, self_accounts=["qq:12345"])
    writer.sync_batch()
    writer.close()
    reader = EvidenceIndex(store.path, path)
    try:
        assert "qq:12345" in reader.self_accounts
        assert not reader.lookup(["qq:12345"], ["name"])
    finally:
        reader.close()


def test_same_person_unrelated_reason_or_decision_is_not_sufficient():
    from neuralcloud.workflow import MemoryWorkflow

    fact = {"predicate": "reason", "value": "因为天气取消了旅行"}
    assert not MemoryWorkflow._answers_value("12345为什么换工作", "cause", fact)
    assert MemoryWorkflow._answers_value("12345为什么取消旅行", "cause", fact)
    fact = {"predicate": "decision", "value": "决定购买相机"}
    assert not MemoryWorkflow._answers_value("12345搬家的决定", "fact", fact)


def test_imperative_jiao_is_not_a_name_and_nickname_is_not_full_name(store, setup):
    flow, idx = setup
    add(store, "command", "我叫你做好准备你耳朵聋吗", "12345")
    add(store, "command2", "叫我去接你", "12345")
    add(store, "command3", "我叫小张来接你", "12345")
    idx.sync_batch()
    assert not idx.lookup(["qq:12345"], ["name"])
    add(store, "intro", "你可以叫我小林", "12345")
    idx.sync_batch()
    assert not flow.recall("12345的真实姓名")["assessment"]["sufficient"]
    add(store, "full", "你可以叫我小林，全名林大川", "12345")
    idx.sync_batch()
    assert flow.recall("12345的全名")["assessment"]["sufficient"]


def test_friend_name_is_a_relationship_question_and_other_account_stays_explicit(store, setup):
    flow, idx = setup
    add(store, "owner", "我叫小林", "12345")
    idx.sync_batch()
    r = flow.recall("12345的朋友叫什么名字")
    assert not r["assessment"]["sufficient"]
    assert flow.plan("12345的朋友叫什么名字", None, "auto", [])[2] == ["relationship"]
    for query in ["12345最喜欢哪首歌", "12345上个月去过哪里", "12345有没有申请过假期", "12345平时如何通勤"]:
        assert flow.plan(query, RecallSubject(platform="qq", account_id="67890"), "auto", [])[1] == ["qq:12345"]
