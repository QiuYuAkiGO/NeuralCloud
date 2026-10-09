"""Mandatory semantics remain bound to their own slots, including decaying joy."""
from datetime import timedelta
from itertools import product

import pytest

from harness.emotion.models import EmotionFrame, GuidanceOutput
from harness.emotion.semantics import LABEL_TEXT, NEEDS, required_expression, validate_guidance_text
from harness.emotion.service import EmotionService
from tests.test_emotion_service import FakeAux, NOW, request, settings


def frame(**changes):
    base = EmotionFrame(context_id="contract", scope="local:main", revision=1, as_of=NOW,
                        status="model", labels={}, dialogue_trends={}, intensity=.8,
                        weights={"homeostasis":0., "dialogue":1., "personality":0.},
                        strengths={"homeostasis":0., "dialogue":.8, "personality":.2}, dominant_source="dialogue")
    return base.model_copy(update=changes)


@pytest.mark.parametrize("label,trend,amount", list(product(LABEL_TEXT, ("recovering", "rising", "steady"), (.2, .8))))
def test_expression_retains_each_degree_and_trend(label, trend, amount):
    current = frame(labels={label:amount}, dialogue_trends={label:trend})
    expression = required_expression(current)
    assert ("轻微" if amount==.2 else "明显") + LABEL_TEXT[label] in expression["feeling"]
    assert {"recovering":"减弱", "rising":"增强", "steady":"稳定"}[trend] in expression["feeling"]
    assert validate_guidance_text(GuidanceOutput(**expression, delivery="措辞清晰，节奏自然。").text, frame=current)


@pytest.mark.parametrize("need,trend", list(product(NEEDS, ("recovering", "worsening", "steady"))))
def test_expression_retains_primary_need_and_its_own_trend(need, trend):
    current = frame(dominant_source="homeostasis", homeostatic_needs=[{
        "dimension":need[0], "direction":need[1], "semantic_need":NEEDS[need], "trend":trend}])
    expression = required_expression(current)
    assert NEEDS[need] in expression["needs"]
    assert {"recovering":"正在缓和", "worsening":"正在加重", "steady":"仍然存在"}[trend] in expression["needs"]
    assert validate_guidance_text(GuidanceOutput(**expression, delivery="措辞清晰，节奏自然。").text, frame=current)


@pytest.mark.parametrize("mutation,code", [
    (lambda raw: {**raw, "feeling":raw["feeling"].replace("明显", "轻微")}, "emotion_guidance_feeling_mismatch"),
    (lambda raw: {**raw, "feeling":raw["feeling"].replace("惊讶，强度正在减弱", "惊讶，强度正在增强")}, "emotion_guidance_feeling_mismatch"),
    (lambda raw: {**raw, "needs":raw["needs"] + "已无需熟悉稳定。"}, "emotion_guidance_need_mismatch"),
])
def test_wrong_degree_neighbor_trend_and_need_are_rejected_even_if_old_cues_match(mutation, code):
    class CorruptAux(FakeAux):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            if "EMOTION_GUIDANCE" in kwargs["system"]:
                self.invalid = mutation(result)
                return self.invalid
            return result

    aux = CorruptAux(appraisal=lambda payload: {"events":[{"source_id":identity, "signals":[
        {"label":"interest", "intensity":.9, "confidence":1.},
        {"label":"surprise", "intensity":.8, "confidence":1.}]} for identity in payload["new_source_ids"]]})
    service = EmotionService(settings(), aux)
    service.observe(request())
    current = service.observe(request("later", messages=[], as_of=NOW+timedelta(minutes=1), homeostasis={
        "dominant_needs":[{"dimension":"novelty", "direction":"high", "normalized_deviation":.95,
                           "confidence":1., "trend":"recovering"}]}))
    result = service.guidance(current)
    assert validate_guidance_text(GuidanceOutput(**aux.invalid).text, frame=current)
    assert result.status == "fallback" and result.diagnostics["fallback_reason"] == code
    assert validate_guidance_text(result.text, frame=current)
    assert result.diagnostics["model_calls"] == 1
    assert service.guidance(current) == result and len(aux.calls) == 2


def test_bound_slots_do_not_bypass_delivery_guards():
    service = EmotionService(settings(), FakeAux(delivery="我已经联系了群主。"))
    result = service.guidance(service.observe(request()))
    assert result.status == "fallback" and result.diagnostics["fallback_reason"] == "emotion_guidance_invalid"
    assert "联系了群主" not in result.text


def test_empty_frame_does_not_invent_a_need_or_strong_mood():
    current = frame(intensity=0., dominant_source="personality")
    expression = required_expression(current)
    assert expression == {"feeling":"当前没有明显心情，保持自然平静。", "needs":"当前没有已确认的额外需要。"}
    assert validate_guidance_text(GuidanceOutput(**expression, delivery="措辞自然。").text, frame=current)
