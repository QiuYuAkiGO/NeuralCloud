from __future__ import annotations

import pytest

from harness.config import Settings
from harness.core.inference import CandidateResponse
from harness.llm.client import MockLLM
from harness.pipeline.pipeline import InferencePipeline
from harness.validator.validator import ResponseValidator, rule_check
from tests.factories import NOW, seed_running_example, vaelith


BRIEF = "这句话让你非常生气，暂时直白而坚定地说明边界。"
RULE_VIOLATING_TEXT = "我的内稳态 cognitive_load 偏离度=0.75。"


@pytest.mark.parametrize("path", ["fast", "deep"])
def test_disabled_pipeline_keeps_generation_and_emotion_but_skips_all_validation(service, path):
    if path == "deep":
        seed_running_example(service)
    llm = MockLLM(chat_script=[RULE_VIOLATING_TEXT])
    stages = []

    class Observer:
        def on_stage(self, kind, result, payload):
            stages.append((kind, result, payload))

    class UnexpectedValidator:
        def should_run(self, path):
            pytest.fail("disabled pipeline must not sample a validator")

        def check(self, *args, **kwargs):
            pytest.fail("disabled pipeline must not validate")

    pipeline = InferencePipeline(
        service,
        settings=Settings(
            validator_enabled=False,
            validator_sample_rate=1.0,
            emotion_mode="on",
            continuation_enabled=False,
            record_interaction=False,
        ),
        llm=llm,
        validator=UnexpectedValidator(),
        observer=Observer(),
    )
    text = "你昨天不是说不会再管我了吗？" if path == "deep" else "早上好。"
    final = pipeline.prepare(text, now=NOW, session_id="validator-off", emotion_guidance=BRIEF)

    assert final.path == path
    assert final.text == RULE_VIOLATING_TEXT
    assert final.revisions == 0 and final.usage.revision_calls == 0
    assert final.validation.skipped is True
    assert len(llm.chat_calls) == 1
    system = "\n".join(m["content"] for m in llm.chat_calls[0] if m["role"] == "system")
    assert system.count(BRIEF) == 1
    validation_stages = [(result, payload) for kind, result, payload in stages if kind == "validate"]
    assert len(validation_stages) == 1
    assert validation_stages[0][1]["reason"] == "disabled_by_configuration"
    assert validation_stages[0][1]["skipped"] is True


def test_direct_validator_switch_skips_rules_and_model_and_can_be_restored():
    settings = Settings(validator_enabled=False, validator_sample_rate=1.0)

    class UnexpectedLLM:
        def complete_json(self, **kwargs):
            pytest.fail("disabled validator must not call a model")

    validator = ResponseValidator(settings=settings, llm=UnexpectedLLM())
    persona = vaelith()
    candidate = CandidateResponse(text=RULE_VIOLATING_TEXT, model_version="test")
    assert rule_check(candidate.text, persona=persona, insights=[], emotion_guidance=BRIEF).verdict == "revise"
    for path in ("fast", "deep"):
        assert validator.should_run(path) is False
    for text in (RULE_VIOLATING_TEXT, "收到。"):
        result = validator.check(candidate.model_copy(update={"text": text}), persona=persona,
                                 homeostatic_context=True, emotion_guidance=BRIEF)
        assert result.verdict == "pass" and result.skipped is True

    settings.validator_enabled = True
    assert validator.should_run("deep") is True
    restored = validator.check(candidate, persona=persona, emotion_guidance=BRIEF, run_llm=False)
    assert restored.verdict == "revise" and restored.skipped is False


def test_default_enabled_pipeline_still_revises(service):
    llm = MockLLM(chat_script=[RULE_VIOLATING_TEXT],
                  chat_routes={"Revision instruction": "这句话让我很生气。"})
    pipeline = InferencePipeline(service, settings=Settings(
        emotion_mode="on", validator_sample_rate=0.0,
        continuation_enabled=False, record_interaction=False), llm=llm)
    final = pipeline.prepare("你好。", now=NOW, session_id="validator-default", emotion_guidance=BRIEF)
    assert pipeline.settings.validator_enabled is True
    assert final.text == "这句话让我很生气。"
    assert final.revisions == 1 and final.validation.skipped is False
