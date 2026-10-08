"""Local text channel adapter. Transport facts remain owned by Runtime."""
from __future__ import annotations

import asyncio

from harness.interruption import TextInterruption


class RuntimeTextTurns:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.engine = TextInterruption(supervisor.state_dir / "text-interruption.sqlite3",
            supervisor.manifest.text_interruption, self.assess)
        self.locks = {}

    async def assess(self, request):
        if self.supervisor._response_block_reason() is not None:
            raise RuntimeError("interruption_owner_paused")
        return await self.supervisor.cognitive.assess_text_interruption(request)

    def scope(self, event):
        return "text:" + str(event.payload.get("channel") or "chat") + ":" + str(event.payload.get("session_id") or "sess_default")

    def receive(self, event):
        if self.engine.enabled and event.event_type.value == "user_message":
            self.engine.store.register_run(event.correlation_id, event.event_id)
            self.engine.receive(self.scope(event), event.event_id, str(event.payload.get("speaker") or "user"), str(event.payload.get("text") or ""))

    async def dispatch(self, event, invoke):
        if not self.engine.enabled or event.event_type.value != "user_message":
            return await invoke()
        if not self.engine.active:
            return await self._invoke(event, invoke)
        async with self.locks.setdefault(self.scope(event), asyncio.Lock()):
            return await self._invoke(event, invoke)

    async def _invoke(self, event, invoke):
        self.receive(event)  # Also covers durable queue restoration.
        self.engine.begin(self.scope(event), event.event_id, str(event.payload.get("text") or ""),
            event.received_at.timestamp()+self.supervisor.command_timeout_seconds,
            author=str(event.payload.get("speaker") or "user"))
        try:
            return await invoke()
        finally:
            self.engine.finish(event.event_id)

    async def disposition(self, event, action, prepare_id=None):
        await self.supervisor._publish("cognitive.ui", {"type": "reply_disposition",
            "run_id": event.correlation_id, "event_id": event.event_id,
            "data": {"action": action, "prepare_id": prepare_id}})

    async def gate(self, event, prepared):
        action = await self.engine.before_send(event.event_id, prepared.decision.text)
        if action != "continue":
            await self.disposition(event, action, prepared.prepare_id)
        return action

    async def published(self, event, prepared, delivery):
        if not self.engine.active or event.event_type.value != "user_message" or not delivery.success:
            return
        self.engine.delivered(event.event_id, delivery.delivered_segments, receipt_id=prepared.prepare_id)
        row = self.engine.store.get(event.event_id)
        await self.supervisor._publish("cognitive.ui", {"type": "final", "stage": "final",
            "run_id": event.correlation_id, "event_id": event.event_id,
            "data": {"text": "\n\n".join(delivery.delivered_segments), "segments": delivery.delivered_segments,
                     "response_id": prepared.prepare_id, "output_generation": row["generation"] if row else 0,
                     "authorized_delivery": True}})

    def command_context(self, event):
        return self.engine.continuation(event.event_id)

    def deadline(self, event, default):
        return self.engine.deadline(event.event_id, default)

    def suppress_preview(self, payload):
        # Raw Cognitive completions are candidate output. Only Runtime's own
        # authorized final event is published when this channel is active.
        return (self.engine.active
            and (payload.get("type") == "final" or payload.get("stage") in {"final", "reply_segment"})
            and self.engine.store.has_run(payload.get("run_id")))
