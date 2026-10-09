"""Scoped evidence survives final compilation and reaches the existing critic."""
from __future__ import annotations

import json

import pytest

from harness.cognitive.shared_memory import CognitiveSharedMemory, SharedMemoryCompiler, _bind_shared_evidence
from harness.compiler.compiler import ContextCompiler, MEMORY_EMPTY_CONSTRAINT, MEMORY_EVIDENCE_CONSTRAINT
from harness.config import Settings
from harness.core.inference import (
    CandidateResponse, ContextSegment, MemoryEvidenceContext, MemoryEvidenceEntry,
    ModelContext, SituationActor, SituationState, SituationTrigger,
)
from harness.memory.shared import SharedLocalEvent, SharedMemoryStore, SharedReadScope
from harness.core.models import InteractionEvent
from harness.pipeline.pipeline import InferencePipeline
from harness.validator.validator import ResponseValidator, rule_check
from tests.factories import NOW


AUDIENCE = "qq:group:10001"


def _source(*, text="我喜欢红茶。", status="user_claim", kind="history", source_id="old-1",
            audience=AUDIENCE, refs=None, speaker_id=None, current_speaker_id=None, observed_at=None):
    entry = MemoryEvidenceEntry(
        source_id=source_id, text=text, audience=audience, source_channel="qq_group",
        source_conversation_id="10001", source_refs=refs or ["verified-original-1"],
        status=status, kind=kind,
        speaker_id=speaker_id, observed_at=observed_at,
    )
    segment = ContextSegment(kind="user", why="verified_qq_context",
        text=json.dumps({"entries": [{"record_id": source_id, "text": text}]}, ensure_ascii=False),
        source_ids=[source_id])
    return segment, MemoryEvidenceContext.bind(audience=AUDIENCE, entries=[entry], segment=segment,
                                               current_speaker_id=current_speaker_id)


def _compile(segment, evidence, *, required=True):
    situation = SituationState(trigger=SituationTrigger(ref="current-1"), utterance="说说这个。",
                               actor=SituationActor())
    return ContextCompiler(Settings(context_token_budget=3000)).compile(
        situation, additional_segments=[segment], memory_evidence=evidence,
        required_additional_context=required,
    )


def test_valid_history_and_json_escaped_newlines_reach_final_context():
    segment, evidence = _source(text='我喜欢红茶。\n还有"乌龙茶"。')
    context = _compile(segment, evidence)
    assert context.memory_evidence.entries == evidence.entries
    assert context.memory_evidence.has_history
    assert "memory_empty_guard" not in {item.why for item in context.segments}
    assert MEMORY_EVIDENCE_CONSTRAINT[:30] in context.render_messages()[0]["content"]
    assert evidence.entries[0].segment_sha256 not in str(context.render_messages())
    assert ModelContext.model_validate_json(context.model_dump_json()).memory_evidence == context.memory_evidence


@pytest.mark.parametrize("damage", ["cut", "changed", "source_id", "audience", "private", "trigger_ref"])
def test_missing_or_wrong_scope_or_current_records_never_authorize_history(damage):
    segment, evidence = _source()
    required = True
    if damage == "cut":
        segment, evidence = _source(text="这是一段很长的历史。" * 150)
        required = False
    elif damage == "changed":
        segment = segment.model_copy(update={"text": segment.text + " "})
    elif damage == "source_id":
        segment = segment.model_copy(update={"source_ids": ["arbitrary-id"]})
    else:
        changes = {"audience": "qq:group:other"} if damage == "audience" else (
            {"audience": "qq:private:20001:20002", "source_channel": "qq_private"} if damage == "private" else
            {"source_refs": ["current-1"]})
        evidence = evidence.model_copy(update={"entries": [evidence.entries[0].model_copy(update=changes)]})
    context = _compile(segment, evidence, required=required)
    assert context.memory_evidence.entries == []
    assert not context.memory_evidence.has_history
    result = rule_check("你上次说喜欢红茶。", persona=None, insights=[], memory_empty=False,
                        memory_evidence=context.memory_evidence)
    assert "fabricated_memory" in result.violations


