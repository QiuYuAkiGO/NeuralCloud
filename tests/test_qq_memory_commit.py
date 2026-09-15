"""Commit/recall acceptance through real immutable QQ and shared SQLite stores."""

from copy import deepcopy
import asyncio
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from gui.presets import CharacterPreset, hydrate_memory, snapshot_memory
from harness.cognitive.qq_memory import QQMemoryWorker, build_commit, memory_state, select_recall
from harness.config import Settings
from harness.memory.impressions import ImpressionMemory
from harness.memory.qq_assimilation import QQAssimilationQueue, QQMemoryProposal
from harness.memory.service import build_inmemory_service
from harness.memory.shared import SharedMemoryStore, SharedReadScope
from harness.memory.store.inmemory import InMemoryStores
from harness.qq.memory import (
    QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput, protocol_message_evidence_ref,
)
from tests.factories import vaelith


NOW = datetime(2026, 9, 14, 5, tzinfo=UTC)
LATER = NOW + timedelta(days=30)
EVIDENCE = "qq-protocol://sha256/" + "a" * 64


class SourceFixture:
    def __init__(self, tmp_path):
        self.visual = QQVisualMemoryStore(tmp_path / "visual.sqlite3")
        self.shared = SharedMemoryStore(tmp_path / "shared.sqlite3")
        self.queue = QQAssimilationQueue(self.shared.path, owner_id="misty", preset_id="misty", self_id="999",
                                         allowed_group_ids=("100", "101"))
        self.index = 0

    def add(self, *, group="100", member="200", text="我会解释今天的攻略步骤", at=NOW, legacy=False, image_analysis=None):
        self.index += 1
        suffix = str(self.index)
        profile_input = QQVerifiedProfileInput(group_id=group, member_id=member, display_name="同名群友",
            display_name_source="qq_profile" if legacy else "qq_metadata",
            verification="qq_ui_verified" if legacy else "qq_protocol_verified", capture_id="profile-" + suffix,
            observed_at=at, group_evidence=[EVIDENCE], member_evidence=[EVIDENCE])
        profile = (self.visual.record_profile(profile_input) if legacy else self.visual.record_protocol_profile(profile_input)).profile
        images = image_analysis or []
        extra_evidence = [item["evidence_ref"] for item in images if item.get("evidence_ref")]
        incoming = QQVisualObservationInput(group_id=group, member_id=member, profile_record_id=profile.record_id,
            capture_id="observation-" + suffix, capture_item_id=suffix, observed_at=at, text=text,
            evidence=[EVIDENCE, *extra_evidence],
            author_attribution="qq_ui_verified" if legacy else "qq_protocol_verified",
            attribution_evidence=[EVIDENCE] if legacy else [EVIDENCE, protocol_message_evidence_ref(group, member, suffix)],
            message_id=None if legacy else suffix, message_id_source=None if legacy else "qq_metadata",
            image_analysis=images)
        result = self.visual.record_observation(incoming) if legacy else self.visual.record_protocol_observation(incoming)
        imported = self.shared.import_qq_database(self.visual.path, allowed_group_ids=["100", "101"], self_member_id="999")
        assert imported.success
        return result.observation

    def jobs(self):
        return self.queue.enqueue(now=LATER, backfill=True)


@pytest.fixture
def sources(tmp_path):
    return SourceFixture(tmp_path)


def _preset():
    return CharacterPreset(preset_id="misty", name="Misty", session_id="sess_test", persona=vaelith(), updated_at=NOW)


def _proposal(job, *, impression=True, statement="这位群友在攻略讨论中愿意分享具体做法", stance="support",
              target=None, context="游戏攻略讨论", topic="攻略解释", kind="person", subjects=None, selected=None, summary="群友讨论攻略", insight=None):
    refs = [source.source_id for source in job.sources]
    if subjects is None:
        subjects = [f"qq:member:{job.sources[0].member_id}"] if kind == "person" else []
    impressions = []
    if impression:
        impressions.append({"kind": kind, "subject_refs": subjects, "topic": topic, "context": context,
            "statement": statement, "stance": stance, "source_ids": selected or refs,
            "target_impression_id": target})
    return QQMemoryProposal.model_validate({"episodes": [{"source_ids": refs, "summary": summary,
        "insight": insight, "importance": 0.8, "valence": 0.0}], "impressions": impressions})


