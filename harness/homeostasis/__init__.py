"""Public subsystem exports without eager construction-root imports."""

from importlib import import_module

_EXPORTS = {
    "DIMENSIONS": ("harness.homeostasis.models", "DIMENSIONS"),
    "CoreAffect": ("harness.homeostasis.models", "CoreAffect"),
    "DerivedEmotion": ("harness.homeostasis.models", "DerivedEmotion"),
    "DimensionProfile": ("harness.homeostasis.models", "DimensionProfile"),
    "EventAppraisal": ("harness.homeostasis.models", "EventAppraisal"),
    "FactorBelief": ("harness.homeostasis.models", "FactorBelief"),
    "HomeostaticDominantNeed": ("harness.homeostasis.models", "HomeostaticDominantNeed"),
    "HomeostaticEmotionHint": ("harness.homeostasis.models", "HomeostaticEmotionHint"),
    "HomeostasisEngine": ("harness.homeostasis.engine", "HomeostasisEngine"),
    "HomeostaticInteroceptiveSummary": ("harness.homeostasis.models", "HomeostaticInteroceptiveSummary"),
    "HomeostaticProfile": ("harness.homeostasis.models", "HomeostaticProfile"),
    "HomeostaticObservation": ("harness.homeostasis.models", "HomeostaticObservation"),
    "HomeostaticSnapshot": ("harness.homeostasis.models", "HomeostaticSnapshot"),
    "HomeostaticTransitionTrace": ("harness.homeostasis.models", "HomeostaticTransitionTrace"),
    "PolicyCandidate": ("harness.homeostasis.models", "PolicyCandidate"),
    "QQDriveContext": ("harness.homeostasis.models", "QQDriveContext"),
    "QQHomeostaticFact": ("harness.homeostasis.models", "QQHomeostaticFact"),
    "QQSocialDriveProfile": ("harness.homeostasis.models", "QQSocialDriveProfile"),
    "ProfileRevisionStore": ("harness.homeostasis.profiles", "ProfileRevisionStore"),
    "load_profile": ("harness.homeostasis.profiles", "load_profile"),
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value