def test_current_trigger_reaches_critic_without_authorizing_history():
    segment, evidence = _source(text="吓哭了", kind="current", source_id="current-1",
                                speaker_id="alice", current_speaker_id="alice")
    context = _compile(segment, evidence)
    assert context.memory_evidence.entries == evidence.entries
    assert not context.memory_evidence.has_history
    result = rule_check("你上次说喜欢红茶。", persona=None, insights=[],
                        memory_evidence=context.memory_evidence)
    assert "fabricated_memory" in result.violations
    critic = _Critic()
    check = ResponseValidator(settings=Settings(validator_sample_rate=1), llm=critic)
    check.check(CandidateResponse(text="你说吓哭了"), persona=None,
                memory_evidence=context.memory_evidence, run_llm=True)
    projected = critic.calls[0][1]["memory_evidence"]
    assert projected["rows"][0][0] == projected["current_speaker_id"]
    assert projected["rows"][0][-1] == "吓哭了"
    assert projected["metadata"][0][-2:] == ["user_claim", "current"]


@pytest.mark.parametrize("damage", ["cut", "changed", "source_id", "audience", "trigger_segment", "status"])
def test_current_evidence_requires_complete_scoped_source_admission(damage):
    segment, evidence = _source(text="吓哭了", kind="current", source_id="current-1")
    segments = [segment]
    if damage == "cut":
        segments = []
    elif damage == "changed":
        segments = [segment.model_copy(update={"text": "吓哭"})]
    elif damage == "source_id":
        segments = [segment.model_copy(update={"source_ids": []})]
    elif damage == "trigger_segment":
        segments = [segment.model_copy(update={"why": "trigger_utterance"})]
    else:
        change = {"audience": "qq:group:other"} if damage == "audience" else {"status": "retained_fact"}
        evidence = evidence.model_copy(update={"entries": [evidence.entries[0].model_copy(update=change)]})
    assert evidence.admitted(segments, trigger_ref="current-1").entries == []


def test_source_ids_and_json_claims_do_not_create_metadata():
    segment, _ = _source()
    context = _compile(segment, MemoryEvidenceContext(audience=AUDIENCE))
    assert not context.memory_evidence.has_history
    situation = SituationState(trigger=SituationTrigger(ref="current-1"), utterance="hi", actor=SituationActor())
    boolean_context = ContextCompiler(Settings()).compile(situation, has_additional_memory=True)
    assert any(item.why == "memory_empty_guard" for item in boolean_context.segments)


class _Critic:
    def __init__(self, verdict="pass"):
        self.calls = []
        self.verdict = verdict

    def complete_json(self, *, system, user):
        self.calls.append((system, json.loads(user)))
        return {"verdict": self.verdict, "violations": ["fabricated_memory"] if self.verdict == "revise" else [],
                "severity": .8 if self.verdict == "revise" else 0,
                "instruction": "只依据已经提供的红茶偏好，删除去火星的往事。" if self.verdict == "revise" else None}


class _Character:
    def __init__(self, text):
        self.text = text
        self.contexts = []

    def generate(self, context, **kwargs):
        self.contexts.append(context)
        return CandidateResponse(text=self.text)


@pytest.mark.parametrize("status", ["user_claim", "verified_delivery", "retained_fact", "provisional", "disputed"])
def test_existing_pipeline_critic_receives_exact_evidence_and_status(service, status):
    segment, evidence = _source(status=status)
    critic = _Critic()
    character = _Character("你上次说喜欢红茶，那就先看红茶。")
    settings = Settings(validator_sample_rate=1, max_revisions=0, continuation_enabled=False,
                        record_interaction=False, context_token_budget=3000)
    pipeline = InferencePipeline(service, settings=settings, character=character,
        validator=ResponseValidator(settings=settings, llm=critic))
    final = pipeline.prepare("你好。", now=NOW, session_id="evidence-test",
                             context_segments=[segment], memory_evidence=evidence)
    assert final.validation.verdict == "pass"
    assert len(character.contexts) == len(critic.calls) == 1
    actual = character.contexts[0].memory_evidence
    assert actual.entries == evidence.entries
    _, payload = critic.calls[0]
    projected = payload["memory_evidence"]
    admitted = dict(zip(projected["columns"], projected["rows"][0]))
    metadata = dict(zip(projected["metadata_columns"], projected["metadata"][admitted["metadata_id"]]))
    assert admitted["text"] == evidence.entries[0].text
    assert metadata["status"] == status
    scope = projected["scopes"][metadata["scope_id"]]
    assert scope["source_channel"] == "qq_group"
    assert scope["source_conversation_id"] is not None
    assert "segment_sha256" not in json.dumps(projected)