def _commit(preset, job, **kwargs):
    return build_commit(preset, job, _proposal(job, **kwargs), Settings(), now=LATER)


def _memory(preset):
    stores = InMemoryStores()
    for event in preset.episodic:
        stores.episodic.insert(event)
    return SimpleNamespace(stores=stores, qq_memory_state=deepcopy(preset.qq_memory_state))


def _impression(preset):
    return next(iter(preset.qq_memory_state["impressions"].values()))


def _four_day_commit(sources):
    for day in range(4):
        sources.add(at=NOW + timedelta(days=day))
    preset = _preset()
    jobs = sources.jobs()
    assert len(jobs) == 4
    for job in jobs:
        preset = _commit(preset, job)
    return preset


def test_commit_keeps_raw_sqlite_observations_and_input_preset_unchanged(sources):
    sources.add()
    jobs = sources.jobs()
    before = sources.shared.query(SharedReadScope(audience="local:main"), limit=100).model_dump_json()
    preset = _preset()
    original = preset.model_dump_json()
    committed = _commit(preset, jobs[0])
    assert preset.model_dump_json() == original
    assert committed is not preset
    assert sources.shared.query(SharedReadScope(audience="local:main"), limit=100).model_dump_json() == before


def test_episode_binds_scope_authors_source_evidence_and_timestamp(sources):
    sources.add(text="我自称擅长这个游戏")
    job = sources.jobs()[0]
    committed = _commit(_preset(), job)
    event = committed.episodic[0]
    assert event.scope.conversation_id == "qq:group:100"
    assert event.scope.visibility == ["local:main", "qq:group:100"]
    assert event.participants == ["qq:member:200"]
    assert event.source_refs == [job.sources[0].source_id]
    assert event.evidence == sorted(job.sources[0].record.evidence_refs)
    assert event.objective_fact == "200: 我自称擅长这个游戏"
    assert event.time == NOW
    assert event.processing_version == "qq-memory-v1"


def test_single_experience_creates_only_tentative_impression_and_candidate(sources):
    sources.add()
    committed = _commit(_preset(), sources.jobs()[0])
    impression = _impression(committed)
    assert impression["first_impression"] == impression["current_impression"]
    assert impression["status"] == "provisional"
    assert impression["confidence"] == 0.15
    assert len(committed.candidates) == 1
    assert not committed.semantic


def test_continued_round_retains_each_window_without_independent_support(sources):
    sources.add()
    first = _commit(_preset(), sources.jobs()[0])
    sources.add(at=NOW + timedelta(minutes=3), text="我接着说今天这份攻略的步骤")
    job = sources.jobs()[0]
    result = _commit(first, job, target=_impression(first)["impression_id"])
    impression = _impression(result)
    assert len(result.episodic) == len(result.qq_memory_state["receipts"]) == 2
    assert len(impression["evidence"]) == 1
    assert len(impression["continuation_evidence"]) == 1
    assert impression["confidence"] == result.candidates[0].confidence == 0.15
    assert len(result.candidates[0].evidence) == 1
    assert not result.semantic
    assert all(event.hit_count == 0 for event in result.episodic)
    assert _commit(result, job, target=impression["impression_id"]) is result


def test_four_days_of_independent_evidence_form_one_stable_judgment(sources):
    committed = _four_day_commit(sources)
    assert _impression(committed)["status"] == "stable"
    assert len(committed.semantic) == 1
    assert len(committed.semantic[0].supporting_events) == 4
    assert committed.candidates[0].status == "consolidated"


