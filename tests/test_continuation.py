from __future__ import annotations

from gui.observer import RecordingObserver
from harness.config import Settings
from harness.llm.client import MockLLM, REVISED_IN_CHARACTER_REPLY
from harness.pipeline.continuation import (
    PROTOCOL_DIRECTIVE,
    build_nudge,
    fresh_bodies,
    hide_markers,
    is_echo,
    split_protocol_segments,
    strip_marker,
)
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
    assert "[CONTINUE]" not in body
    assert body == "我说 只是气话"
    assert marker == "end"


def test_strip_marker_hides_mid_continue_when_trailing_end() -> None:
    body, marker = strip_marker("我说 [CONTINUE] 只是气话 [END]")
    assert body == "我说 只是气话"
    assert marker == "end"


def test_strip_marker_tolerates_punctuation_and_fullwidth() -> None:
    body, marker = strip_marker("还有下文【CONTINUE】。")
    assert body == "还有下文"
    assert marker == "continue"


def test_hide_markers_strips_all_protocol_tokens() -> None:
    assert hide_markers("第一段 [CONTINUE]\n第二段 [END]") == "第一段\n第二段"
    assert "[CONTINUE]" not in hide_markers("嗯。[CONTINUE]")


def test_split_protocol_segments_one_shot_dump() -> None:
    parts, marker = split_protocol_segments("第一段 [CONTINUE]\n第二段 [END]")
    assert parts == ["第一段", "第二段"]
    assert marker == "end"


def test_split_protocol_segments_trailing_continue() -> None:
    parts, marker = split_protocol_segments("第一段 [CONTINUE]")
    assert parts == ["第一段"]
    assert marker == "continue"


def test_build_nudge_does_not_quote_last_segment() -> None:
    text = build_nudge(["第一段", "第二段"])
    assert "2 段" in text
    assert "第一段" not in text
    assert "第二段" not in text
    assert "[CONTINUE]" in text
    assert "[END]" in text


def test_is_echo_catches_word_order_restatement() -> None:
    first = (
        "哼，担心？本小姐与那丫头的关系，还轮不到旁人操心。\n"
        "不过…既然她愿意承认衬裙的讲究，本小姐倒也不介意与她再喝一次下午茶。\n\n"
        "怎么？你以为本小姐是那种为一句\"裙撑可有可无\"就记仇的人？"
        "太小看人了。该纠正的观点已经纠正，该喝的红茶自然还会继续喝。"
    )
    restated = (
        "担心？哼，本小姐与那丫头的关系，还轮不到旁人操心。\n\n"
        "不过…既然她愿意承认衬裙的讲究，本小姐倒也不介意与她再喝一次下午茶。\n\n"
        "怎么？你以为本小姐是那种为一句\"裙撑可有可无\"就记仇的人？"
        "太小看人了。该纠正的观点已经纠正，该喝的红茶自然还会继续喝。"
    )
    assert is_echo(restated, [first])
    assert is_echo(first, [first])
    assert not is_echo("又没说真的会不管。", ["……不想管。"])


def test_fresh_bodies_stops_at_first_echo_in_batch() -> None:
    accepted, echoed = fresh_bodies(["第一段", "第一段", "第三段"], [])
    assert accepted == ["第一段"]
    assert echoed is True
    accepted, echoed = fresh_bodies(["第一段", "第二段"], [])
    assert accepted == ["第一段", "第二段"]
    assert echoed is False


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
    observer = RecordingObserver()
    pipeline = _pipeline(service, llm, record_interaction=True)
    pipeline.observer = observer
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_cont")
    assert final.segments == ["第一段", "第二段"]
    assert final.text == "第一段\n\n第二段"
    assert "[CONTINUE]" not in final.text
    assert "[END]" not in final.text
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
    assert any(
        message.get("role") == "assistant" and message.get("content") == "第一段"
        for message in llm.chat_calls[1]
    )
    nudge = next(
        message.get("content", "")
        for message in llm.chat_calls[1]
        if "你已经说了" in message.get("content", "")
    )
    assert "第一段" not in nudge
    assert PROTOCOL_DIRECTIVE in "\n".join(message.get("content", "") for message in llm.chat_calls[0])
    assert final.usage.calls == 2
    assert final.usage.revision_calls == 0
    assert final.usage.total_tokens > 0
    segments_out = [event["data"]["text"] for event in observer.events if event["stage"] == "reply_segment"]
    assert segments_out == ["第一段", "第二段"]
    assert all("[CONTINUE]" not in text and "[END]" not in text for text in segments_out)


