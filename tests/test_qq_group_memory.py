from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import subprocess
import sys

from pydantic import ValidationError
import pytest

from harness.qq.memory import (
    QQVerifiedProfileInput,
    QQVisualMemoryStorageError,
    QQVisualMemoryStore,
    QQVisualObservationInput,
)


BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)


def profile(store, member="20001", *, group="10001", name="虚构同名成员", revision=0):
    return store.record_profile(QQVerifiedProfileInput(
        group_id=group, member_id=member, display_name=name, display_name_source="qq_profile",
        verification="qq_ui_verified", capture_id=f"synthetic-profile:{group}:{member}:{revision}",
        observed_at=BASE + timedelta(days=revision),
        group_evidence=[f"test-evidence://fictional/group/{group}"],
        member_evidence=[f"test-evidence://fictional/member/{member}/{revision}"],
    )).profile


def observe(store, person, number, *, text="虚构日常记录", claim=None):
    return store.record_observation(QQVisualObservationInput(
        group_id=person.group_id, member_id=person.member_id, profile_record_id=person.record_id,
        capture_id=f"synthetic-chat:{person.group_id}:{person.member_id}:{number}", capture_item_id="bubble:1",
        observed_at=BASE + timedelta(minutes=number), text=text,
        evidence=[f"test-evidence://fictional/chat/{number}"],
        author_attribution="qq_ui_verified",
        attribution_evidence=[f"test-evidence://fictional/attribution/{person.member_id}/{number}"],
        claims=[{"statement": claim or text, "supporting_quote": text, "subject": "self"}],
    )).observation