@pytest.mark.parametrize("initial_text,initial_statement,counter_text,revised_statement", [
    ("我会解释今天的攻略步骤", "这位群友在攻略讨论中愿意分享具体做法",
     "这次我不想说明具体步骤", "这位群友通常愿意解释，但这次明确拒绝，需要结合情境理解"),
    ("我不想说明攻略的具体步骤", "这位群友在攻略讨论中通常不愿展开说明",
     "我一步步说明：先查看地图，再选择路线", "这位群友先前不愿展开说明，但这次耐心给出步骤，需要结合情境理解"),
])
def test_counter_revises_current_keeps_first_and_retracts_stable_projection(
        sources, initial_text, initial_statement, counter_text, revised_statement):
    for day in range(4):
        sources.add(at=NOW + timedelta(days=day), text=initial_text)
    previous = _preset()
    for job in sources.jobs():
        previous = _commit(previous, job, statement=initial_statement)
    old = _impression(previous)
    sources.add(at=NOW + timedelta(days=4), text=counter_text)
    committed = _commit(previous, sources.jobs()[0], stance="counter", target=old["impression_id"],
                        statement=revised_statement)
    current = _impression(committed)
    assert current["first_impression"] == old["first_impression"]
    assert current["current_impression"] != old["current_impression"]
    assert current["status"] == "disputed"
    assert current["confidence"] < old["confidence"]
    assert not committed.semantic
    assert len(current["history"]) == 5
    assert committed.episodic[-1].hit_count == 0


@pytest.mark.parametrize("stance", ["support", "counter"])
def test_delayed_evidence_after_maintenance_preserves_clocks_and_replays_once(sources, stance):
    from harness.memory.qq_backfill import _maintain

    previous = _four_day_commit(sources)
    reviewed_at = NOW + timedelta(days=8)
    previous = _maintain(previous, Settings(), reviewed_at)
    before = _impression(previous)
    anchors = {event.event_id: event.strength_updated_at for event in previous.episodic}
    assert all(anchor == reviewed_at for anchor in anchors.values())

    # This observation was made after the prior evidence, but reached the
    # cleaner only after the existing memories had been aged to wall time.
    observed_at = NOW + timedelta(days=5)
    sources.add(at=observed_at, text="这次我愿意解释具体步骤" if stance == "support" else "这次我不想解释具体步骤")
    job = sources.jobs()[0]
    committed = _commit(previous, job, stance=stance, target=before["impression_id"])
    current = _impression(committed)
    assert current["evidence"][-1]["time"] == observed_at.isoformat().replace("+00:00", "Z")
    assert current["history"][-1]["revised_at"] == observed_at.isoformat().replace("+00:00", "Z")
    assert datetime.fromisoformat(current["strength_updated_at"]) == reviewed_at
    assert datetime.fromisoformat(current["updated_at"]) == observed_at
    expected_confidence = (before["confidence"] + 0.35 * (1 - before["confidence"]) if stance == "support"
                           else before["confidence"] * 0.65)
    assert current["confidence"] == pytest.approx(expected_confidence)
    for event in committed.episodic:
        if event.event_id in anchors:
            assert event.strength_updated_at >= anchors[event.event_id]
        assert event.hit_count == 0
    assert _commit(committed, job, stance=stance, target=before["impression_id"]) is committed


def test_same_job_replay_has_no_effect_even_when_proposal_changes(sources):
    sources.add()
    job = sources.jobs()[0]
    previous = _commit(_preset(), job)
    again = _commit(previous, job, statement="无效的新观点")
    assert again is previous
    assert len(again.qq_memory_state["receipts"]) == 1
    assert _impression(again)["confidence"] == 0.15


