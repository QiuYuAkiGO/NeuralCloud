"""Official group transport sharing Misty's existing cognition and durable sender."""

from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib

from harness.qq.direct import DirectQQActor
from harness.qq.direct_media import close_media_work
from harness.qq.direct_store import canonical, normalize_message, utcnow
from harness.qq.memory import ProtocolMessageID
from harness.qq.official_client import OfficialQQClient
from harness.qq.official_identity import message_id, raw_message_id, split_identity
from harness.qq.official_state import initialize, require_platform_scope, revoke
from harness.qq.onebot import OneBotError
from harness.qq.conversation import conversation_scope, private_scope_id
from pydantic import TypeAdapter


class OfficialQQActor(DirectQQActor):
    def __init__(self, settings, *, official_client_factory=OfficialQQClient, **kwargs):
        if settings.transport != "qqbot":
            raise ValueError("qqbot_transport_required")
        super().__init__(settings, **kwargs)
        self.official_client_factory = official_client_factory
        self.state.update(
            service="qq-direct-official-v1",
            transport="qqbot",
            app_id=settings.qqbot_app_id,
            history_supported=False,
            proactive_supported=False,
        )
        self.history_supported = False
        self._sent_proofs = {}
        initialize(self.store)

    def active(self):
        return super().active() and self.client is not None and self.client.connected

    def write_status(self, **kwargs):
        client = getattr(self, "client", None)
        self.state["gateway_ingress"] = dict(getattr(client, "ingress_counts", {}))
        return super().write_status(**kwargs)

    def _ingress_count(self, stage):
        counts = self.state.setdefault("actor_ingress", {})
        counts[stage] = min(2147483647, counts.get(stage, 0) + 1)

    def current_conversations(self, *, send=False):
        permitted = set()
        for group in super().current_conversations(send=send):
            try:
                require_platform_scope(self.settings, group)
            except ValueError:
                continue
            permitted.add(group)
        return permitted

    async def on_event(self, payload, **kwargs):
        self._ingress_count("callback")
        if not self.active():
            self._ingress_count("inactive")
            return
        try:
            normalized = normalize_message(payload, self_id=self.settings.self_member_id, groups=self.groups, private_peers=self.private_peers)
        except (ValueError, KeyError, TypeError):
            self._ingress_count("invalid")
            await super().on_event(payload, **kwargs)
            return
        if normalized is not None:
            if normalized["group_id"] not in self.current_conversations():
                self._ingress_count("scope_unavailable")
                return
            # The actual authenticated inbound event proves this group/author;
            # a configured numeric group label or READY alone never does.
            ref = self.store.evidence(payload)
            self.group_evidence[normalized["group_id"]] = ref
            self.aliases[normalized["group_id"]] = []
            self._ingress_count("accepted_scope")
        else:
            from harness.qq.official_enrollment import capture_enrollment
            capture_enrollment(self.settings, payload)
            self._ingress_count("ignored_type_scope_or_bot")
        await super().on_event(payload, **kwargs)

    async def on_lifecycle(self, item):
        from harness.qq.official_identity import identity

        if item["event_type"] in {"FRIEND_ADD", "FRIEND_DEL", "C2C_MSG_REJECT", "C2C_MSG_RECEIVE"}:
            peer = identity(self.settings.qqbot_app_id, "user", item["data"].get("openid"))
            if peer not in self.private_peers:
                return
            group = private_scope_id(self.settings.self_member_id, peer)
        else:
            group = identity(self.settings.qqbot_app_id, "group", item["data"]["group_openid"])
            if group not in self.groups:
                return
        if item["event_type"] in {"GROUP_DEL_ROBOT", "GROUP_MSG_REJECT", "FRIEND_DEL", "C2C_MSG_REJECT"}:
            revoke(
                self.store,
                app_id=self.settings.qqbot_app_id,
                group_id=group,
                event_type=item["event_type"],
                evidence_ref=self.store.evidence(item),
                occurred_at=utcnow().isoformat(),
            )
            # Cancel every admitted generation across this transport epoch.
            self.epoch += 1
        # A receive/add event does not silently grant permission again: require
        # an explicit audited operator reauthorization after platform revocation.
        self.state["last_lifecycle_type"] = item["event_type"]

    def sent_message_id(self, result):
        value = TypeAdapter(ProtocolMessageID).validate_python(result["message_id"])
        raw_message_id(self.settings.qqbot_app_id, value)
        return value

    async def transmit_text(self, event, text, *, timeout):
        app_id = self.settings.qqbot_app_id
        scope = conversation_scope(event["group_id"])
        target = ({"user_openid": split_identity(scope["peer_id"], kind="user", app_id=app_id)}
            if scope["kind"] == "private" else
            {"group_openid": split_identity(event["group_id"], kind="group", app_id=app_id)})
        trigger = raw_message_id(app_id, event["message_id"])
        with closing(self.store.connect()) as db:
            rows = db.execute(
                "SELECT segment_index FROM qq_direct_sends WHERE event_id=? AND status='attempted'",
                (event["event_id"],),
            ).fetchall()
        if len(rows) != 1:
            raise OneBotError("qqbot_send_reservation_required")
        sequence = rows[0]["segment_index"] + 1
        response = await self.client.reply(
            **target, message_id=trigger, seq=sequence, text=text, timeout=timeout
        )
        sent = message_id(app_id, response.get("id"))
        proof = {
            "transport": "qqbot",
            "app_id": app_id,
            "action": "official_reply_ack",
            "group_id": event["group_id"],
            "trigger_message_id": event["message_id"],
            "message_id": sent,
            "msg_seq": sequence,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "response": response,
        }
        # ACK binds the authenticated POST path/body to its returned message id.
        # It is not an independent history read or a claim the user read it.
        ref = self.store.evidence(proof)
        self._sent_proofs[sent] = (canonical(proof), ref)
        return {"message_id": sent, "ack_ref": ref, "confirmation": "official_api_ack"}

    async def verify_sent(self, event, message_id, text, **kwargs):
        proof, ref = self._sent_proofs.pop(message_id, (None, None))
        if proof is None:
            raise OneBotError("qqbot_send_ack_missing")
        import json

        value = json.loads(proof)
        if (
            value["group_id"] != event["group_id"]
            or value["trigger_message_id"] != event["message_id"]
            or value["text_sha256"] != hashlib.sha256(text.encode()).hexdigest()
        ):
            raise OneBotError("qqbot_send_ack_mismatch")
        return ref

    async def process_contact(self):
        return False  # Official active-push authorization is not granted here.

    async def worker(self):
        while not self.stop.is_set():
            if self.active():
                row = self.store.pending(self.run_id, scheduler_mode=self.settings.qq_reply_scheduler)
                if row:
                    if (
                        self.event_policy(row["event"])["scheduler"] == "coalesced"
                        and not row.get("reply_request")
                        and not row["event"]["historical"]
                        and not row["event"]["own"]
                        and self.conversations[row["event"]["group_id"]].send
                    ):
                        self.enqueue_reply(row["event"])
                        continue
                    await self.process(row)
                    continue
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=0.5)
            except TimeoutError:
                pass

    async def run(self):
        worker = asyncio.create_task(self.worker())
        try:
            while not self.stop.is_set():
                try:
                    self.write_status(state="connecting", observation_gap=True)
                    self.client = self.official_client_factory(
                        self.settings.qqbot_credentials_file,
                        app_id=self.settings.qqbot_app_id,
                        on_event=self.on_event,
                        on_lifecycle=self.on_lifecycle,
                        send_users={split_identity(p.peer_id, kind="user", app_id=self.settings.qqbot_app_id)
                            for p in self.settings.private_contacts if p.send},
                        send_groups={
                            split_identity(g.group_id, kind="group", app_id=self.settings.qqbot_app_id)
                            for g in self.settings.group_roster
                            if g.send
                        },
                    )
                    await self.client.open()
                    self.connected_at = utcnow()
                    self.connected = True
                    self.epoch += 1
                    self.client.enabled.set()
                    self.write_status(
                        state="watching" if self.active() else "paused", last_error_type=None, observation_gap=False
                    )
                    while not self.stop.is_set() and not self.client.closed.is_set():
                        self.write_status()
                        try:
                            await asyncio.wait_for(self.stop.wait(), timeout=2)
                        except TimeoutError:
                            pass
                except Exception:
                    self.state["last_error_type"] = "qqbot_connection_unavailable"
                finally:
                    self.connected = False
                    self.epoch += 1
                    if self.client is not None:
                        await self.client.close()
                    self.write_status(
                        state="disconnected", observation_gap=True, last_disconnect_at=utcnow().isoformat()
                    )
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=5)
                except TimeoutError:
                    pass
        finally:
            self.stop.set()
            await worker
            await close_media_work()
            await self.text_turns.close()
            self.cognition.close()
            if self.llm is not None:
                self.llm.close()
            self.write_status(running=False, state="stopped")
