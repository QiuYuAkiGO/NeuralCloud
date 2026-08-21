"""Character model: persona-conditioned generation from a compiled ModelContext (02 §阶段 8)."""

from __future__ import annotations

from harness.config import Settings, get_settings
from harness.core.inference import CandidateResponse, ModelContext
from harness.llm.client import LLMClient, MockLLM


class CharacterModel:
    def __init__(self, llm: LLMClient | None = None, *, settings: Settings | None = None) -> None:
        self.llm = llm or MockLLM()
        self.settings = settings or get_settings()

    def generate(
        self,
        context: ModelContext,
        *,
        revision_instruction: str | None = None,
        continuation_instruction: str | None = None,
        revision: int = 0,
        stream: bool = False,
    ) -> CandidateResponse:
        if stream:
            raise NotImplementedError("streaming is reserved for a later milestone")
        messages = context.render_messages()
        if self.settings.continuation_enabled:
            from harness.pipeline.continuation import PROTOCOL_DIRECTIVE

            messages = [*messages, {"role": "system", "content": PROTOCOL_DIRECTIVE}]
        if continuation_instruction:
            messages = [*messages, {"role": "system", "content": continuation_instruction}]
        if revision_instruction:
            messages = [
                *messages,
                {"role": "system", "content": f"Revision instruction: {revision_instruction}"},
            ]
        text = self.llm.complete_chat(messages)
        return CandidateResponse(
            text=text.strip(),
            model_version=self.settings.model_version,
            revision=revision,
        )