def test_context_change_creates_a_separate_initial_impression(sources):
    sources.add()
    previous = _commit(_preset(), sources.jobs()[0])
    old = deepcopy(_impression(previous))
    sources.add(at=NOW + timedelta(days=1), text="闲聊时候我不谈攻略")
    committed = _commit(previous, sources.jobs()[0], stance="context", target=old["impression_id"], context="闲聊休息时")
    assert len(committed.qq_memory_state["impressions"]) == 2
    assert committed.qq_memory_state["impressions"][old["impression_id"]] == old
    assert all(item["status"] == "provisional" for item in committed.qq_memory_state["impressions"].values())


@pytest.mark.parametrize("text,member", [("Misty自己的总结", "999"), ("[引用消息 123] 我之前说过的话", "200")])
def test_own_and_quoted_messages_can_be_retained_without_impressions(sources, text, member):
    sources.add(text=text, member=member)
    committed = _commit(_preset(), sources.jobs()[0], impression=False)
    assert len(committed.episodic) == 1
    assert not committed.qq_memory_state["impressions"]
    assert not committed.candidates


@pytest.mark.parametrize("text,member", [("我总结一下大家的性格", "999"), ("[CQ:reply,id=123]", "200")])
def test_own_or_quoted_message_cannot_independently_create_impression(sources, text, member):
    sources.add(text=text, member=member)
    with pytest.raises(ValueError):
        _commit(_preset(), sources.jobs()[0])


def test_other_author_evidence_cannot_launder_quoted_subject_into_independent_support(sources):
    sources.add(text="[引用消息 321]", member="200")
    sources.add(text="今天玩另一款游戏", member="201", at=NOW + timedelta(seconds=5))
    with pytest.raises(ValueError):
        _commit(_preset(), sources.jobs()[0], subjects=["qq:member:200"])


def test_quote_with_fresh_author_response_can_supply_its_own_evidence(sources):
    sources.add(text="[引用消息 321] 我再补充一下攻略的具体步骤")
    job = sources.jobs()[0]
    assert job.sources[0].contains_quote
    assert not job.sources[0].quote_only
    assert _impression(_commit(_preset(), job))["confidence"] == 0.15


def test_contrast_strengthening_uses_the_configured_decay_policy(sources):
    sources.add()
    previous = _commit(_preset(), sources.jobs()[0])
    old = _impression(previous)
    event = previous.episodic[0].model_copy(update={"tier": "mid", "expire_at": None})
    previous = previous.model_copy(update={"episodic": [event]})
    sources.add(at=NOW + timedelta(days=4), text="今天我不想解释攻略")
    job = sources.jobs()[0]
    proposal = _proposal(job, stance="counter", target=old["impression_id"])
    committed = build_commit(previous, job, proposal, Settings(qq_memory_decay_mid_days=1), now=LATER)
    strengthened = next(row for row in committed.episodic if row.event_id == event.event_id)
    assert strengthened.strength == pytest.approx(2 ** -4 + 0.15)


def test_legacy_recapture_retains_an_episode_but_cannot_deepen_impression(sources):
    sources.add(text="我喜欢猫", legacy=True)
    sources.add(text="我喜欢猫", legacy=True, at=NOW + timedelta(days=1))
    first, duplicate = sources.jobs()
    assert not duplicate.sources[0].independent_eligible
    previous = _commit(_preset(), first)
    committed = _commit(previous, duplicate, impression=False)
    assert len(committed.episodic) == 2
    assert _impression(committed)["confidence"] == 0.15


def test_uncertain_proposal_does_not_create_impression(sources):
    sources.add()
    committed = _commit(_preset(), sources.jobs()[0], stance="uncertain")
    assert len(committed.episodic) == 1
    assert not committed.qq_memory_state["impressions"]


def test_two_members_with_same_name_and_preference_have_distinct_impressions(sources):
    sources.add(member="200")
    sources.add(member="201", at=NOW + timedelta(minutes=5))
    previous = _preset()
    for job in sources.jobs():
        previous = _commit(previous, job)
    assert len(previous.qq_memory_state["impressions"]) == 2
    assert len(previous.candidates) == 2
    assert {item["subject_refs"][0] for item in previous.qq_memory_state["impressions"].values()} == {"qq:member:200", "qq:member:201"}


