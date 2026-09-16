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

import httpx

from harness.llm.client import OpenAICompatClient
from harness.core.error_notice import ERROR_FALLBACK_SEND_SECONDS
from harness.qq.decision import (QQVerifiedSelfAlias, _frozen_decision, effective_reply_segments,
    get_qq_delivery, get_qq_segment_deliveries,
    record_qq_protocol_delivery, record_qq_protocol_segment_delivery, sendable_speak_outcome)
from harness.qq.direct_config import DirectQQSettings, atomic_status
from harness.qq.direct_images import REPLY_RESERVE_SECONDS, load_and_describe_image
from harness.qq.direct_store import DirectStore, message_parts, normalize_message, numeric, utcnow
from harness.qq.direct_store import normalize_evidence
from harness.qq.conversation import conversation_scope, private_scope_id
from harness.qq.memory import (QQImageAnalysis, QQVerifiedProfileInput, QQVisualMemoryStore,
    QQVisualObservationInput, protocol_message_evidence_ref)
from harness.qq.onebot import OneBotClient, OneBotError
from harness.qq.session import QQSessionSettings
from harness.qq.shared import QQSharedCognitiveClient
from harness.qq.telemetry import QQDeadline, QQTurnTrace
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
        self.run_id = self.store.start_run(settings.self_member_id)
        self.memory = QQVisualMemoryStore(self.session.memory_db)
        self.groups = {group.group_id: group for group in settings.group_roster if group.observe}
        self.private_peers = {p.peer_id: p for p in settings.private_contacts if p.observe}
        self.conversations = dict(self.groups) | {
            private_scope_id(settings.self_member_id, peer): contact for peer, contact in self.private_peers.items()}
        self.client_factory = client_factory
        self.client = None
        self.cognition = cognition or QQSharedCognitiveClient(settings.shared_runtime_url, settings.shared_control_token_file)
        self.llm = llm or OpenAICompatClient(load_vision_settings(settings.model_config_path, model=settings.vision_model))
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
            connected=False, automatic_send=False, images_enabled=True, last_error_type=None,
            self_member_id=settings.self_member_id, observation_gap=True,
            repository_root=str(Path(__file__).resolve().parents[2]), state_db=str(settings.state_db),
            group_roster=[group.model_dump() for group in settings.group_roster],
            private_contacts=[contact.model_dump() for contact in settings.private_contacts],
            last_received_at=None, last_delivery_at=None, last_decision_action=None)

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

    def check_send(self, event, deadline, epoch):
        deadline.check()
        if not self.active() or self.stop.is_set() or self.epoch != epoch:
            raise OneBotError("direct_owner_unavailable")
        if not self.conversations[event["group_id"]].send:
            raise OneBotError("direct_group_read_only")
        if self.store.uncertain():
            raise OneBotError("direct_delivery_unknown")
        if self.store.newer(event):
            raise OneBotError("direct_newer_context")

    def write_status(self, **values):
        self.state.update(values)
        self.state.update(heartbeat_at=utcnow().isoformat(), connected=self.connected,
            automatic_send=self.active() and not self.store.uncertain(), counts=self.store.counts(),
            paused=self.paused(), desktop_owner_active=self.desktop_active())
        atomic_status(self.settings.status_file, self.state)

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
            stale = (utcnow() - datetime.fromisoformat(event["message_time"])).total_seconds() > 60
            historical = historical or stale or datetime.fromisoformat(event["message_time"]) < self.connected_at - timedelta(seconds=2)
            ref = self.store.evidence(payload)
            if self.store.receive(event, run_id=self.run_id, evidence_ref=ref, historical=historical):
                self.state["last_received_at"] = event["received_at"]
                self.wakeup.set()
        except (ValueError, TypeError, KeyError):
            self.state["last_error_type"] = "direct_incoming_invalid"
            self.state["invalid_event_count"] = self.state.get("invalid_event_count", 0) + 1
        except Exception:
            self.state["last_error_type"] = "direct_incoming_storage_failed"
            raise OneBotError("onebot_event_callback_failed") from None

    async def verify_connection(self):
        login = await self.client.call("get_login_info", timeout=8)
        if not isinstance(login, dict) or numeric(login.get("user_id")) != self.settings.self_member_id:
            raise OneBotError("onebot_logged_in_account_mismatch")
        state = await self.client.call("get_status", timeout=8)
        if state.get("online") is not True or state.get("good") is False:
            raise OneBotError("onebot_account_offline")
        groups = await self.client.call("get_group_list", timeout=8)
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

    async def observe(self, event, deadline):
        ref = event["event_evidence_ref"]
        message_ref = protocol_message_evidence_ref(event["group_id"], event["member_id"], event["message_id"])
        profile = self.memory.record_protocol_profile(QQVerifiedProfileInput(group_id=event["group_id"],
            member_id=event["member_id"], display_name=event["display_name"], display_name_source="qq_metadata",
            verification="qq_protocol_verified", capture_id=event["event_id"] + ":profile",
            observed_at=datetime.fromisoformat(event["received_at"]),
            group_evidence=[self.group_evidence[event["group_id"]]], member_evidence=[ref])).profile
        analyses, refs = [], [ref, message_ref]
        sendable = (not event["historical"] and not event["own"]
                    and self.conversations[event["group_id"]].send)
        reserve = REPLY_RESERVE_SECONDS if sendable else 0.0
        owned_client = httpx.AsyncClient(trust_env=False, follow_redirects=False) if event["images"] else None
        try:
            for index, image in enumerate(event["images"], 1):
                value = await load_and_describe_image(image, call_api=self.client.call,
                    cache_dir=self.settings.image_cache_dir, llm=self.llm, deadline=deadline,
                    allowed_local_roots=tuple(self.settings.allowed_local_image_roots),
                    http_client=owned_client, reserve_seconds=reserve)
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
                    description=value.description or None, ocr_text=ocr,
                    uncertainties=uncertainties, error=None if value.status == "described" else error))
        finally:
            if owned_client is not None:
                await owned_client.aclose()
        observation = self.memory.record_protocol_observation(QQVisualObservationInput(group_id=event["group_id"],
            member_id=event["member_id"], profile_record_id=profile.record_id, capture_id=event["event_id"],
            capture_item_id=event["event_id"], observed_at=datetime.fromisoformat(event["message_time"]), text=event["text"],
            evidence=refs, author_attribution="qq_protocol_verified", attribution_evidence=[ref, message_ref],
            extraction_method="onebot11_message_array", message_id=event["message_id"], message_id_source="qq_metadata",
            image_analysis=analyses)).observation
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

    async def send_decision(self, event, decision, deadline, epoch):
        async with self._delivery_lock:
            await self._send_decision(event, decision, deadline, epoch)

    async def _send_decision(self, event, decision, deadline, epoch):
        if not decision.new_decision or decision.replayed or not sendable_speak_outcome(decision.outcome) or decision.action != "speak":
            return
        texts = effective_reply_segments(decision)
        self.check_send(event, deadline, epoch)
        # Receipt validation happens after sending; reject an unfrozen or
        # changed plan before the first protocol mutation as well.
        try:
            path = self.session.memory_db.resolve()
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
                db.row_factory = sqlite3.Row
                frozen = _frozen_decision(db, event["group_id"], decision.decision_id)
            fields = ("decision_id", "input_hash", "group_id", "self_member_id", "action", "outcome",
                "reply", "reply_segments", "source_record_ids", "new_observation_record_ids",
                "initiated_by", "idle_intent_id", "addressing")
            if (not sendable_speak_outcome(frozen.outcome) or frozen.initiated_by != "message"
                    or any(getattr(frozen, name) != getattr(decision, name) for name in fields)
                    or self.store.get(event["event_id"])["source_record_id"] not in frozen.source_record_ids):
                raise ValueError("frozen_plan_mismatch")
        except Exception:
            raise OneBotError("direct_frozen_plan_mismatch") from None
        if self.store.cooldown(event["group_id"], 10 if decision.addressing == "direct" else 60):
            self.store.update(event["event_id"], state="rate_limited")
            return
        receipts = []
        for index, text in enumerate(texts):
            self.check_send(event, deadline, epoch)
            if deadline.check() < _MIN_SEND_SECONDS:
                raise OneBotError("direct_send_budget_insufficient")
            send = self.store.reserve(event, decision, index, text)
            try:
                self.check_send(event, deadline, epoch)
                if deadline.check() < _MIN_SEND_SECONDS:
                    raise OneBotError("direct_send_budget_insufficient")
            except Exception:
                self.store.update_send(send, "cancelled")
                raise
            scope = conversation_scope(event["group_id"])
            action = "send_private_msg" if scope["kind"] == "private" else "send_group_msg"
            target = {"user_id": scope["peer_id"]} if scope["kind"] == "private" else {"group_id": int(event["group_id"])}
            send = self.store.update_send(send, "attempted", attempted_at=utcnow().isoformat())
            # SQLite can wait for a write lock. Check again after its commit,
            # before any protocol call, while non-transmission is still known.
            if deadline.remaining() < _MIN_SEND_SECONDS:
                self.store.cancel_untransmitted(send)
                raise OneBotError("direct_send_budget_insufficient")
            try:
                result = await self.client.call(action, target | {
                    "message": [{"type": "text", "data": {"text": text}}], "auto_escape": True},
                    timeout=min(10, deadline.check()))
                message_id = numeric(result.get("message_id"), signed=True)
                send = self.store.update_send(send, "attempted", message_id=message_id)
                ack = self.store.evidence({"action": action, "group_id": event["group_id"],
                    "self_id": self.settings.self_member_id, "result": result})
                # Read-only receipt verification may finish after the send TTL.
                proof = await self.verify_sent(event, message_id, text)
                delivered_at = utcnow()
                receipt = record_qq_protocol_segment_delivery(memory_db=self.session.memory_db,
                    group_id=event["group_id"], self_member_id=self.settings.self_member_id,
                    decision_id=decision.decision_id, segment_index=index, segment_count=len(texts), delivered_text=text,
                    source_record_ids=[send["source_record_id"]], delivered_at=delivered_at,
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
            delivered_text="\n\n".join(texts), source_record_ids=[self.store.get(event["event_id"])["source_record_id"]],
            evidence_refs=list(dict.fromkeys(ref for receipt in receipts for ref in receipt.evidence_refs)),
            protocol_message_ids=[receipt.protocol_message_id for receipt in receipts],
            delivered_at=max(receipt.delivered_at for receipt in receipts))
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
                                or receipt.source_record_ids != [send["source_record_id"]]):
                            raise OneBotError("direct_reconciliation_binding_invalid")
                    else:
                        receipt = record_qq_protocol_segment_delivery(memory_db=self.session.memory_db,
                            group_id=send["group_id"], self_member_id=self.settings.self_member_id,
                            decision_id=send["decision_id"], segment_index=index, segment_count=len(texts),
                            delivered_text=send["text"], source_record_ids=[send["source_record_id"]],
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
        trace.add_counter("protocol_queue_wait_ms", round(max(0, (utcnow() - first_seen).total_seconds()) * 1000))
        try:
            self.write_status(state="reading_message", last_error_type=None)
            with trace.stage("protocol.read"):
                observation = await self.observe(event, deadline)
            if event["historical"] or event["own"] or not self.conversations[event["group_id"]].send:
                self.store.update(event["event_id"], state="observed")
                return
            self.check_send(event, deadline, epoch)
            self.store.update(event["event_id"], state="preparing")
            self.write_status(state="deciding")
            with trace.stage("protocol.shared_prepare"):
                decision = await asyncio.to_thread(self.cognition, transport="onebot", deadline=deadline, trace=trace,
                    group_id=event["group_id"], self_member_id=self.settings.self_member_id,
                    trigger_member_id=event["member_id"], verified_self_aliases=self.aliases[event["group_id"]])
            if (decision.group_id != event["group_id"] or decision.self_member_id != self.settings.self_member_id
                    or observation.record_id not in decision.source_record_ids):
                raise OneBotError("direct_decision_source_mismatch")
            self.state["last_decision_action"] = decision.action
            self.store.update(event["event_id"], state="ready" if decision.action == "speak" else "wait",
                              decision_id=decision.decision_id)
            if decision.action == "speak":
                self.write_status(state="sending")
                send_deadline = QQDeadline.after(ERROR_FALLBACK_SEND_SECONDS) if decision.outcome == "error_fallback" else deadline
                with trace.stage("protocol.delivery"):
                    await self.send_decision(event, decision, send_deadline, epoch)
        except Exception as exc:
            current = self.store.get(event["event_id"])
            if current["state"] != "unknown":
                with closing(self.store.connect()) as db:
                    sent = db.execute("SELECT 1 FROM qq_direct_sends WHERE event_id=? AND status='sent'", (event["event_id"],)).fetchone()
                self.store.update(event["event_id"], state="partial" if sent else "expired" if deadline.expired else "failed")
            code = exc.code if isinstance(exc, OneBotError) else "direct_turn_expired" if deadline.expired else "direct_turn_failed"
            self.state["last_error_type"] = code
        finally:
            trace.flush(self.settings.state_db.with_suffix(".trace.jsonl"))
            self.write_status(state="watching" if self.active() else "paused")

    async def worker(self):
        while not self.stop.is_set():
            if self.active():
                row = self.store.pending(self.run_id)
                if row is not None:
                    await self.process(row)
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
            self.cognition.close()
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
