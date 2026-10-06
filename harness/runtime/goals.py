"""Persistent production goals over the shared capability execution boundary.

The standing goal owns no personality or physiology. It schedules bounded
episodes; Cognitive proposes, channel authorities prove effects, Homeostasis
alone updates psychological state. Interrupted calls are reconciled, never run
again merely because a goal or process acquired a new identity.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import time
from uuid import uuid4

from harness.regulation.store import FINISHED, PSYCHOLOGICAL_DIMENSIONS, STANDING_ID, GoalOperation, GoalSubmission, RegulationStore
from harness.runtime.capabilities import GOAL_RUNTIME_ERROR_CODES, TERMINAL, before_deadline, digest
from harness.runtime.models import RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle, utc_now


def _when(value):
    return datetime.fromisoformat(value) if value else None


_OUTCOME_CONTEXT_FIELDS = ("call_id", "status", "evidence_refs", "at", "channel", "error_code", "step_id")


def _outcome_projection(row):
    """Keep status and evidence refs; drop receipt bodies from the model context."""
    if not isinstance(row, dict):
        return row
    projected = {key: row[key] for key in _OUTCOME_CONTEXT_FIELDS if key in row}
    feedback = (row.get("channel_result") or {}).get("information_feedback")
    if feedback is not None:
        projected["information_feedback"] = feedback
    return projected


def _review_outcomes(episode):
    """Keep recent results and one independent receipt for each executed step."""
    episode = episode or {}
    results = episode.get("results", [])
    indices = set(range(max(0, len(results) - 16), len(results)))
    for step in episode.get("steps", []):
        if step.get("status") != "executed":
            continue
        refs = set(step.get("evidence_refs", [])[:1])
        for i, outcome in enumerate(results):
            if outcome.get("status") in {"completed", "committed"} and refs.intersection(outcome.get("evidence_refs", [])):
                indices.add(i)
                break
    return [_outcome_projection(results[i]) for i in sorted(indices)]


def _episode_context(episode):
    """Project bounded recent outcomes, keeping the complete durable ledger."""
    if episode is None:
        return None
    projected = {}
    for key, value in episode.items():
        if key in {"seen_calls", "baseline", "last_evaluation", "last_observation"}:
            continue
        if key == "results":
            projected[key] = _review_outcomes(episode)
        elif key == "appraisals":
            projected[key] = list(value or [])[-8:]
        elif key == "predictions":
            projected[key] = list(value or [])[-8:]
        else:
            projected[key] = value
    return projected


def _refs(value):
    """Only known receipt/evidence fields, never arbitrary model text."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_refs" and isinstance(item, list):
                found.extend(v if isinstance(v, str) else v.get("ref", "") for v in item)
            elif key == "evidence_ref" and isinstance(item, str):
                found.append(item)
            elif key in {"receipt", "outcome", "channel_result"}:
                found.extend(_refs(item))
    return sorted(set(v for v in found if isinstance(v, str) and v))


def deviation(snapshot, dimensions):
    snapshot = snapshot.get("snapshot", snapshot)
    total = 0.0
    for name in dimensions:
        factor = snapshot.get("dimensions", {}).get(name, {})
        value = factor.get("value")
        band = factor.get("target_band")
        if value is None or not band:
            continue
        total += max(band[0] - value, 0, value - band[1])
    return total / max(1, len(dimensions))


