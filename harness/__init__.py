"""Public harness exports, loaded only when their subsystem is requested.

An independent body transport must be importable without loading cognition,
memory backends or the Runtime composition root. Existing imports stay valid.
"""

from importlib import import_module

_EXPORTS = {
    "EvidenceRequiredError": "harness.core.exceptions", "PersonaLockedError": "harness.core.exceptions",
    "FinalResponse": "harness.core.inference", "EpisodicEvent": "harness.core.models",
    "InteractionEvent": "harness.core.models", "PersonaCore": "harness.core.models",
    "RetrievalQuery": "harness.core.models", "SemanticFact": "harness.core.models",
    "MemoryService": "harness.memory.service", "build_inmemory_service": "harness.memory.service",
    "HomeostasisEngine": "harness.homeostasis", "HomeostaticProfile": "harness.homeostasis",
    "HomeostaticSnapshot": "harness.homeostasis", "InferencePipeline": "harness.pipeline.pipeline",
    "build_pipeline": "harness.pipeline.pipeline", "RuntimeEvent": "harness.runtime",
    "RuntimeLifecycle": "harness.runtime", "RuntimeSupervisor": "harness.runtime",
}


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value

__all__ = [
    "EpisodicEvent",
    "EvidenceRequiredError",
    "FinalResponse",
    "InferencePipeline",
    "InteractionEvent",
    "HomeostasisEngine",
    "HomeostaticProfile",
    "HomeostaticSnapshot",
    "MemoryService",
    "PersonaCore",
    "PersonaLockedError",
    "RetrievalQuery",
    "RuntimeEvent",
    "RuntimeLifecycle",
    "RuntimeSupervisor",
    "SemanticFact",
    "build_inmemory_service",
    "build_pipeline",
]
