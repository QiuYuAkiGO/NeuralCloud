"""Bounded, Runtime-driven web steps before the original single response prepare."""

from __future__ import annotations

from datetime import datetime, timedelta

from harness.embodiment.web_contracts import WebError
from harness.runtime.models import RuntimeLifecycle, utc_now
from harness.runtime.web import READ, SEARCH, _hash


async def prepare_web_context(supervisor, event, *, channel="chat", deadline=None):
    web = getattr(supervisor, "web", None)
    method = getattr(supervisor.cognitive, "prepare_web_step", None)
    if web is None or not web.conversation_enabled or method is None:
        return None, None
    if supervisor.manifest.grounded_agency is not None:
        # Frozen research conditions have no negotiated web observation contract.
        return None, None
    identity = event.event_id
    scope = channel + ":" + identity
    payload = {key: value for key, value in event.payload.items()
               if key not in {"runtime_web_context", "runtime_web_deadline_at", "web_context", "offers"}}
    binding = _hash([channel, payload, web._revision])
    row = web.calls.round(identity)
    if row is None:
        now = utc_now()
        final_deadline = min(deadline, now + timedelta(seconds=60)) if deadline else now + timedelta(seconds=60)
        row = {"binding": binding, "scope": scope, "status": "started", "step": 0,
            "runtime_instance_id": supervisor.runtime_instance_id,
            "activity_revision": supervisor.capabilities.activity_revision,
            "deadline_at": final_deadline.isoformat(),
            "web_deadline_at": min(now + timedelta(seconds=30), final_deadline - timedelta(seconds=30)).isoformat(),
            "searches": 0, "reads": 0, "failures": []}
        web.calls.put_round(identity, row)
        web.register_urls(scope, str(payload.get("query" if channel == "qq" else "text") or ""))
    final_deadline = datetime.fromisoformat(row["deadline_at"])
    if row["binding"] != binding:
        return {"sources": [], "failures": [{"code": "web_round_superseded"}], "status": "unavailable", "evidence_refs": []}, final_deadline
    if row["status"] == "completed":
        return row["context"], final_deadline
    if row["runtime_instance_id"] != supervisor.runtime_instance_id:
        row["failures"].append({"code": "web_round_interrupted"})
        row["status"] = "completed"
        row["context"] = web.context(scope, failures=row["failures"])
        web.calls.put_round(identity, row)
        return row["context"], final_deadline
    web_deadline = datetime.fromisoformat(row["web_deadline_at"])

    def invalid():
        return (supervisor.lifecycle != RuntimeLifecycle.RUNNING or not web.conversation_enabled
                or row["activity_revision"] != supervisor.capabilities.activity_revision)

    try:
        while row["step"] < 3 and utc_now() + timedelta(seconds=1) < web_deadline and not invalid():
            offers = [offer for offer in await web.catalog(scope, autonomous=False, deadline=web_deadline)
                      if offer.ready and (offer.capability_id != SEARCH or row["searches"] < 2)
                      and (offer.capability_id != READ or row["reads"] < 1)]
            if not offers:
                break
            step = row["step"]
            envelope = supervisor._make_command(event, command_type="PrepareWebStep",
                idempotency_key=f"web-step:{identity}:{step}",
                expected_revision=supervisor._cognitive_snapshot.revision,
                payload={"channel": channel, "event_payload": payload, "step": step,
                    "deadline_at": web_deadline.isoformat(), "offers": [o.model_dump(mode="json") for o in offers],
                    "runtime_web_context": web.context(scope, failures=row["failures"])})
            envelope = envelope.model_copy(update={"deadline": min(envelope.deadline, web_deadline)})
            selected = await supervisor._invoke_domain(service="cognitive", envelope=envelope, method=method, event=event)
            if invalid() or utc_now() >= web_deadline:
                raise WebError("web_call_superseded")
            choice = selected.result.get("choice", {})
            if choice.get("kind") == "finish":
                break
            offer = next((item for item in offers if item.offer_id == choice.get("offer_id")), None)
            if choice.get("kind") != "call" or offer is None:
                raise WebError("web_choice_invalid")
            result = await web.execute(f"dialogue:{identity}:{step}", offer, choice.get("arguments", {}),
                                       deadline=web_deadline, guard=invalid)
            row["failures"].extend(result.failures[:max(0, 6 - len(row["failures"]))])
            if result.status == "partial" and not result.failures and len(row["failures"]) < 6:
                row["failures"].append({"code": "web_partial_result", "capability": offer.capability_id})
            row["step"] += 1
            row["searches" if offer.capability_id == SEARCH else "reads"] += 1
            web.calls.put_round(identity, row)
    except Exception as exc:
        row["failures"].append({"code": exc.code if isinstance(exc, WebError) else "web_preparation_failed"})
    row["status"] = "completed"
    row["context"] = web.context(scope, failures=row["failures"])
    web.calls.put_round(identity, row)
    return row["context"], final_deadline
