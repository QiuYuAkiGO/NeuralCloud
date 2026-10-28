"""The comparison must change policy/context, never the observed image or labels."""
import json

from tools.production_acceptance.image_pragmatics_probe import (
    APPLAUSE, CASES, OBSERVATION, OUTPUT_RULE, messages, systems,
)


def test_pairing_keeps_observation_and_user_context_identical():
    prompts = systems({"identity": {"name": "Misty"}})
    for identifier, context, criterion in CASES:
        observation = APPLAUSE if identifier.startswith("applause_") else OBSERVATION
        before = messages(prompts["baseline"], context, observation)
        after = messages(prompts["candidate"], context, observation)
        assert before[1:] == after[1:]
        assert criterion not in json.dumps(after, ensure_ascii=False)
        assert json.loads(after[-1]["content"])["images"] == [observation]
        assert before[0]["content"].endswith(OUTPUT_RULE)
        assert after[0]["content"].endswith(OUTPUT_RULE)


def test_previous_misty_question_is_an_assistant_turn_not_user_speech():
    context = next(context for identifier, context, _ in CASES if identifier == "overtime")
    transcript = messages("system", context)
    assert transcript[1] == {"role": "assistant", "content": "你同意明天加班吗？"}
    assert transcript[2] == {"role": "user", "content": "[图片 image_1]"}
