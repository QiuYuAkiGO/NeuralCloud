"""Character model: persona-conditioned generation from a compiled ModelContext (02 §阶段 8)."""

from __future__ import annotations

from harness.config import Settings, get_settings
from harness.core.inference import CandidateResponse, ModelContext
from harness.llm.client import ChatResult, LLMClient, MockLLM, estimate_chat_usage


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
        spoken_segments: list[str] | None = None,
        revision: int = 0,
        stream: bool = False,
    ) -> CandidateResponse:
        if stream:
            raise NotImplementedError("streaming is reserved for a later milestone")
        messages = context.render_messages()
        if self.settings.continuation_enabled:
            from harness.pipeline.continuation import PROTOCOL_DIRECTIVE

            messages = [*messages, {"role": "system", "content": PROTOCOL_DIRECTIVE}]
        if spoken_segments:
            messages = [
                *messages,
                *[{"role": "assistant", "content": seg} for seg in spoken_segments if seg],
            ]
        if continuation_instruction:
            messages = [*messages, {"role": "system", "content": continuation_instruction}]
        if revision_instruction:
            messages = [
                *messages,
                {"role": "system", "content": f"Revision instruction: {revision_instruction}"},
            ]
        result = self._complete(messages, stream=stream)
        text = result.text.strip()
        return CandidateResponse(
            text=text,
            model_version=self.settings.model_version,
            revision=revision,
            usage=result.usage.for_call(revision=revision),
        )

    def _complete(self, messages: list[dict[str, str]], *, stream: bool) -> ChatResult:
        from harness.llm.deadline import remaining_model_seconds

        remaining = remaining_model_seconds()
        result_fn = getattr(self.llm, "complete_chat_result", None)
        if callable(result_fn):
            result = result_fn(messages, stream=stream,
                               **({"timeout_seconds": remaining} if remaining is not None else {}))
            remaining_model_seconds()
            usage = result.usage if result.usage.total_tokens or result.usage.prompt_tokens else estimate_chat_usage(
                messages, result.text
            )
            if isinstance(result, ChatResult):
                return result.model_copy(update={"usage": usage})
            return ChatResult(text=result.text, usage=usage)
        text = self.llm.complete_chat(messages, stream=stream)
        return ChatResult(text=text, usage=estimate_chat_usage(messages, text))