def test_old_fact_beyond_recent_window_is_found_with_original_profile_evidence(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    old_profile = profile(store, name="旧虚构昵称")
    old = observe(store, old_profile, 0, text="我不喜欢蓝色，我喜欢红茶。")
    for number in range(1, 101):
        observe(store, old_profile, number)
    new_profile = profile(store, name="新虚构昵称", revision=1)
    context = store.group_context(group_id="10001", query="以前喜欢红茶吗", history_limit_per_member=1)
    person, retrieval = context.members[0], context.member_retrieval[0]
    assert context.participants == [old_profile.member_id]
    assert len(retrieval.recent_record_ids) == 5
    assert old.record_id in retrieval.history_record_ids
    assert retrieval.total_observations == 101
    assert retrieval.history_candidate_count == retrieval.history_candidates_scanned == 96
    assert retrieval.candidate_scan_truncated is False
    assert retrieval.history_match_count == 1
    assert retrieval.history_truncated is False
    assert "红茶" in retrieval.history_matches[0].matched_terms
    assert old in person.observations
    assert {item.record_id for item in person.profiles} == {old_profile.record_id, new_profile.record_id}
    assert person.observations[-1].claims[0].kind == "unverified_author_assertion"
    assert person.observations[-1].text.startswith("我不喜欢蓝色")
    assert person.observations[-1].message_id is None


def test_same_name_members_get_separate_budgets_and_never_merge_other_group(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    active, quiet = profile(store), profile(store, "20002")
    elsewhere = profile(store, "20001", group="10002")
    for number in range(20):
        observe(store, active, number, text="我偏好蓝色。")
    for number in range(2):
        observe(store, quiet, number, text="我偏好绿色。")
    observe(store, elsewhere, 99, text="其他群的身份声明")
    context = store.group_context(
        group_id="10001", query="颜色偏好", recent_limit_per_member=1, history_limit_per_member=1,
    )
    assert context.participants == ["20001", "20002"]
    assert [len(person.observations) for person in context.members] == [2, 2]
    assert all(person.group_id == "10001" for person in context.members)
    assert all(item.text == "我偏好绿色。" for item in context.members[1].observations)
    assert context.member_retrieval[0].history_truncated is True
    assert context.member_retrieval[1].history_truncated is False
    assert context.member_retrieval[1].observations_omitted_count == 0


def test_query_matches_claims_with_explicit_basis_without_claiming_objective_truth(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    person = profile(store)
    old = observe(store, person, 0, text="我常喝这个。", claim="作者自述偏好乌龙茶")
    observe(store, person, 1)
    context = store.group_context(group_id="10001", query="乌龙茶", recent_limit_per_member=1)
    match = context.member_retrieval[0].history_matches[0]
    assert match.record_id == old.record_id
    assert match.matched_fields == ["claims.statement"]
    assert context.members[0].observations[-1].text == "我常喝这个。"
    assert context.members[0].observations[-1].claims[0].kind == "unverified_author_assertion"


def test_no_query_selects_earliest_history_and_query_miss_does_not_fallback(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    person = profile(store)
    records = [observe(store, person, number) for number in range(8)]
    context = store.group_context(group_id="10001", recent_limit_per_member=2, history_limit_per_member=2)
    retrieval = context.member_retrieval[0]
    assert retrieval.recent_record_ids == [records[7].record_id, records[6].record_id]
    assert retrieval.history_record_ids == [records[0].record_id, records[1].record_id]
    assert retrieval.history_match_count == 6
    assert retrieval.history_truncated is True
    assert context.retrieval.history_selection == "earliest_outside_recent"
    assert all(item.reason == "earliest_history" for item in retrieval.history_matches)
    miss = store.group_context(group_id="10001", query="不存在的火星关键词", recent_limit_per_member=2)
    assert miss.member_retrieval[0].history_record_ids == []
    assert miss.member_retrieval[0].history_match_count == 0
    assert len(miss.members[0].observations) == 2


@pytest.mark.parametrize("query,text", [("猫", "我喜欢猫。"), ("cafe", "我喜欢ＣＡＦＥ。"), ("ＣＡＦＥ", "Cafe is nice.")])
def test_literal_chinese_single_character_and_unicode_normalization(tmp_path, query, text):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    person = profile(store)
    old = observe(store, person, 0, text=text)
    observe(store, person, 1)
    context = store.group_context(group_id="10001", query=query, recent_limit_per_member=1)
    assert context.member_retrieval[0].history_record_ids == [old.record_id]


def test_explicit_empty_unknown_and_cross_group_ids_never_fallback(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    profile(store)
    profile(store, "20002", group="10002")
    assert store.group_context(group_id="10001", member_ids=[]).participants == []
    unknown = store.group_context(group_id="10001", member_ids=["29999", "20002"])
    assert unknown.members == []
    assert unknown.retrieval.missing_member_ids == ["29999", "20002"]
    assert store.group_context(group_id="10003").members == []
    known = store.group_context(group_id="10001", member_ids=["20001", "20001"])
    assert known.participants == ["20001"]
    assert known.members[0].observations == []
    assert known.member_retrieval[0].total_observations == 0


def test_member_caps_order_distinct_profiles_and_exact_truncation(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    for member in ("9", "10", "99999999999999999999"):
        profile(store, member)
    profile(store, "9", revision=1)
    context = store.group_context(group_id="10001", max_members=2)
    assert context.participants == ["9", "10"]
    assert context.retrieval.group_known_member_count == 3
    assert context.retrieval.candidate_member_count == 3
    assert context.retrieval.members_truncated is True
    assert context.retrieval.omitted_member_count == 1
    explicit = store.group_context(group_id="10001", member_ids=["99999999999999999999", "10"], max_members=2)
    assert explicit.participants == ["99999999999999999999", "10"]
    assert explicit.retrieval.members_truncated is False


def test_zero_budget_and_exact_history_limit_report_real_omissions(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    person = profile(store)
    records = [observe(store, person, number, text="红茶") for number in range(4)]
    exact = store.group_context(group_id="10001", query="红茶", recent_limit_per_member=2, history_limit_per_member=2)
    retrieval = exact.member_retrieval[0]
    assert retrieval.history_match_count == 2
    assert retrieval.history_truncated is False
    assert retrieval.observations_omitted_count == 0
    assert len({item.record_id for item in exact.members[0].observations}) == 4
    history_only = store.group_context(group_id="10001", query="红茶", recent_limit_per_member=0, history_limit_per_member=1)
    assert history_only.member_retrieval[0].recent_record_ids == []
    assert history_only.member_retrieval[0].history_record_ids == [records[-1].record_id]
    recent_only = store.group_context(group_id="10001", recent_limit_per_member=1, history_limit_per_member=0)
    assert len(recent_only.members[0].observations) == 1
    assert recent_only.member_retrieval[0].history_record_ids == []


def test_history_is_read_in_a_new_python_process(tmp_path):
    database = tmp_path / "group.sqlite3"
    store = QQVisualMemoryStore(database)
    person = profile(store)
    old = observe(store, person, 0, text="我记得虚构的红茶。")
    for number in range(1, 20):
        observe(store, person, number)
    result = subprocess.run(
        [sys.executable, "-c",
         "from harness.qq.memory import QQVisualMemoryStore; import sys; "
         "print(QQVisualMemoryStore(sys.argv[1]).group_context(group_id='10001', query='红茶').model_dump_json())",
         str(database)],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=20,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    payload = json.loads(result.stdout)
    assert old.record_id in payload["member_retrieval"][0]["history_record_ids"]
    old_record = next(item for item in payload["members"][0]["observations"] if item["record_id"] == old.record_id)
    assert old_record["evidence"] == old.evidence
    assert old_record["message_id"] is None


def test_queries_preserve_existing_rows_schema_and_single_person_interface(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    person = profile(store)
    observe(store, person, 0)
    previous = store.reply_context(group_id="10001", member_id="20001")

    def snapshot():
        connection = sqlite3.connect(store.path)
        try:
            return {
                table: (connection.execute(f"PRAGMA table_info({table})").fetchall(),
                        connection.execute(f"SELECT * FROM {table}").fetchall())
                for table in ("qq_visual_profiles", "qq_visual_observations")
            }
        finally:
            connection.close()

    before = snapshot()
    store.group_context(group_id="10001", query="虚构")
    assert snapshot() == before
    assert store.reply_context(group_id="10001", member_id="20001") == previous


def test_query_term_budget_reports_truncation_without_changing_member_scope(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    person = profile(store)
    observe(store, person, 0)
    context = store.group_context(group_id="10001", query="".join(chr(0x4E00 + offset) for offset in range(100)))
    assert context.retrieval.terms_truncated is True
    assert len(context.retrieval.terms) == 64
    assert context.participants == ["20001"]


def test_long_literal_query_term_is_not_silently_dropped(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    person = profile(store)
    identifier = "a" * 65
    old = observe(store, person, 0, text=f"虚构标识 {identifier}")
    observe(store, person, 1)
    context = store.group_context(group_id="10001", query=identifier + " 红茶", recent_limit_per_member=1)
    assert identifier in context.retrieval.terms
    assert context.retrieval.terms_truncated is False
    assert context.member_retrieval[0].history_record_ids == [old.record_id]


def test_account_relationship_claim_remains_with_author_during_group_history_search(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    author, other = profile(store), profile(store, "20002")
    claim = store.record_observation(QQVisualObservationInput(
        group_id="10001", member_id="20001", profile_record_id=author.record_id,
        capture_id="synthetic-account-claim", capture_item_id="bubble:1", observed_at=BASE,
        text="20002是我小号。", evidence=["test-evidence://fictional/account-claim"],
        author_attribution="qq_ui_verified", attribution_evidence=["test-evidence://fictional/author"],
        claims=[{"statement": "作者声称20002是自己的小号", "supporting_quote": "20002是我小号",
                 "subject": "account_relationship", "subject_label": "20002"}],
    )).observation
    observe(store, author, 1)
    context = store.group_context(group_id="10001", query="20002", recent_limit_per_member=1)
    assert claim.record_id in context.member_retrieval[0].history_record_ids
    assert context.members[1].profiles == [other]
    assert context.members[1].observations == []
    assert store.group_context(group_id="10001", member_ids=["20002"], query="小号").members[0].observations == []


def test_one_group_query_uses_one_snapshot_even_if_a_new_observation_arrives(tmp_path, monkeypatch):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    first, second = profile(store), profile(store, "20002")
    observe(store, first, 0)
    original_second = observe(store, second, 0)
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
    finally:
        connection.close()
    method = store._group_member_context
    inserted = False

    def concurrent_write(connection, request, member_id, terms):
        nonlocal inserted
        if not inserted:
            inserted = True
            observe(QQVisualMemoryStore(store.path), second, 1, text="新到达的虚构消息")
        return method(connection, request, member_id, terms)

    monkeypatch.setattr(store, "_group_member_context", concurrent_write)
    context = store.group_context(group_id="10001")
    assert context.members[1].observations == [original_second]
    assert context.member_retrieval[1].total_observations == 1
    assert len(QQVisualMemoryStore(store.path).group_context(group_id="10001").members[1].observations) == 2


def test_old_corrupt_identity_scope_is_not_silently_returned_or_skipped(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    person = profile(store)
    old = observe(store, person, 0, text="红茶")
    observe(store, person, 1)
    connection = sqlite3.connect(store.path)
    try:
        with connection:
            connection.execute(
                "UPDATE qq_visual_observations SET record_json = ? WHERE record_id = ?",
                (old.model_copy(update={"member_id": "20002"}).model_dump_json(), old.record_id),
            )
    finally:
        connection.close()
    with pytest.raises(QQVisualMemoryStorageError):
        store.group_context(group_id="10001", query="红茶", recent_limit_per_member=1)


@pytest.mark.parametrize("table", ["qq_visual_profiles", "qq_visual_observations"])
def test_malformed_json_remains_a_storage_error_even_during_ordering(tmp_path, table):
    store = QQVisualMemoryStore(tmp_path / "group.sqlite3")
    person = profile(store)
    observe(store, person, 0)
    connection = sqlite3.connect(store.path)
    try:
        with connection:
            connection.execute(f"UPDATE {table} SET record_json = ?", ("{broken",))
    finally:
        connection.close()
    with pytest.raises(QQVisualMemoryStorageError):
        store.group_context(group_id="10001")


@pytest.mark.parametrize(
    "changes",
    [{"group_id": "昵称"}, {"member_ids": [123]}, {"member_ids": ["昵称"]},
     {"query": " "}, {"query": "😀！？"}, {"query": "x" * 513},
     {"max_members": 0}, {"max_members": 101}, {"max_members": True},
     {"recent_limit_per_member": -1}, {"history_limit_per_member": 51},
     {"recent_limit_per_member": 0, "history_limit_per_member": 0}],
)
def test_invalid_query_scope_and_budgets_fail_before_opening_database(tmp_path, changes):
    database = tmp_path / "never-created.sqlite3"
    with pytest.raises((ValueError, ValidationError)):
        QQVisualMemoryStore(database).group_context(**{"group_id": "10001", **changes})
    assert not database.exists()
