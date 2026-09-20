"""Scripted cognition for isolated self-check tests: real Runtime wiring, no live model."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from types import SimpleNamespace

from harness.contracts.decisions import PreparedResponse, ReplyDecision
from harness.contracts.envelope import DomainCommandResult, VersionVector
from harness.diary.models import DiaryOriginal
from harness.qq.decision import QQReplyDecision
from harness.runtime.adapters import DomainSnapshot, ServiceHealth
from harness.runtime.models import utc_now
from harness.runtime.web import READ, SEARCH


class ScriptedSelfCheckCognitive:
    def __init__(self):
        self.revision = 0
        self.calls: list[str] = []
        self._ledger: dict[tuple[str, str], DomainCommandResult] = {}
        self.pipeline = SimpleNamespace(settings=SimpleNamespace(emotion_mode="off", emotion_affect_read_timeout_seconds=0.2))

    async def health(self):
        return ServiceHealth(service="cognitive", healthy=True, revision=self.revision)

    async def get_snapshot(self):
        return DomainSnapshot(service="cognitive", revision=self.revision, payload={})

    async def pause_maintenance(self, *, timeout: float) -> bool:
        del timeout
        return True

    async def resume_maintenance(self) -> None:
        return None

    async def close(self):
        return None

    def _applied(self, command, payload):
        key = (command.command_type, command.idempotency_key)
        previous = self._ledger.get(key)
        if previous is not None:
            return previous.model_copy(update={"status": "idempotent_replay", "idempotency_hit": True})
        result = DomainCommandResult[dict](
            command_id=command.command_id, command_type=command.command_type, status="applied",
            request_hash=command.effective_request_hash(), committed_revision=self.revision,
            acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
            result=payload, completed_at=utc_now(),
        )
        self._ledger[key] = result
        return result

    async def prepare_response(self, command):
        self.calls.append("PrepareResponse")
        now = utc_now()
        prepared = PreparedResponse(
            event_id=command.event_id, correlation_id=command.correlation_id,
            session_id=str(command.payload.get("session_id") or "sess_selfcheck"),
            decision=ReplyDecision(text="自检收到了。", segments=["自检收到了。"]),
            read_versions=VersionVector(), prepared_at=now, expires_at=now + timedelta(seconds=30),
            model_version="selfcheck-script", prompt_version="selfcheck-v1",
        )
        return self._applied(command, prepared.model_dump(mode="json"))

    async def commit_interaction(self, command):
        self.calls.append("CommitInteraction")
        self.revision += 1
        return self._applied(command, {"memory_created": True})

    async def prepare_web_step(self, command):
        self.calls.append("PrepareWebStep")
        offers = command.payload.get("offers") or []
        context = command.payload.get("runtime_web_context") or {}
        sources = context.get("sources") or []
        search = next((item for item in offers if item.get("capability_id") == SEARCH), None)
        read = next((item for item in offers if item.get("capability_id") == READ), None)
        if search and not sources:
            choice = {"kind": "call", "offer_id": search["offer_id"], "arguments": {"query": "selfcheck evidence"}}
        elif read and sources and not sources[0].get("content"):
            choice = {"kind": "call", "offer_id": read["offer_id"],
                      "arguments": {"source_refs": [sources[0]["source_ref"]]}}
        else:
            choice = {"kind": "finish"}
        return self._applied(command, {"choice": choice})

    async def prepare_qq_response(self, command):
        self.calls.append("PrepareQQResponse")
        payload = command.payload
        trace = payload["trace"]
        now = datetime.now(UTC)
        decision = QQReplyDecision(
            decision_id="qq-decision:selfcheck", input_hash="selfcheck", created_at=trace["first_seen_at"],
            action="wait", reason="self-check wait after web context", decision_source="model",
            outcome="natural_wait", group_id=payload["group_id"], self_member_id=payload["self_member_id"],
            participants=[], source_record_ids=[], new_observation_record_ids=[], evidence_refs=[],
            delivery_refs=[], model="selfcheck-script", persona_version="selfcheck", preset_id="selfcheck",
            trace_id=trace["trace_id"],
        )
        return self._applied(command, {"decision": decision.model_dump(mode="json"),
                                       "trace": {"trace_id": trace["trace_id"], "event_id": trace["event_id"],
                                                 "completed_at": now.isoformat()}})

    async def prepare_diary_entry(self, command):
        self.calls.append("PrepareDiaryEntry")
        payload = command.payload
        text = "自检记下了今天的窗口。"
        return self._applied(command, {"decision": {
            "entry_id": payload["entry_id"],
            "decision_id": "diary-decision:" + hashlib.sha256(command.command_id.encode()).hexdigest()[:32],
            "action": "write", "text": text, "source_event_id": command.event_id,
            "prepared_at": utc_now().isoformat(), "deadline_at": payload["deadline_at"], "day": payload["day"],
        }, "timing": {"model_calls": 1}})

    async def commit_diary_entry(self, command):
        self.calls.append("CommitDiaryEntry")
        self.revision += 1
        original = command.payload.get("original") or {}
        if original:
            DiaryOriginal.model_validate(original)
        return self._applied(command, {"memory_created": True})

    async def prepare_goal_decision(self, command):
        self.calls.append("PrepareGoalDecision")
        from harness.cognitive.capabilities import catalog_hash
        from harness.contracts.capability_calls import CapabilityChoice, CapabilityOffer, PreparedCapabilityChoice
        offers = [CapabilityOffer.model_validate(item) for item in command.payload.get("offers") or []]
        now = utc_now()
        deadline = datetime.fromisoformat(command.payload["deadline_at"])
        selected = PreparedCapabilityChoice(
            decision_id="capability-decision:selfcheck", opportunity_id=command.payload["opportunity_id"],
            catalog_hash=catalog_hash(offers), choice=CapabilityChoice(kind="wait"),
            prepared_at=now, deadline_at=deadline,
        )
        return self._applied(command, {
            "decision": {"episode_operation": "wait", "choice": {"kind": "wait"}},
            "prepared_capability_choice": selected.model_dump(mode="json"),
            "timing": {"model_calls": 1, "completed_at": now.isoformat()},
        })
