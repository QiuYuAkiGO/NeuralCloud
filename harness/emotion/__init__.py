"""Auxiliary-model emotion observation, deterministic mixing and expression hints."""
from harness.emotion.models import (EmotionAppraisal, EmotionEventAppraisal, EmotionFrame, EmotionGuidance,
                                   EmotionInput, EmotionMessage, EmotionRecovery, EmotionSignal)
from harness.emotion.semantics import (compose_weights, fallback_guidance, project_persona,
                                      semantic_homeostasis, validate_guidance_text)
from harness.emotion.service import EmotionService

__all__ = ["EmotionService", "EmotionInput", "EmotionMessage", "EmotionFrame", "EmotionGuidance",
           "EmotionAppraisal", "EmotionEventAppraisal", "EmotionSignal", "EmotionRecovery",
           "project_persona", "semantic_homeostasis", "compose_weights", "fallback_guidance", "validate_guidance_text"]
