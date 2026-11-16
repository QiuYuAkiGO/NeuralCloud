import pytest
from fastapi.testclient import TestClient

from neuralcloud.api import create_app
from neuralcloud.contracts import RecallSubject, SourceEvent, Correction
from neuralcloud.retrieval import IdentityIndex, Retriever


def ingest(store, key, text, **kw):
    return store.ingest(
        SourceEvent(
            character_id="misty-test",
            source_instance="fixture",
            event_id=key,
            session_id="a",
            occurred_at=1,
            content=text,
            **kw,
        )
    )


def seeded(store, tmp_path):
    saved = ingest(store, "creator", "秋语（全名秋语随风，QQ号2285027817）是 Misty 的唯一创造者。", pinned=True)
    identities = IdentityIndex(tmp_path / "identity.sqlite3")
    identities.seed(
        [
            dict(
                platform="qq",
                account_id="2285027817",
                record_id=saved["record_id"],
                revision=1,
                confirmation="operator_confirmed",
                name="秋语",
                relation="创造者",
            )
        ],
        store,
    )
    return identities, saved


def correction(store, saved, **kw):
    return store.correct(
        Correction(
            character_id="misty-test",
            source_instance="fixture",
            event_id="fix",
            record_id=saved["record_id"],
            expected_revision=1,
            reason="correction",
            **kw,
        )
    )


def test_incident_identity_survives_topic_noise_and_does_not_bind_neighbor(store, tmp_path):
    identities, saved = seeded(store, tmp_path)
    for i in range(25):
        ingest(store, str(i), "那还行", participants=["2285027817"])
    engine = Retriever(store, identities)
    subject = RecallSubject(platform="qq", account_id="2285027817")
    for query in ["对了，你还记得我是谁吗", "我该怎么称呼", "2285027817 群成员 身份 昵称 秋日私语 1967 VRChat"]:
        result = engine.recall(query, subject=subject)
        assert result["items"][0]["record_id"] == saved["record_id"]
        assert result["items"][0]["identity"]["confirmation"] == "operator_confirmed"
    wrong = engine.recall("我是谁", subject=RecallSubject(platform="qq", account_id="2285027818"))
    assert not any(r.get("identity") for r in wrong["items"])
    identities.close()


@pytest.mark.parametrize("change", [dict(retract=True), dict(content="此身份确认已撤销")])
def test_identity_never_survives_revision_or_retraction(store, tmp_path, change):
    identities, saved = seeded(store, tmp_path)
    correction(store, saved, **change)
    assert identities.lookup(RecallSubject(platform="qq", account_id="2285027817"), store) == []
    identities.close()


def test_participant_is_not_identity_and_conflicting_evidence_stays_labeled(store, tmp_path):
    identities, saved = seeded(store, tmp_path)
    ingest(store, "talk", "我是路人", participants=["2285027818"])
    assert not identities.lookup(RecallSubject(platform="qq", account_id="2285027818"), store)
    other = ingest(store, "conflict", "有人称此账号叫春雨")
    identities.seed(
        [
            dict(
                platform="qq",
                account_id="2285027817",
                record_id=other["record_id"],
                revision=1,
                confirmation="third_party",
                name="春雨",
                relation="称呼",
            )
        ],
        store,
    )
    results = identities.lookup(RecallSubject(platform="qq", account_id="2285027817"), store)
    assert len(results) == 2 and all(r["identity"]["conflict"] for r in results)
    assert results[0]["identity"]["confirmation"] == "operator_confirmed"
    identities.close()


def test_relaxed_search_preserves_exact_account_and_unrelated_pin_not_returned(store):
    right = ingest(store, "a", "2285027817 自称秋语")
    ingest(store, "b", "2285027818 自称秋语")
    ingest(store, "c", "宇宙飞船", pinned=True)
    results = Retriever(store).recall("2285027817 昵称 秋语 VRChat")["items"]
    assert [r["record_id"] for r in results] == [right["record_id"]]


def test_same_name_with_conflicting_relation_is_not_silently_merged(store, tmp_path):
    identities, saved = seeded(store, tmp_path)
    other = ingest(store, "contradiction", "一则未经确认的相反关系描述")
    identities.seed(
        [
            dict(
                platform="qq",
                account_id="2285027817",
                record_id=other["record_id"],
                revision=1,
                confirmation="third_party",
                name="秋语",
                relation="不是创造者",
            )
        ],
        store,
    )
    results = identities.lookup(RecallSubject(platform="qq", account_id="2285027817"), store)
    assert len(results) == 2 and all(row["identity"]["conflict"] for row in results)
    assert results[0]["record_id"] == saved["record_id"]
    assert results[1]["identity"]["confirmation"] == "third_party"
    identities.close()


def test_semantic_error_degrades_to_valid_identity(store, tmp_path):
    identities, saved = seeded(store, tmp_path)

    class Broken:
        def search(self, *a, **kw):
            raise TimeoutError()

    result = Retriever(store, identities, Broken(), hybrid=True).recall(
        "我是谁", subject=RecallSubject(platform="qq", account_id="2285027817")
    )
    assert result["diagnostics"]["degraded"] == "TimeoutError"
    assert result["items"][0]["record_id"] == saved["record_id"]
    identities.close()


def test_explicit_name_phrase_stays_exact_in_keyword_and_vector_results(store):
    right = ingest(store, "phrase", "秋语随风喜欢蓝色")
    wrong = ingest(store, "separate", "秋语昨天说话，语随字出现，随风又是另一个词")

    class Search:
        def search(self, *a, **kw):
            return [dict(record_id=wrong["record_id"], revision=1, start=0, end=12, score=0.99)]

    for query in ['"秋语随风"', "“秋语随风”"]:
        result = Retriever(store, semantic=Search(), hybrid=True).recall(query)
        assert [r["record_id"] for r in result["items"]] == [right["record_id"]]


def test_semantic_revalidates_after_search_and_returns_late_excerpt(store):
    text = "前文。" * 1000 + "关键证据在这里"
    saved = ingest(store, "long", text)

    class Search:
        def search(self, *a, **kw):
            return [dict(record_id=saved["record_id"], revision=1, start=3000, end=len(text), score=0.9)]

    engine = Retriever(store, semantic=Search(), hybrid=True)
    result = engine.recall("最后证据")
    assert result["items"][0]["content"] == "关键证据在这里"
    correction(store, saved, retract=True)
    assert not engine.recall("最后证据")["items"]


def test_public_contract_and_legacy_compatibility(store, tmp_path):
    identities, saved = seeded(store, tmp_path)
    with TestClient(create_app(store, "x" * 32, retriever=Retriever(store, identities))) as client:
        headers = {"Authorization": "Bearer " + "x" * 32}
        r = client.post(
            "/v1/recall",
            headers=headers,
            json={"query": "我是谁", "subject": {"platform": "qq", "account_id": "2285027817"}},
        )
        assert r.status_code == 200 and r.json()["items"][0]["identity"]["name"] == "秋语"
        assert client.post("/v1/recall", headers=headers, json={"query": "秋语", "mode": "legacy"}).status_code == 200
        assert (
            client.post(
                "/v1/recall",
                headers=headers,
                json={"query": "秋语", "subject": {"platform": "qq", "account_id": "2285", "admin": True}},
            ).status_code
            == 422
        )
    identities.close()
