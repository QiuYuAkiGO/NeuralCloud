"""Production body context and bounded scheduling over the existing Runtime journal."""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta

from harness.contracts.decisions import ActionOutcomeEvidence, GoalProposal, PreparedResponse
from harness.production.store import ProductionStore
from harness.runtime.models import EventDispatchResult, RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle, utc_now
from harness.runtime.queue import QueueClosedError


class ProductionRuntime:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.config = supervisor.manifest.production
        self.store = ProductionStore(supervisor.state_dir / "production.sqlite3", self.config)
        from harness.production.user_tasks import UserTaskRunner
        self.user_tasks = UserTaskRunner(self)
        from harness.production.tool_accounting import ToolAccounting
        self.tool_accounting = ToolAccounting(self.store)
        self._provider = None
        self._body = self.store.state("last_verified_body")
        self._body_error = "unavailable"
        self._body_lock = asyncio.Lock()
        self.epoch = 0
        self.tasks = {}
        self._timer = None

    def attach_body_provider(self, provider):
        self._provider = provider

    @property
    def provider(self):
        return self._provider or getattr(self.supervisor.embodiment, "provider", None)

    def observe(self, event):
        self.store.observe(event)

    @staticmethod
    def session(event):
        value = event.payload
        return str(value.get("group_id") or value.get("session_id") or "owner")

    async def refresh_body(self):
        async with self._body_lock:
            try:
                provider = self.provider
                if provider is None or not callable(getattr(provider, "snapshot", None)):
                    raise ValueError("production_body_unavailable")
                value = provider.snapshot()
                value = await asyncio.wait_for(value, self.config.body_timeout_seconds) if inspect.isawaitable(value) else value
                body = value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)
                if (body.get("physiology_mode") != "production_v1" or not body.get("episode_id")
                        or body.get("sample_sequence") is None):
                    raise ValueError("production_body_contract_required")
                expected = self.supervisor.manifest.embodiment.body_instance_id
                if expected and body.get("body_instance_id") != expected:
                    raise ValueError("production_body_identity_mismatch")
                if (self._body and body.get("episode_id") == self._body.get("episode_id")
                        and body.get("sample_sequence", 0) < self._body.get("sample_sequence", 0)):
                    raise ValueError("production_body_sample_regressed")
                captured = datetime.fromisoformat(body["captured_at"])
                if not -2 <= (utc_now() - captured).total_seconds() <= self.config.body_max_age_seconds:
                    raise ValueError("production_body_stale")
                self._body, self._body_error = body, None
                self.store.put_state("last_verified_body", body)
                for request in self.store.unresolved_rest():
                    if (request["body_instance_id"], request["episode_id"]) == (body["body_instance_id"], body["episode_id"]):
                        self.store.put_state("rest-request:" + request["command_id"], {**request,
                            "observed_current_state": {"captured_at": body["captured_at"], "resting": body.get("resting"),
                                "ledger_revision": body.get("ledger_revision"), "evidence_refs": body.get("evidence_refs", [])}})
            except Exception as exc:
                # Retain previous evidence for diagnostics, never advertise it as fresh.
                self._body_error = type(exc).__name__
            return self._body

    def _body_available(self):
        if self._body_error or self._body is None:
            return False
        captured = datetime.fromisoformat(self._body["captured_at"])
        return -2 <= (utc_now() - captured).total_seconds() <= self.config.body_max_age_seconds

    async def context(self, scope, *, autonomous=False):
        if not self._body_available():
            await self.refresh_body()
        context = self.context_snapshot(scope, autonomous=autonomous)
        declared = getattr(getattr(self.supervisor.embodiment, "provider", None), "supported_intents", ())
        if callable(declared):
            declared = declared()
        if inspect.isawaitable(declared):
            declared = await declared
        context["authorized_action_context"]["provider_intents"] = list(declared or ())
        return context

    def context_snapshot(self, scope, *, autonomous=False):
        """A nonblocking frozen projection; absent fresh facts remain absent."""
        available = self._body_available()
        body = self._body or {}
        exhausted = bool(body.get("exhausted") or body.get("energy", {}).get("terminal"))
        return {"schema_version": "production-context-v1", "scope": str(scope),
                "runtime_lifecycle": self.supervisor.lifecycle.value,
                "configuration": self.config.model_dump(mode="json"), "autonomous": bool(autonomous),
                "body": {"available": available, "error": self._body_error or ("expired" if not available else None),
                         "snapshot": body if available else {}},
                "last_known_body_identity": {"body_instance_id": body.get("body_instance_id"), "episode_id": body.get("episode_id")},
                "restricted_dialogue": not available or exhausted or bool(body.get("resting")),
                "budget": self.store.budget(), "goals": self.store.goals(audience=str(scope)), "epoch": self.epoch,
                "authorized_action_context": {"provider_intents": [],
                    "permitted_intents": list(self.config.permitted_intents),
                    "body_instance_id": body.get("body_instance_id"),
                    "runtime_instance_id": self.supervisor.runtime_instance_id}}

    async def rest(self, command_id, *, resting, deadline=None):
        from harness.embodiment.contracts import BodyRestCommand, BodyRestReceipt
        key = "rest-request:" + command_id
        old = self.store.state(key)
        if old:
            if old["resting"] != resting:
                raise ValueError("production_rest_identity_conflict")
            if old["status"] == "succeeded":
                return BodyRestReceipt.model_validate(old["receipt"])
            raise ValueError("production_rest_outcome_unknown")
        context = await self.context("owner")
        if not context["body"]["available"]:
            raise ValueError("production_body_unavailable")
        body = context["body"]["snapshot"]
        command = BodyRestCommand(command_id=command_id, body_instance_id=body["body_instance_id"], episode_id=body["episode_id"])
        deadline = deadline or utc_now() + timedelta(seconds=self.config.body_timeout_seconds)
        if deadline <= utc_now():
            raise TimeoutError("production_rest_expired")
        request = {"command_id": command_id, "body_instance_id": body["body_instance_id"], "episode_id": body["episode_id"],
                   "resting": resting, "deadline_at": deadline.isoformat(), "status": "dispatching", "requested_at": utc_now().isoformat()}
        self.store.put_state(key, request)
        try:
            receipt = await asyncio.wait_for(self.provider.start_rest(command) if resting else self.provider.end_rest(command),
                min(self.config.body_timeout_seconds, max(0, (deadline - utc_now()).total_seconds())))
            if (receipt.command_id != command_id or receipt.body_instance_id != command.body_instance_id
                    or receipt.episode_id != command.episode_id or receipt.resting != resting):
                raise ValueError("production_rest_receipt_mismatch")
            self.store.put_state(key, {**request, "status": "succeeded", "receipt": receipt.model_dump(mode="json")})
        except BaseException:
            self.store.put_state(key, {**request, "status": "unknown"})
            raise
        # An idempotent rest receipt may carry an old snapshot. Its effect is
        # authoritative, but it must never roll the live body projection back.
        await self.refresh_body()
        return receipt

    async def flush_usage(self):
        # A usage request can change energy even if its response is lost. Keep
        # capture and settlement ordered, and never expose the pre-cost cache
        # while waiting to learn the new authoritative state.
        async with self._body_lock:
            attempted = await self._flush_usage_locked()
        if attempted:
            await self.refresh_body()

    async def assign_usage(self, request):
        replay = self.store.usage_assignment(request)
        if replay is not None:
            return replay
        await self.refresh_body()
        if not self._body_available():
            raise ValueError("production_usage_assignment_body_unavailable")
        if any(self._body.get(key) != request[key] for key in ("body_instance_id", "episode_id")):
            raise ValueError("production_usage_assignment_target_mismatch")
        return self.store.assign_usage(request, verified_body=dict(self._body))

    async def begin_tool_operation(self, channel, operation_id, capability):
        context = await self.context("owner")
        binding = context["body"]["snapshot"] or context["last_known_body_identity"]
        return self.tool_accounting.begin(channel, operation_id, capability, binding=binding)

    def mark_tool_dispatched(self, channel, operation_id):
        self.tool_accounting.dispatched(channel, operation_id)

    def finish_tool_operation(self, channel, operation_id, *, outcome, evidence_refs=()):
        row = self.tool_accounting.get(channel, operation_id)
        if row is not None and row["status"] == "prepared" and outcome != "completed":
            self.tool_accounting.cancel(channel, operation_id)
            return
        self.tool_accounting.finish(channel, operation_id, outcome=outcome, evidence_refs=evidence_refs)

    def cancel_tool_operation(self, channel, operation_id):
        self.tool_accounting.cancel(channel, operation_id)

    async def _flush_usage_locked(self):
        from harness.embodiment.contracts import CognitiveUsage
        if not self._body_available() or not callable(getattr(self.provider, "record_usage", None)):
            return False
        attempted = False
        for entry in self.store.pending_usage(body_instance_id=self._body.get("body_instance_id"), episode_id=self._body.get("episode_id")):
            usage = entry["usage"]
            binding = entry.get("assignment") or usage
            # Body identity must be frozen by the caller. A later episode cannot
            # absorb costs whose original authority is unknown.
            if not binding.get("body_instance_id") or not binding.get("episode_id"):
                continue
            if (binding["body_instance_id"], binding["episode_id"]) != (
                self._body.get("body_instance_id"), self._body.get("episode_id")
            ):
                continue
            try:
                raw = usage.get("raw_usage") if usage.get("source") in {"api", "provider"} else None
                raw = {k: v for k, v in raw.items() if isinstance(k, str) and type(v) is int and v >= 0} if isinstance(raw, dict) else {}
                tokens = raw.get("total_tokens")
                if tokens is None:
                    for prompt, completion in (("prompt_tokens", "completion_tokens"), ("input_tokens", "output_tokens"),
                            ("prompt_eval_count", "eval_count")):
                        if prompt in raw and completion in raw:
                            tokens = raw[prompt] + raw[completion]
                            break
                actual = type(tokens) is int and tokens > 0
                record = CognitiveUsage.model_validate({"inference_id": entry["attempt_id"],
                    "body_instance_id": binding["body_instance_id"], "episode_id": binding["episode_id"],
                    "model_class": str(usage.get("model_class") or "production"),
                    "cost_class": str(usage.get("cost_class") or "inference"),
                    "quantity": tokens if actual else 1, "metering": "tokens" if actual else "call", "source": "cognition",
                    "raw_usage": raw or None})
                if usage.get("source") == "delivery":
                    record = record.model_copy(update={"model_class": "protocol", "cost_class": "protocol_delivery",
                        "quantity": 1, "metering": "operation", "source": "delivery", "raw_usage": None})
                elif usage.get("source") == "tool":
                    record = record.model_copy(update={"model_class": "tool", "cost_class": "tool_execution",
                        "quantity": 1, "metering": "operation", "source": "tool", "raw_usage": None})
                attempted = True
                self._body_error = "usage_settlement_pending_refresh"
                receipt = await asyncio.wait_for(self.provider.record_usage(record), self.config.body_timeout_seconds)
                self.store.ack_usage(entry["usage_id"], receipt.model_dump(mode="json"))
            except Exception:
                # Retry the same inference id; a lost response is not a new cost.
                break
        return attempted

    def _result(self, event, process_seq, kind="no_action", **detail):
        if detail.get("reason"):
            self.store.put_state("last_reason", str(detail["reason"]))
        return EventDispatchResult(event_id=event.event_id, receive_seq=event.receive_seq,
            process_seq=process_seq, status="completed", decision_kind=kind, detail=detail)

    def status(self):
        body = self._body or {}
        captured = datetime.fromisoformat(body["captured_at"]) if body.get("captured_at") else None
        available = self._body_available()
        return {"schema_version": "production-status-v1", "body_available": available,
                "body_age_seconds": max(0, (utc_now() - captured).total_seconds()) if captured else None,
                "body_identity": {"body_instance_id": body.get("body_instance_id"), "episode_id": body.get("episode_id")},
                "body_error": self._body_error,
                "resting": body.get("resting") if available else None,
                "exhausted": body.get("exhausted") if available else None,
                "energy": body.get("energy") if available else None,
                "sample_sequence": body.get("sample_sequence") if available else None,
                "ledger_revision": body.get("ledger_revision") if available else None,
                "budget": self.store.budget(), "goals": self.store.goals(), "usage_pending": self.store.pending_usage_count(),
                "usage_unassigned": self.store.unassigned_usage_count(),
                "rest_pending": len(self.store.unresolved_rest()),
                "tasks": len(self.tasks), "queued": self.supervisor.queue.qsize(),
                "user_tasks": self.user_tasks.store.summary(),
                "user_task_error": self.user_tasks.last_error,
                "last_reason": self.store.state("last_reason")}

    async def dispatch(self, event, process_seq):
        s = self.supervisor
        if event.event_type in {RuntimeEventType.BODY_STATE, RuntimeEventType.AGENCY_TERMINAL}:
            await self.refresh_body()
            return self._result(event, process_seq, "production_body", available=self._body_available())
        if event.event_type in {RuntimeEventType.QQ_FACT, RuntimeEventType.PERCEPTION, RuntimeEventType.DIAGNOSTIC}:
            return self._result(event, process_seq, "production_observation")
        if event.event_type == RuntimeEventType.HEARTBEAT:
            s._last_heartbeat_at = utc_now()
            await self.refresh_body()
            self.collect_deliveries()
            await self.flush_usage()
            await s.capabilities.heartbeat()
            await self.schedule_opportunity()
            return self._result(event, process_seq, "production_heartbeat")
        autonomous = event.event_type != RuntimeEventType.USER_MESSAGE
        context = await self.context(self.session(event), autonomous=autonomous)
        if autonomous and (not self.config.autonomous_enabled or not context["body"]["available"] or not context["budget"]["remaining"]):
            return self._result(event, process_seq, reason="production_autonomy_unavailable")
        epoch = self.epoch
        deadline = event.received_at + timedelta(seconds=s.command_timeout_seconds)
        if deadline <= utc_now():
            return self._result(event, process_seq, reason="production_request_expired")
        payload = {"mode": "production", "production_context": context,
                   "text": str(event.payload.get("text") or ""), "session_id": self.session(event),
                   "channel": str(event.payload.get("channel") or "runtime"), "source_time": event.source_time}
        envelope = s._make_command(event, command_type="PrepareResponse", idempotency_key="production:prepare:" + event.event_id,
                                   expected_revision=None, payload=payload)
        envelope = envelope.model_copy(update={"deadline": min(envelope.deadline, deadline)})
        result = await s._invoke_domain(service="cognitive", envelope=envelope, method=s.cognitive.prepare_response, event=event)
        prepared = result.result
        if not isinstance(prepared, PreparedResponse):
            prepared = PreparedResponse.model_validate(prepared)
        if epoch != self.epoch or s.lifecycle != RuntimeLifecycle.RUNNING:
            return self._result(event, process_seq, reason="production_result_fenced")
        effects = getattr(prepared, "production_effects", None) or {}
        goal_update = effects.get("goal_update")
        parent_goal = None
        if goal_update is not None:
            body_refs = context["body"]["snapshot"].get("evidence_refs", []) if context["body"]["available"] else []
            references = [item.get("ref", "") if isinstance(item, dict) else str(item) for item in body_refs]
            references.append(event.event_id)
            references.extend(effects.get("verified_evidence_refs", []))
            existing = self.store.goal(goal_update.get("goal_id"), audience=context["scope"]) if goal_update.get("goal_id") else None
            if existing:
                references.extend(existing.get("outcomes", []))
                references.extend(existing.get("evidence_refs", []))
            parent_goal = self.store.apply_goal_update(goal_update, event_id=event.event_id, verified_evidence=references, audience=context["scope"])
        if isinstance(prepared.decision, GoalProposal):
            if prepared.decision.policy_id in {"production:rest:start", "production:rest:end"}:
                receipt = await self.rest(prepared.decision.goal_id, resting=prepared.decision.policy_id.endswith(":start"), deadline=deadline)
                evidence = ActionOutcomeEvidence(prepare_id=prepared.prepare_id, goal_id=prepared.decision.goal_id,
                    observed_at=utc_now(), success=True, summary=f"explicit rest state verified by body authority: resting={receipt.resting}",
                    evidence_refs=[item.ref if hasattr(item, "ref") else str(item) for item in receipt.evidence_refs])
                await s._commit_prepared(event, prepared, evidence)
                if parent_goal is not None:
                    self.store.record_goal_outcome(parent_goal["id"], evidence_id=receipt.command_id, progress=True,
                        result={"status": "completed", "channel": "body", "effect": "explicit_rest", "evidence_refs": evidence.evidence_refs})
                return self._result(event, process_seq, "production_rest", resting=receipt.resting)
            if context["restricted_dialogue"]:
                return self._result(event, process_seq, reason="production_body_restricted")
            if parent_goal is not None:
                self.store.put_state("action-goal:" + prepared.decision.goal_id, parent_goal["id"])
            else:
                self.store.save_goal(prepared.decision.goal_id, prepared.decision.model_dump(mode="json"), audience=context["scope"])
        detail = await s._handle_decision(event, prepared, prepared.decision)
        if parent_goal is not None and parent_goal["status"] == "active" and not isinstance(prepared.decision, GoalProposal):
            self.store.record_goal_outcome(parent_goal["id"], evidence_id="review:" + event.event_id, progress=False)
        return self._result(event, process_seq, "production_decision", **detail)

    def bind_qq_decision(self, result, context):
        if isinstance(result, dict) and isinstance(result.get("decision"), dict):
            identity = result["decision"].get("decision_id")
            body = context.get("body", {}).get("snapshot") or context.get("last_known_body_identity") or {}
            if identity:
                self.store.put_state("qq-decision-body:" + identity, {
                    "body_instance_id": body.get("body_instance_id"), "episode_id": body.get("episode_id")})

    def collect_deliveries(self):
        bridge = getattr(self.supervisor, "qq_capability_bridge", None)
        if bridge is None or not callable(getattr(bridge, "verified_deliveries", None)):
            return
        for sequence, receipt in bridge.verified_deliveries(after=self.store.state("qq-delivery-cursor", 0)):
            if receipt is not None:
                binding = self.store.state("qq-decision-body:" + receipt.decision_id)
                if binding is not None:
                    self.store.record_delivery(receipt.receipt_id, **binding)
            self.store.put_state("qq-delivery-cursor", sequence)

    async def schedule_opportunity(self):
        s = self.supervisor
        if not self.config.autonomous_enabled or s.lifecycle != RuntimeLifecycle.RUNNING:
            return
        context = await self.context("owner", autonomous=True)
        if not context["body"]["available"] or not context["budget"]["remaining"]:
            return
        now = utc_now()
        last = self.store.state("last_opportunity_at")
        signal = self.store.state("signal_revision", 0)
        new_signal = signal > self.store.state("last_opportunity_signal", 0)
        due = sorted((goal["id"], goal["deadline_at"]) for goal in self.store.goals(audience="owner")
                     if goal["status"] == "active" and goal.get("deadline_at") and datetime.fromisoformat(goal["deadline_at"]) <= now)
        due = [list(item) for item in due]
        new_deadline = bool(due) and due != self.store.state("last_due_goals", [])
        if last and not new_signal and not new_deadline and (now - datetime.fromisoformat(last)).total_seconds() < self.config.autonomous_review_seconds:
            return
        if any(event.source == "production_opportunity" for event in s.queue.snapshot()) or any(
            event.source == "production_opportunity" for event in self.tasks.values()
        ):
            return
        await s.submit_event(event_type=RuntimeEventType.IDLE, priority=RuntimeEventPriority.IDLE,
            source="production_opportunity", payload={"session_id": "owner", "channel": "runtime"})
        self.store.put_state("last_opportunity_at", now.isoformat())
        self.store.put_state("last_opportunity_signal", signal)
        self.store.put_state("last_due_goals", due)

    async def _produce_heartbeats(self):
        s = self.supervisor
        while s.lifecycle not in {RuntimeLifecycle.STOPPED, RuntimeLifecycle.STOPPING}:
            if s.lifecycle == RuntimeLifecycle.RUNNING:
                self.user_tasks.tick()
                scheduled = s.scheduler.make_due_event()
                if scheduled:
                    await s.submit_event(**scheduled)
            remaining = s.scheduler.seconds_until_deadline()
            await asyncio.sleep(min(1, max(.01, remaining)) if remaining is not None else .1)

    def _eligible(self, event):
        from harness.runtime.models import CONTROL_EVENT_TYPES
        if not self.supervisor._dispatch_predicate()(event):
            return False
        if event.event_type in CONTROL_EVENT_TYPES:
            return True
        if event.event_type == RuntimeEventType.HEARTBEAT:
            return not any(e.event_type == RuntimeEventType.HEARTBEAT for e in self.tasks.values())
        work = [e for e in self.tasks.values() if e.event_type != RuntimeEventType.HEARTBEAT]
        return len(work) < self.config.foreground_slots and self.session(event) not in {self.session(e) for e in work}

    async def run(self):
        from harness.runtime.models import CONTROL_EVENT_TYPES
        s = self.supervisor
        self._timer = asyncio.create_task(self._produce_heartbeats(), name="production-heartbeat")
        try:
            while s.lifecycle != RuntimeLifecycle.STOPPED:
                try:
                    event = await s.queue.get(predicate=self._eligible)
                except QueueClosedError:
                    break
                if event.event_type in CONTROL_EVENT_TYPES:
                    await s.dispatch(event)
                    continue
                # The input remains pending in recovery until dispatch emits its
                # terminal processing_completed, never just because a task starts.
                record = s._append_journal("job_scheduled", event=event,
                    data={"session": self.session(event), "epoch": self.epoch})
                if record is None:
                    await s.queue.put(event)
                    await s.pause(reason="production job scheduling requires durable journal")
                    continue
                task = asyncio.create_task(s.dispatch(event), name="production-job:" + event.event_id)
                self.tasks[task] = event
                task.add_done_callback(self._finished)
        finally:
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)
            await self.cancel_jobs()

    def _finished(self, task):
        self.tasks.pop(task, None)
        if not task.cancelled():
            task.exception()
        asyncio.create_task(self.supervisor.queue.wake())

    async def cancel_jobs(self):
        self.epoch += 1
        await self.user_tasks.close()
        bridge = getattr(self.supervisor, "qq_capability_bridge", None)
        if bridge is not None and callable(getattr(bridge, "revoke_contacts", None)):
            bridge.revoke_contacts()
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
