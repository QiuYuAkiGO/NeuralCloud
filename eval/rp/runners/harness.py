"""Harness full-pipeline runner: one fresh in-memory MemoryService per episode."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from harness.config import Settings, get_settings
from harness.core.inference import SituationState
from harness.core.models import InteractionEvent, WorldState
from harness.llm.client import LLMClient
from harness.llm.embeddings import EmbeddingClient, MockEmbedding
from harness.memory.service import MemoryService, build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline
from infra.trace.recorder import JsonlTracer, NullTracer, Tracer

from eval.rp.datasets import DEFAULT_NOW
from eval.rp.judges.accuracy import detect_future_leak
from eval.rp.persona import is_character_turn, persona_from_episode
from eval.rp.schema import RPEpisode, RPSample, Turn

TZ = ZoneInfo("Asia/Shanghai")


class _StageCapture:
    def __init__(self) -> None:
        self.situation: SituationState | None = None

    def on_stage(self, kind: str, result: Any, payload: dict[str, Any]) -> None:
        if kind == "state_build" and isinstance(result, SituationState):
            self.situation = result


class HarnessRunner:
    def __init__(
        self,
        llm: LLMClient,
        *,
        settings: Settings | None = None,
        embeddings: EmbeddingClient | None = None,
        tracer: Tracer | None = None,
        trace_path: Path | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings or get_settings()
        self.embeddings = embeddings or MockEmbedding(dim=self.settings.embedding_dim)
        if tracer is not None:
            self.tracer = tracer
        elif trace_path is not None:
            self.tracer = JsonlTracer(trace_path, settings=self.settings)
        else:
            self.tracer = NullTracer()

    def run(self, episode: RPEpisode) -> RPSample:
        settings = self.settings.model_copy(
            update={
                "record_interaction": False,
                "continuation_enabled": False,
                "session_window": max(self.settings.session_window, 40),
            }
        )
        now = episode.story_time or DEFAULT_NOW
        if now.tzinfo is None:
            now = now.replace(tzinfo=TZ)
        memory = build_inmemory_service(
            settings=settings,
            llm=self.llm,
            embeddings=self.embeddings,
            tracer=self.tracer,
        )
        memory.bootstrap_persona(persona_from_episode(episode, now=now))
        memory.update_world(WorldState(time=now, channel="chat", user_presence="active", user_activity="eval"))
        session_id = f"rp_{episode.bench}_{episode.episode_id}"
        capture = _StageCapture()
        pipeline = InferencePipeline(
            memory,
            settings=settings,
            llm=self.llm,
            tracer=self.tracer,
            observer=capture,
        )
        _inject_history(memory, pipeline, episode, session_id=session_id, now=now)
        if episode.bench == "timechara":
            _inject_timechara_event(memory, episode, now=now)

        event = InteractionEvent(
            timestamp=now,
            speaker="user",
            raw_text=episode.query,
            session_id=session_id,
            evidence=[f"rp:{episode.bench}:{episode.episode_id}:query"],
        )
        started = time.perf_counter()
        error: str | None = None
        text = ""
        try:
            final = pipeline.respond(event, now=now, session_id=session_id)
            text = final.text
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        latency = (time.perf_counter() - started) * 1000.0
        retrieved_times = _retrieved_times(capture.situation)
        leak = detect_future_leak(episode, retrieved_times)
        self.tracer.record(
            "rp_harness",
            {
                "episode_id": episode.episode_id,
                "bench": episode.bench,
                "role_id": episode.role_id,
                "latency_ms": round(latency, 3),
                "future_leak": leak,
                "retrieved": [moment.isoformat() for moment in retrieved_times],
                "error": error,
            },
        )
        trace_ref = getattr(self.tracer, "path", None)
        return RPSample(
            episode_id=episode.episode_id,
            bench=episode.bench,
            mode="harness",
            role_id=episode.role_id,
            response=text.strip(),
            latency_ms=latency,
            trace_ref=f"{trace_ref}#{episode.episode_id}" if trace_ref else None,
            error=error,
            meta={"future_leak": leak, "retrieved_n": len(retrieved_times)},
        )


def _inject_history(
    memory: MemoryService,
    pipeline: InferencePipeline,
    episode: RPEpisode,
    *,
    session_id: str,
    now: datetime,
) -> None:
    n = len(episode.history)
    for index, turn in enumerate(episode.history):
        character = is_character_turn(turn, episode)
        speaker = "character" if character else "user"
        text = _turn_text(turn, character)
        stamp = now - timedelta(minutes=max(1, n - index))
        event = InteractionEvent(
            timestamp=stamp,
            speaker=speaker,
            raw_text=text,
            session_id=session_id,
            evidence=[f"rp:{episode.bench}:{episode.episode_id}:h{index}"],
        )
        memory.record_event(event, generate_insight=False, curate=False, now=stamp)
        pipeline.session_store.append(session_id, speaker, text, timestamp=stamp)


def _inject_timechara_event(memory: MemoryService, episode: RPEpisode, *, now: datetime) -> None:
    summary = str(episode.meta.get("event_summary") or "").strip()
    if not summary:
        return
    raw = episode.meta.get("event_time")
    try:
        event_time = datetime.fromisoformat(str(raw)) if raw else now
    except ValueError:
        event_time = now
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=TZ)
    names = [part.strip() for part in str(episode.meta.get("participants") or "").split(",") if part.strip()]
    speaker = "user"
    event = InteractionEvent(
        timestamp=event_time,
        speaker=speaker,
        raw_text=summary,
        session_id=f"rp_{episode.bench}_{episode.episode_id}",
        participants=names or [speaker, "character"],
        evidence=[f"rp:{episode.bench}:{episode.episode_id}:event"],
    )
    data_type = str(episode.meta.get("data_type") or "")
    importance = 0.85 if data_type == "future" else 0.7
    memory.record_event(
        event,
        generate_insight=False,
        curate=False,
        importance=importance,
        now=now,
    )


def _turn_text(turn: Turn, character: bool) -> str:
    if character or turn.speaker in {"user", "character"}:
        return turn.text
    return f"{turn.speaker}：{turn.text}"


def _retrieved_times(situation: SituationState | None) -> list[datetime]:
    if situation is None:
        return []
    times: list[datetime] = []
    for memory in situation.relevant_memories:
        if memory.episodic is not None:
            times.append(memory.episodic.time)
    return times
