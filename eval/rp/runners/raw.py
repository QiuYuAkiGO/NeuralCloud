"""Raw (no-harness) generation: profile + history → OpenAI-compatible chat."""

from __future__ import annotations

import time
from pathlib import Path

from harness.config import Settings, get_settings
from harness.llm.client import LLMClient
from infra.trace.recorder import JsonlTracer, NullTracer, Tracer

from eval.rp.persona import is_character_turn, render_profile
from eval.rp.schema import RPEpisode, RPSample


class RawRunner:
    def __init__(
        self,
        llm: LLMClient,
        *,
        settings: Settings | None = None,
        tracer: Tracer | None = None,
        trace_path: Path | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings or get_settings()
        if tracer is not None:
            self.tracer = tracer
        elif trace_path is not None:
            self.tracer = JsonlTracer(trace_path, settings=self.settings)
        else:
            self.tracer = NullTracer()

    def run(self, episode: RPEpisode) -> RPSample:
        messages = build_raw_messages(episode)
        started = time.perf_counter()
        error: str | None = None
        try:
            text = self.llm.complete_chat(messages)
        except Exception as exc:  # noqa: BLE001 — keep the eval loop alive
            text = ""
            error = str(exc)
        latency = (time.perf_counter() - started) * 1000.0
        self.tracer.record(
            "rp_raw",
            {
                "episode_id": episode.episode_id,
                "bench": episode.bench,
                "role_id": episode.role_id,
                "latency_ms": round(latency, 3),
                "error": error,
            },
        )
        trace_ref = getattr(self.tracer, "path", None)
        return RPSample(
            episode_id=episode.episode_id,
            bench=episode.bench,
            mode="raw",
            role_id=episode.role_id,
            response=(text or "").strip(),
            latency_ms=latency,
            trace_ref=f"{trace_ref}#{episode.episode_id}" if trace_ref else None,
            error=error,
        )


def build_raw_messages(episode: RPEpisode) -> list[dict[str, str]]:
    profile = render_profile(episode.profile)
    if episode.bench == "roleeval":
        system = "你是角色知识评测助手。只根据题目选择 A/B/C/D，先写选项字母。"
        if profile:
            system += "\n" + profile
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": episode.query},
        ]
    if episode.bench == "timechara":
        period = episode.meta.get("character_period") or ""
        system = (
            f"You are {episode.role_id}"
            + (f" at {period}" if period else "")
            + ". Stay in character. Do not reveal events after your current point in the story."
        )
        if profile:
            system += "\n" + profile
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": episode.query},
        ]
    system = profile + f"\n现在请你扮演一个角色扮演专家。请你根据上述信息扮演{episode.role_id}进行对话。"
    messages = [{"role": "system", "content": system.strip()}]
    for turn in episode.history:
        role = "assistant" if is_character_turn(turn, episode) else "user"
        content = f"{turn.speaker}：{turn.text}" if turn.speaker not in {"user", "character"} else turn.text
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": episode.query})
    return messages
