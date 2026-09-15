"""Basic chat continuity at the production Cognitive host boundary; no live models.

Scripted responses prove the context/delivery/persistence contract, not model quality.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from gui.presets import load_builtin
from harness.cognitive.service import CognitiveServiceHost
from harness.compiler.compiler import ContextCompiler
from harness.config import Settings
from harness.contracts.decisions import DeliveryEvidence, ReplyDecision
from harness.contracts.envelope import DomainCommandEnvelope
from harness.core.inference import CandidateResponse, SessionTurn, SituationActor, SituationState, SituationTrigger
from harness.core.models import CharacterSnapshot
from harness.llm.character import CharacterModel
from harness.llm.client import MockLLM
from harness.validator.validator import ResponseValidator


def _situation() -> SituationState:
    return SituationState(
        trigger=SituationTrigger(ref="evt_followup"),
        utterance="那就用刚才的名字吧。",
        actor=SituationActor(),
        snapshot=CharacterSnapshot(persona=load_builtin("misty").persona),
    )


def _assert_misty_context(messages: list[dict[str, str]]) -> None:
    system = "\n".join(item["content"] for item in messages if item["role"] == "system")
    identity = load_builtin("misty").persona.identity
    assert f"人格锚点：{identity.name}" in system
    assert "Vaelith" not in system
    assert identity.profile is not None
    for value in [
        *identity.core_traits,
        *identity.speech_style,
        *identity.knowledge_boundary,
        *identity.profile.aliases,
        *identity.profile.hobbies,
        *identity.profile.appearance,
        *identity.profile.personal_items,
        identity.profile.signature_line,
        "年龄=19岁",
        "生日=5月20日",
        "身高=157cm",
    ]:
        assert value in system


def test_misty_compiler_keeps_complete_identity_and_newest_exchange() -> None:
    turns = [
        SessionTurn(role="user", speaker="阿宁", text="旧话题" * 500, event_id="old"),
        SessionTurn(role="user", speaker="阿宁", text="照片可以叫什么？", event_id="recent-user"),
        SessionTurn(role="character", speaker="小雾", text="叫《雨后微光》，好吗？", event_id="recent-reply"),
    ]
    context = ContextCompiler(Settings(context_token_budget=3000)).compile(_situation(), session=turns)
    _assert_misty_context(context.render_messages())
    session = next(item for item in context.segments if item.kind == "session")
    assert "阿宁: 照片可以叫什么？" in session.text
    assert "小雾: 叫《雨后微光》，好吗？" in session.text
    assert "旧话题" not in session.text
    assert session.source_ids == ["recent-user", "recent-reply"]
    assert context.budget.used <= context.budget.total


def test_single_long_recent_turn_keeps_its_ending_and_labels_omission() -> None:
    context = ContextCompiler(Settings()).compile(
        _situation(),
        session=[SessionTurn(role="character", speaker="小雾", text="讨论摄影。" * 400 + "最后选定《雨后微光》。", event_id="long")],
    )
    session = next(item for item in context.segments if item.kind == "session")
    assert session.text.startswith("近期对话：\n小雾: …")
    assert session.text.endswith("最后选定《雨后微光》。")
    assert session.source_ids == ["long"]
    assert session.token_estimate <= 400


@pytest.mark.parametrize("budget", [0, 1, 8, 40, 400])
def test_recent_window_never_exceeds_its_budget(budget: int) -> None:
    from harness.compiler.compiler import _fit_session, _session_text, estimate_tokens

    turns = [SessionTurn(role="character", speaker="小雾", text="较长的回答" * 200)]
    fitted = _fit_session(turns, budget)
    assert estimate_tokens(_session_text(fitted)) <= budget


def _command(kind: str, number: int, payload: dict) -> DomainCommandEnvelope[dict]:
    now = datetime.now(UTC)
    return DomainCommandEnvelope[dict](
        command_type=kind,
        event_id=f"evt_chat_{number}",
        receive_seq=number,
        correlation_id=f"corr_chat_{number}",
        runtime_instance_id="runtime_basic_chat",
        manifest_revision=1,
        idempotency_key=f"{kind}:{number}",
        issued_at=now,
        deadline=now + timedelta(seconds=60),
        payload=payload,
    )


def _host(tmp_path, monkeypatch, responses: list[str]) -> tuple[CognitiveServiceHost, MockLLM]:
    # Real OS ownership semantics within this test's isolated lock directory.
    monkeypatch.setattr("harness.runtime.ownership._lease_root", lambda: tmp_path / "test-domain-leases")
    monkeypatch.setenv("NC_MODE", "demo")
    monkeypatch.delenv("NC_COGNITIVE_MANIFEST_PATH", raising=False)
    monkeypatch.delenv("NC_RUNTIME_MANIFEST_PATH", raising=False)
    monkeypatch.delenv("NC_QQ_SESSION_CONFIG", raising=False)
    monkeypatch.setenv("NC_SHARED_MEMORY_DB", str(tmp_path / "shared-memory.sqlite3"))
    monkeypatch.setenv("NC_CALL_LOG_DIR", str(tmp_path / "calls"))
    host = CognitiveServiceHost(
        config_path=tmp_path / "gui.json",
        presets_dir=tmp_path / "presets",
        state_path=tmp_path / "cognitive.json",
    )
    settings = host.chat.settings
    settings.continuation_enabled = False
    settings.call_log_enabled = False
    settings.parser_llm_enabled = False
    settings.planner_llm_enabled = False
    settings.tier_llm_enabled = False
    settings.validator_sample_rate = 1
    main = MockLLM(chat_script=responses)
    host.chat.pipeline.character = CharacterModel(main, settings=settings)
    return host, main


async def _prepare(host, main, number, text):
    session_id = host.chat.active_session_id
    before_turns = host.chat.pipeline.session_store.snapshot()
    before_events = list(host.chat.memory.stores.episodic.list_all())
    command = _command("PrepareResponse", number, {"text": text, "speaker": "阿宁", "session_id": session_id})
    result = await host.prepare_response(command)
    assert result.status == "applied", result.error
    assert result.result is not None and isinstance(result.result.decision, ReplyDecision)
    assert host.chat.pipeline.session_store.snapshot() == before_turns
    assert host.chat.memory.stores.episodic.list_all() == before_events
    _assert_misty_context(main.chat_calls[-1])
    count = len(main.chat_calls)
    replay = await host.prepare_response(command)
    assert replay.status == "idempotent_replay"
    assert replay.result == result.result
    assert len(main.chat_calls) == count
    return result.result


async def _commit(host, number, prepared, *, delivered_segments=None):
    segments = delivered_segments if delivered_segments is not None else prepared.decision.segments
    evidence = DeliveryEvidence(
        prepare_id=prepared.prepare_id,
        delivered_at=datetime.now(UTC),
        channel="chat",
        success=True,
        delivered_segments=segments,
        external_ref=f"test-delivery:{number}",
    )
    command = _command("CommitInteraction", number, {
        "prepare_id": prepared.prepare_id,
        "evidence": evidence.model_dump(mode="json"),
    })
    result = await host.commit_interaction(command)
    assert result.status == "applied", result.error
    turns = host.chat.pipeline.session_store.recent(prepared.session_id)
    replay = await host.commit_interaction(command)
    assert replay.status == "idempotent_replay"
    assert host.chat.pipeline.session_store.recent(prepared.session_id) == turns
    assert host.chat.memory.get_persona() == load_builtin("misty").persona


def test_production_misty_multiturn_delivery_and_restart(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        host, main = _host(tmp_path, monkeypatch, [
            "我叫小雾，19岁。生日是5月20日。平时喜欢听歌和摄影。",
            "可以试试《雨后微光》。",
        ])
        try:
            first = await _prepare(host, main, 1, "介绍一下你自己吧。")
            await _commit(host, 1, first)
            second = await _prepare(host, main, 2, "我今天拍了雨后的窗边，想请你帮照片取个名字。")
            assert "阿宁: 介绍一下你自己吧。" in main.chat_calls[-1][0]["content"]
            assert "小雾: 我叫小雾，19岁。" in main.chat_calls[-1][0]["content"]
            # Persist a prepared response, then restart before delivery/commit.
            assert "雨后微光" not in "\n".join(t.text for t in host.chat.transcript)
        finally:
            await host.close()

        host, main = _host(tmp_path, monkeypatch, ["嗯，照片就叫《雨后微光》。", "我还是19岁，生日是5月20日。"])
        try:
            assert host.chat.memory.get_persona() == load_builtin("misty").persona
            assert len(host.chat.pipeline.session_store.recent(host.chat.active_session_id)) == 2
            await _commit(host, 2, second)
            third = await _prepare(host, main, 3, "那就用刚才的名字吧。")
            assert "可以试试《雨后微光》。" in main.chat_calls[-1][0]["content"]
            await _commit(host, 3, third)
            fourth = await _prepare(host, main, 4, "对了，你几岁，生日是哪一天？")
            await _commit(host, 4, fourth)
            committed = host.chat.pipeline.session_store.recent(host.chat.active_session_id)
        finally:
            await host.close()

        host, _ = _host(tmp_path, monkeypatch, ["嗯。"])
        try:
            assert host.chat.memory.get_persona() == load_builtin("misty").persona
            assert host.chat.pipeline.session_store.recent(host.chat.active_session_id) == committed
            assert len(committed) == 8
            assert {turn.speaker for turn in committed} == {"阿宁", "小雾"}
            assert host.chat.memory.stores.episodic.get("evt_chat_2") is not None
        finally:
            await host.close()

    asyncio.run(scenario())


def test_validator_receives_full_misty_identity_and_allows_gentle_expression() -> None:
    class Judge:
        payload: dict | None = None

        def complete_json(self, *, system, user):
            self.payload = json.loads(user)
            return {"verdict": "pass", "violations": [], "severity": 0}

    persona = load_builtin("misty").persona
    judge = Judge()
    validator = ResponseValidator(llm=judge)
    result = validator.check(CandidateResponse(text="我有点担心你。想说的话，我会认真听。"), persona=persona)
    assert result.verdict == "pass"
    assert judge.payload["persona"] == persona.identity.model_dump(mode="json")
