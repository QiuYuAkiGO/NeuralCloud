from __future__ import annotations

from harness.config import Settings
from harness.llm.client import MockLLM, REVISED_IN_CHARACTER_REPLY
from harness.pipeline.continuation import PROTOCOL_DIRECTIVE, build_nudge, strip_marker
from harness.pipeline.pipeline import InferencePipeline
from tests.factories import NOW, seed_running_example


def test_strip_marker_trailing_continue() -> None:
    body, marker = strip_marker("还有下文 [CONTINUE]")
    assert body == "还有下文"
    assert marker == "continue"


def test_strip_marker_trailing_end_with_whitespace() -> None:
    body, marker = strip_marker("说完了。[END]\n")
    assert body == "说完了。"
    assert marker == "end"


def test_strip_marker_missing_defaults_to_end() -> None:
    body, marker = strip_marker("嗯。")
    assert body == "嗯。"
    assert marker == "end"


def test_strip_marker_mid_text_is_not_a_signal() -> None:
    text = "我说 [CONTINUE] 只是气话"
    body, marker = strip_marker(text)
    assert body == text
    assert marker == "end"


def test_strip_marker_keeps_mid_continue_when_trailing_end() -> None:
    body, marker = strip_marker("我说 [CONTINUE] 只是气话 [END]")
    assert body == "我说 [CONTINUE] 只是气话"
    assert marker == "end"


def test_build_nudge_recaps_last_segment() -> None:
    text = build_nudge(["第一段", "第二段"])
    assert "2 段" in text
    assert "第二段" in text
    assert "[CONTINUE]" in text
    assert "[END]" in text


def _pipeline(service, llm: MockLLM, **kwargs) -> InferencePipeline:
    options = {
        "continuation_enabled": True,
        "max_reply_segments": 3,
        "validator_sample_rate": 0.0,
        "record_interaction": False,
        "complexity_threshold": 1.0,
    }
    options.update(kwargs)
    return InferencePipeline(service, llm=llm, settings=Settings(**options))


def test_two_segment_continuation_records_one_memory(service) -> None:
    llm = MockLLM(chat_script=["第一段 [CONTINUE]", "第二段 [END]"])
    pipeline = _pipeline(service, llm, record_interaction=True)
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_cont")
    assert final.segments == ["第一段", "第二段"]
    assert final.text == "第一段\n\n第二段"
    assert final.stopped_by == "marker"
    character_turns = [turn for turn in pipeline.session_store.recent("sess_cont") if turn.role == "character"]
    assert [turn.text for turn in character_turns] == ["第一段", "第二段"]
    replies = [
        event
        for event in service.stores.episodic.list_all()
        if event.session_id == "sess_cont" and event.participants[:1] == ["character"]
    ]
    assert len(replies) == 1
    assert replies[0].objective_fact == "第一段\n\n第二段"
    assert any("你已经说了" in message.get("content", "") for message in llm.chat_calls[1])
    assert PROTOCOL_DIRECTIVE in "\n".join(message.get("content", "") for message in llm.chat_calls[0])


def test_missing_marker_stops_after_one_segment(service) -> None:
    llm = MockLLM(chat_script=["就这些。"])
    pipeline = _pipeline(service, llm)
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_one")
    assert final.segments == ["就这些。"]
    assert final.stopped_by == "marker"
    assert len(llm.chat_calls) == 1


def test_max_reply_segments_force_stop(service) -> None:
    llm = MockLLM(chat_script=["还没完 [CONTINUE]"])
    pipeline = _pipeline(service, llm, max_reply_segments=2)
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_max")
    assert final.segments == ["还没完", "还没完"]
    assert final.stopped_by == "max_segments"
    assert len(llm.chat_calls) == 2


def test_empty_segment_stops_without_session_or_memory(service) -> None:
    llm = MockLLM(chat_script=["[CONTINUE]"])
    pipeline = _pipeline(service, llm, record_interaction=True)
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_empty")
    assert final.segments == []
    assert final.text == ""
    assert final.stopped_by == "empty"
    turns = pipeline.session_store.recent("sess_empty")
    assert [turn.role for turn in turns] == ["user"]
    replies = [
        event
        for event in service.stores.episodic.list_all()
        if event.session_id == "sess_empty" and event.participants[:1] == ["character"]
    ]
    assert replies == []


def test_each_segment_still_goes_through_validator(service) -> None:
    seed_running_example(service)
    llm = MockLLM(chat_script=["因为我还是担心你啊。对不起。[CONTINUE]"])
    pipeline = InferencePipeline(
        service,
        llm=llm,
        settings=Settings(
            continuation_enabled=True,
            max_reply_segments=3,
            validator_sample_rate=1.0,
            max_revisions=2,
            record_interaction=False,
            complexity_threshold=0.5,
        ),
    )
    final = pipeline.respond("你昨天不是说不会再管我了吗？", now=NOW, session_id="sess_0881")
    assert final.revisions >= 1
    assert "对不起" not in final.text
    assert "担心你" not in final.text
    assert final.segments == [REVISED_IN_CHARACTER_REPLY]
    assert final.stopped_by == "marker"


def test_two_clean_segments_each_validated(service) -> None:
    seed_running_example(service)
    llm = MockLLM(chat_script=["……不想管。[CONTINUE]", "又没说真的会不管。[END]"])
    pipeline = InferencePipeline(
        service,
        llm=llm,
        settings=Settings(
            continuation_enabled=True,
            max_reply_segments=3,
            validator_sample_rate=1.0,
            max_revisions=2,
            record_interaction=False,
            complexity_threshold=0.5,
        ),
    )
    final = pipeline.respond("你昨天不是说不会再管我了吗？", now=NOW, session_id="sess_0881")
    assert final.segments == ["……不想管。", "又没说真的会不管。"]
    assert final.stopped_by == "marker"
    assert final.revisions == 0


def test_continuation_disabled_keeps_marker_and_single_shot(service) -> None:
    llm = MockLLM(chat_script=["第一段 [CONTINUE]", "第二段 [END]"])
    pipeline = _pipeline(service, llm, continuation_enabled=False)
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_off")
    assert final.stopped_by == "disabled"
    assert final.segments == ["第一段 [CONTINUE]"]
    assert final.text == "第一段 [CONTINUE]"
    assert len(llm.chat_calls) == 1
    blob = "\n".join(message.get("content", "") for message in llm.chat_calls[0])
    assert PROTOCOL_DIRECTIVE not in blob
    character_turns = [turn for turn in pipeline.session_store.recent("sess_off") if turn.role == "character"]
    assert len(character_turns) == 1
