"""Bindings between authenticated Runtime entrances and the agent loop."""
from __future__ import annotations

from contextlib import asynccontextmanager, closing
import threading
import time
import os

import httpx

from harness.agentic.context import ContextBuilder, TokenCounter, persona_system
from harness.agentic.runner import AgentRunner, ModelPriority
from harness.agentic.store import AgentStore, digest
from harness.agentic.worker import AgentWorker
from harness.core.inference import FinalResponse
from harness.core.models import InteractionEvent
from harness.llm.client import OpenAICompatClient
from harness.memory.shared import SharedLocalEvent


def enabled_service(pipeline):
    service = getattr(pipeline.memory, "agentic", None)
    if not pipeline.settings.agentic_enabled:
        return None
    if service is None or not service.enabled:
        raise ValueError("agentic_service_not_bound")
    return service


class AgentService:
    def __init__(self, host, *, counter=None, llm=None):
        self.host = host
        self.settings = host.chat.settings
        self.preset_id = host.chat.active_preset_id
        self.persona_provider = lambda: host.chat.memory.get_persona()
        # Fail closed if an unrelated preset is selected; never infer identity
        # solely from a preset filename or id.
        persona_system(self.persona_provider())
        self.store = AgentStore(host.shared.production_store)
        from harness.agentic.affect_store import AffectStore
        from harness.agentic.internal_state import read_runtime_state
        self.affect = AffectStore(self.store, self.settings.agentic_regulation)
        self.store.affect = self.affect
        self.runtime_state_provider = read_runtime_state
        self.counter = counter or TokenCounter(self.settings)
        self.llm = llm or OpenAICompatClient(self.settings)
        self.priority = ModelPriority()
        self.context = ContextBuilder(self.store, self.counter, self.settings)
        self.runner = AgentRunner(store=self.store, context=self.context, llm=self.llm,
            persona_provider=self.persona_provider, priority=self.priority)
        self.worker = AgentWorker(self)
        self.sync_lock = threading.RLock()
        self.turn_locks = {}
        host.chat.memory.agentic = self

    @property
    def enabled(self):
        return self.settings.agentic_enabled and self.host.chat.active_preset_id == self.preset_id

    def conversation(self, audience, entrance):
        return self.store.conversation(audience=audience, entrance=entrance,
            persona=digest(persona_system(self.persona_provider())))["id"]

    def ingest_local(self, event):
        if event.session_id != self.host.chat.active_session_id:
            raise ValueError("agentic_local_session_not_authorized")
        original = SharedLocalEvent(event_id=event.event_id + ".user", session_id=event.session_id,
            author_principal="local:main", role="user", text=event.raw_text,
            occurred_at=event.timestamp, display_name=event.speaker,
            evidence_refs=(f"local-chat:{event.session_id}:{event.event_id}:user",))
        record = self.host.shared.store._prepare("local_incoming", original.event_id, original, original.visibility)
        self.archive_record(record)
        return record.record_id

    def archive_record(self, record):
        if record.kind not in {"local_incoming", "local_outgoing", "qq_observation", "qq_delivery", "qq_segment_delivery"}:
            return
        audience = record.session_id if record.channel in {"qq_private", "qq_group"} else "local:main"
        entrance = audience if audience.startswith("qq:") else "local:" + record.session_id
        conversation = self.conversation(audience, entrance)
        experience = self.store.memory.from_shared(record)
        role = "assistant" if experience.kind == "self_expression" else "user"
        # A conservative cached chunk bound needs no model availability. Exact
        # tokenizer accounting is mandatory when projecting an actual request.
        self.store.append(conversation, experience, role=role,
            tokens=max(1, len(experience.text.encode("utf-8"))), assimilate=role == "user")

    def sync_shared(self):
        if not self.enabled:
            return
        with self.sync_lock:
            stream = "agentic-shared:" + str(self.host.shared.store.path.absolute())
            page = self.host.shared.store.read_after(cursor=self.store.memory.cursor(stream), limit=256)
            for record in page.records:
                self.archive_record(record)
            self.store.memory.ingest([], stream=stream, cursor=page.next_cursor)

    def run(self, *, audience, entrance, text, source_id="", timeout=None, diary_allowed=False, vision=None, wake_only=False, runtime_state=None, web_validator=None):
        conversation = self.conversation(audience, entrance)
        with self.sync_lock:
            lock = self.turn_locks.setdefault(conversation, threading.RLock())
        with lock:
            from harness.agentic.background import dispatch
            system = persona_system(self.persona_provider())
            internal_state = None
            if self.settings.agentic_regulation.mode == 'on':
                from harness.agentic.internal_state import TurnState
                runtime = runtime_state if runtime_state is not None else self.runtime_state_provider()
                if runtime.get('mode') == 'on' and runtime.get('running'):
                    internal_state = TurnState(self, conversation, runtime)
            actions = None
            web = None
            # Only authenticated foreground source turns receive network tools.
            if source_id and not wake_only and dispatch.get() is None:
                from harness.agentic.web import AgentWeb
                candidate = AgentWeb(conversation, source_id, text, validator=web_validator)
                if candidate.available():
                    web = candidate
            if internal_state is not None and internal_state.runtime.get('autonomous_enabled'):
                from harness.agentic.actions import RestActions
                actions = RestActions(internal_state)
            mobile = None
            if source_id and not wake_only and dispatch.get() is None:
                from harness.agentic.mobile import AgentMobile
                mobile = AgentMobile.for_audience(audience, conversation, validator=web_validator)
            result = self.runner.run(conversation, system=system,
                current={"role": "user", "content": text + ("\n[source_id=" + source_id + "]" if source_id else ""),
                    "source_id": source_id}, source_id=source_id, timeout=timeout, diary_allowed=diary_allowed, vision=vision,
                internal_state=internal_state, actions=actions, wake_only=wake_only, foreground=dispatch.get() is None, web=web, mobile=mobile)
            if result.action is not None:
                from datetime import datetime, timedelta, timezone
                from harness.runtime.native_regulation import NativeRestRequest
                from infra.trace.request_spans import current_trace_context
                frozen = internal_state.runtime
                request = NativeRestRequest(request_id='native-rest:' + digest([conversation, source_id,
                    current_trace_context().get('command_id'), frozen['runtime_instance_id'],
                    frozen['rest']['revision'], result.action.rest.model_dump()]),
                    runtime_instance_id=frozen['runtime_instance_id'], manifest_revision=frozen['manifest_revision'],
                    expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
                    expected_rest_revision=frozen['rest']['revision'], **result.action.rest.model_dump(),
                    source_refs=[source_id] if source_id else [])
                job_id = 'job:' + digest([conversation, source_id, request.request_id])[:32]
                if not self.store.jobs(conversation, job_id):
                    self.store.enqueue(conversation, source=source_id, operation=request.request_id, kind='control',
                        payload={'request':request.model_dump(mode='json'), 'evidence':{'conversation':conversation,
                            'references':[{'record_id':ref,'revision':rev} for ref,rev in result.versions.items()]}})
            return result

    def prepare_pipeline(self, event, *, autonomous=False):
        started = time.monotonic()
        source = "" if autonomous else self.ingest_local(event)
        result = self.run(audience="local:main", entrance="local:" + event.session_id,
            text=event.raw_text, source_id=source, diary_allowed=not autonomous)
        conversation = self.conversation("local:main", "local:" + event.session_id)
        return FinalResponse(text=result.finish.text, segments=[result.finish.text] if result.finish.text else [],
            path="deep" if result.calls > 1 else "fast", situation_id="agentic:" + event.event_id,
            event_id=event.event_id, usage=result.usage, latency_ms=(time.monotonic() - started) * 1000,
            agentic={"conversation": conversation, "shown_sources": sorted(result.shown),
                "references": [{"record_id": ref, "revision": revision} for ref, revision in result.versions.items()],
                "notice_ids": list(result.notice_ids), "web_sources": list(result.web_sources)})

    def production_choice(self, llm, payload, schema, *, context, command_id, autonomous,
                          meter=None, record_use=None, **kwargs):
        from harness.cognitive.production import ProductionChoice
        from harness.production.metering import production_call_scope
        event = InteractionEvent.model_validate(payload["current_event"])
        source = payload.get("current_source_id") or ""
        with production_call_scope(context, command_id=command_id, autonomous=autonomous, meter=meter):
            result = self.run(audience="local:main", entrance="local:" + event.session_id,
                text=event.raw_text, source_id=source, diary_allowed=not autonomous)
        choice = ProductionChoice(kind="reply" if result.finish.action == "emit" else "wait",
            reason="main_model_agentic_decision", text=result.finish.text or None,
            used_memory_ids=[ref for ref in result.finish.used_source_ids if ref in result.shown])
        choice._shown_refs = choice._shown_memory_refs = frozenset(result.shown)
        conversation = self.conversation("local:main", "local:" + event.session_id)
        choice._agentic = {"conversation": conversation, "shown_sources": sorted(result.shown),
            "references": [{"record_id": ref, "revision": revision} for ref, revision in result.versions.items()],
            "notice_ids": list(result.notice_ids), "web_sources": list(result.web_sources)}
        choice._agentic_usage = result.usage
        return choice

    def prepare_qq(self, *, audience, entries, instruction, timeout=None, vision_tool=None, request_validator=None, vision_trace=None,
                   wake_only=False, runtime_state=None):
        from harness.memory.evidence_manifest import EvidenceManifestStore
        manifests = EvidenceManifestStore(self.store.memory)
        conversation = self.conversation(audience, audience)
        originals = {}
        with closing(self.store.memory._connect()) as db:
            for entry in entries:
                try:
                    source, _, _ = manifests._resolve(db, entry.source_id, audience)
                except ValueError:
                    continue
                if source.kind != "legacy_episode":
                    originals[source.record_id] = source
        latest = None
        for source in sorted(originals.values(), key=lambda s: (s.occurred_at, s.record_id)):
            role = "assistant" if source.kind == "self_expression" else "user"
            self.store.append(conversation, source, role=role, tokens=len(source.text.encode()), assimilate=role == "user")
            if role == "user":
                latest = source
        # The admitted gate instruction is host-authored. It is never archived
        # as a user's statement or submitted to automatic memory assimilation.
        text = instruction + ("\n当前群消息（作者 " + latest.author_id + "）：\n" + latest.text if latest else "")
        vision = None
        if vision_tool is not None:
            from harness.agentic.vision import AgentVision
            from harness.agentic.store import canonical
            vision = AgentVision(vision_tool, self.store, conversation, originals,
                request_validator=request_validator, trace=vision_trace)
            if vision.sources:
                text += "\n本轮可按需查看的附件（仅元数据，尚未看图）：\n" + canonical(vision.public())
            else:
                vision = None
        result = self.run(audience=audience, entrance=audience, text=text,
            source_id=latest.record_id if latest else "", timeout=timeout, vision=vision, wake_only=wake_only, runtime_state=runtime_state,
            web_validator=request_validator)
        return FinalResponse(text=result.finish.text, segments=[result.finish.text] if result.finish.text else [],
            path="deep" if result.calls > 1 else "fast", situation_id="agentic:" + conversation,
            event_id=latest.record_id if latest else conversation, usage=result.usage,
            agentic={"conversation": conversation, "shown_sources": sorted(result.shown),
                "references": [{"record_id": ref, "revision": revision} for ref, revision in result.versions.items()],
                "web_sources": list(result.web_sources)})

    def validate_evidence(self, evidence, *, audience):
        if evidence is None:
            return
        conv = self.store.get(evidence["conversation"])
        if conv["audience"] != audience or not self.store.memory.validate_references(
                evidence["references"], audience=audience):
            raise ValueError("agentic_prepared_evidence_changed")

    def acknowledge_delivery(self, final, receipt):
        if final.agentic:
            self.store.acknowledge_notices(final.agentic["conversation"], final.agentic.get("notice_ids", []), receipt)

    async def flush_affect_feedback(self):
        if self.settings.agentic_regulation.mode != 'on':
            return
        from datetime import datetime, timezone
        from harness.agentic.internal_state import runtime_endpoint
        from harness.homeostasis.native_appraisal import NativeAppraisalReceipt
        pending = self.affect.feedback()
        if not pending:
            return
        url, headers = runtime_endpoint()
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(url + '/v1/internal/agentic/state', headers=headers)
            response.raise_for_status()
            state = response.json()
            if state.get('mode') != 'on' or not state.get('running'):
                return
            receipts = []
            for item in pending:
                identity = digest([self.store.memory.owner_id, self.store.memory.preset_id, item['source_id']])
                assessment = {**item['assessment'], 'topic': digest(item['assessment']['topic']),
                    'wants_engagement': item['engagement'] == 'engage'}
                receipt = NativeAppraisalReceipt(receipt_id='native:' + item['kind'] + ':' + identity,
                    cause_id=identity, kind=item['kind'], occurred_at=item['occurred_at'],
                    source_refs=item['source_refs'], assessment=assessment,
                    engagement=item['engagement'], intent_until=datetime.fromtimestamp(item['intent_until'], timezone.utc))
                receipts.append(receipt.model_dump(mode='json'))
            response = await client.post(url + '/v1/internal/agentic/affect', headers=headers,
                json={'runtime_instance_id':state['runtime_instance_id'], 'manifest_revision':state['manifest_revision'],
                      'receipts':receipts})
            response.raise_for_status()
            if set(response.json().get('accepted', [])) != {r['receipt_id'] for r in receipts}:
                raise ValueError('native_affect_receipt_mismatch')
            self.affect.acknowledge(pending)

    async def commit_rest(self, job):
        from harness.agentic.internal_state import runtime_endpoint
        conv = self.store.get(job['conversation'])
        self.validate_evidence(job['payload']['evidence'], audience=conv['audience'])
        request = job['payload']['request']
        url, headers = runtime_endpoint()
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(url + '/v1/internal/agentic/rest/' + request['request_id'], headers=headers)
            if response.status_code == 404:
                response = await client.post(url + '/v1/internal/agentic/rest', headers=headers, json=request)
            response.raise_for_status()
            receipt = response.json()
            if receipt.get('request_id') != request['request_id'] or receipt.get('request_hash') != digest(request):
                raise ValueError('native_rest_receipt_mismatch')
            return receipt

    @asynccontextmanager
    async def background_scope(self, command_id):
        from harness.production.maintenance import ProductionMaintenanceDeferred
        if self.host.adapter._maintenance_paused:
            raise ProductionMaintenanceDeferred("agentic_runtime_paused")
        if getattr(self.host, "production_configured", False):
            from harness.cognitive.production import maintenance_scope
            async with maintenance_scope(self.host, command_id=command_id):
                yield
            return
        # The SQL memory authority is also used by non-embodied deployments.
        # Do not require a fictitious physiology provider just to save a memory.
        from harness.agentic.context import local_endpoint
        endpoint = os.environ.get("NC_RUNTIME_URL", "http://127.0.0.1:8770").rstrip("/")
        if not local_endpoint(endpoint):
            raise ValueError("agentic_runtime_requires_local_endpoint")
        token = os.environ.get("NC_SERVICE_TOKEN", "")
        if not token:
            raise ProductionMaintenanceDeferred("agentic_runtime_authentication_unavailable")
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(endpoint + "/v1/internal/agentic/status",
                    headers={"Authorization": "Bearer " + token})
                response.raise_for_status()
                allowed = response.json().get("running") is True
        except (httpx.HTTPError, ValueError):
            raise ProductionMaintenanceDeferred("agentic_runtime_unavailable") from None
        if not allowed or self.host.adapter._maintenance_paused:
            raise ProductionMaintenanceDeferred("agentic_runtime_paused")
        yield

    def pause_background(self):
        with self.priority.lock:
            if self.priority.task is not None and self.priority.loop is not None:
                self.priority.loop.call_soon_threadsafe(self.priority.task.cancel)

    async def commit_diary(self, original, *, day):
        from harness.agentic.context import local_endpoint
        endpoint = os.environ.get("NC_RUNTIME_URL", "http://127.0.0.1:8770").rstrip("/")
        if not local_endpoint(endpoint):
            raise ValueError("agentic_runtime_requires_local_endpoint")
        token = os.environ.get("NC_SERVICE_TOKEN", "")
        if not token:
            raise ValueError("agentic_diary_runtime_authentication_required")
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(endpoint + "/v1/internal/agentic/diary",
                headers={"Authorization": "Bearer " + token}, json={"original": original.model_dump(mode="json"), "day": day})
            response.raise_for_status()
            receipt = response.json()
            if receipt.get("entry_id") != original.entry_id or receipt.get("body_hash") != original.body_hash:
                raise ValueError("agentic_diary_receipt_mismatch")
        self.host.shared.commit_diary_entry(original)

    def task_snapshot(self):
        conversation = self.conversation("local:main", "local:" + self.host.chat.active_session_id)
        tasks = []
        for job in self.store.jobs(conversation, kind="diary"):
            artifact = self.store.artifact(job["id"])
            text = None
            if job["status"] == "applied" and artifact and self.store.validate_sources(conversation, artifact["sources"]):
                text = artifact["original"]["text"]
            tasks.append({"id": job["id"], "status": job["status"], "day": job["payload"]["day"], "text": text})
        return {"enabled": self.enabled, "conversation": conversation, "tasks": tasks, "last_error": self.worker.last_error}

    def health(self):
        with closing(self.store.memory._connect()) as db:
            jobs = [dict(row) for row in db.execute("SELECT kind,status,COUNT(*) AS count FROM agent_jobs_v2 GROUP BY kind,status")]
        return {"enabled": self.enabled, "local_only": self.settings.agentic_local_only,
            "tokenizer_backend": self.counter._backend,
            "model": self.settings.llm_model, "persona_sha256": digest(persona_system(self.persona_provider())),
            "background_running": self.worker.task is not None and not self.worker.task.done(),
            "foreground_turns": self.priority.foreground, "last_background_error": self.worker.last_error,
            "input_token_limit": self.context.cap, "jobs": jobs, "affect": self.affect.status()}

    async def start(self):
        await self.worker.start()
        from harness.agentic.mobile_autonomy import MobileAutonomy
        self.mobile_autonomy = MobileAutonomy(self)
        await self.mobile_autonomy.start()

    async def close(self):
        if getattr(self, 'mobile_autonomy', None):
            await self.mobile_autonomy.close()
        await self.worker.close()
        self.counter.close()
        self.llm.close()