def test_missing_marker_stops_after_one_segment(service) -> None:
    llm = MockLLM(chat_script=["就这些。"])
    pipeline = _pipeline(service, llm)
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_one")
    assert final.segments == ["就这些。"]
    assert final.stopped_by == "marker"
    assert len(llm.chat_calls) == 1


def test_max_reply_segments_force_stop(service) -> None:
    llm = MockLLM(chat_script=["还没完 [CONTINUE]", "还是没完 [CONTINUE]", "仍然没完 [CONTINUE]"])
    pipeline = _pipeline(service, llm, max_reply_segments=2)
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_max")
    assert final.segments == ["还没完", "还是没完"]
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


def test_one_shot_dump_splits_without_extra_generation(service) -> None:
    llm = MockLLM(chat_script=["第一段 [CONTINUE]\n第二段 [END]", "不应再调用"])
    pipeline = _pipeline(service, llm)
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_dump")
    assert final.segments == ["第一段", "第二段"]
    assert final.text == "第一段\n\n第二段"
    assert final.stopped_by == "marker"
    assert "[CONTINUE]" not in final.text
    assert "[END]" not in final.text
    assert len(llm.chat_calls) == 1
    character_turns = [turn for turn in pipeline.session_store.recent("sess_dump") if turn.role == "character"]
    assert [turn.text for turn in character_turns] == ["第一段", "第二段"]


def test_continuation_disabled_hides_marker_and_stays_single_shot(service) -> None:
    llm = MockLLM(chat_script=["第一段 [CONTINUE]", "第二段 [END]"])
    pipeline = _pipeline(service, llm, continuation_enabled=False)
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_off")
    assert final.stopped_by == "disabled"
    assert final.segments == ["第一段"]
    assert final.text == "第一段"
    assert "[CONTINUE]" not in final.text
    assert len(llm.chat_calls) == 1
    blob = "\n".join(message.get("content", "") for message in llm.chat_calls[0])
    assert PROTOCOL_DIRECTIVE not in blob
    character_turns = [turn for turn in pipeline.session_store.recent("sess_off") if turn.role == "character"]
    assert [turn.text for turn in character_turns] == ["第一段"]


def test_continuation_echo_stops_without_second_segment(service) -> None:
    first = (
        "哼，担心？本小姐与那丫头的关系，还轮不到旁人操心。"
        "不过…既然她愿意承认衬裙的讲究，本小姐倒也不介意与她再喝一次下午茶。"
        "怎么？你以为本小姐是那种为一句\"裙撑可有可无\"就记仇的人？"
        "太小看人了。该纠正的观点已经纠正，该喝的红茶自然还会继续喝。"
    )
    restated = (
        "担心？哼，本小姐与那丫头的关系，还轮不到旁人操心。"
        "不过…既然她愿意承认衬裙的讲究，本小姐倒也不介意与她再喝一次下午茶。"
        "怎么？你以为本小姐是那种为一句\"裙撑可有可无\"就记仇的人？"
        "太小看人了。该纠正的观点已经纠正，该喝的红茶自然还会继续喝。"
    )
    llm = MockLLM(chat_script=[f"{first} [CONTINUE]", f"{restated} [END]"])
    observer = RecordingObserver()
    pipeline = _pipeline(service, llm, record_interaction=True)
    pipeline.observer = observer
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_echo")
    assert final.segments == [first]
    assert final.stopped_by == "duplicate"
    assert final.usage.calls == 2
    character_turns = [turn for turn in pipeline.session_store.recent("sess_echo") if turn.role == "character"]
    assert [turn.text for turn in character_turns] == [first]
    replies = [
        event
        for event in service.stores.episodic.list_all()
        if event.session_id == "sess_echo" and event.participants[:1] == ["character"]
    ]
    assert len(replies) == 1
    assert replies[0].objective_fact == first
    segments_out = [event["data"]["text"] for event in observer.events if event["stage"] == "reply_segment"]
    assert segments_out == [first]


def test_one_shot_echo_keeps_first_body_only(service) -> None:
    llm = MockLLM(chat_script=["第一段 [CONTINUE]\n第一段 [END]", "不应再调用"])
    pipeline = _pipeline(service, llm)
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_dump_echo")
    assert final.segments == ["第一段"]
    assert final.stopped_by == "duplicate"
    assert len(llm.chat_calls) == 1


def test_continuation_disabled_drops_duplicate_in_same_dump(service) -> None:
    llm = MockLLM(chat_script=["第一段 [CONTINUE]\n第一段 [END]", "不应再调用"])
    pipeline = _pipeline(service, llm, continuation_enabled=False)
    final = pipeline.respond("请接着说。", now=NOW, session_id="sess_off_echo")
    assert final.stopped_by == "disabled"
    assert final.segments == ["第一段"]
    assert len(llm.chat_calls) == 1