def test_one_real_memory_does_not_disable_semantic_check_of_unrelated_claim(service):
    segment, evidence = _source(text="我喜欢红茶。")
    critic = _Critic("revise")
    character = _Character("你上次说我们一起去过火星。")
    settings = Settings(validator_sample_rate=1, max_revisions=0, continuation_enabled=False,
                        record_interaction=False, context_token_budget=3000)
    pipeline = InferencePipeline(service, settings=settings, character=character,
        validator=ResponseValidator(settings=settings, llm=critic))
    final = pipeline.prepare("你好。", now=NOW, session_id="unsupported-memory",
                             context_segments=[segment], memory_evidence=evidence)
    assert final.validation.verdict == "revise"
    assert "fabricated_memory" in final.validation.violations
    assert len(character.contexts) == len(critic.calls) == 1
    system, payload = critic.calls[0]
    assert "Evidence for event A cannot support" in system
    assert "do not demand verbatim" in system
    assert "火星" not in str(payload["memory_evidence"])
    assert payload["text"] == character.text


def test_existing_sampling_budget_is_unchanged(service):
    segment, evidence = _source()
    critic = _Critic()
    settings = Settings(validator_sample_rate=0, continuation_enabled=False, record_interaction=False)
    pipeline = InferencePipeline(service, settings=settings, character=_Character("红茶也挺好。"),
        validator=ResponseValidator(settings=settings, llm=critic))
    result = pipeline.prepare("你好。", now=NOW, session_id="no-extra-critic",
                              context_segments=[segment], memory_evidence=evidence)
    assert result.validation.skipped
    assert critic.calls == []


def test_shared_facade_preserves_original_scope_and_authority(tmp_path):
    store = SharedMemoryStore(tmp_path / "shared.sqlite3")
    store.record_local(SharedLocalEvent(event_id="operator-fact-1", session_id="operator-confirmed-facts",
        author_principal="local:main", role="user", text="我喜欢红茶。", occurred_at=NOW,
        evidence_refs=("local:operator-fact-1",)))
    selected = store.query(SharedReadScope(audience="local:main"), limit=4)
    payload = selected.model_payload()
    segment = _bind_shared_evidence(ContextSegment(kind="user", why="verified_shared_channel_memory",
        text=json.dumps({"operator_confirmed_facts": payload}, ensure_ascii=False),
        source_ids=[item["record_id"] for item in payload["entries"]]), selected)
    situation = SituationState(trigger=SituationTrigger(ref="current-1"), utterance="你好。", actor=SituationActor())
    result = SharedMemoryCompiler(Settings()).compile(situation, additional_segments=[segment])
    assert result.memory_evidence.has_history
    entry = result.memory_evidence.entries[0]
    assert entry.status == "user_claim"
    assert entry.source_channel == "local_chat"
    assert entry.source_conversation_id == "operator-confirmed-facts"
    assert entry.speaker_id == "local:main"
    assert entry.observed_at == NOW
    assert "operator-fact-1" in entry.source_refs
    # Merely reconstructing a source-looking segment cannot grant authority.
    forged = segment.model_copy(update={"memory_evidence": None})
    result = SharedMemoryCompiler(Settings()).compile(situation, additional_segments=[forged])
    assert not result.memory_evidence.has_history


def test_legacy_context_and_retrieval_boolean_remain_readable():
    model = ModelContext.model_validate({"segments": [], "budget": {"total": 3000}})
    assert model.memory_evidence is None
    result = rule_check("你上次说喜欢红茶。", persona=None, insights=[], memory_empty=False)
    assert "fabricated_memory" not in result.violations
    assert "不记得" not in MEMORY_EMPTY_CONSTRAINT
    result = rule_check("你上次说喜欢红茶。", persona=None, insights=[], memory_empty=True)
    assert "不记得" not in result.instruction


@pytest.mark.parametrize("text", [
    "你之前说的是哪段代码？贴一下我再看。",
    "你上次说的是什么来着？",
    "我们上次聊的是哪件事情？",
    "你昨天说的是哪条消息，贴一下。",
])
def test_empty_history_allows_specific_reference_clarification(text):
    result = rule_check(text, persona=None, insights=[], memory_empty=True,
                        memory_evidence=MemoryEvidenceContext(audience=AUDIENCE))
    assert "fabricated_memory" not in result.violations


