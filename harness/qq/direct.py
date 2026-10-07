"""OneBot QQ actor: protocol inbox, retained image understanding, shared Misty cognition."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import time

from harness.llm.client import OpenAICompatClient
from harness.llm.structured import build_structured_client
from harness.core.error_notice import ERROR_FALLBACK_SEND_SECONDS
from harness.qq.decision import (QQVerifiedSelfAlias, _frozen_decision, effective_reply_segments,
    get_qq_delivery, get_qq_segment_deliveries,
    record_qq_protocol_delivery, record_qq_protocol_segment_delivery, sendable_speak_outcome)
from harness.qq.direct_config import DirectQQSettings, atomic_status
from harness.qq.attention import AttentionStore, attention_path, text_units
from harness.qq.direct_images import DirectImageObservation, REPLY_RESERVE_SECONDS, load_and_describe_image
from harness.qq.direct_media import close_media_work, stage
from harness.qq.direct_store import DirectStore, message_parts, normalize_message, numeric, utcnow
from harness.qq.direct_store import normalize_evidence, verified_protocol_addressing
from harness.qq.conversation import conversation_scope, private_scope_id
from harness.qq.memory import (QQImageAnalysis, QQVerifiedProfileInput, QQVisualMemoryStore,
    QQVisualObservation, QQVisualObservationInput, protocol_message_evidence_ref)
from harness.qq.onebot import OneBotClient, OneBotError
from harness.qq.session import QQSessionSettings
from harness.qq.shared import QQSharedCognitiveClient
from harness.qq.reply_requests import ReplyRequestChanged, binding as reply_binding
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace, classify_error, current_qq_trace, qq_trace_scope
from harness.qq.vision import load_vision_settings


_CONNECTION_ERRORS = frozenset({"onebot_logged_in_account_mismatch", "onebot_account_offline",
    "onebot_groups_invalid", "onebot_configured_group_not_joined", "onebot_action_failed",
    "onebot_friends_invalid", "onebot_configured_peer_not_friend",
    "onebot_invalid_result", "onebot_disconnected", "onebot_request_timeout"})

logger = logging.getLogger(__name__)
_MIN_SEND_SECONDS = 2


def source_hash():
    root = Path(__file__).resolve().parents[2]
    files = sorted((root / "harness/qq").glob("*.py"))
    return "qq-direct:" + hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()


class DirectQQActor:
    def __init__(self, settings, *, client_factory=OneBotClient, cognition=None, llm=None):
        self.settings = settings
        self.session = QQSessionSettings.model_validate_json(settings.session_config_path.read_text(encoding="utf-8"))
        if (settings.self_member_id != self.session.self_member_id
                or any(g.group_id not in self.session.allowed_group_ids for g in settings.group_roster)
                or any(p.peer_id not in self.session.allowed_private_peer_ids for p in settings.private_contacts)):
            raise ValueError("direct_session_scope_mismatch")
        self.store = DirectStore(settings.state_db, settings.evidence_dir)
        self.attention = AttentionStore(attention_path(settings))
        self.run_id = self.store.start_run(settings.self_member_id)
        self.memory = QQVisualMemoryStore(self.session.memory_db)
        self.groups = {group.group_id: group for group in settings.group_roster if group.observe}
        self.private_peers = {p.peer_id: p for p in settings.private_contacts if p.observe}
        self.conversations = dict(self.groups) | {
            private_scope_id(settings.self_member_id, peer): contact for peer, contact in self.private_peers.items()}
        self.client_factory = client_factory
        self.client = None
        self.cognition = cognition or QQSharedCognitiveClient(settings.shared_runtime_url, settings.shared_control_token_file)
        self.llm = llm or (None if settings.qq_image_mode == "off" or settings.qq_image_analysis == "on_demand" else
            build_structured_client(load_vision_settings(settings.model_config_path, model=settings.vision_model))
            or OpenAICompatClient(load_vision_settings(settings.model_config_path, model=settings.vision_model)))
        self.stop = asyncio.Event()
        self.wakeup = asyncio.Event()
        self._delivery_lock = asyncio.Lock()
        self.connected = False
        self.connected_at = utcnow()
        self.epoch = 0
        self.group_evidence = {}
        self.aliases = {}
        self.history_supported = True
        self.state = dict(service="qq-direct-onebot-v1", pid=os.getpid(), run_id=self.run_id,
            started_at=utcnow().isoformat(), source_hash=source_hash(), running=True, state="starting",
            connected=False, automatic_send=False, images_enabled=settings.qq_image_mode != "off", last_error_type=None,
            self_member_id=settings.self_member_id, observation_gap=True,
            repository_root=str(Path(__file__).resolve().parents[2]), state_db=str(settings.state_db),
            group_roster=[group.model_dump() for group in settings.group_roster],
            private_contacts=[contact.model_dump() for contact in settings.private_contacts],
            qq_reply_scheduler=settings.qq_reply_scheduler, qq_reply_scheduler_source="direct_persisted",
            qq_media_cache_enabled=settings.qq_media_cache_enabled,
            qq_media_parallel_enabled=settings.qq_media_parallel_enabled,
            qq_image_mode=settings.qq_image_mode, qq_image_mode_source="direct_persisted",
            qq_image_analysis=settings.qq_image_analysis,
            qq_latency_conversations=settings.qq_latency_conversations,
            last_received_at=None, last_delivery_at=None, last_decision_action=None)

    def scope_policy(self, group_id):
        selected = (self.settings.qq_latency_conversations is None
            or f"{self.settings.self_member_id}/{group_id}" in self.settings.qq_latency_conversations)
        return {"scheduler": self.settings.qq_reply_scheduler if selected else "shadow",
            "media_cache": self.settings.qq_media_cache_enabled if selected else False,
            "media_parallel": self.settings.qq_media_parallel_enabled if selected else False,
            # This resource limit is independent of experimental latency rollout.
            "image_mode": self.settings.qq_image_mode}

    def event_policy(self, event):
        # Older inbox records predate scope rollout. Newly received records
        # freeze these diagnostic choices without changing protocol authority.
        value = event.get("_direct_policy", self.scope_policy(event["group_id"]))
        if isinstance(value, dict) and set(value) == {"scheduler", "media_cache", "media_parallel"}:
            value = value | {"image_mode": "all"}
        if (not isinstance(value, dict) or set(value) != {"scheduler", "media_cache", "media_parallel", "image_mode"}
                or value["scheduler"] not in {"legacy", "shadow", "coalesced"}
                or type(value["media_cache"]) is not bool or type(value["media_parallel"]) is not bool
                or type(value["image_mode"]) is not str or value["image_mode"] not in {"all", "one", "off"}):
            raise OneBotError("direct_request_binding_invalid")
        return value

    def paused(self):
        try:
            control = json.loads(self.settings.control_file.read_text(encoding="utf-8"))
            if control.get("stop") is True:
                self.stop.set()
                return True
            return control.get("paused") is not False
        except (ValueError, OSError):
            return True

    def desktop_active(self):
        try:
            status = json.loads(self.settings.desktop_status_file.read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(status["heartbeat_at"])
            return status.get("running") is True and -5 <= (utcnow() - stamp).total_seconds() <= 90
        except (ValueError, OSError, KeyError, TypeError):
            # Unknown ownership cannot enable another automatic sender.
            return True

    def active(self):
        return self.settings.enabled and self.connected and not self.paused() and not self.desktop_active()

    def current_conversations(self, *, send=False):
        """Revocations apply before collection and dispatch, not only after restart.

        Configuration changes may remove authority from this actor, but cannot
        add a new peer or rebind its transport/state while it is running.
        """
        try:
            path = os.environ.get("NC_QQ_DIRECT_CONFIG")
            current = DirectQQSettings.read(Path(path)) if path else self.settings
            if not current.enabled or any(getattr(current, key) != getattr(self.settings, key) for key in (
                    "self_member_id", "ws_url", "token_file", "state_db", "evidence_dir", "session_config_path")):
                return set()
            session = QQSessionSettings.model_validate_json(current.session_config_path.read_text(encoding="utf-8"))
            if session.self_member_id != self.settings.self_member_id:
                return set()
            groups = {entry.group_id for entry in current.group_roster
                      if entry.observe and (not send or entry.send) and entry.group_id in session.allowed_group_ids}
            peers = {private_scope_id(current.self_member_id, entry.peer_id) for entry in current.private_contacts
                     if entry.observe and (not send or entry.send) and entry.peer_id in session.allowed_private_peer_ids}
            return (groups | peers).intersection(self.conversations)
        except (OSError, ValueError):
            return set()

    def check_send(self, event, deadline, epoch, *, decision=None, untransmitted_send=None):
        deadline.check()
        self.check_attention(event)
        if not self.active() or self.stop.is_set() or self.epoch != epoch:
            raise OneBotError("direct_owner_unavailable")
        if event["group_id"] not in self.current_conversations(send=True):
            raise OneBotError("direct_group_read_only")
        if (untransmitted_send is not None and (decision is None
                or untransmitted_send.get("event_id") != event["event_id"]
                or untransmitted_send.get("decision_id") != decision.decision_id)):
            raise OneBotError("direct_delivery_unknown")
        uncertain = (self.store.uncertain() if untransmitted_send is None
                     else self.store.uncertain(untransmitted_send=untransmitted_send))
        if uncertain:
            raise OneBotError("direct_delivery_unknown")
        if event.get("_production_contact"):
            from harness.qq.direct_contacts import request, validate_source
            try:
                configured = os.environ.get("NC_QQ_DIRECT_CONFIG")
                current = DirectQQSettings.read(Path(configured)) if configured else self.settings
                with closing(self.store.connect()) as db:
                    _, contact = request(db, event["_production_contact"])
                    intent, source, _ = validate_source(current, self.session, db, contact)
                if (contact["event_id"] != event["event_id"] or intent.run_id != event["run_id"]
                        or (decision is not None and (decision.initiated_by != "production"
                            or decision.idle_intent_id != intent.intent_id
                            or source["source_record_id"] not in decision.source_record_ids))):
                    raise ValueError("direct_contact_binding_invalid")
            except Exception:
                raise OneBotError("direct_owner_unavailable") from None
            # New natural messages are perceived normally; a control revocation
            # and the original deadline still prevent this autonomous effect.
            return
        request = self.store.reply_requests.check(event)
        addressing = verified_protocol_addressing(event, evidence_dir=self.settings.evidence_dir,
            self_id=self.settings.self_member_id, groups=self.groups, private_peers=self.private_peers)
        protected = addressing in {"self_mention", "private"}
        if protected and decision is not None:
            row = self.store.get(event["event_id"])
            observation = row["event"].get("bound_observation", {})
            source = row["source_record_id"]
            refs = {event["event_evidence_ref"], protocol_message_evidence_ref(
                event["group_id"], event["member_id"], event["message_id"])}
            expected_source = "verified_private_message" if addressing == "private" else "verified_self_mention"
            active_authority = (request is not None and request["authority_anchor"] == event["event_id"]
                and request["addressing"] == addressing)
            protected = (decision.addressing == "direct" and decision.addressing_source == expected_source
                and decision.trigger_member_id == event["member_id"]
                and source in decision.source_record_ids
                and (source in decision.new_observation_record_ids or active_authority)
                and source in decision.addressing_source_record_ids
                and refs.issubset(decision.addressing_evidence_refs)
                and observation.get("record_id") == source
                and observation.get("author_attribution") == "qq_protocol_verified"
                and observation.get("group_id") == event["group_id"]
                and observation.get("member_id") == event["member_id"]
                and observation.get("message_id") == event["message_id"]
                and observation.get("capture_id") == event["event_id"]
                and refs.issubset(observation.get("attribution_evidence", [])))
        if self.store.newer(event, same_member_only=protected,
                excluded_event_ids=request["source_event_ids"] if request else ()):
            raise OneBotError("direct_newer_context")

    def write_status(self, **values):
        self.state.update(values)
        self.state.update(heartbeat_at=utcnow().isoformat(), connected=self.connected,
            automatic_send=self.active() and not self.store.uncertain(), counts=self.store.counts(),
            paused=self.paused(), desktop_owner_active=self.desktop_active(),
            qq_latency_conversations=self.settings.qq_latency_conversations,
            qq_conversation_policies={group: self.scope_policy(group) for group in self.conversations},
            attention_policies=self.attention.policies(), attention_counts=self.attention.counts())
        atomic_status(self.settings.status_file, self.state)

    def check_attention(self, event):
        reason = self.attention.check(event["group_id"], revision=event.get("_attention_revision"),
                                      received_at=None if event.get("_attention_explicit_read") else event.get("received_at"))
        if reason:
            raise OneBotError(reason)

    async def on_event(self, payload, *, historical=False, history_peer_id=None):
        # Staging the new bridge must not duplicate the active GUI collector.
        if not self.active():
            return
        try:
            if history_peer_id is not None:
                if not historical or history_peer_id not in self.private_peers:
                    raise ValueError("direct_private_history_scope_invalid")
                payload = {"action": "get_friend_msg_history", "self_id": self.settings.self_member_id,
                    "peer_id": history_peer_id, "result": payload}
                event = normalize_evidence(payload, self_id=self.settings.self_member_id, groups=self.groups,
                    private_peers=self.private_peers)
            else:
                event = normalize_message(payload, self_id=self.settings.self_member_id, groups=self.groups,
                    private_peers=self.private_peers)
            if event is None:
                return
            if event["group_id"] not in self.current_conversations():
                return
            event["_direct_policy"] = self.scope_policy(event["group_id"])
            stale = (utcnow() - datetime.fromisoformat(event["message_time"])).total_seconds() > 60
            historical = historical or stale or datetime.fromisoformat(event["message_time"]) < self.connected_at - timedelta(seconds=2)
            ref = self.store.evidence(payload)
            event["_attention_revision"] = self.attention.get(event["group_id"])["revision"]
            if self.store.receive(event, run_id=self.run_id, evidence_ref=ref, historical=historical):
                addressing = verified_protocol_addressing(event | {"event_evidence_ref": ref}, evidence_dir=self.settings.evidence_dir,
                    self_id=self.settings.self_member_id, groups=self.groups, private_peers=self.private_peers)
                admission = self.attention.admit(event["event_id"], event["group_id"], received_at=event["received_at"],
                    units=text_units(event["text"])+256*len(event["images"]), addressed=addressing in {"self_mention", "private"},
                    budget_enabled=self.settings.attention_budget_enabled and not historical and not event["own"],
                    group_count=len(self.groups))
                reason = admission["disposition"]
                if historical and self.settings.attention_budget_enabled and event["group_id"].isdigit() and reason == "admitted":
                    reason = "attention_backlog"
                if reason != "admitted":
                    self.store.update(event["event_id"], state="archived", terminal_reason=reason, terminal_stage="protocol.attention")
                    self.state["last_received_at"] = event["received_at"]
                    return
                if (self.event_policy(event)["scheduler"] == "coalesced" and not historical and not event["own"]
                        and self.conversations[event["group_id"]].send):
                    self.enqueue_reply(self.store.get(event["event_id"])["event"])
                self.state["last_received_at"] = event["received_at"]
                self.wakeup.set()
        except (ValueError, TypeError, KeyError):
            self.state["last_error_type"] = "direct_incoming_invalid"
            self.state["invalid_event_count"] = self.state.get("invalid_event_count", 0) + 1
        except Exception:
            self.state["last_error_type"] = "direct_incoming_storage_failed"
            raise OneBotError("onebot_event_callback_failed") from None

    def enqueue_reply(self, event):
        addressing = verified_protocol_addressing(event, evidence_dir=self.settings.evidence_dir,
            self_id=self.settings.self_member_id, groups=self.groups, private_peers=self.private_peers)
        return self.store.reply_requests.ingest(event, addressing=addressing)

    async def verify_connection(self):
        login = await self.client.call("get_login_info", timeout=8)
        if not isinstance(login, dict) or numeric(login.get("user_id")) != self.settings.self_member_id:
            raise OneBotError("onebot_logged_in_account_mismatch")
        state = await self.client.call("get_status", timeout=8)
        if state.get("online") is not True or state.get("good") is False:
            raise OneBotError("onebot_account_offline")
        groups = await self.client.call("get_group_list", timeout=8) if self.groups else []
        if not isinstance(groups, list):
            raise OneBotError("onebot_groups_invalid")
        joined = {numeric(group.get("group_id")) for group in groups if isinstance(group, dict)}
        if not set(self.groups).issubset(joined):
            raise OneBotError("onebot_configured_group_not_joined")
        login_ref = self.store.evidence({"action": "get_login_info", "result": login})
        self.group_evidence.clear()
        self.aliases.clear()
        for group_id in self.groups:
            profile_ref = login_ref
            group = next(item for item in groups if numeric(item.get("group_id")) == group_id)
            ref = self.store.evidence({"action": "get_group_list", "self_id": self.settings.self_member_id, "group": group})
            self.group_evidence[group_id] = ref
            aliases = [self.settings.self_member_id]
            try:
                profile = await self.client.call("get_group_member_info", {"group_id": int(group_id),
                    "user_id": int(self.settings.self_member_id), "no_cache": True}, timeout=5)
                if numeric(profile.get("user_id")) != self.settings.self_member_id or numeric(profile.get("group_id")) != group_id:
                    raise OneBotError("onebot_self_profile_mismatch")
                profile_ref = self.store.evidence({"action": "get_group_member_info", "result": profile})
                aliases.extend(value for value in (profile.get("card"), profile.get("nickname"))
                               if isinstance(value, str) and value.strip() and len(value) <= 128)
            except OneBotError:
                pass
            self.aliases[group_id] = [QQVerifiedSelfAlias(group_id=group_id,
                self_member_id=self.settings.self_member_id, alias=alias, evidence_refs=[profile_ref, ref],
                verification="qq_protocol_verified") for alias in dict.fromkeys(aliases)]
        if self.private_peers:
            friends = await self.client.call("get_friend_list", timeout=8)
            if not isinstance(friends, list):
                raise OneBotError("onebot_friends_invalid")
            by_id = {numeric(friend.get("user_id")): friend for friend in friends if isinstance(friend, dict)}
            if not set(self.private_peers).issubset(by_id):
                raise OneBotError("onebot_configured_peer_not_friend")
            for peer_id in self.private_peers:
                scope = private_scope_id(self.settings.self_member_id, peer_id)
                ref = self.store.evidence({"action": "get_friend_list", "self_id": self.settings.self_member_id,
                    "peer_id": peer_id, "friend": by_id[peer_id]})
                self.group_evidence[scope] = ref
                names = [self.settings.self_member_id]
                if isinstance(login.get("nickname"), str) and 0 < len(login["nickname"].strip()) <= 128:
                    names.append(login["nickname"])
                self.aliases[scope] = [QQVerifiedSelfAlias(group_id=scope, self_member_id=self.settings.self_member_id,
                    alias=name, evidence_refs=[login_ref, ref], verification="qq_protocol_verified")
                    for name in dict.fromkeys(names)]

    async def observe(self, event, deadline, *, trace=None, defer_image_analysis=None, image_admission=None):
        self.check_attention(event)
        policy = self.event_policy(event)
        ref = event["event_evidence_ref"]
        message_ref = protocol_message_evidence_ref(event["group_id"], event["member_id"], event["message_id"])
        if event.get("bound_observation") is not None:
            # A new request revision can include the same already committed
            # message. Reuse only that exact immutable message, never a prior
            # profile from another message or freshly re-inferred image claims.
            existing = QQVisualObservation.model_validate(event["bound_observation"])
            with closing(sqlite3.connect(self.session.memory_db.as_uri() + "?mode=ro", uri=True)) as db:
                raw = db.execute("SELECT record_json FROM qq_visual_observations WHERE record_id=?",
                                 (existing.record_id,)).fetchone()
            if (raw is None or QQVisualObservation.model_validate_json(raw[0]) != existing
                    or existing.capture_id != event["event_id"] or existing.capture_item_id != event["event_id"]
                    or existing.group_id != event["group_id"] or existing.member_id != event["member_id"]
                    or existing.message_id != event["message_id"] or existing.text != event["text"]
                    or existing.author_attribution != "qq_protocol_verified"
                    or not {ref, message_ref}.issubset(existing.attribution_evidence)):
                raise OneBotError("direct_decision_source_mismatch")
            if trace:
                trace.add_counter("protocol_observation_reused")
            return existing
        profile_input = QQVerifiedProfileInput(group_id=event["group_id"],
            member_id=event["member_id"], display_name=event["display_name"], display_name_source="qq_metadata",
            verification="qq_protocol_verified", capture_id=event["event_id"] + ":profile",
            observed_at=datetime.fromisoformat(event["received_at"]),
            group_evidence=[self.group_evidence[event["group_id"]]], member_evidence=[ref])
        analyses, refs = [], [ref, message_ref]
        async def read_image(index, image):
            self.check_attention(event)
            mode = policy["image_mode"]
            observation_only = (event["historical"] or event["own"]
                                or not self.conversations[event["group_id"]].send)
            admitted = (mode != "off" and (mode != "one" or not observation_only)
                and ((event["event_id"], index) in image_admission if image_admission is not None
                     else mode != "one" or index == 1))
            if not admitted:
                if trace:
                    trace.add_counter("image_policy_skipped")
                reason = ("图片处理已关闭" if mode == "off" else
                          "受限图片策略不处理历史、自身回显或只读会话附件" if observation_only else
                          "本次回复最多处理一张图片，此附件未准入" if mode == "one" else
                          "本次回复的图片策略未准入此附件")
                return DirectImageObservation(error="vision_unavailable", uncertainties=[
                    reason + "；保留原始消息中的附件记录，未下载或识别图片。"])
            if trace:
                trace.add_counter("image_policy_admitted")
            from infra.trace.request_spans import trace_scope
            with trace_scope(attention_context="qq:" + event["group_id"],
                             attention_revision=event.get("attention_revision"),
                             event_id=event["event_id"], attention_evidence_refs=[ref, message_ref]):
                return await load_and_describe_image(image, call_api=self.client.call,
                    cache_dir=self.settings.image_cache_dir, llm=self.llm, deadline=deadline,
                    allowed_local_roots=tuple(self.settings.allowed_local_image_roots), trace=trace,
                    cache_enabled=policy["media_cache"],
                    reserve_seconds=0.0 if observation_only else REPLY_RESERVE_SECONDS,
                    defer_analysis=defer_image_analysis,
                    acquire_only=self.settings.qq_image_analysis == "on_demand",
                    priority=10 if event["historical"] or event["own"] else 0)
        indexed_images = list(enumerate(event["images"], 1))
        values = (await asyncio.gather(*(read_image(index, image) for index, image in indexed_images))
                  if policy["media_parallel"] else [await read_image(index, image) for index, image in indexed_images])
        for index, value in enumerate(values, 1):
            if value.evidence_ref:
                refs.append(value.evidence_ref)
            error = value.error if value.error in {"download_failed", "unsupported_media", "vision_failed",
                "deadline_expired", "invalid_image", "image_too_large", "unavailable"} else "unavailable"
            uncertainties = value.uncertainties[:16]
            ocr = value.ocr_text or None
            if ocr is not None and len(ocr) > 6000:
                ocr = ocr[:6000]
                uncertainties = [*uncertainties[:15], "OCR因上下文预算截断；原图证据仍保留。"]
            analyses.append(QQImageAnalysis(image_index=index, status=value.status, evidence_ref=value.evidence_ref,
                analysis_ref=value.analysis_ref,
                description=value.description or None, ocr_text=ocr,
                uncertainties=uncertainties,
                error=None if value.status == "described" or value.error == "vision_on_demand" else error))
        with stage(trace, "protocol.observation_commit"):
            self.check_attention(event)
            _, written = self.memory.record_protocol_message(profile_input, lambda profile:
                QQVisualObservationInput(group_id=event["group_id"],
                    member_id=event["member_id"], profile_record_id=profile.record_id, capture_id=event["event_id"],
                    capture_item_id=event["event_id"], observed_at=datetime.fromisoformat(event["message_time"]), text=event["text"],
                    evidence=refs, author_attribution="qq_protocol_verified", attribution_evidence=[ref, message_ref],
                    extraction_method="onebot11_message_array", message_id=event["message_id"], message_id_source="qq_metadata",
                    image_analysis=analyses))
            observation = written.observation
        self.store.update(event["event_id"], state="bound", source_record_id=observation.record_id,
                          observation=observation.model_dump(mode="json"))
        return observation

    async def verify_sent(self, event, message_id, text, *, timeout=10, history_count=20):
        scope = conversation_scope(event["group_id"])
        if scope["kind"] == "private":
            if scope["self_member_id"] != self.settings.self_member_id or scope["peer_id"] not in self.private_peers:
                raise OneBotError("onebot_sent_private_scope_invalid")
            # Neither get_msg nor an own-message WS event proves the recipient
            # on this NapCat version. Bind confirmation to the queried friend.
            params = {"user_id": scope["peer_id"], "count": 20,
                "reverse_order": False, "disable_get_url": False}
            history = await self.client.call("get_friend_msg_history", params, timeout=timeout)
            matches = [item for item in history.get("messages", [])
                if isinstance(item, dict) and str(item.get("message_id")) == message_id]
            if len(matches) != 1:
                raise OneBotError("onebot_sent_private_unverified")
            envelope = {"action": "get_friend_msg_history", "self_id": self.settings.self_member_id,
                "peer_id": scope["peer_id"], "result": matches[0]}
            confirmed = normalize_evidence(envelope, self_id=self.settings.self_member_id,
                groups=(), private_peers=self.private_peers)
            if (confirmed is None or confirmed["group_id"] != event["group_id"] or not confirmed["own"]
                    or confirmed["text"] != text or confirmed["message_id"] != message_id or confirmed["images"]):
                raise OneBotError("onebot_sent_message_mismatch")
            return self.store.evidence(envelope)
        proof_action = "get_msg"
        try:
            result = await self.client.call("get_msg", {"message_id": int(message_id)}, timeout=timeout)
        except OneBotError as exc:
            # NapCat loses its message-ID cache on restart. A bounded history
            # read can still prove that exact ID; never retry a send.
            if exc.code != "onebot_action_failed":
                raise
            result = {}
        if not result.get("group_id"):
            history = await self.client.call("get_group_msg_history", {"group_id": int(event["group_id"]),
                "count": history_count}, timeout=timeout)
            matches = [item for item in history.get("messages", [])
                       if isinstance(item, dict) and str(item.get("message_id")) == message_id]
            if len(matches) != 1:
                raise OneBotError("onebot_sent_group_unverified")
            result = matches[0]
            proof_action = "get_group_msg_history"
        sender = result.get("sender", {})
        if (numeric(result.get("group_id")) != event["group_id"]
                or numeric(result.get("message_id"), signed=True) != message_id
                or numeric(sender.get("user_id")) != self.settings.self_member_id
                or message_parts(result.get("message"))[0] != text):
            raise OneBotError("onebot_sent_message_mismatch")
        return self.store.evidence({"action": proof_action, "group_id": event["group_id"], "result": result})

    async def send_decision(self, event, decision, deadline, epoch, *, trace=None):
        async with self._delivery_lock:
            await self._send_decision(event, decision, deadline, epoch, trace=trace or current_qq_trace())

    async def transmit_text(self, event, text, *, timeout):
        """The sole protocol text-send primitive for replies and frozen notices."""
        scope = conversation_scope(event["group_id"])
        action = "send_private_msg" if scope["kind"] == "private" else "send_group_msg"
        target = {"user_id": scope["peer_id"]} if scope["kind"] == "private" else {"group_id": int(event["group_id"])}
        return await self.client.call(action, target | {
            "message": [{"type": "text", "data": {"text": text}}], "auto_escape": True}, timeout=timeout)

    async def _send_decision(self, event, decision, deadline, epoch, *, trace=None):
        if not decision.new_decision or decision.replayed or not sendable_speak_outcome(decision.outcome) or decision.action != "speak":
            return
        texts = effective_reply_segments(decision)
        self.check_send(event, deadline, epoch, decision=decision)
        # Receipt validation happens after sending; reject an unfrozen or
        # changed plan before the first protocol mutation as well.
        try:
            path = self.session.memory_db.resolve()
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
                db.row_factory = sqlite3.Row
                frozen = _frozen_decision(db, event["group_id"], decision.decision_id)
            fields = ("decision_id", "input_hash", "group_id", "self_member_id", "action", "outcome",
                "reply", "reply_segments", "source_record_ids", "new_observation_record_ids",
                "initiated_by", "idle_intent_id", "addressing", "trigger_member_id",
                "addressing_source", "addressing_source_record_ids", "addressing_evidence_refs")
            expected_origin = "production" if event.get("_production_contact") else "message"
            if (not sendable_speak_outcome(frozen.outcome) or frozen.initiated_by != expected_origin
                    or any(getattr(frozen, name) != getattr(decision, name) for name in fields)
                    or self.store.get(event["event_id"])["source_record_id"] not in frozen.source_record_ids):
                raise ValueError("frozen_plan_mismatch")
        except Exception:
            raise OneBotError("direct_frozen_plan_mismatch") from None
        if self.store.cooldown(event["group_id"], 10 if decision.addressing == "direct" else 60):
            self.store.update(event["event_id"], state="rate_limited",
                              terminal_reason="direct_cooldown", terminal_stage="protocol.delivery")
            return
        receipts = []
        for index, text in enumerate(texts):
            self.check_send(event, deadline, epoch, decision=frozen)
            if deadline.check() < _MIN_SEND_SECONDS:
                raise OneBotError("direct_send_budget_insufficient")
            send = self.store.reserve(event, decision, index, text)
            try:
                self.check_send(event, deadline, epoch, decision=frozen)
                if deadline.check() < _MIN_SEND_SECONDS:
                    raise OneBotError("direct_send_budget_insufficient")
            except Exception:
                self.store.update_send(send, "cancelled")
                raise
            scope = conversation_scope(event["group_id"])
            action = "send_private_msg" if scope["kind"] == "private" else "send_group_msg"
            send = self.store.update_send(send, "attempted", attempted_at=utcnow().isoformat())
            # SQLite can wait for a write lock. Check again after its commit,
            # before any protocol call, while non-transmission is still known.
            try:
                self.check_send(event, deadline, epoch, decision=frozen, untransmitted_send=send)
                if deadline.check() < _MIN_SEND_SECONDS:
                    raise OneBotError("direct_send_budget_insufficient")
            except Exception as exc:
                self.store.cancel_untransmitted(send, reason=getattr(exc, "code", "direct_send_budget_insufficient"))
                raise
            try:
                result = await self.transmit_text(event, text, timeout=min(10, deadline.check()))
                message_id = numeric(result.get("message_id"), signed=True)
                send = self.store.update_send(send, "attempted", message_id=message_id)
                ack = self.store.evidence({"action": action, "group_id": event["group_id"],
                    "self_id": self.settings.self_member_id, "result": result})
                # Read-only receipt verification may finish after the send TTL.
                proof = await self.verify_sent(event, message_id, text)
                if index == 0 and trace is not None:
                    trace.record("protocol.first_verified_segment", started_mono=trace.clock())
                    trace.add_counter("protocol_first_verified_segment_ms", round(max(0,
                        trace.clock() - trace.first_seen_mono) * 1000))
                delivered_at = utcnow()
                receipt = record_qq_protocol_segment_delivery(memory_db=self.session.memory_db,
                    group_id=event["group_id"], self_member_id=self.settings.self_member_id,
                    decision_id=decision.decision_id, segment_index=index, segment_count=len(texts), delivered_text=text,
                    source_record_ids=[] if event.get("_production_contact") else [send["source_record_id"]], delivered_at=delivered_at,
                    protocol_message_id=message_id, evidence_refs=[ack, proof,
                        protocol_message_evidence_ref(event["group_id"], self.settings.self_member_id, message_id)])
                self.store.update_send(send, "sent", receipt_id=receipt.receipt_id, delivered_at=delivered_at.isoformat())
                receipts.append(receipt)
                self.state["last_delivery_at"] = delivered_at.isoformat()
            except Exception:
                self.store.update_send(send, "unknown")
                self.store.update(event["event_id"], state="unknown")
                raise OneBotError("direct_delivery_unknown") from None
        record_qq_protocol_delivery(memory_db=self.session.memory_db, group_id=event["group_id"],
            self_member_id=self.settings.self_member_id, decision_id=decision.decision_id,
            delivered_text="\n\n".join(texts), source_record_ids=[] if event.get("_production_contact") else [self.store.get(event["event_id"])["source_record_id"]],
            evidence_refs=list(dict.fromkeys(ref for receipt in receipts for ref in receipt.evidence_refs)),
            protocol_message_ids=[receipt.protocol_message_id for receipt in receipts],
            delivered_at=max(receipt.delivered_at for receipt in receipts))
        if trace is not None:
            trace.record("protocol.fully_verified", started_mono=trace.clock())
            trace.add_counter("protocol_fully_verified_ms", round(max(0,
                trace.clock() - trace.first_seen_mono) * 1000))
        self.store.update(event["event_id"], state="sent")

    async def reconcile_unknown(self):
        """Recover uncertain sends, using read-only protocol calls.

        Missing ACK identities require a unique, exact persisted own echo and
        independent protocol confirmation. No retry, reservation, continuation
        or renewed reply deadline is created, including after process restart.
        The delivery lock prevents racing an in-flight sender's receipt write.
        """
        if not self.active() or self.stop.is_set() or self._delivery_lock.locked():
            return
        async with self._delivery_lock:
            epoch, client = self.epoch, self.client
            budget = QQDeadline.after(15)
            recovered = 0
            checked = 0
            for send in self.store.outstanding_reconciliation(limit=4, include_unidentified=True):
                if (not self.active() or self.stop.is_set() or self.epoch != epoch
                        or self.client is not client or client.closed.is_set() or budget.expired):
                    break
                checked += 1
                self.write_status()
                try:
                    row = self.store.get(send["event_id"])
                    event = row["event"]
                    if (send["self_member_id"] != self.settings.self_member_id
                            or send["group_id"] not in self.conversations
                            or event["self_member_id"] != self.settings.self_member_id
                            or event["group_id"] != send["group_id"]
                            or row["source_record_id"] != send["source_record_id"]
                            or row["decision_id"] != send["decision_id"]):
                        raise OneBotError("direct_reconciliation_binding_invalid")
                    with closing(sqlite3.connect(self.session.memory_db.resolve().as_uri() + "?mode=ro",
                                                 uri=True, timeout=3)) as db:
                        db.row_factory = sqlite3.Row
                        frozen = _frozen_decision(db, send["group_id"], send["decision_id"])
                    texts = effective_reply_segments(frozen)
                    index = send["segment_index"]
                    if (frozen.self_member_id != self.settings.self_member_id or not sendable_speak_outcome(frozen.outcome)
                            or type(index) is not int or not 0 <= index < len(texts)
                            or texts[index] != send["text"] or send["source_record_id"] not in frozen.source_record_ids):
                        raise OneBotError("direct_reconciliation_binding_invalid")
                    echo = None
                    message_id = send["message_id"]
                    if message_id is None:
                        echo = self.store.reconciliation_echo(send)
                        if echo is None:
                            raise OneBotError("direct_reconciliation_echo_unresolved")
                        message_id = echo["message_id"]
                    async with asyncio.timeout(budget.check()):
                        proof = await self.verify_sent(event, message_id, send["text"],
                                                       timeout=min(3, budget.check()), history_count=200)
                    if (not self.active() or self.stop.is_set() or self.epoch != epoch
                            or self.client is not client or client.closed.is_set()):
                        break
                    if echo is not None:
                        send = self.store.bind_reconciliation_echo(send, event_id=echo["event_id"], proof_ref=proof)
                    prefix = get_qq_segment_deliveries(memory_db=self.session.memory_db,
                        group_id=send["group_id"], decision_id=send["decision_id"])
                    if index < len(prefix):
                        receipt = prefix[index]
                        if (receipt.verification != "qq_protocol_verified"
                                or receipt.protocol_message_id != send["message_id"]
                                or receipt.source_record_ids != ([] if event.get("_production_contact") else [send["source_record_id"]])):
                            raise OneBotError("direct_reconciliation_binding_invalid")
                    else:
                        receipt = record_qq_protocol_segment_delivery(memory_db=self.session.memory_db,
                            group_id=send["group_id"], self_member_id=self.settings.self_member_id,
                            decision_id=send["decision_id"], segment_index=index, segment_count=len(texts),
                            delivered_text=send["text"], source_record_ids=[] if event.get("_production_contact") else [send["source_record_id"]],
                            delivered_at=utcnow(), protocol_message_id=send["message_id"],
                            evidence_refs=([send["recovery_echo_ref"]] if send.get("recovery_echo_ref") else []) +
                                [proof, protocol_message_evidence_ref(send["group_id"],
                                self.settings.self_member_id, send["message_id"])])
                    prefix = get_qq_segment_deliveries(memory_db=self.session.memory_db,
                        group_id=send["group_id"], decision_id=send["decision_id"])
                    if len(prefix) == len(texts):
                        aggregate = get_qq_delivery(memory_db=self.session.memory_db,
                            group_id=send["group_id"], decision_id=send["decision_id"])
                        if aggregate is None:
                            record_qq_protocol_delivery(memory_db=self.session.memory_db,
                                group_id=send["group_id"], self_member_id=self.settings.self_member_id,
                                decision_id=send["decision_id"], delivered_text="\n\n".join(texts),
                                source_record_ids=sorted({ref for item in prefix for ref in item.source_record_ids}),
                                evidence_refs=sorted({ref for item in prefix for ref in item.evidence_refs}),
                                protocol_message_ids=[item.protocol_message_id for item in prefix],
                                delivered_at=max(item.delivered_at for item in prefix))
                        self.store.update(send["event_id"], state="sent")
                    else:
                        self.store.update(send["event_id"], state="partial")
                    # Clear the uncertainty gate only after all native receipt
                    # and event updates succeed. A storage failure remains
                    # retryable by this read-only path after the next restart.
                    self.store.confirm_reconciled(send, receipt_id=receipt.receipt_id,
                                                  delivered_at=receipt.delivered_at)
                    recovered += 1
                    self.state["last_delivery_at"] = receipt.delivered_at.isoformat()
                except Exception:
                    # A failed read or inconsistent proof keeps the original
                    # unknown state; it can never trigger a replacement send.
                    self.state["last_reconciliation_error"] = "direct_reconciliation_unresolved"
            self.state.update(last_reconciliation_at=utcnow().isoformat(),
                              last_reconciliation_checked=checked, last_reconciliation_recovered=recovered)
            if recovered and not self.store.uncertain():
                self.state.update(last_error_type=None, last_reconciliation_error=None)
            self.write_status()

    async def process(self, row):
        event, epoch = row["event"], self.epoch
        request = row.get("reply_request")
        policy = self.event_policy(event)
        if request is not None:
            # A persisted request remains a coalesced intent after rollout is disabled.
            policy = policy | {"scheduler": "coalesced"}
        first_seen = datetime.fromisoformat(event["received_at"])
        if event["historical"] or event["own"]:
            # These observations never enter cognition or delivery. A short
            # per-item read budget bounds the delay before the worker can pick
            # up a live message, even while initial history is being imported.
            deadline = QQDeadline.after(12)
        else:
            deadline = QQDeadline.from_first_seen(first_seen)
            if not self.conversations[event["group_id"]].send:
                deadline = QQDeadline(min(deadline.deadline_mono, deadline.clock() + 12), deadline.clock)
        trace = QQTurnTrace(event_id=event["event_id"], first_seen_at=first_seen,
            first_seen_mono=max(0, time.monotonic() - max(0, (utcnow() - first_seen).total_seconds())))
        trace.bind_conversation(group_id=event["group_id"], self_member_id=event["self_member_id"])
        trace.add_counter("protocol_queue_wait_ms", round(max(0, (utcnow() - first_seen).total_seconds()) * 1000))
        trace.add_counter("scheduler_mode_" + policy["scheduler"])
        trace.add_counter("media_cache_enabled", int(policy["media_cache"]))
        trace.add_counter("media_parallel_enabled", int(policy["media_parallel"]))
        trace.add_counter("image_mode_" + policy["image_mode"])
        image_count = sum(len(self.store.get(source)["event"]["images"]) for source in request["source_event_ids"]) if request else len(event["images"])
        trace.add_counter("input_image_count", image_count)
        trace.add_counter("input_has_images", int(image_count > 0))
        trace.add_counter("input_reply_eligible", int(not event["historical"] and not event["own"]
            and self.conversations[event["group_id"]].send))
        if request:
            trace.add_counter("reply_request_revision", request["revision"])
            trace.add_counter("reply_request_source_count", len(request["source_event_ids"]))
        terminal_reason = None
        terminal_stage = "protocol.read"
        try:
            self.check_attention(event)
            if event["group_id"] not in self.current_conversations():
                raise OneBotError("direct_owner_unavailable")
            self.write_status(state="reading_message", last_error_type=None)
            defer_image_analysis = None
            if (event["images"] and request is None and event.get("_reply_request") is None
                    and not event["historical"] and not event["own"]
                    and self.conversations[event["group_id"]].send
                    and event.get("protocol_addressing") == "ambient"
                    and verified_protocol_addressing(event, evidence_dir=self.settings.evidence_dir,
                        self_id=self.settings.self_member_id, groups=self.groups,
                        private_peers=self.private_peers) == "ambient"):
                def defer_image_analysis():
                    # Retain the immutable text and pixels even when this
                    # ambient reply is obsolete, but do not spend foreground
                    # vision time on it. Explicit requests and batch sources
                    # always keep their normal image preparation.
                    try:
                        self.check_send(event, deadline, epoch)
                    except OneBotError as exc:
                        return exc.code == "direct_newer_context"
                    except QQDeadlineExpired:
                        return False
                    return False
            with trace.stage("protocol.read"):
                self.store.reply_requests.check(event)
                image_admission = None
                if policy["image_mode"] != "all":
                    sources = (request["source_event_ids"] if request else [event["event_id"]])
                    # Sources only append on revision. The first attachment is
                    # therefore stable across revisions, including a failed or
                    # already-bound first image; failure never grants a new slot.
                    selected_image = next(((source, 1) for source in sources
                        if self.store.get(source)["event"]["images"]), None)
                    image_admission = (frozenset({selected_image}) if policy["image_mode"] == "one"
                                       and selected_image is not None else frozenset())
                observation = await self.observe(event, deadline, trace=trace,
                    defer_image_analysis=defer_image_analysis, image_admission=image_admission)
                if request:
                    for source in request["source_event_ids"][1:]:
                        self.store.reply_requests.check(event)
                        source_row = self.store.get(source)
                        await self.observe(source_row["event"], deadline, trace=trace, image_admission=image_admission)
            if event["historical"] or event["own"] or not self.conversations[event["group_id"]].send:
                terminal_reason = ("observed_history" if event["historical"] else
                                   "observed_own" if event["own"] else "observed_read_only")
                self.store.update(event["event_id"], state="observed")
                return
            terminal_stage = "protocol.precheck"
            with trace.stage("protocol.precheck"):
                self.check_send(event, deadline, epoch)
            self.store.reply_requests.check(event, state="preparing")
            self.store.update(event["event_id"], state="preparing", reply_request=event.get("_reply_request"))
            self.write_status(state="deciding")
            terminal_stage = "protocol.shared_prepare"
            with trace.stage("protocol.shared_prepare"):
                self.check_attention(event)
                decision = await asyncio.to_thread(self.cognition, transport="onebot", deadline=deadline, trace=trace,
                    group_id=event["group_id"], self_member_id=self.settings.self_member_id,
                    trigger_member_id=event["member_id"], verified_self_aliases=self.aliases[event["group_id"]],
                    **(reply_binding(request) if request else {}))
            terminal_stage = "protocol.decision"
            trace.add_counter("protocol_error_fallback", int(decision.outcome == "error_fallback"))
            self.store.reply_requests.check(event, state="frozen")
            if (decision.group_id != event["group_id"] or decision.self_member_id != self.settings.self_member_id
                    or observation.record_id not in decision.source_record_ids
                    or (request and not {self.store.get(source)["source_record_id"] for source in request["source_event_ids"]}
                        .issubset(decision.source_record_ids))):
                raise OneBotError("direct_decision_source_mismatch")
            self.state["last_decision_action"] = decision.action
            self.store.update(event["event_id"], state="ready" if decision.action == "speak" else "wait",
                              decision_id=decision.decision_id, reply_request=event.get("_reply_request"))
            if decision.action == "speak":
                terminal_stage = "protocol.delivery"
                self.write_status(state="sending")
                send_deadline = QQDeadline.after(ERROR_FALLBACK_SEND_SECONDS) if decision.outcome == "error_fallback" else deadline
                with trace.stage("protocol.delivery"), qq_trace_scope(trace):
                    await self.send_decision(event, decision, send_deadline, epoch)
                current = self.store.get(event["event_id"])
                terminal_reason = (current["terminal_reason"] or
                    ("sent" if current["state"] == "sent" else
                     "decision_replayed" if decision.replayed or not decision.new_decision else "decision_not_sendable"))
            else:
                terminal_reason = (decision.error_type if decision.error_type else
                                   decision.outcome if decision.outcome in {"natural_wait", "guard_wait"} else "decision_wait")
        except Exception as exc:
            if isinstance(exc, ReplyRequestChanged):
                terminal_reason = exc.code
                if exc.code == "direct_request_revision_changed":
                    # Ingress has already queued the new revision. Do not let
                    # this obsolete evaluation overwrite its still-live anchor.
                    trace.record_termination(reason=terminal_reason, stage=terminal_stage)
                    terminal_reason = None
                    return
            terminal_reason = classify_error(exc)
            if isinstance(exc, ReplyRequestChanged):
                terminal_reason = exc.code
            if isinstance(exc, sqlite3.Error) or (isinstance(exc, OSError) and not isinstance(exc, (TimeoutError, ConnectionError))):
                terminal_reason = "storage_error"
            elif terminal_stage != "protocol.shared_prepare" and terminal_reason.startswith("model_"):
                terminal_reason = "internal_error"
            current = self.store.get(event["event_id"])
            if current["state"] != "unknown":
                with closing(self.store.connect()) as db:
                    sent = db.execute("SELECT 1 FROM qq_direct_sends WHERE event_id=? AND status='sent'", (event["event_id"],)).fetchone()
                self.store.update(event["event_id"], state="partial" if sent else "archived" if terminal_reason.startswith("attention_") else "expired" if deadline.expired else "failed")
            code = terminal_reason if isinstance(exc, OneBotError) else "direct_turn_expired" if deadline.expired else "direct_turn_failed"
            self.state["last_error_type"] = code
        finally:
            if terminal_reason is not None:
                self.store.update(event["event_id"], terminal_reason=terminal_reason, terminal_stage=terminal_stage)
                trace.record_termination(reason=terminal_reason, stage=terminal_stage)
                self.store.reply_requests.finish(event, self.store.get(event["event_id"])["state"])
            trace.flush(self.settings.state_db.with_suffix(".trace.jsonl"))
            self.write_status(state="watching" if self.active() else "paused")

    async def process_contact(self):
        from harness.qq.direct_contacts import finish, internal_event
        from harness.qq.idle import QQIdleIntent
        with closing(self.store.connect()) as db, db:
            # Reconcile prior effects read-only after interruption. Never turn
            # a pending claim into another model attempt or protocol send.
            pending = db.execute("SELECT id,result_json FROM qq_production_contact_results WHERE json_extract(result_json,'$.status') IN ('pending','uncertain')").fetchall()
            for row in pending:
                previous = json.loads(row[1])
                if not previous.get("event_id"):
                    continue
                observed = db.execute("SELECT state FROM qq_direct_events WHERE event_id=?", (previous["event_id"],)).fetchone()
                if observed is not None and observed[0] not in {"preparing", "ready"}:
                    status = "completed" if observed[0] == "sent" else "uncertain" if observed[0] in {"unknown", "partial"} else "rejected"
                    finish(db, row[0], {**previous, "status": status})
            candidate = db.execute("""SELECT q.id,q.request_json FROM qq_production_contact_requests q
                LEFT JOIN qq_production_contact_results r ON q.id=r.id
                WHERE q.authorized=1 AND r.id IS NULL ORDER BY q.rowid LIMIT 1""").fetchone()
        if candidate is None:
            return False
        identity, contact = candidate[0], json.loads(candidate[1])
        event = None
        try:
            intent = QQIdleIntent.model_validate(contact["intent"])
            event = internal_event(self.store, self.settings, self.session, contact)
            current = self.store.get(event["event_id"])
            if current["state"] != "preparing" or current["decision_id"] is not None:
                # An interrupted preparation or effect is never a fresh attempt.
                raise OneBotError("direct_contact_already_claimed")
            # Durable claim precedes cognition so actor restarts cannot repeat it.
            with closing(self.store.connect()) as db, db:
                finish(db, identity, {"status": "pending", "event_id": event["event_id"]})
            deadline = QQDeadline.after(max(0, (intent.deadline_at - utcnow()).total_seconds()))
            trace = QQTurnTrace(event_id=event["event_id"], first_seen_at=intent.created_at)
            epoch = self.epoch
            self.check_send(event, deadline, epoch)
            decision = await asyncio.to_thread(self.cognition, transport="onebot", deadline=deadline, trace=trace,
                group_id=intent.group_id, self_member_id=intent.self_member_id, idle_intent=intent,
                verified_self_aliases=self.aliases.get(intent.group_id, []))
            if (decision.group_id != intent.group_id or decision.self_member_id != intent.self_member_id
                    or decision.initiated_by != "production" or decision.idle_intent_id != intent.intent_id):
                raise OneBotError("direct_contact_decision_invalid")
            self.store.update(event["event_id"], state="ready" if decision.action == "speak" else "wait", decision_id=decision.decision_id)
            if decision.action == "speak":
                await self.send_decision(event, decision, deadline, epoch, trace=trace)
            state = self.store.get(event["event_id"])["state"]
            result = {"status": "completed" if state == "sent" else "wait" if state == "wait" else "rejected",
                      "event_id": event["event_id"], "decision_id": decision.decision_id}
            receipt = get_qq_delivery(memory_db=self.session.memory_db, group_id=intent.group_id, decision_id=decision.decision_id)
            if receipt:
                result["evidence_refs"] = receipt.evidence_refs
                result["receipt_id"] = receipt.receipt_id
        except Exception as exc:
            state = self.store.get(event["event_id"])["state"] if event else None
            result = {"status": "uncertain" if state in {"unknown", "partial"} else "rejected",
                      "error_type": getattr(exc, "code", type(exc).__name__), "event_id": event["event_id"] if event else None}
        with closing(self.store.connect()) as db, db:
            finish(db, identity, result)
        return True

    async def worker(self):
        from harness.qq.task_notifications import process_notice
        while not self.stop.is_set():
            if self.active():
                if await process_notice(self):
                    continue
                row = self.store.pending(self.run_id, scheduler_mode=self.settings.qq_reply_scheduler)
                if row is not None:
                    if (self.event_policy(row["event"])["scheduler"] == "coalesced" and not row.get("reply_request")
                            and not row["event"]["historical"] and not row["event"]["own"]
                            and self.conversations[row["event"]["group_id"]].send):
                        # Recover the narrow receive/intent-write interruption
                        # without dropping the durable source or refreshing TTL.
                        self.enqueue_reply(row["event"])
                        continue
                    await self.process(row)
                    continue
                if await self.process_contact():
                    continue
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=.5)
            except TimeoutError:
                pass

    async def history(self):
        if not self.history_supported or not self.active():
            return
        for group_id in self.groups:
            if group_id not in self.current_conversations():
                continue
            try:
                result = await self.client.call("get_group_msg_history", {"group_id": int(group_id),
                    "count": self.settings.history_count, "disable_get_url": False}, timeout=8)
                for message in result.get("messages", []):
                    await self.on_event(message | {"post_type": "message", "message_type": "group",
                        "group_id": int(group_id), "self_id": int(self.settings.self_member_id)}, historical=True)
            except OneBotError:
                # This is an optional OneBot implementation extension.
                self.state["history_error"] = "history_unavailable"
        for peer_id in self.private_peers:
            if private_scope_id(self.settings.self_member_id, peer_id) not in self.current_conversations():
                continue
            try:
                result = await self.client.call("get_friend_msg_history", {"user_id": peer_id,
                    "count": self.settings.history_count, "reverse_order": False, "disable_get_url": False}, timeout=8)
                for message in result.get("messages", []):
                    await self.on_event(message, historical=True, history_peer_id=peer_id)
            except OneBotError:
                # NapCat reports an error for a friend with no local history.
                # The live friend channel remains usable for its first message.
                self.state["private_history_error"] = "history_unavailable_or_empty"

    async def run(self):
        worker = asyncio.create_task(self.worker())
        last_connection_error = None
        try:
            while not self.stop.is_set():
                connection_error = None
                try:
                    self.write_status(state="connecting")
                    self.client = self.client_factory(self.settings.ws_url,
                        self.settings.token_file.read_text(encoding="utf-8").strip(), on_event=self.on_event)
                    await self.client.open()
                    await self.verify_connection()
                    self.connected_at = utcnow()
                    self.connected = True
                    self.epoch += 1
                    logger.info("qq_direct_connected")
                    last_connection_error = None
                    self.write_status(state="watching" if self.active() else "paused", last_error_type=None,
                                      observation_gap=False)
                    next_history = 0
                    next_reconciliation = 0
                    while not self.stop.is_set() and not self.client.closed.is_set():
                        if self.active() and time.monotonic() >= next_reconciliation:
                            await self.reconcile_unknown()
                            from harness.qq.task_notifications import reconcile_notices
                            await reconcile_notices(self)
                            next_reconciliation = time.monotonic() + 30
                        if self.active() and time.monotonic() >= next_history:
                            await self.history()
                            next_history = time.monotonic() + self.settings.history_poll_seconds
                        self.write_status()
                        try:
                            await asyncio.wait_for(self.stop.wait(), timeout=2)
                        except TimeoutError:
                            pass
                except Exception as exc:
                    connection_error = (exc.code if isinstance(exc, OneBotError) and exc.code in _CONNECTION_ERRORS
                                        else "onebot_connection_unavailable")
                    self.state["last_error_type"] = connection_error
                finally:
                    self.connected = False
                    error = connection_error or getattr(self.client, "last_error_type", None) or "onebot_disconnected"
                    self.state.update(observation_gap=True, last_disconnect_at=utcnow().isoformat(),
                                      last_gap_error_type=error)
                    if not self.stop.is_set():
                        self.state["last_error_type"] = error
                        if error != last_connection_error:
                            # Only locally defined codes reach the journal;
                            # remote exception text can contain credentials.
                            safe_error = error if error in _CONNECTION_ERRORS or error in {
                                "onebot_event_callback_failed", "onebot_event_queue_overflow",
                                "onebot_invalid_envelope"} else "onebot_connection_unavailable"
                            logger.warning("qq_direct_disconnected error_type=%s; retrying", safe_error)
                            last_connection_error = error
                    if self.client:
                        await self.client.close()
                self.write_status(state="disconnected")
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=3)
                except TimeoutError:
                    pass
        finally:
            self.stop.set()
            await worker
            await close_media_work()
            self.cognition.close()
            if self.llm is not None:
                self.llm.close()
            self.write_status(running=False, state="stopped")


def main(argv=None):
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.setLevel(logging.INFO)
    parser = argparse.ArgumentParser(description="Misty QQ via authenticated OneBot 11, with retained image understanding")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    settings = DirectQQSettings.read(args.config)
    # OS lock prevents two direct actors from owning the same durable inbox.
    lock_path = settings.state_db.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        actor = DirectQQActor(settings)
        # Preserve the launcher's absolute binding. Windows MSIX may map the
        # physical path into LocalCache when resolve() traverses virtualization.
        actor.state["config_path"] = str(args.config.absolute())
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, lambda *_: actor.stop.set())
        asyncio.run(actor.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
