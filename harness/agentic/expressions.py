"""Reuse existing board/diary authorization and commit contracts."""
import asyncio
from datetime import datetime, timezone
import hashlib
import time

from harness.agentic.store import canonical


async def prepare(host, command, request, deadline, *, kind):
    started = time.monotonic()
    remaining = min(request.model_timeout_seconds, (deadline - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        raise TimeoutError("agentic_expression_expired")
    payload = {"instruction": "准备一篇私人日记，至多4000字。" if kind == "diary" else "准备一条私人留言板内容，正文至多1600字，标题至多120字。",
        "request": request.request_text, "trigger": request.trigger}
    if kind == "diary":
        payload["day"] = request.day
        entrance = "diary:private"
    else:
        entrance = "board:" + request.body_instance_id + ":" + request.board_id
        payload["verified_prior_entries"] = [{"title": entry.title, "text": entry.text,
            "entry_id": entry.receipt.entry_id} for entry in request.read_entries[-4:]]
    async with asyncio.timeout(remaining):
        result = await asyncio.to_thread(host.agentic.run, audience="local:main", entrance=entrance,
            text=canonical(payload), source_id="", timeout=remaining)
    now = datetime.now(timezone.utc)
    if now >= deadline:
        raise TimeoutError("agentic_expression_expired")
    fields = {"action": "write" if result.finish.action == "emit" else "wait", "text": result.finish.text,
        "entry_id": request.entry_id, "decision_id": kind + "-decision:" + hashlib.sha256(command.command_id.encode()).hexdigest(),
        "prepared_at": now, "deadline_at": deadline}
    if kind == "diary":
        from harness.cognitive.diary import DiaryDecision
        decision = DiaryDecision(**fields, source_event_id=request.source_event_id, day=request.day)
    else:
        from harness.cognitive.board import BoardDecision
        decision = BoardDecision(**fields, title=result.finish.title, body_instance_id=request.body_instance_id,
            board_id=request.board_id, source_event_ids=tuple(dict.fromkeys([request.source_event_id, *sorted(result.shown)]))[:32])
    return {"decision": decision.model_dump(mode="json"), "timing": {"model_calls": result.calls,
        "total_ms": round((time.monotonic() - started) * 1000, 3), "usage": result.usage.model_dump(mode="json")}}