@pytest.mark.parametrize("text", [
    "你之前说喜欢红茶。",
    "你之前说的是哪段代码？你上次说我们去过火星。",
    "你上次说我们去过火星，你之前说的是哪段代码？",
    "你之前说的是哪段代码；你答应过带我去火星。",
    "你上次说的哪段代码让火星爆炸了？",
])
def test_clarification_does_not_exempt_another_historical_assertion(text):
    result = rule_check(text, persona=None, insights=[], memory_empty=True,
                        memory_evidence=MemoryEvidenceContext(audience=AUDIENCE))
    assert "fabricated_memory" in result.violations


@pytest.mark.parametrize("author,current,expected", [
    ("20001", "20001", "pass"),
    ("20001", "20002", "revise"),
    (None, "20002", "revise"),
    ("20001", None, "revise"),
])
def test_author_identity_and_current_interlocutor_reach_existing_critic(service, author, current, expected):
    segment, evidence = _source(speaker_id=author, current_speaker_id=current, observed_at=NOW)

    class AuthorCritic:
        calls = []

        def complete_json(self, *, system, user):
            request = json.loads(user)
            self.calls.append(request)
            assert "Another group member's statement cannot be reassigned" in system
            assert "unknown fields provide no author identity evidence" in system
            assert "only the source observation/delivery time" in system
            projected = request["memory_evidence"]
            current_alias = projected["current_speaker_id"]
            assert (current_alias is None) == (current is None)
            actual = dict(zip(projected["columns"], projected["rows"][0]))
            assert (actual["speaker_id"] is None) == (author is None)
            metadata = dict(zip(projected["metadata_columns"], projected["metadata"][actual["metadata_id"]]))
            observed = metadata["observed_date"] + "T" + actual["observed_time"]
            assert observed == evidence.model_dump(mode="json")["entries"][0]["observed_at"]
            # Scripted critic checks the interface has enough information to
            # distinguish two otherwise identical first-person source claims.
            supported = actual["speaker_id"] is not None and actual["speaker_id"] == current_alias
            return {"verdict": "pass" if supported else "revise",
                    "violations": [] if supported else ["fabricated_memory"],
                    "severity": 0 if supported else .8,
                    "instruction": None if supported else "不要把其他成员的陈述说成当前对象说过。"}

    critic = AuthorCritic()
    settings = Settings(validator_sample_rate=1, max_revisions=0, continuation_enabled=False,
                        record_interaction=False, context_token_budget=3000)
    character = _Character("你上次说喜欢红茶。")
    pipeline = InferencePipeline(service, settings=settings, character=character,
        validator=ResponseValidator(settings=settings, llm=critic))
    result = pipeline.prepare("你好。", now=NOW, session_id="memory-author",
                              context_segments=[segment], memory_evidence=evidence)
    assert result.validation.verdict == expected
    assert len(critic.calls) == len(character.contexts) == 1
    assert character.contexts[0].memory_evidence.current_speaker_id == current


def test_reference_clarification_uses_existing_critic_without_rule_revision():
    critic = _Critic()
    result = ResponseValidator(llm=critic).check(
        CandidateResponse(text="你之前说的是哪段代码？贴一下我再看。"), persona=None,
        memory_evidence=MemoryEvidenceContext(audience=AUDIENCE, current_speaker_id="20002"),
    )
    assert result.verdict == "pass"
    assert len(critic.calls) == 1
    assert "pure clarification" in critic.calls[0][0]


@pytest.mark.parametrize("channel,expected", [("chat", "local:main"), ("internal", None)])
def test_shared_current_speaker_requires_real_local_chat(tmp_path, channel, expected):
    shared = CognitiveSharedMemory(object(), tmp_path / "shared.sqlite3")
    shared.store.record_local(SharedLocalEvent(event_id="operator-fact-1", session_id="operator-confirmed-facts",
        author_principal="local:main", role="user", text="我喜欢红茶。", occurred_at=NOW,
        evidence_refs=("local:operator-fact-1",)))
    event = InteractionEvent(raw_text="红茶", timestamp=NOW, channel=channel, speaker="user", session_id="local-main")
    segments = shared.local_context(event)
    assert segments
    situation = SituationState(trigger=SituationTrigger(ref=event.event_id), utterance=event.raw_text,
                               actor=SituationActor())
    result = SharedMemoryCompiler(Settings()).compile(situation, additional_segments=segments)
    assert result.memory_evidence.current_speaker_id == expected
    assert result.memory_evidence.entries[0].speaker_id == "local:main"