def test_cross_group_target_is_rejected_without_mutation(sources):
    sources.add()
    previous = _commit(_preset(), sources.jobs()[0])
    sources.add(group="101", at=NOW + timedelta(days=1))
    before = previous.model_dump_json()
    with pytest.raises(ValueError):
        _commit(previous, sources.jobs()[0], stance="counter", target=_impression(previous)["impression_id"])
    assert previous.model_dump_json() == before


def test_foreign_preset_job_cannot_commit(sources):
    sources.add()
    job = sources.jobs()[0]
    with pytest.raises(ValueError, match="owner_mismatch"):
        _commit(_preset().model_copy(update={"preset_id": "other"}), job)


def test_unknown_source_or_uncovered_source_cannot_be_committed(sources):
    sources.add()
    job = sources.jobs()[0]
    proposal = _proposal(job).model_dump()
    proposal["episodes"][0]["source_ids"] = ["unknown:source"]
    with pytest.raises(ValueError):
        build_commit(_preset(), job, QQMemoryProposal.model_validate(proposal), Settings(), now=LATER)


def test_image_description_keeps_model_attribution_outside_original_speech(sources):
    image_ref = "qq-direct-image:sha256:" + "b" * 64
    sources.add(text="看看图片", image_analysis=[{"image_index": 1, "status": "described", "evidence_ref": image_ref,
                                               "description": "一只黑猫", "ocr_text": "你好",
                                               "uncertainties": ["无法判断是作者的宠物"]}])
    committed = _commit(_preset(), sources.jobs()[0], impression=False)
    assert "200: 看看图片" in committed.episodic[0].objective_fact
    assert "[图片模型解读，非作者原话] 一只黑猫" in committed.episodic[0].objective_fact
    assert "[图片OCR模型识别，非作者原话] 你好" in committed.episodic[0].objective_fact


def test_unknown_image_does_not_acquire_invented_objective_description(sources):
    sources.add(text="看看图片", image_analysis=[{"image_index": 1, "status": "unknown", "error": "vision_failed"}])
    committed = _commit(_preset(), sources.jobs()[0], impression=False)
    assert committed.episodic[0].objective_fact == "200: 看看图片"


def test_preset_roundtrip_preserves_impressions_evidence_and_receipts(sources):
    sources.add()
    committed = _commit(_preset(), sources.jobs()[0])
    reloaded = CharacterPreset.model_validate_json(committed.model_dump_json())
    assert reloaded.qq_memory_state == committed.qq_memory_state
    assert ImpressionMemory.model_validate(_impression(reloaded)).evidence


def test_hydrate_snapshot_preserves_projection_and_receipts(sources):
    sources.add()
    committed = _commit(_preset(), sources.jobs()[0])
    memory = build_inmemory_service(settings=Settings(tier_llm_enabled=False))
    try:
        hydrate_memory(memory, committed)
        saved = snapshot_memory(memory, preset_id="misty", session_id="sess_test", transcript=[])
        assert saved.qq_memory_state == committed.qq_memory_state
        assert len(saved.episodic) == len(committed.episodic)
    finally:
        memory.close()


def test_recall_is_scope_bounded_even_on_no_matches(sources):
    sources.add(group="100", text="本群秘密")
    committed = _commit(_preset(), sources.jobs()[0])
    memory = _memory(committed)
    assert select_recall(memory, group_id="101", query="本群秘密", now=NOW)["entries"] == []
    assert select_recall(memory, group_id="100", now=NOW)["entries"]


def test_recall_does_not_change_strength_or_receipts(sources):
    sources.add()
    committed = _commit(_preset(), sources.jobs()[0])
    memory = _memory(committed)
    before = deepcopy(memory.qq_memory_state)
    events = [entry.model_dump() for entry in memory.stores.episodic.list_all()]
    select_recall(memory, group_id="100", query="攻略", member_id="200", now=NOW)
    assert memory.qq_memory_state == before
    assert [entry.model_dump() for entry in memory.stores.episodic.list_all()] == events