class GoalController:
    def __init__(self, supervisor, state_dir, *, clock=utc_now):
        self.supervisor, self.clock = supervisor, clock
        self.config = supervisor.manifest.goal_regulation
        self.store = RegulationStore(state_dir / "regulation.sqlite3")
        self._closed = False
        supervisor.add_listener(self.on_event)

    @property
    def enabled(self):
        return self.config.enabled

    def _save(self, kind, item):
        self.store.put(kind, item["id"], item)
        return item

    def _goals(self):
        return sorted((g for g in self.store.all("goal") if g["id"] != STANDING_ID), key=lambda g: g["created_at"])

    def _episodes(self):
        return sorted(self.store.all("episode"), key=lambda e: e["created_at"])

    def _state(self):
        return self.store.get("state", "scheduler") or {"calls": [], "evidence_refs": [], "signal_revision": 0}

    def _put_state(self, state):
        self.store.put("state", "scheduler", state)

    def _request_version(self, goal):
        from harness.regulation.context import REQUEST_CONTEXT_VERSION
        return digest([REQUEST_CONTEXT_VERSION, self.config.model_dump(mode="json"), goal["revision"]])

    def request_fault(self, goal):
        fault = self.store.get("request_fault", goal["id"])
        return fault if fault and fault["version"] == self._request_version(goal) else None

    def _record_request_fault(self, call, goal):
        if goal and call.get("error_code") in {"goal_invalid_request", "goal_context_too_large", "goal_invalid_appraisal_evidence",
                "goal_invalid_known_evidence", "goal_active_episode_binding_mismatch", "goal_snapshot_binding_mismatch"}:
            self.store.put("request_fault", goal["id"], {"goal_id": goal["id"], "code": call["error_code"],
                "at": call.get("completed_at", self.clock().isoformat()), "version": self._request_version(goal),
                "recovery": "request_contract_or_configuration_change"})

    def health(self):
        faults = [fault for goal in [self.store.get("goal", STANDING_ID), *self._goals()]
                  if goal and goal["status"] not in FINISHED and (fault := self.request_fault(goal))]
        return {"status": "degraded" if faults else "available", "request_faults": faults}

    def _expire_autonomous_episode(self, episode, now):
        if (episode["goal_id"] != STANDING_ID or episode["status"] in FINISHED | {"paused"}
                or episode.get("unresolved_call")):
            return False
        if any(c.get("goal_binding", {}).get("episode_id") == episode["id"] and c["status"] not in TERMINAL
               for c in self.supervisor.capabilities.calls.all()):
            return False
        expired = (now - _when(episode["created_at"])).total_seconds() >= self.config.maximum_autonomous_episode_seconds
        exhausted = len(episode.get("seen_calls", [])) >= self.config.maximum_autonomous_episode_attempts
        if not expired and not exhausted:
            return False
        episode.update(status="abandoned", ended_at=now.isoformat(), blocked_reason=None,
            end_reason="episode_time_limit" if expired else "episode_attempt_limit", causal_effect_verified=False)
        goal = self.store.get("goal", STANDING_ID)
        goal.update(status="observing", next_review_at=(now + timedelta(
            seconds=self.supervisor.capabilities.settings.opportunity_seconds)).isoformat())
        with self.store.objects.atomic():
            self._save("episode", episode)
            self._save("goal", goal)
        return True

    def budget(self, *, now=None):
        now = now or self.clock()
        calls = [_when(v) for v in self._state()["calls"] if (now - _when(v)).total_seconds() < 3600]
        next_at = now
        if calls:
            next_at = max(next_at, calls[-1] + timedelta(seconds=self.config.minimum_reconsideration_interval))
        attention = getattr(self.supervisor, "attention_controller", None)
        maximum = self.config.maximum_model_calls_per_hour
        if attention is not None and attention.enabled:
            maximum = max(0, min(12, maximum) - 4)
        if maximum == 0:
            next_at = max(next_at, now + timedelta(hours=1))
        elif len(calls) >= maximum:
            next_at = max(next_at, calls[0] + timedelta(hours=1))
        return {"calls_last_hour": len(calls), "maximum_calls_per_hour": maximum,
            "next_available_at": next_at.isoformat()}

    async def on_event(self, topic, payload):
        if not self.enabled or self._closed or topic != "event.accepted":
            return
        kind = payload.get("event_type")
        content = payload.get("payload", {})
        incoming = kind == "user_message" or kind == "qq_turn" and not content.get("idle_intent")
        facts = kind == "qq_fact" and content.get("qq_fact", {}).get("kind") in {"verified_message", "verified_interaction"}
        if incoming or facts or kind == "environment_change":
            state = self._state()
            state["signal_revision"] += 1
            # User events are attributable statements, not proof of positive support.
            references = [payload["event_id"]] if incoming else _refs(content.get("qq_fact", {}))
            state["evidence_refs"] = list(dict.fromkeys([*state["evidence_refs"], *references]))[-64:]
            self._put_state(state)
        if incoming:
            for episode in self._episodes():
                if episode["status"] not in FINISHED and episode.get("blocked_reason") != "user_paused":
                    episode.update(status="waiting", blocked_reason="user_interrupted",
                        next_review_at=(self.clock() + timedelta(seconds=self.supervisor.capabilities.settings.quiet_seconds)).isoformat())
                    self._save("episode", episode)

    async def submit(self, payload):
        if not self.enabled:
            raise ValueError("goal regulation is disabled in the active manifest")
        request = GoalSubmission.model_validate(payload)
        existed = self.store.get("goal", "goal:" + digest(request.request_id)[:32])
        result = self.store.submit(request, self.clock())
        if not existed:
            self.supervisor.capabilities.activity_revision += 1
            # Higher-priority explicit goals preserve the autonomous episode.
            for episode in self._episodes():
                if episode["goal_id"] == STANDING_ID and episode["status"] not in FINISHED:
                    episode.update(status="waiting", blocked_reason="user_goal_preemption")
                    self._save("episode", episode)
            current = self.supervisor.action_coordinator.current
            if current is not None and any(r.get("action_id") == current.action_id and
                    r.get("goal_binding", {}).get("goal_id") == STANDING_ID for r in self.supervisor.capabilities.calls.all()):
                await self.supervisor.action_coordinator.request_preemption(reason="explicit goal preempts regulation activity")
            self.supervisor._append_journal("production_goal_submitted", data={"goal": result})
        return result

    async def operate(self, identity, operation, request_id):
        replay = self.store.get("operation", digest([identity, request_id])) is not None
        result = self.store.operation(identity, operation, request_id, self.clock())
        if replay:
            return result
        self.supervisor.capabilities.activity_revision += 1
        if operation in {"pause", "cancel"}:
            current = self.supervisor.action_coordinator.current
            if current is not None:
                row = next((r for r in self.supervisor.capabilities.calls.all()
                    if r.get("action_id") == current.action_id and r.get("goal_binding", {}).get("goal_id") == identity), None)
                if row:
                    await self.supervisor.action_coordinator.request_preemption(reason="goal " + operation)
        self.supervisor._append_journal("production_goal_control", data={"goal_id": identity, "operation": operation, "request_id": request_id})
        return result

    def invalid_reason(self, row):
        binding = row.get("goal_binding")
        if not binding:
            return None
        goal = self.store.get("goal", binding["goal_id"])
        if not self.enabled or goal is None or goal["status"] in FINISHED | {"paused"}:
            return "goal_inactive"
        if goal["revision"] != binding["goal_revision"]:
            return "goal_revision_changed"
        if goal["id"] == STANDING_ID and any(g["status"] not in FINISHED | {"paused"} for g in self._goals()):
            return "explicit_goal_has_priority"
        episode = self.store.get("episode", binding["episode_id"])
        if episode and episode["status"] in FINISHED | {"paused"}:
            return "episode_inactive"
        return None

    def _active(self, goal_id):
        return next((e for e in self._episodes() if e["goal_id"] == goal_id and e["status"] not in FINISHED), None)

    def _has_work_ahead(self):
        # A slow/unavailable provider may take longer than one heartbeat. A
        # routine tick queued during discovery must not starve every goal.
        return any(event.event_type != RuntimeEventType.HEARTBEAT
                   for event in self.supervisor.queue.snapshot())

    def _observe_salience(self):
        # Wake on hysteretic psychological transitions, never on each floating
        # point drift or snapshot revision. Energy is outside this controller.
        summary = self.supervisor._response_homeostatic_interoception() or {}
        signature = sorted(
            (n.get("dimension"), n.get("direction")) for n in summary.get("dominant_needs", [])
            if n.get("dimension") in PSYCHOLOGICAL_DIMENSIONS)
        signature = digest(signature)
        state = self._state()
        if state.get("psychological_signature") != signature:
            if "psychological_signature" in state:
                state["signal_revision"] += 1
            state["psychological_signature"] = signature
            self._put_state(state)

    async def heartbeat(self):
        if not self.enabled:
            return
        cap, now = self.supervisor.capabilities, self.clock()
        self.reconcile()
        for episode in self._episodes():
            self._expire_autonomous_episode(episode, now)
        self.observe(self.supervisor._homeostasis_snapshot.payload)
        self._observe_salience()
        if (self.supervisor.lifecycle != RuntimeLifecycle.RUNNING or cap.active
                or self._has_work_ahead() or self.supervisor.action_coordinator.pending()
                or time.monotonic() - cap.last_activity < cap.settings.quiet_seconds):
            return
        if any(row["status"] not in TERMINAL for row in cap.calls.all()):
            return
        goal = next((g for g in self._goals() if g["status"] not in FINISHED | {"paused"}), None)
        if goal is None:
            goal = self.store.get("goal", STANDING_ID)
            if not self.config.autonomous_enabled or goal["status"] in FINISHED | {"paused"}:
                return
        episode = self._active(goal["id"])
        if self.request_fault(goal):
            return
        state = self._state()
        if (not episode and goal.get("blocked_reason") == "no_progress"
                and goal.get("blocked_signal_revision") == state["signal_revision"]):
            return
        if episode:
            if episode["status"] == "paused" or episode.get("unresolved_call"):
                return
            if episode.get("blocked_reason") == "no_progress" and episode.get("blocked_signal_revision") == state["signal_revision"]:
                return
            if _when(episode.get("next_review_at")) and now < _when(episode["next_review_at"]):
                return
        elif goal["id"] == STANDING_ID:
            if _when(goal.get("next_review_at")) and now < _when(goal["next_review_at"]):
                return
            summary = self.supervisor._response_homeostatic_interoception() or {}
            if summary.get("salience") not in {"noticeable", "marked", "critical"}:
                return
            # Energy alone cannot initiate a psychological regulation episode.
            if deviation(self.supervisor._homeostasis_snapshot.payload, PSYCHOLOGICAL_DIMENSIONS) <= 0:
                return
        available_at = _when(self.budget(now=now)["next_available_at"])
        if now < available_at:
            if episode:
                episode.update(status="waiting", blocked_reason="budget", next_review_at=available_at.isoformat())
                self._save("episode", episode)
            return
        offers = await cap.catalog(context_scope="goal:" + goal["id"])
        # Keep existing expression cooldowns without delaying a desktop plan's
        # independent next step by a full autonomous-opportunity interval.
        cooldown = set()
        for call in cap.calls.all():
            offer = call.get("offer", {})
            if offer.get("channel") in {"board", "qq", "voice"} and call.get("decision", {}).get("choice", {}).get("kind") == "act":
                if (now - _when(call["created_at"])).total_seconds() < cap.settings.opportunity_seconds:
                    cooldown.add(offer.get("capability_id"))
        offers = [o for o in offers if o.supported and o.available and o.ready and o.valid_until > now and o.capability_id not in cooldown]
        if self._has_work_ahead() or time.monotonic() - cap.last_activity < cap.settings.quiet_seconds:
            return
        identity, episode_id = "capability:" + uuid4().hex, episode["id"] if episode else "episode:" + uuid4().hex
        snapshot = self.supervisor._homeostasis_snapshot
        used_appraisal_refs = {ref for e in self._episodes() for a in e.get("appraisals", []) for ref in a["evidence_refs"]}
        state_snapshot = snapshot.payload.get("snapshot", snapshot.payload)
        from harness.regulation.context import select_evidence
        try:
            refs, appraisal_refs = select_evidence(snapshot=state_snapshot, goal=goal, episode=episode, offers=offers,
                fresh_refs=state["evidence_refs"], used_refs=used_appraisal_refs)
        except ValueError:
            self._record_request_fault({"error_code": "goal_context_too_large"}, goal)
            return
        binding = {"goal_id": goal["id"], "goal_revision": goal["revision"], "episode_id": episode_id}
        row = dict(opportunity_id=identity, event_id="capability-event:" + uuid4().hex, status="queued", created_at=now.isoformat(),
            deadline_at=(now + timedelta(seconds=cap.settings.deadline_seconds)).isoformat(), generation=cap.generation,
            runtime_instance_id=self.supervisor.runtime_instance_id, activity_revision=cap.activity_revision,
            homeostatic_summary=self.supervisor._response_homeostatic_interoception() or {},
            offers=[o.model_dump(mode="json") for o in offers], goal_binding=binding,
            goal_request={"snapshot_revision": snapshot.revision, "goal_context": {
                "episode_id": episode_id, "standing_goal": self.store.get("goal", STANDING_ID), "requested_goal": goal,
                "active_episode": _episode_context(episode), "known_evidence_refs": refs, "appraisal_evidence_refs": appraisal_refs,
                "outcomes": _review_outcomes(episode),
                "budget": self.budget(), "snapshot": state_snapshot}})
        # Catch broken envelopes locally before consuming Cognitive capacity.
        from harness.contracts.goals import GoalDecisionRequest
        from pydantic import ValidationError
        try:
            GoalDecisionRequest.model_validate({k: row[k] for k in (
                "opportunity_id", "generation", "deadline_at", "homeostatic_summary", "offers")} | row["goal_request"])
        except ValidationError:
            row.update(status="error", error_code="goal_invalid_request", completed_at=now.isoformat())
            cap.calls.put(row)
            self._record_request_fault(row, goal)
            return
        # Reserve rate budget before accepting an event, including failed attempts.
        with self.store.objects.atomic():
            state["calls"] = [v for v in state["calls"] if (now - _when(v)).total_seconds() < 3600] + [now.isoformat()]
            self._put_state(state)
            self.store.put("reservation", identity, binding, immutable=True)
        cap.calls.put(row)
        event = await self.supervisor.submit_event(event_type=RuntimeEventType.CAPABILITY_OPPORTUNITY,
            priority=RuntimeEventPriority.IDLE, source="production_goal", event_id=row["event_id"],
            payload={"opportunity_id": identity}, idempotency_key=identity)
        if event is None:
            cap._finish(row, "error", error_type="goal_queue_rejected")

    async def accept(self, row, result, event):
        """Persist proposed progress before dispatch and independently gate it."""
        from harness.contracts.goals import PreparedGoalDecision
        from harness.contracts.capability_calls import PreparedCapabilityChoice
        decision = PreparedGoalDecision.model_validate(result.result["decision"])
        prepared = PreparedCapabilityChoice.model_validate(result.result["prepared_capability_choice"])
        if (prepared != decision.capability_choice or decision.opportunity_id != row["opportunity_id"]
                or decision.snapshot_revision != row["goal_request"]["snapshot_revision"]):
            raise ValueError("goal decision capability or snapshot binding mismatch")
        known = set(row["goal_request"]["goal_context"]["known_evidence_refs"])
        if not set(decision.evidence_refs) <= known:
            raise ValueError("goal decision contains ungrounded evidence")
        binding, now = row["goal_binding"], self.clock()
        if self.invalid_reason(row) or self.supervisor.capabilities.invalid_reason(row):
            raise ValueError("goal decision superseded")
        goal = self.store.get("goal", binding["goal_id"])
        episode = self.store.get("episode", binding["episode_id"])
        raw = decision.model_dump(mode="json")
        # A durable accepted decision is replayed; it must never append a second step.
        accepted = self.store.get("decision", decision.decision_id)
        if accepted:
            if accepted["hash"] != digest(raw):
                raise ValueError("goal decision identity conflict")
            return result.result["prepared_capability_choice"]
        operation = decision.episode_operation
        prior_window = None
        if episode is None and operation not in {"start", "wait"}:
            raise ValueError("goal episode does not exist")
        snapshot = row["goal_request"]["goal_context"]["snapshot"]
        if episode is None and operation == "start":
            episode = dict(id=binding["episode_id"], episode_id=binding["episode_id"], goal_id=goal["id"],
                statement=decision.purpose or goal["statement"], status="active", created_at=now.isoformat(),
                target_dimensions=list(decision.target_dimensions), steps=[], results=[],
                success_criteria=goal["success_criteria"] if goal["id"] != STANDING_ID else list(decision.completion_criteria),
                baseline=snapshot, no_progress=0, stable_since=None, revision=1, predictions=[],
                next_review_at=now.isoformat(), blocked_reason=None, appraisals=[], seen_calls=[])
            goal["status"] = "active"
        if episode is not None:
            if episode.get("unresolved_call"):
                raise ValueError("unknown effects require reconciliation before replanning")
            if operation in {"start", "replan"}:
                if operation == "replan":
                    prior_window = (episode["id"] + ":" + str(episode["revision"]),
                        {"episode_id": episode["id"], "target_dimensions": episode["target_dimensions"],
                         "baseline": episode["baseline"], "last_evaluation": episode.get("last_evaluation"),
                         "ended_at": now.isoformat()})
                    episode.update(target_dimensions=list(decision.target_dimensions), baseline=snapshot,
                        evaluation_started_at=now.isoformat(), stable_since=None)
                    for key in ("last_observed_at", "last_snapshot_revision", "last_evaluation", "last_observation"):
                        episode.pop(key, None)
                    if goal["id"] == STANDING_ID:
                        episode["success_criteria"] = list(decision.completion_criteria)
                retained = [s for s in episode["steps"] if s["status"] != "planned"]
                for step in retained:
                    if step["status"] == "failed":
                        step["status"] = "superseded"
                if len(retained) + len(decision.plan) > self.config.maximum_plan_steps:
                    raise ValueError("goal plan step budget exceeded")
                episode["revision"] += 1
                new = [dict(id=episode["id"] + ":step:" + uuid4().hex[:12], statement=p.statement,
                    completion_criteria=list(p.completion_criteria), status="planned", evidence_refs=[], call_id=None)
                    for p in decision.plan]
                episode["steps"] = retained + new
                episode["predictions"].append({"at": now.isoformat(), "purpose": decision.purpose,
                    "target_dimensions": list(decision.target_dimensions), "completion_criteria": list(decision.completion_criteria),
                    "expected_effects": dict(decision.expected_effects)})
            if operation == "complete":
                self._verify_completion(episode, decision)
                episode.update(status="completed", ended_at=now.isoformat(), blocked_reason=None, end_reason="activity_verified")
                if episode["target_dimensions"]:
                    episode.update(effect_status="pending", effect_review_until=(now + timedelta(seconds=max(
                        self.config.review_seconds * 2, self.config.stability_seconds * 2))).isoformat())
                if goal["id"] != STANDING_ID:
                    goal.update(status="completed", revision=goal["revision"] + 1)
                else:
                    goal.update(status="observing", next_review_at=(now + timedelta(seconds=self.supervisor.capabilities.settings.opportunity_seconds)).isoformat())
            elif operation == "abandon":
                episode.update(status="abandoned", ended_at=now.isoformat(), end_reason="model_abandoned")
                if goal["id"] != STANDING_ID:
                    goal.update(status="abandoned", revision=goal["revision"] + 1)
                else:
                    goal.update(status="observing", next_review_at=(now + timedelta(
                        seconds=self.supervisor.capabilities.settings.opportunity_seconds)).isoformat())
            elif decision.choice.kind in {"act", "observe"}:
                step = next((s for s in episode["steps"] if s["status"] == "planned"), None)
                if step is None:
                    raise ValueError("executable goal choice requires an unfinished planned step")
                step.update(call_id=row["opportunity_id"], status="pending")
                row["goal_binding"]["step_id"] = step["id"]
                episode.update(status="acting", blocked_reason=None, unresolved_call=row["opportunity_id"])
            else:
                episode.update(status="waiting", next_review_at=(now + timedelta(seconds=self.config.review_seconds)).isoformat())
            with self.store.objects.atomic():
                if prior_window is not None:
                    self.store.put("evaluation_window", *prior_window, immutable=True)
                self._save("episode", episode)
                self._save("goal", goal)
        else:
            goal.update(next_review_at=(now + timedelta(seconds=self.supervisor.capabilities.settings.opportunity_seconds)).isoformat())
            self._save("goal", goal)
        self.store.put("decision", decision.decision_id, {"hash": digest(raw), "decision": raw, "episode_id": binding["episode_id"]}, immutable=True)
        row["goal_decision"] = raw
        self.supervisor.capabilities.calls.put(row)
        if episode and decision.appraisal is not None:
            await self._apply_appraisal(row, decision, event, episode)
        return result.result["prepared_capability_choice"]

    def _verify_completion(self, episode, decision):
        if decision.choice.kind in {"act", "observe"} or not episode["steps"]:
            raise ValueError("completion requires a separate evidence review")
        if any(s["status"] not in {"executed", "superseded"} for s in episode["steps"]):
            raise ValueError("necessary goal steps are unfinished")
        proven = {ref for r in episode["results"] if r["status"] in {"completed", "committed"} for ref in r["evidence_refs"]}
        assessments = {item.criterion: item for item in decision.criterion_results}
        required = {*episode["success_criteria"], *[c for s in episode["steps"] if s["status"] != "superseded" for c in s["completion_criteria"]]}
        for criterion in required:
            proof = assessments.get(criterion)
            if proof is None or not proof.satisfied or not proof.evidence_refs or not set(proof.evidence_refs) <= proven:
                raise ValueError("goal completion criterion lacks independent outcome evidence")
            matching_steps = [s for s in episode["steps"] if s["status"] != "superseded" and criterion in s["completion_criteria"]]
            if matching_steps and not all(set(proof.evidence_refs) & set(s["evidence_refs"]) for s in matching_steps):
                raise ValueError("goal criterion evidence belongs to a different step")
        episode["completion_verification"] = {"kind": "model_review_with_independent_channel_evidence",
            "criterion_results": [r.model_dump(mode="json") for r in decision.criterion_results]}

    async def _apply_appraisal(self, row, decision, event, episode):
        appraisal = decision.appraisal
        # All binding fields and evidence authority come from the reserved request.
        if appraisal.episode_id != episode["id"] or appraisal.basis_snapshot_revision != row["goal_request"]["snapshot_revision"]:
            raise ValueError("goal appraisal binding mismatch")
        verified = set(row["goal_request"]["goal_context"].get("appraisal_evidence_refs", []))
        outcomes = set()
        # Only the action verifier's observed effects establish longitudinal
        # evidence. HTTP success, plan completion and attention toggles do not.
        for prior in episode.get("results", []):
            raw = prior.get("outcome")
            if prior.get("channel") != "desktop" or not isinstance(raw, dict):
                continue
            try:
                from harness.runtime.models import ActionBackendResult
                observed = ActionBackendResult.model_validate(raw)
                if not observed.unknown and observed.external_effect_ref and observed.evidence_refs:
                    outcomes.update(ref.ref for ref in observed.evidence_refs)
            except ValueError:
                continue
        envelope = self.supervisor._make_command(event, command_type="ApplyAppraisal",
            idempotency_key="goal-appraisal:" + decision.decision_id,
            expected_revision=appraisal.basis_snapshot_revision,
            payload={"episode_id": episode["id"], "appraisal": appraisal.model_dump(mode="json"),
                "allow_heartbeat_rebase": True,
                "basis_snapshot_at": row["goal_request"]["goal_context"]["snapshot"].get("updated_at"),
                "verified_outcome_refs": sorted(outcomes & verified),
                "verified_evidence_refs": row["goal_request"]["goal_context"].get("appraisal_evidence_refs", [])})
        envelope = envelope.model_copy(update={"deadline": min(envelope.deadline, decision.deadline_at)})
        try:
            async with self.supervisor._homeostasis_command_lock:
                outcome = await before_deadline(self.supervisor._invoke_domain(service="homeostasis", envelope=envelope,
                    method=self.supervisor.homeostasis.apply_appraisal, event=event), envelope.deadline)
                if outcome.status == "applied":
                    self.supervisor._homeostasis_snapshot = await self.supervisor.homeostasis.get_snapshot()
        except Exception as exc:
            # Stale/rejected self-assessment never becomes evidence or a retry loop.
            self.store.put("appraisal_rejection", decision.decision_id,
                {"episode_id": episode["id"], "error": type(exc).__name__}, immutable=True)

    def record_homeostasis(self, envelope, result, *, episodes=None):
        """Project committed domain traces, including crash-replayed appraisals."""
        if not self.enabled or result.status not in {"applied", "idempotent_replay"}:
            return
        trace = (result.result or {}).get("trace")
        if not isinstance(trace, dict):
            return
        source = trace.get("source", "")
        category = "self_report" if source.startswith("psychological:") else (
            "drift" if envelope.command_type == "ApplyHeartbeat" else "fact" if source == "verified_outcome" else "legacy_event")
        for episode in self._episodes() if episodes is None else episodes:
            when = _when(trace.get("occurred_at")) or envelope.issued_at
            end = _when(episode.get("effect_review_until") or episode.get("ended_at"))
            if when < _when(episode["created_at"]) or end and when > end:
                continue
            if category == "self_report" and envelope.payload.get("episode_id") != episode["id"]:
                continue
            identity = digest([episode["id"], envelope.command_id])
            with self.store.objects.atomic():
                previous = self.store.get("contribution", identity)
                previous_event = (previous or {}).get("trace", {}).get("event_id")
                event_id = trace.get("event_id")
                if previous_event and event_id and previous_event != event_id:
                    # Historical 32-bit command IDs can collide across events.
                    # Keep the first immutable object and bind the second to its
                    # full request, without accepting changed traces for one event.
                    if event_id != envelope.event_id or trace.get("receive_seq") != envelope.receive_seq:
                        raise ValueError("contribution trace does not match its command event")
                    identity = digest([episode["id"], envelope.command_id, envelope.effective_request_hash()])
                self.store.put("contribution", identity, {"episode_id": episode["id"], "source": category,
                    "command_id": envelope.command_id, "trace": trace}, immutable=True)
                appraisal = (result.result or {}).get("appraisal")
                if category == "self_report" and appraisal:
                    if appraisal["appraisal_id"] not in {a["id"] for a in episode["appraisals"]}:
                        episode["appraisals"].append({"id": appraisal["appraisal_id"], "kind": appraisal["appraisal_kind"],
                            "evidence_refs": trace.get("evidence_refs", []), "revision": result.committed_revision,
                            "at": result.completed_at.isoformat()})
                        self._save("episode", episode)

    def recover_contributions(self):
        if not self.enabled:
            return
        episodes = self._episodes()
        if not episodes:
            return
        from harness.contracts.envelope import DomainCommandEnvelope, DomainCommandResult
        recovery = self.supervisor.recovery
        identity = recovery.projector.state.get("journal_genesis", "empty")
        checkpoint = self.store.get("runtime_recovery", identity)
        after = recovery.validated_cursor(checkpoint)
        boundary = recovery.projector.cursor
        if after >= boundary:
            return
        # Recovery only adds appraisals to these episodes. Reuse that projection
        # instead of decoding every episode again for each historical result.
        # Commit contributions and their cursor together, avoiding a disk flush
        # for every replayed command while preserving immutable conflict checks.
        with self.store.objects.atomic():
            for record in recovery.records("domain_command_result", "domain_command_late_result", after=after):
                if record.record_seq > boundary:
                    break
                if record.data.get("service") != "homeostasis":
                    continue
                result = DomainCommandResult.model_validate(record.data["result"])
                if result.status not in {"applied", "idempotent_replay"} or not isinstance((result.result or {}).get("trace"), dict):
                    continue
                intent = recovery.command_intent_for_result(record)
                self.record_homeostasis(DomainCommandEnvelope.model_validate(intent.data["envelope"]),
                                        result, episodes=episodes)
            # This cursor lives with the effects, so a restored module database
            # also restores its recovery position. Replay remains idempotent.
            self.store.put("runtime_recovery", identity, recovery.cursor_token(boundary))

    def qq_context(self, request):
        """Only a bounded typed projection may enter group-visible cognition."""
        intent = request.idle_intent
        if intent is None or not intent.opportunity_ref:
            return None
        call = self.supervisor.capabilities.calls.get(intent.opportunity_ref)
        if not call or not call.get("goal_binding"):
            return None
        if self.supervisor.capabilities.invalid_reason(call):
            raise ValueError("QQ goal opportunity no longer active")
        offer = call.get("offer", {})
        scope = offer.get("scope", {})
        if (scope.get("group_id") != request.group_id or scope.get("self_member_id") != request.self_member_id
                or call.get("decision", {}).get("decision_id") != intent.selection_ref or call["generation"] != intent.scheduler_generation):
            raise ValueError("QQ goal scope or selection changed")
        episode = self.store.get("episode", call["goal_binding"]["episode_id"])
        if episode is None:
            raise ValueError("QQ goal episode missing")
        from harness.contracts.goals import QQGoalContext
        goal = self.store.get("goal", episode["goal_id"])
        step_index = next((i for i, s in enumerate(episode["steps"]) if s.get("call_id") == call["opportunity_id"]), None)
        if step_index is None:
            raise ValueError("QQ goal step no longer pending")
        return QQGoalContext(group_id=request.group_id, self_member_id=request.self_member_id,
            opportunity_id=call["opportunity_id"], selection_ref=intent.selection_ref, generation=call["generation"],
            episode_id=episode["id"], step_index=step_index, origin=goal["origin"],
            episode_operation=call["goal_decision"]["episode_operation"], capability_id=offer["capability_id"],
            target_dimensions=episode["target_dimensions"]).model_dump(mode="json")

    def reconcile(self):
        for call in self.supervisor.capabilities.calls.all():
            binding = call.get("goal_binding")
            if not binding:
                continue
            episode = self.store.get("episode", binding["episode_id"])
            if episode is None:
                # Preparation can fail before an episode exists. Those failures
                # still consume the no-progress budget, including across boots.
                if call["status"] not in TERMINAL:
                    continue
                goal = self.store.get("goal", binding["goal_id"])
                if goal is None or call["opportunity_id"] in goal.get("seen_preparations", []):
                    continue
                goal.setdefault("seen_preparations", []).append(call["opportunity_id"])
                self._record_request_fault(call, goal)
                if call["status"] not in {"wait", "defer", "no_action"}:
                    goal["no_progress"] = goal.get("no_progress", 0) + 1
                if goal["no_progress"] >= self.config.maximum_no_progress and goal["status"] not in FINISHED | {"paused"}:
                    goal.update(blocked_reason="no_progress", blocked_signal_revision=self._state()["signal_revision"])
                self._save("goal", goal)
                continue
            if call["opportunity_id"] in episode["seen_calls"]:
                continue
            if call["status"] not in TERMINAL:
                if call["status"] == "uncertain":
                    episode.update(unresolved_call=call["opportunity_id"])
                    if episode["status"] not in FINISHED | {"paused"}:
                        episode.update(blocked_reason="unknown_effect", status="waiting")
                    self._save("episode", episode)
                continue
            self._record_request_fault(call, self.store.get("goal", binding["goal_id"]))
            step = next((s for s in episode["steps"] if s.get("call_id") == call["opportunity_id"]), None)
            evidence = _refs(call)
            successful = call["status"] in {"completed", "committed"} and bool(evidence)
            result = dict(call_id=call["opportunity_id"], status=call["status"], evidence_refs=evidence,
                at=call.get("completed_at", self.clock().isoformat()), channel=call.get("channel"),
                receipt=call.get("receipt"), outcome=call.get("outcome"), channel_result=call.get("channel_result"))
            error_code = call.get("error_code")
            if isinstance(error_code, str) and error_code in GOAL_RUNTIME_ERROR_CODES:
                result["error_code"] = error_code
            if step:
                step.update(status="executed" if successful else "failed", evidence_refs=evidence)
                result["step_id"] = step["id"]
            episode["results"].append(result)
            episode["seen_calls"].append(call["opportunity_id"])
            if episode.get("unresolved_call") == call["opportunity_id"]:
                episode.pop("unresolved_call", None)
            waiting = call["status"] in {"wait", "defer", "no_action"}
            episode["no_progress"] = 0 if successful or waiting else episode["no_progress"] + 1
            if episode["status"] not in FINISHED | {"paused"}:
                episode.update(status="waiting", next_review_at=(self.clock() + timedelta(seconds=
                    self.config.minimum_reconsideration_interval if successful else self.config.review_seconds)).isoformat())
                if episode["no_progress"] >= self.config.maximum_no_progress:
                    episode.update(blocked_reason="no_progress", blocked_signal_revision=self._state()["signal_revision"])
                else:
                    episode["blocked_reason"] = "effect_pending" if successful else "model_wait" if waiting else "activity_not_completed"
            self._save("episode", episode)

    def observe(self, snapshot):
        snapshot = snapshot.get("snapshot", snapshot)
        now = self.clock()
        for episode in self._episodes():
            dimensions = episode["target_dimensions"]
            if not dimensions or episode["status"] in FINISHED and episode.get("effect_status") != "pending":
                continue
            previous = _when(episode.get("last_observed_at"))
            if previous and (now - previous).total_seconds() < 60:
                continue
            if previous and (now - previous).total_seconds() > max(90, 2 * self.supervisor.manifest.heartbeat_seconds):
                # A restart or observation gap cannot prove continuous stability.
                episode["stable_since"] = None
            updated_at = _when(snapshot.get("updated_at"))
            if (updated_at is None or (now - updated_at).total_seconds() > max(90, 2 * self.supervisor.manifest.heartbeat_seconds)
                    or snapshot.get("revision", 0) <= episode.get("last_snapshot_revision", -1)):
                continue
            before, after = deviation(episode["baseline"], dimensions), deviation(snapshot, dimensions)
            within = after == 0
            episode["stable_since"] = episode.get("stable_since") or now.isoformat() if within else None
            stable = within and (now - _when(episode["stable_since"])).total_seconds() >= self.config.stability_seconds
            window_start = _when(episode.get("evaluation_started_at"))
            def in_window(item):
                return window_start is None or bool(item.get("at") and _when(item["at"]) >= window_start)
            facts = list(dict.fromkeys(ref for r in episode["results"] if in_window(r) for ref in r["evidence_refs"]))
            appraisals = [a["id"] for a in episode["appraisals"] if in_window(a)]
            contributions = self.store.contributions(episode["id"],
                after_revision=episode["baseline"].get("revision") if episode.get("evaluation_started_at") else None)
            amounts = {source: {dim: 0.0 for dim in dimensions} for source in ("fact", "self_report", "drift", "legacy_event", "coupling")}
            for c in contributions:
                t = c["trace"]
                for dim in dimensions:
                    coupled = t.get("coupling_impacts", {}).get(dim, 0.0)
                    amounts[c["source"]][dim] += t.get("applied_impacts", {}).get(dim, 0.0) - coupled
                    amounts["coupling"][dim] += coupled
            change = before - after
            outcome = "improved" if change > 1e-9 else "worsened" if change < -1e-9 else "unchanged"
            if stable:
                outcome = "stable"
            supporting_sources = {c["source"] for c in contributions if any(
                abs(c["trace"].get("applied_impacts", {}).get(dim, 0.0)) > 1e-9 for dim in dimensions)}
            if outcome in {"improved", "stable"} and supporting_sources == {"self_report"}:
                outcome = "self_report_only"
            previous_snapshot = episode.get("last_observation", episode["baseline"])
            interval_change = deviation(previous_snapshot, dimensions) - after
            interval_outcome = "improved" if interval_change > 1e-9 else "worsened" if interval_change < -1e-9 else "unchanged"
            evaluation = dict(id="review:" + digest([episode["id"], snapshot.get("revision"), now.isoformat()])[:32],
                episode_id=episode["id"], at=now.isoformat(), outcome=outcome,
                before={k: episode["baseline"]["dimensions"][k]["value"] for k in dimensions},
                after={k: snapshot["dimensions"][k]["value"] for k in dimensions},
                deviation_before=before, deviation_after=after, stable_since=episode["stable_since"],
                sources={"fact": facts, "self_report": appraisals, "drift": any(c["source"] == "drift" for c in contributions)},
                contributions=amounts, contributing_sources=sorted(supporting_sources), evidence_refs=facts,
                causal_effect_verified=False, snapshot_revision=snapshot.get("revision"))
            evaluation.update(comparison_basis="plan_baseline", baseline_at=episode["baseline"].get("updated_at"),
                interval_outcome=interval_outcome, previous_observed_at=episode.get("last_observed_at"),
                target_dimensions=dimensions)
            self._save("evaluation", evaluation)
            episode.update(last_observed_at=now.isoformat(), last_evaluation=evaluation["id"],
                last_snapshot_revision=snapshot.get("revision"),
                last_observation={"dimensions": {k: snapshot["dimensions"][k] for k in dimensions}})
            if stable and episode["goal_id"] == STANDING_ID and not episode.get("unresolved_call") and episode["status"] != "paused":
                episode.update(status="completed", ended_at=now.isoformat(), end_reason="stability_observed",
                    regulation_effect=outcome, causal_effect_verified=False)
                standing = self.store.get("goal", STANDING_ID)
                standing.update(status="observing", next_review_at=(now + timedelta(seconds=self.config.review_seconds)).isoformat())
                self._save("goal", standing)
            if episode.get("effect_status") == "pending" and (stable or now >= _when(episode["effect_review_until"])):
                episode.update(effect_status="reviewed", regulation_effect=outcome, causal_effect_verified=False)
            self._save("episode", episode)

    def snapshot(self):
        episodes = self._episodes()
        active = next((e for e in episodes if e["status"] not in FINISHED | {"paused"} and e["goal_id"] != STANDING_ID), None)
        active = active or next((e for e in episodes if e["status"] not in FINISHED | {"paused"}), None)
        evaluations = sorted(self.store.all("evaluation"), key=lambda e: e["at"])[-120:]
        from collections import Counter
        recent = [c for c in self.supervisor.capabilities.calls.all() if c.get("goal_binding") and c.get("created_at")
                  and (self.clock() - _when(c["created_at"])).total_seconds() < 3600]
        metrics = {"window_seconds": 3600, "attempts": len(recent),
            "valid_decisions": sum(bool(c.get("goal_decision")) for c in recent),
            "statuses": dict(Counter(c["status"] for c in recent)),
            "executed_capabilities": dict(Counter(c.get("offer", {}).get("capability_id") for c in recent
                if c["status"] in {"completed", "committed"} and c.get("offer"))),
            "errors": dict(Counter(c.get("error_code", c.get("error_type", "unspecified")) for c in recent if c["status"] == "error"))}
        feedback = getattr(self.supervisor, "qq_direct_feedback", None)
        return {"enabled": self.enabled, "health": self.health(), "activity_metrics": metrics,
            "qq_feedback": feedback.status() if feedback else None,
            "standing_goal": self.store.get("goal", STANDING_ID), "goals": self._goals(),
            "active_episode": active, "episodes": episodes[-30:], "evaluations": evaluations,
            "budget": self.budget(), "next_review_at": (active or {}).get("next_review_at")}

    def close(self):
        self._closed = True
        self.store.close()


def create_goal_router(control, *, authenticate):
    from fastapi import APIRouter, Depends, HTTPException
    router = APIRouter(dependencies=[Depends(authenticate)])

    @router.get("/v1/goals")
    async def goals():
        return control.supervisor.goals.snapshot()

    @router.post("/v1/goals")
    async def submit(request: GoalSubmission):
        try:
            return await control.supervisor.goals.submit(request.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/v1/goals/{goal_id}/{operation}")
    async def operate(goal_id: str, operation: str, request: GoalOperation):
        if operation not in {"pause", "resume", "cancel"}:
            raise HTTPException(status_code=404, detail="unknown goal operation")
        try:
            return await control.supervisor.goals.operate(goal_id, operation, request.request_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return router
