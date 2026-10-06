"""Independent, bounded attention reviews; never retrieve closed group content."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import timedelta
import json
from uuid import uuid4

from harness.contracts.capability_calls import CapabilityOffer, PreparedCapabilityChoice
from harness.runtime.models import RuntimeLifecycle, utc_now


class AttentionController:
    def __init__(self, supervisor, bridge):
        self.supervisor, self.bridge = supervisor, bridge
        self.task = None
        self.last_error = None

    @property
    def enabled(self):
        snapshot = self.supervisor._homeostasis_snapshot.payload
        snapshot = snapshot.get("snapshot", snapshot)
        return (snapshot.get("dynamics") or {}).get("mode") == "active"

    def _state(self):
        with closing(self.bridge.attention_store().connect()) as db:
            row = db.execute("SELECT value FROM attention_control WHERE key='review'").fetchone()
        return json.loads(row[0]) if row else {"calls": [], "next_attempt": None}

    def _save(self, state):
        with closing(self.bridge.attention_store().connect()) as db, db:
            db.execute("INSERT INTO attention_control VALUES ('review',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps(state),))

    def calls(self, now=None):
        from datetime import datetime
        now = now or utc_now()
        return [datetime.fromisoformat(t) for t in self._state()["calls"] if (now-datetime.fromisoformat(t)).total_seconds() < 3600]

    def schedule(self, event):
        from datetime import datetime
        if not self.enabled or self.task is not None and not self.task.done():
            return
        owner, now = self.supervisor, utc_now()
        if owner.lifecycle != RuntimeLifecycle.RUNNING:
            return
        state = self._state()
        if state.get("next_attempt") and now < datetime.fromisoformat(state["next_attempt"]):
            return
        goals = getattr(owner, "goals", None)
        maximum = min(12, goals.config.maximum_model_calls_per_hour) if goals and goals.enabled else 12
        if len(self.calls(now)) >= min(4, maximum):
            return
        normal = goals.budget(now=now)["calls_last_hour"] if goals and goals.enabled else 0
        if normal + len(self.calls(now)) >= maximum:
            return
        snapshot = owner._homeostasis_snapshot.payload
        snapshot = snapshot.get("snapshot", snapshot)
        pressure = snapshot.get("regulation_pressure", {})
        due = self.bridge.attention_store().due(now=now)
        if not due and not pressure.get("suggest_reduce_input"):
            return
        offers = self.bridge.attention_catalog()
        if not offers:
            return
        state["calls"] = [t.isoformat() for t in self.calls(now)] + [now.isoformat()]
        state["next_attempt"] = (now+timedelta(minutes=15)).isoformat()
        self._save(state)  # Failed calls count and survive a restart.
        self.task = asyncio.create_task(self._review(event, offers, due, pressure), name="qq-attention-review")

    async def _review(self, event, offers, due, pressure):
        owner, identity = self.supervisor, "attention-review:" + uuid4().hex
        generation = owner.capabilities.generation
        deadline = utc_now()+timedelta(seconds=55)
        snapshot = owner._homeostasis_snapshot.payload
        snapshot = snapshot.get("snapshot", snapshot)
        dynamics = snapshot.get("dynamics") or {}
        summary = {"attention_control_only": True, "regulation_pressure": pressure,
                   "fatigue": dynamics.get("fatigue"), "boredom": dynamics.get("boredom"),
                   "dimensions": snapshot.get("dimensions", {}), "due_groups": [p["group_id"] for p in due]}
        command = owner._make_command(event, command_type="PrepareCapabilityCall", idempotency_key=identity,
            expected_revision=None, payload={"opportunity_id": identity, "generation": generation,
                "deadline_at": deadline.isoformat(), "homeostatic_summary": summary, "offers": offers})
        try:
            result = await owner._invoke_domain(service="cognitive", envelope=command,
                method=owner.cognitive.prepare_capability_call, event=event)
            decision = PreparedCapabilityChoice.model_validate(result.result["decision"])
            from harness.cognitive.capabilities import catalog_hash, _bound_choice
            validated = [CapabilityOffer.model_validate(o) for o in offers]
            if decision.opportunity_id != identity or decision.catalog_hash != catalog_hash(validated):
                raise ValueError("attention_decision_binding_invalid")
            if owner.lifecycle != RuntimeLifecycle.RUNNING or not self.enabled or generation != owner.capabilities.generation:
                raise ValueError("attention_control_superseded")
            changed = None
            if decision.choice.offer_id is not None:
                offer = next(o for o in validated if o.offer_id == decision.choice.offer_id)
                _bound_choice(decision.choice, offer, now=utc_now())
                receipt = self.bridge.enqueue_attention({"opportunity_id": identity, "generation": generation,
                    "capability_id": offer.capability_id, "scope": offer.scope, "activation_revision": offer.activation_revision,
                    "deadline_at": deadline.isoformat(), "arguments": decision.choice.arguments})
                changed = receipt["receipt"]["current"]["group_id"]
            # One combined review covers all due groups; no-action preserves
            # closure and schedules another review, it never expires the policy.
            store = self.bridge.attention_store()
            for policy in due:
                if policy["group_id"] == changed:
                    continue
                try:
                    store.set({"group_id": policy["group_id"], "mode": "closed", "reason": policy["reason"]},
                        origin="model", action_id=identity+":"+policy["group_id"], expected_revision=policy["revision"],
                        allowed_groups={o.scope["group_id"] for o in validated})
                except ValueError:
                    pass  # A newer user operation takes precedence.
            self.last_error = None
            owner._append_journal("attention_review_completed", event=event,
                data={"review_id": identity, "changed_group": changed, "due_groups": summary["due_groups"]})
        except Exception as exc:
            self.last_error = type(exc).__name__
            owner._append_journal("attention_review_failed", event=event,
                data={"review_id": identity, "error_type": self.last_error})

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