def test_recall_respects_eight_items_and_four_thousand_character_budget(sources):
    for index in range(12):
        sources.add(member=str(200 + index), text=f"攻略内容{index}", at=NOW + timedelta(minutes=index))
    preset = _preset()
    for job in sources.jobs():
        preset = _commit(preset, job)
    result = select_recall(_memory(preset), group_id="100", query="攻略", now=NOW + timedelta(minutes=12),
                           max_characters=4000)
    assert 0 < len(result["entries"]) <= 8
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 4000


def test_recall_tiny_budget_never_exceeds_requested_limit(sources):
    sources.add()
    preset = _commit(_preset(), sources.jobs()[0])
    try:
        result = select_recall(_memory(preset), group_id="100", max_characters=2, now=NOW)
    except ValueError:
        return
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 2


def test_recall_omits_oversize_entries_instead_of_truncating_evidence(sources):
    sources.add()
    preset = _commit(_preset(), sources.jobs()[0], impression=False, summary="很长的事件描述" * 100)
    result = select_recall(_memory(preset), group_id="100", max_characters=250, now=NOW)
    assert result["entries"] == []
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 250


@pytest.mark.parametrize("mode", ["forgotten", "expired_short"])
def test_recall_cannot_show_an_impression_whose_only_evidence_is_inactive(sources, mode):
    sources.add()
    preset = _commit(_preset(), sources.jobs()[0])
    event = preset.episodic[0]
    changes = {"tier": "forgotten"} if mode == "forgotten" else {"expire_at": NOW - timedelta(seconds=1)}
    preset = preset.model_copy(update={"episodic": [event.model_copy(update=changes)]})
    assert select_recall(_memory(preset), group_id="100", now=NOW)["entries"] == []


def test_recall_surfaces_counterevidence_and_tentative_authority(sources):
    preset = _four_day_commit(sources)
    sources.add(at=NOW + timedelta(days=4), text="我今天不解释了")
    preset = _commit(preset, sources.jobs()[0], stance="counter", target=_impression(preset)["impression_id"])
    entries = select_recall(_memory(preset), group_id="100", member_id="200", now=NOW + timedelta(days=4))["entries"]
    impression = next(entry for entry in entries if entry["kind"] == "person")
    assert impression["counterevidence"]
    assert impression["authority"] == "misty_tentative_interpretation"


def test_unknown_memory_state_schema_fails_closed():
    with pytest.raises(ValueError, match="version_unknown"):
        memory_state({"schema_version": "unexpected-v99"})


def test_failed_preset_save_does_not_install_maintenance_or_claim_receipts(sources, tmp_path, monkeypatch):
    sources.add()
    preset = _commit(_preset(), sources.jobs()[0])
    memory = _memory(preset)
    before = deepcopy(memory.qq_memory_state)
    events = [entry.model_dump() for entry in memory.stores.episodic.list_all()]

    def fail_save(value):
        raise OSError("simulated atomic snapshot failure")

    async def exercise():
        host = SimpleNamespace(
            _control_lock=asyncio.Lock(),
            chat=SimpleNamespace(memory=memory, settings=Settings(), library=SimpleNamespace(save=fail_save)),
            adapter=SimpleNamespace(_lock=asyncio.Lock(), _ledger={}),
        )
        monkeypatch.setattr("harness.cognitive.qq_memory.snapshot_owner", lambda value: preset)
        worker = QQMemoryWorker(host, control_path=tmp_path / "control.json")
        with pytest.raises(OSError, match="snapshot failure"):
            await worker.maintain(now=LATER)

    asyncio.run(exercise())
    assert memory.qq_memory_state == before
    assert [entry.model_dump() for entry in memory.stores.episodic.list_all()] == events
