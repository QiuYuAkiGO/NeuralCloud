"""One durable, model-owned opportunity at a time across existing bodies.

This controller selects and dispatches. Channel authorities still own effects
and receipts; an RPC acknowledgement is never a substitute for that evidence.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3
import time
from uuid import uuid4

from harness.runtime.models import EventDispatchResult, RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle, utc_now


TERMINAL = {"completed", "committed", "wait", "no_action", "defer", "error", "expired", "superseded", "rejected", "cancelled"}

# These diagnostics cross into persisted goal progress and the local UI. Never
# retain arbitrary exception text, which can contain model output or body data.
_GOAL_RUNTIME_ERRORS = {
    "capability_catalog_binding_invalid": "capability_catalog_binding_invalid",
    "capability_decision_binding_invalid": "capability_decision_binding_invalid",
    "goal decision capability or snapshot binding mismatch": "goal_decision_binding_mismatch",
    "goal decision contains ungrounded evidence": "goal_ungrounded_evidence",
    "goal decision superseded": "goal_decision_superseded",
    "goal decision identity conflict": "goal_decision_identity_conflict",
    "goal episode does not exist": "goal_episode_unavailable",
    "unknown effects require reconciliation before replanning": "goal_unknown_effect_pending",
    "goal plan step budget exceeded": "goal_plan_step_budget_exceeded",
    "executable goal choice requires an unfinished planned step": "goal_planned_step_required",
    "completion requires a separate evidence review": "goal_separate_review_required",
    "necessary goal steps are unfinished": "goal_complete_steps_unfinished",
    "goal completion criterion lacks independent outcome evidence": "goal_complete_outcome_evidence_required",
    "goal criterion evidence belongs to a different step": "goal_criterion_step_evidence_mismatch",
    "goal appraisal binding mismatch": "goal_appraisal_binding_mismatch",
}
GOAL_RUNTIME_ERROR_CODES = frozenset((*_GOAL_RUNTIME_ERRORS.values(), "goal_runtime_error"))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


async def before_deadline(awaitable, deadline):
    async with asyncio.timeout(max(0, (deadline - utc_now()).total_seconds())) as budget:
        result = await awaitable
    if budget.expired() or utc_now() >= deadline:
        raise TimeoutError("capability_deadline_exceeded")
    return result


class CapabilityCalls:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS calls(opportunity_id TEXT PRIMARY KEY,record_json TEXT NOT NULL)")
        self.db.commit()

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def configure(self, fingerprint):
        generation = self.meta("generation", 0)
        if fingerprint != self.meta("configuration"):
            generation += 1
            with self.db:
                for key, value in (("configuration", fingerprint), ("generation", generation)):
                    self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
        return generation

    def get(self, identity):
        row = self.db.execute("SELECT record_json FROM calls WHERE opportunity_id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, row):
        with self.db:
            self.db.execute("INSERT INTO calls VALUES (?,?) ON CONFLICT(opportunity_id) DO UPDATE SET record_json=excluded.record_json",
                (row["opportunity_id"], json.dumps(row, ensure_ascii=False)))

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT record_json FROM calls ORDER BY rowid DESC")]


class CapabilityController:
    def __init__(self, supervisor, state_dir):
        self.supervisor = supervisor
        self.calls = CapabilityCalls(state_dir / "capability-calls.sqlite3")
        self.generation = self.calls.configure(digest(self.settings.model_dump(mode="json")))
        self.last_activity = time.monotonic()
        self.activity_revision = 0
        self.active = None
        self.last_error = None
        supervisor.add_listener(self.on_event)

    @property
    def settings(self):
        return self.supervisor.manifest.autonomous_capabilities

    def owner(self, channel):
        self.generation = self.calls.configure(digest(self.settings.model_dump(mode="json")))
        return self.settings.owners.get(channel, "off")

    async def on_event(self, topic, value):
        if topic != "event.accepted":
            return
        kind, payload = value.get("event_type"), value.get("payload", {})
        incoming = kind == "user_message" or (kind == "qq_fact" and payload.get("qq_fact", {}).get("kind") == "verified_message")
        # QQ_TURN may itself be the downstream preparation of our own call.
        incoming = incoming or (kind == "qq_turn" and not payload.get("idle_intent"))
        if incoming:
            self.last_activity = time.monotonic()
            self.activity_revision += 1

    def _finish(self, row, status, **values):
        row.update(status=status, **values)
        if status in TERMINAL:
            row["completed_at"] = utc_now().isoformat()
        self.calls.put(row)
        return row

    def invalid_reason(self, row):
        goals = getattr(self.supervisor, "goals", None)
        if goals is not None and row.get("goal_binding") and goals.invalid_reason(row):
            return "superseded"
        self.generation = self.calls.configure(digest(self.settings.model_dump(mode="json")))
        if row["generation"] != self.generation or not self.settings.enabled:
            return "superseded"
        if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
            return "superseded"
        if utc_now() >= datetime.fromisoformat(row["deadline_at"]):
            return "expired"
        if row["runtime_instance_id"] != self.supervisor.runtime_instance_id or row["activity_revision"] != self.activity_revision:
            return "superseded"
        offer = row.get("offer")
        if offer and (self.owner(offer["channel"]) != "shared" or offer["capability_id"] not in self.settings.allowed_capabilities):
            return "superseded"
        return None

    async def catalog(self, *, executing_action_id=None, deadline=None, web_scope=None):
        # Refresh and observation share the caller's original end-to-end limit.
        limit = deadline or utc_now() + timedelta(seconds=18)
        return await before_deadline(self._catalog(executing_action_id=executing_action_id, web_scope=web_scope), limit)

    async def _catalog(self, *, executing_action_id=None, web_scope=None):
        from harness.contracts.capability_calls import CapabilityOffer
        from harness.embodiment.board_contracts import BoardReadRequest
        from harness.cognitive.action_planner import _INTENT_PROFILES
        sup, now, offers = self.supervisor, utc_now(), []
        allowed = set(self.settings.allowed_capabilities)
        web = getattr(sup, "web", None)
        if web is not None and self.owner("web") == "shared":
            try:
                offers.extend(offer for offer in await web.catalog(web_scope or "autonomous")
                              if offer.capability_id in allowed)
            except Exception as exc:
                self.last_error = "web:" + type(exc).__name__
        board = getattr(sup, "board", None)
        if board is not None and self.owner("board") == "shared":
            try:
                status = await board.ready()
                body = status.embodiment
                read = await board.provider.read(BoardReadRequest(scope=board.settings.scope, limit=8, latest=True,
                    requested_at=now, deadline=now + timedelta(seconds=5)))
                busy = board.active_entry or any(row["status"] in {"queued", "preparing", "prepared", "append_started", "uncertain", "written_pending_memory"} for row in board.turns.all())
                for capability in ("expression.board.read.v1", "expression.board.append.v1"):
                    if capability not in allowed:
                        continue
                    ready = not busy and (capability.endswith("read.v1") or not board.turns.paused)
                    offers.append(CapabilityOffer(offer_id="board:" + capability, channel="board", capability_id=capability,
                        provider_id=body.provider_id or "board", body_instance_id=board.settings.body_instance_id,
                        scope=board.settings.scope.model_dump(mode="json"), activation_revision=digest({"scope":board.settings.scope.model_dump(), "capabilities":sorted(body.effective_capabilities)}),
                        input_schema={"type":"object", "properties":{}, "additionalProperties":False},
                        observation_refs=[entry.receipt.evidence_ref for entry in read.entries],
                        supported=capability in body.effective_capabilities, available=body.state.value == "available", ready=bool(ready),
                        valid_until=now + timedelta(seconds=10), observation={"entries":[entry.model_dump(mode="json") for entry in read.entries]}))
            except Exception as exc:
                self.last_error = "board:" + type(exc).__name__
        bridge = getattr(sup, "qq_capability_bridge", None)
        if bridge is not None and self.owner("qq") == "shared":
            for value in bridge.catalog():
                offer = CapabilityOffer.model_validate(value)
                if offer.capability_id in allowed and (not offer.capability_id.endswith("send.v1") or self.settings.qq_send_enabled):
                    offers.append(offer)
        voice = getattr(sup, "voice", None)
        if voice is not None and self.owner("voice") == "shared":
            from harness.contracts.capability_calls import VOICE_PROFILES, VOICE_RENDER, VoiceCapabilityArguments
            try:
                status = await voice.ready()
                body, scope = status.embodiment, voice.settings.scope.model_dump(mode="json")
                available = body.state.value in {"available", "degraded"}
                profiles = await voice.profiles() if available else []
                revision = digest({"scope": scope, "capabilities": sorted(body.effective_capabilities)})
                for capability in (VOICE_PROFILES, VOICE_RENDER):
                    if capability not in allowed:
                        continue
                    supported = capability in body.effective_capabilities
                    permitted = capability != VOICE_RENDER or voice.settings.mode == "grounded"
                    offers.append(CapabilityOffer(offer_id="voice:" + capability, channel="voice", capability_id=capability,
                        provider_id=body.provider_id or "voice", body_instance_id=voice.settings.body_instance_id,
                        scope=scope, activation_revision=revision,
                        input_schema=VoiceCapabilityArguments.model_json_schema() if capability == VOICE_RENDER else
                            {"type": "object", "properties": {}, "additionalProperties": False},
                        observation={"profiles": [{"voice_profile_id": item.voice_profile_id, "revision": item.revision}
                            for item in profiles], "effect": "audio_asset_only", "worker_readiness": "unverified"},
                        supported=supported, available=supported and available and permitted,
                        ready=supported and available and permitted and sup.lifecycle == RuntimeLifecycle.RUNNING,
                        valid_until=now + timedelta(seconds=10)))
            except Exception as exc:
                self.last_error = "voice:" + type(exc).__name__
        if self.owner("desktop") == "shared" and sup.manifest.embodiment.mode != "off" and sup._perception_guard is not None:
            try:
                await sup._refresh_embodiment()
                status = sup.embodiment.status()
                provider = sup.embodiment.provider
                declared = getattr(provider, "supported_intents", None)
                declared = declared() if callable(declared) else declared
                supported = set(declared or ()) & set(_INTENT_PROFILES) & allowed
                observation = await asyncio.wait_for(sup._perception_guard.backend.observe(), timeout=5)
                age = (utc_now() - observation.captured_at).total_seconds()
                if (observation.body_instance_id != status.body_instance_id or age < -2 or age > sup.manifest.embodiment.max_state_age_seconds):
                    raise ValueError("desktop_observation_binding_invalid")
                revision = digest({"body":status.body_instance_id, "capabilities":sorted(status.effective_capabilities), "intents":sorted(declared or ())})
                for capability in sorted(supported):
                    offers.append(CapabilityOffer(offer_id="desktop:" + capability, channel="desktop", capability_id=capability,
                        provider_id=status.provider_id, body_instance_id=status.body_instance_id,
                        scope={"body_instance_id":status.body_instance_id}, activation_revision=revision,
                        input_schema=_INTENT_PROFILES[capability].arguments_model.model_json_schema(),
                        observation_refs=[ref.ref for ref in observation.evidence_refs],
                        observation=observation.model_dump(mode="json"), supported=True,
                        available=status.state.value == "available", ready=status.state.value == "available" and (
                            sup.action_coordinator.current is None or sup.action_coordinator.current.action_id == executing_action_id),
                        valid_until=now + timedelta(seconds=min(10, sup.manifest.embodiment.max_state_age_seconds))))
            except Exception as exc:
                self.last_error = "desktop:" + type(exc).__name__
        return offers

    async def heartbeat(self):
        current = self.calls.configure(digest(self.settings.model_dump(mode="json")))
        self.generation = current
        await self.reconcile()
        goals = getattr(self.supervisor, "goals", None)
        if goals is not None and goals.enabled:
            await goals.heartbeat()
            return
        if (not self.settings.enabled or self.active or self.supervisor.lifecycle != RuntimeLifecycle.RUNNING
                or not self.supervisor.queue.empty() or self.supervisor.action_coordinator.pending()
                or time.monotonic() - self.last_activity < self.settings.quiet_seconds):
            return
        rows = self.calls.all()
        if any(row["status"] not in TERMINAL for row in rows):
            return
        if rows and (utc_now() - datetime.fromisoformat(rows[0]["created_at"])).total_seconds() < self.settings.opportunity_seconds:
            return
        summary = self.supervisor._response_homeostatic_interoception()
        if not summary or summary.get("salience") not in {"noticeable", "marked"}:
            return
        offers = [item for item in await self.catalog() if item.supported and item.available and item.ready and item.valid_until > utc_now()]
        if not offers or not self.supervisor.queue.empty() or time.monotonic() - self.last_activity < self.settings.quiet_seconds:
            return
        identity = "capability:" + uuid4().hex
        now = utc_now()
        row = dict(opportunity_id=identity, event_id="capability-event:" + uuid4().hex, status="queued",
            created_at=now.isoformat(), deadline_at=(now + timedelta(seconds=self.settings.deadline_seconds)).isoformat(),
            generation=self.generation, runtime_instance_id=self.supervisor.runtime_instance_id,
            activity_revision=self.activity_revision, homeostatic_summary=summary,
            offers=[item.model_dump(mode="json") for item in offers])
        self.calls.put(row)
        try:
            event = await self.supervisor.submit_event(event_type=RuntimeEventType.CAPABILITY_OPPORTUNITY,
                priority=RuntimeEventPriority.IDLE, source="homeostatic_capability", event_id=row["event_id"],
                payload={"opportunity_id":identity}, idempotency_key=identity)
            if event is None:
                raise ValueError("capability_queue_rejected")
        except Exception:
            self._finish(row, "error", error_type="queue_rejected")
            raise

    async def dispatch(self, event, process_seq):
        from harness.contracts.capability_calls import CapabilityOffer, PreparedCapabilityChoice
        from harness.contracts.decisions import PreparedResponse
        row = self.calls.get(event.payload.get("opportunity_id", ""))
        if row is None or row["event_id"] != event.event_id:
            raise ValueError("capability_event_binding_invalid")
        if row["status"] not in TERMINAL:
            if row["status"] not in {"queued", "selecting"}:
                # Once dispatched, old generation/deadline means no new effect,
                # not proof that the already-started effect did not happen.
                await self.reconcile()
                row = self.calls.get(row["opportunity_id"])
            elif invalid := self.invalid_reason(row):
                self._finish(row, invalid)
            else:
                self.active = row["opportunity_id"]
                try:
                    self._finish(row, "selecting")
                    is_goal = bool(row.get("goal_binding"))
                    envelope = self.supervisor._make_command(event, command_type="PrepareGoalDecision" if is_goal else "PrepareCapabilityCall",
                        idempotency_key="capability-select:" + row["opportunity_id"], expected_revision=self.supervisor._cognitive_snapshot.revision,
                        payload={"opportunity_id":row["opportunity_id"], "generation":row["generation"],
                            "deadline_at":row["deadline_at"], "homeostatic_summary":row["homeostatic_summary"], "offers":row["offers"],
                            **row.get("goal_request", {})})
                    envelope = envelope.model_copy(update={"deadline":min(envelope.deadline, datetime.fromisoformat(row["deadline_at"]))})
                    result = await before_deadline(self.supervisor._invoke_domain(service="cognitive", envelope=envelope,
                        method=self.supervisor.cognitive.prepare_goal_decision if is_goal else self.supervisor.cognitive.prepare_capability_call, event=event), envelope.deadline)
                    snapshot = await before_deadline(self.supervisor.cognitive.get_snapshot(), datetime.fromisoformat(row["deadline_at"]))
                    self.supervisor._validate_snapshot(snapshot, "cognitive")
                    self.supervisor._cognitive_snapshot = snapshot
                    selected = PreparedCapabilityChoice.model_validate(result.result["prepared_capability_choice" if is_goal else "decision"])
                    from harness.cognitive.capabilities import catalog_hash
                    if selected.catalog_hash != catalog_hash([CapabilityOffer.model_validate(item) for item in row["offers"]]):
                        raise ValueError("capability_catalog_binding_invalid")
                    if selected.opportunity_id != row["opportunity_id"] or selected.deadline_at > datetime.fromisoformat(row["deadline_at"]):
                        raise ValueError("capability_decision_binding_invalid")
                    row.update(decision=selected.model_dump(mode="json"), timing=result.result.get("timing", {}))
                    invalid = self.invalid_reason(row)
                    if not invalid and is_goal:
                        await self.supervisor.goals.accept(row, result, event)
                    if invalid:
                        self._finish(row, invalid)
                    elif selected.choice.kind in {"wait", "defer", "no_action"}:
                        self._finish(row, selected.choice.kind)
                    else:
                        offer = next(CapabilityOffer.model_validate(item) for item in row["offers"] if item["offer_id"] == selected.choice.offer_id)
                        row["offer"] = offer.model_dump(mode="json")
                        fresh = await self.catalog(deadline=datetime.fromisoformat(row["deadline_at"]),
                            web_scope=offer.scope.get("context_id") if offer.channel == "web" else None)
                        current = next((item for item in fresh if item.offer_id == offer.offer_id
                            and item.ready and item.available and item.supported and item.valid_until > utc_now()), None)
                        if current is None or self.binding(current) != self.binding(offer) or self.invalid_reason(row):
                            self._finish(row, "superseded")
                        elif offer.channel == "board":
                            await self.dispatch_board(row, selected, current)
                        elif offer.channel == "qq":
                            bridge = self.supervisor.qq_capability_bridge
                            row.update(status="dispatching", channel="qq")
                            self.calls.put(row)
                            bridge.enqueue({**row, "call_id":row["opportunity_id"], "capability_id":offer.capability_id,
                                "scope":offer.scope, "body_instance_id":offer.body_instance_id,
                                "offer_id":offer.offer_id, "selection_ref":selected.decision_id,
                                "arguments":selected.choice.arguments})
                            self._finish(row, "pending")
                        elif offer.channel == "voice":
                            await self.dispatch_voice(row, selected, current)
                        elif offer.channel == "web":
                            await self.dispatch_web(row, selected, current)
                        else:
                            prepared = PreparedResponse.model_validate(result.result["prepared_response"])
                            row.update(prepared_response=prepared.model_dump(mode="json"), channel="desktop", status="dispatching")
                            self.calls.put(row)
                            detail = await self.supervisor._handle_decision(event, prepared, prepared.decision)
                            action = detail.get("action")
                            if action:
                                self._finish(row, "pending", action_id=action["action_id"])
                            else:
                                self._finish(row, "defer")
                except Exception as exc:
                    diagnostic = {"error_type": type(exc).__name__}
                    if row.get("goal_binding"):
                        diagnostic["error_code"] = _GOAL_RUNTIME_ERRORS.get(str(exc), "goal_runtime_error")
                    self._finish(row, "uncertain" if row.get("status") in {"dispatching", "pending"} else "error", **diagnostic)
                    self.last_error = type(exc).__name__
                finally:
                    self.active = None
        return EventDispatchResult(event_id=event.event_id, receive_seq=event.receive_seq, process_seq=process_seq,
            status="failed" if row["status"] == "error" else "completed", decision_kind="capability_opportunity",
            detail={key:row[key] for key in ("opportunity_id", "status", "error_type", "error_code", "action_id") if key in row})

    @staticmethod
    def binding(offer):
        return (offer.provider_id, offer.body_instance_id, offer.scope, offer.activation_revision, offer.capability_id)

    async def dispatch_web(self, row, selected, offer):
        from harness.embodiment.web_contracts import WebError
        web = self.supervisor.web
        identity = "autonomous:" + row["opportunity_id"]
        row.update(channel="web", status="dispatching", web_call_id=identity)
        self.calls.put(row)
        try:
            result = await web.execute(identity, offer, selected.choice.arguments,
                deadline=datetime.fromisoformat(row["deadline_at"]), autonomous=True,
                guard=lambda: self.invalid_reason(row))
            context = web.context(offer.scope["context_id"], failures=result.failures)
            self._finish(row, "completed", evidence_refs=result.evidence_refs,
                channel_result={"status": "completed", "web_call_id": identity,
                    "effect": "external_information_only", "evidence_refs": result.evidence_refs, "observation": context})
        except Exception as exc:
            self._finish(row, "error", error_type=exc.code if isinstance(exc, WebError) else "web_dispatch_failed")

    async def dispatch_board(self, row, selected, offer):
        from harness.embodiment.board_contracts import BoardReadRequest
        board, now = self.supervisor.board, utc_now()
        if offer.capability_id == "expression.board.read.v1":
            result = await board.provider.read(BoardReadRequest(scope=board.settings.scope, requested_at=now,
                deadline=datetime.fromisoformat(row["deadline_at"]), limit=8, latest=True))
            observation = result.model_dump(mode="json")
            # A bounded authenticated read is itself an observation, including an
            # empty board. Persist its content-addressed response, not just entries.
            reference = "capability-observation:" + digest(observation)
            self._finish(row, "completed", channel="board", outcome={"observation": observation},
                evidence_refs=[reference, *[entry.receipt.evidence_ref for entry in result.entries]])
            return
        row.update(channel="board", status="dispatching")
        self.calls.put(row)
        goal_context = row.get("goal_request", {}).get("goal_context", {})
        goal_text = goal_context.get("requested_goal", {}).get("statement", "")
        purpose = row.get("goal_decision", {}).get("purpose") or (goal_context.get("active_episode") or {}).get("statement", "")
        request = {"request_id":row["opportunity_id"]}
        if goal_text:
            request["text"] = "当前持续目标：" + goal_text + ("\n当前活动：" + purpose if purpose else "")
        accepted = await before_deadline(board.request(request, trigger="homeostasis", capability_call={
            "opportunity_id":row["opportunity_id"], "selection_ref":selected.decision_id,
            "generation":row["generation"], "deadline_at":row["deadline_at"]}), datetime.fromisoformat(row["deadline_at"]))
        self._finish(row, "pending", entry_id=accepted["entry_id"])

    async def dispatch_voice(self, row, selected, offer):
        from harness.cognitive.capabilities import _bound_choice
        from harness.contracts.capability_calls import VOICE_PROFILES, VoiceCapabilityArguments
        from harness.embodiment.voice_contracts import VoiceRenderRequest
        _bound_choice(selected.choice, offer, now=utc_now())
        voice = self.supervisor.voice
        deadline = datetime.fromisoformat(row["deadline_at"])
        if offer.capability_id == VOICE_PROFILES:
            profiles = await before_deadline(voice.profiles(), deadline)
            self._finish(row, "completed", channel="voice", profiles=[
                {"voice_profile_id": item.voice_profile_id, "revision": item.revision} for item in profiles])
            return
        arguments = VoiceCapabilityArguments.model_validate(selected.choice.arguments)
        key = "voice-capability:" + digest({"opportunity_id": row["opportunity_id"], "scope": offer.scope})
        row.update(channel="voice", status="dispatching", voice_idempotency_key=key)
        self.calls.put(row)

        def before_mutation():
            from harness.runtime.voice import VoiceRuntimeError
            if self.invalid_reason(row) or offer.valid_until <= utc_now():
                raise VoiceRuntimeError("voice_capability_superseded")

        try:
            job = await before_deadline(voice.render(VoiceRenderRequest(idempotency_key=key,
                script=arguments.script, seed=arguments.seed), before_mutation=before_mutation), deadline)
        except Exception:
            attempt = voice.attempt(key)
            if attempt is None or attempt["status"] == "not_dispatched":
                self._finish(row, "rejected", error_type="voice_not_dispatched")
                return
            raise
        await self._voice_result(row, job)

    async def _voice_result(self, row, job):
        state = job.state
        values = {"voice_job_id": job.job_id, "channel_status": state}
        if job.result is not None:
            asset = job.result.asset
            # Persist the acknowledged identity before independent artifact reads.
            # Failed verification can later reconcile by GET without another POST.
            row.update(**values)
            self.calls.put(row)
            await self.supervisor.voice.verify_job_artifacts(job)
            values["evidence_refs"] = ["voice-asset:sha256:" + asset.audio_digest,
                "voice-manifest:sha256:" + asset.manifest_digest]
            values["effect"] = "audio_asset_only"
        self._finish(row, {"succeeded": "completed", "failed": "error", "cancelled": "cancelled"}.get(state, "pending"), **values)

    async def reconcile(self):
        for row in self.calls.all():
            if row["status"] in TERMINAL:
                continue
            if row["status"] in {"queued", "selecting"}:
                if row["runtime_instance_id"] != self.supervisor.runtime_instance_id or self.invalid_reason(row):
                    self._finish(row, self.invalid_reason(row) or "superseded")
                continue
            if row.get("channel") == "board":
                board = getattr(self.supervisor, "board", None)
                identity = None
                if board:
                    identity = row.get("entry_id") or "board-entry:" + hashlib.sha256((board.settings.board_id + ":" + row["opportunity_id"]).encode()).hexdigest()[:40]
                turn = board.turns.get(identity) if identity else None
                if turn:
                    status = turn["status"]
                    self._finish(row, status if status in TERMINAL else "error" if status in {"paused", "queue_failed", "not_written"} else "pending", entry_id=identity,
                        receipt=turn.get("receipt"), channel_status=status)
                elif self.invalid_reason(row):
                    self._finish(row, self.invalid_reason(row))
            elif row.get("channel") == "web":
                web = getattr(self.supervisor, "web", None)
                call = web.calls.call(row.get("web_call_id", "")) if web else None
                if call and call["status"] == "completed":
                    from harness.embodiment.web_contracts import WebResult
                    result = WebResult.model_validate_json(call["result"])
                    for source in result.sources:
                        if web.calls.source(call["scope"], source.source_ref) is None:
                            web.calls.put_source(call["scope"], source)
                    self._finish(row, "completed", evidence_refs=result.evidence_refs,
                        channel_result={"status": "completed", "web_call_id": call["id"],
                            "effect": "external_information_only", "evidence_refs": result.evidence_refs,
                            "observation": web.context(call["scope"], failures=result.failures)})
                else:
                    self._finish(row, "error", error_type="web_result_uncertain")
            elif row.get("channel") == "voice":
                voice = getattr(self.supervisor, "voice", None)
                if voice is None or row.get("offer", {}).get("scope") != voice.settings.scope.model_dump(mode="json"):
                    self._finish(row, "uncertain", error_type="voice_scope_unavailable")
                    continue
                attempt = voice.attempt(row.get("voice_idempotency_key", ""))
                if attempt is None or attempt["status"] == "not_dispatched":
                    self._finish(row, "rejected", error_type="voice_not_dispatched")
                    continue
                identity = row.get("voice_job_id") or (attempt.get("job_id") if attempt else None)
                if identity is None:
                    # Lost acknowledgement is not permission to replay a render.
                    self._finish(row, "uncertain", error_type="voice_dispatch_record_unavailable")
                    continue
                try:
                    await self._voice_result(row, await voice.job(identity))
                except Exception:
                    self._finish(row, "uncertain", voice_job_id=identity, error_type="voice_job_unavailable")
            elif row.get("channel") == "qq":
                bridge = getattr(self.supervisor, "qq_capability_bridge", None)
                result = bridge.result(row["opportunity_id"]) if bridge else None
                if result:
                    status = result.get("status", "pending")
                    status = "error" if status == "failed" else status
                    if status in TERMINAL or status == "uncertain":
                        self._finish(row, status, channel_result=result)
                elif self.invalid_reason(row):
                    self._finish(row, "uncertain", error_type="qq_dispatch_record_unavailable")
            elif row.get("channel") == "desktop":
                action = self.supervisor.action_coordinator.get(row.get("action_id", ""))
                if action is None:
                    goal = row.get("prepared_response", {}).get("decision", {}).get("goal_id")
                    context = next(((identity, item) for identity, item in self.supervisor._action_context.items()
                        if item["prepared"].decision.goal_id == goal), None)
                    if context:
                        action = self.supervisor.action_coordinator.get(context[0])
                if action and action.terminal:
                    if action.state.value == "UNKNOWN":
                        self._finish(row, "uncertain", action_id=action.action_id)
                    elif action.action_id not in self.supervisor._action_context:
                        self._finish(row, "completed" if action.state.value == "SUCCEEDED" else "rejected", action_id=action.action_id,
                            outcome=action.outcome)
                elif action and self.invalid_reason(row) and action.state.value in {"AWAITING_APPROVAL", "READY"}:
                    rejected = await self.supervisor.action_coordinator.reject(action.action_id, reason="capability opportunity expired or superseded")
                    await self.supervisor._submit_action_result(rejected)
                elif action is None and self.invalid_reason(row):
                    # Never synthesize or replay an action after uncertain dispatch.
                    self._finish(row, "uncertain")
        goals = getattr(self.supervisor, "goals", None)
        if goals is not None and goals.enabled:
            goals.reconcile()

    async def validate_action(self, action):
        row = next((row for row in self.calls.all() if row.get("action_id") == action.action_id or
            row.get("prepared_response", {}).get("decision", {}).get("goal_id") == action.goal_id), None)
        if row is None:
            return None
        reason = self.invalid_reason(row)
        if reason:
            return reason
        from harness.contracts.capability_calls import CapabilityOffer
        original = CapabilityOffer.model_validate(row["offer"])
        try:
            fresh = await self.catalog(executing_action_id=action.action_id, deadline=datetime.fromisoformat(row["deadline_at"]))
        except TimeoutError:
            return "expired"
        current = next((item for item in fresh if item.offer_id == original.offer_id), None)
        if (current is None or not current.supported or not current.available or not current.ready
                or current.valid_until <= utc_now() or self.binding(current) != self.binding(original)):
            return "capability_binding_changed"
        # Target-bearing operations require the same observed environment; a
        # fresh capture ID alone is not a change in the observed target.
        if original.capability_id not in {"app.open.v1", "process.start.v1", "control.observe.v1", "control.wait.v1"}:
            before = original.observation.get("extensions", {})
            after = current.observation.get("extensions", {})
            def stable(value):
                if isinstance(value, dict):
                    return {k:stable(v) for k,v in value.items() if k not in {"observation_id", "captured_at", "screenshot", "screenshot_ref", "a11y_ref", "a11y_evidence_ref", "evidence_refs"}}
                return [stable(x) for x in value] if isinstance(value, list) else value
            if stable(before) != stable(after):
                return "desktop_target_changed"
            observed = after.get("mistyai.desktop.v1", {})
            original_observed = before.get("mistyai.desktop.v1", {})
            spec = action.embodied_action
            if spec is None:
                return "desktop_typed_action_missing"
            arguments = dict(spec.intent.arguments)
            for name in ("selector", "source", "target"):
                selector = arguments.get(name)
                if not isinstance(selector, dict):
                    continue
                def target(view):
                    nodes = view.get("a11y_nodes", view.get("a11y_tree", {}).get("nodes", []))
                    matches = [node for node in nodes if all(
                        selector.get(key) is None or node.get(key) == selector[key]
                        for key in ("app_id", "window_id", "role", "name"))]
                    return matches[0] if len(matches) == 1 and selector.get("occurrence", 0) == 0 else None
                prior_target, fresh_target = target(original_observed), target(observed)
                if prior_target is None or fresh_target is None or stable(prior_target) != stable(fresh_target):
                    return "desktop_target_missing_or_ambiguous"
                if not observed.get("observation_id"):
                    return "desktop_observation_missing"
                arguments[name] = {**selector, "observation_id": observed["observation_id"]}
            if arguments != spec.intent.arguments:
                refreshed = action.model_copy(update={"embodied_action":spec.model_copy(update={
                    "intent":spec.intent.model_copy(update={"arguments":arguments})})})
                self.supervisor.action_coordinator.restore(refreshed)
                self.supervisor._append_journal("action_target_revalidated", idempotency_key=action.idempotency_key,
                    data={"action":refreshed.model_dump(mode="json"), "observation_refs":current.observation_refs})
        return self.invalid_reason(row)

    async def snapshot(self):
        voice = getattr(self.supervisor, "voice", None)
        web = getattr(self.supervisor, "web", None)
        return {"enabled":self.settings.enabled, "owners":self.settings.owners, "generation":self.generation,
            "qq_send_enabled":self.settings.qq_send_enabled, "last_error":self.last_error,
            "bodies": {"voice": await voice.status() if voice is not None else {"enabled": False, "body": None},
                       "web": await web.status() if web is not None else {"enabled": False, "body": None}},
            "calls":[{key:value for key,value in row.items() if key not in {"offers", "offer", "decision", "prepared_response", "homeostatic_summary"}
                      and not (row.get("channel") == "web" and key == "channel_result")}
                for row in self.calls.all()[:30]],
            "catalog":[item.model_dump(mode="json", exclude={"observation"}) for item in await self.catalog()]}

    def close(self):
        self.calls.db.close()
