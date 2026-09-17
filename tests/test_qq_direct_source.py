"""Real protocol JSON, SQLite provenance and outbox boundaries; no QQ calls."""

from contextlib import closing
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from harness.qq.decision import QQReplyDecision
from harness.qq.direct_config import DirectQQSettings
from harness.qq.direct_source import DirectQQSourceExpired, validate_direct_source
from harness.qq.direct_store import DirectStore, canonical, normalize_message
from harness.qq.memory import QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput, protocol_message_evidence_ref
from harness.qq.shared import QQSharedPrepareRequest
from harness.runtime.qq_turns import _source


HOME, GUEST, SELF, MEMBER = "1044946257", "1060979317", "2015188508", "2285027817"


@pytest.fixture
def direct(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    memory = QQVisualMemoryStore(tmp_path / "memory.sqlite3")
    store = DirectStore(tmp_path / "direct.sqlite3", tmp_path / "evidence")
    run = store.start_run(SELF)
    paths = {name: tmp_path / name for name in ("token_file", "session_config_path", "status_file", "control_file",
        "image_cache_dir", "desktop_status_file", "shared_control_token_file")}
    settings = DirectQQSettings(enabled=True, self_member_id=SELF, group_roster=[
        {"group_id": HOME, "observe": True, "send": True}, {"group_id": GUEST, "observe": True, "send": False}],
        state_db=store.path, evidence_dir=store.evidence_dir, **paths)
    paths["session_config_path"].write_text("{}")
    config_path = tmp_path / "direct.json"
    config_path.write_text(settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG", str(config_path))
    config = SimpleNamespace(allowed_group_ids=[HOME, GUEST], self_member_id=SELF, memory_db=memory.path, watcher_status_file=None)

    def create(*, group=HOME, member=MEMBER, message_id=123, historical=False, images=True, received_at=None,
               message_time=None):
        moment = received_at or now
        raw = {"post_type": "message", "message_type": "group", "self_id": int(SELF), "group_id": int(group),
            "user_id": int(member), "message_id": message_id, "time": int((message_time or moment).timestamp()),
            "sender": {"user_id": int(member), "nickname": "测试成员"},
            "message": [{"type": "text", "data": {"text": "真实原文 "}}]}
        if images:
            raw["message"].append({"type": "image", "data": {"file": "real-image-id", "url": "https://multimedia.nt.qq.com.cn/test"}})
        event = normalize_message(raw, self_id=SELF, groups={HOME, GUEST}, received_at=moment)
        ref = store.evidence(raw)
        assert store.receive(event, run_id=run, evidence_ref=ref, historical=historical)
        profile = memory.record_protocol_profile(QQVerifiedProfileInput(group_id=group, member_id=member,
            display_name="测试成员", display_name_source="qq_metadata", verification="qq_protocol_verified",
            capture_id=event["event_id"] + ":profile", observed_at=moment,
            group_evidence=[ref], member_evidence=[ref])).profile
        message_ref = protocol_message_evidence_ref(group, member, str(message_id))
        observation = memory.record_protocol_observation(QQVisualObservationInput(group_id=group, member_id=member,
            profile_record_id=profile.record_id, capture_id=event["event_id"], capture_item_id=event["event_id"],
            observed_at=datetime.fromisoformat(event["message_time"]), text=event["text"], evidence=[ref, message_ref],
            author_attribution="qq_protocol_verified", attribution_evidence=[ref, message_ref],
            extraction_method="onebot11_message_array", message_id=str(message_id), message_id_source="qq_metadata",
            image_analysis=[{"image_index": 1, "status": "unknown", "error": "unavailable"}] if images else [])).observation
        store.update(event["event_id"], state="bound", source_record_id=observation.record_id,
                     observation=observation.model_dump(mode="json"))
        request = QQSharedPrepareRequest(transport="onebot", group_id=group, self_member_id=SELF,
            trigger_member_id=member, deadline_at=moment + timedelta(seconds=60), trace={
                "trace_id": "qq-trace:" + "a" * 32, "event_id": event["event_id"], "first_seen_at": moment,
                "first_seen_mono": 0})
        return SimpleNamespace(raw=raw, event=store.get(event["event_id"])["event"], observation=observation,
                               profile=profile, request=request, evidence=ref)

    return SimpleNamespace(store=store, memory=memory, run=run, config=config, settings=settings, create=create, now=now)


def rewrite_event(direct, event_id, **updates):
    with closing(direct.store.connect()) as db, db:
        row = db.execute("SELECT record_json FROM qq_direct_events WHERE event_id=?", (event_id,)).fetchone()
        db.execute("UPDATE qq_direct_events SET record_json=? WHERE event_id=?",
                   (canonical(json.loads(row[0]) | updates), event_id))


def ready(direct, source, *, segments=None):
    texts = segments or ["第一段", "第二段"]
    result = QQReplyDecision(decision_id="qq-decision:" + "b" * 32, input_hash="synthetic", created_at=direct.now,
        action="speak", reason="准备回复", reply="\n\n".join(texts), reply_segments=texts, outcome="ready",
        decision_source="model", group_id=source.event["group_id"], self_member_id=SELF,
        participants=[MEMBER], source_record_ids=[source.observation.record_id], new_observation_record_ids=[source.observation.record_id],
        evidence_refs=[source.evidence], delivery_refs=[], model="synthetic", persona_version="same-misty", preset_id="misty")
    direct.store.update(source.event["event_id"], state="ready", decision_id=result.decision_id)
    return result


def test_real_raw_hash_native_binding_and_runtime_branch_accept_same_protocol_authority(direct):
    source = direct.create()
    evidence_file = direct.store.evidence_dir / (source.evidence.rsplit("/", 1)[1] + ".json")
    assert hashlib.sha256(evidence_file.read_bytes()).hexdigest() == source.evidence.rsplit("/", 1)[1]
    assert validate_direct_source(direct.config, source.request) is None
    assert _source(direct.config, None, source.request) is None


def test_legacy_image_without_optional_analysis_ref_keeps_exact_source_binding(direct):
    source = direct.create()
    legacy = source.observation.model_dump(mode="json")
    for image in legacy["image_analysis"]:
        image.pop("analysis_ref", None)
    with closing(direct.memory._connect()) as db, db:
        db.execute("UPDATE qq_visual_observations SET record_json=? WHERE record_id=?",
                   (canonical(legacy), source.observation.record_id))
    rewrite_event(direct, source.event["event_id"], bound_observation=legacy)
    assert validate_direct_source(direct.config, source.request) is None
    assert _source(direct.config, None, source.request) is None


def test_nonnull_analysis_artifact_change_is_not_normalized_away(direct):
    source = direct.create()
    changed = source.observation.model_dump(mode="json")
    changed["image_analysis"][0]["analysis_ref"] = "qq-image-analysis:sha256:" + "a" * 64
    with closing(direct.memory._connect()) as db, db:
        db.execute("UPDATE qq_visual_observations SET record_json=? WHERE record_id=?",
                   (canonical(changed), source.observation.record_id))
    with pytest.raises(ValueError, match="binding_invalid"):
        validate_direct_source(direct.config, source.request)


@pytest.mark.parametrize("field,value", [("group_id", GUEST), ("self_member_id", "999"),
    ("run_id", "missing"), ("member_id", "999"), ("message_id", "999"),
    ("received_at", "2020-01-01T00:00:00+00:00"), ("text", "篡改原文"),
    ("images", []), ("historical", True), ("own", True)])
def test_native_json_cannot_disagree_with_index_or_raw_protocol(direct, field, value):
    source = direct.create()
    rewrite_event(direct, source.event["event_id"], **{field: value})
    with pytest.raises(ValueError):
        validate_direct_source(direct.config, source.request)


@pytest.mark.parametrize("field,value", [("group_id", GUEST), ("self_id", "999"), ("member_id", "999"),
    ("run_id", "missing"), ("message_id", "999"), ("source_record_id", "missing")])
def test_native_sql_identity_cannot_disagree_with_raw_or_memory(direct, field, value):
    source = direct.create()
    with closing(direct.store.connect()) as db, db:
        db.execute(f"UPDATE qq_direct_events SET {field}=? WHERE event_id=?", (value, source.event["event_id"]))
    with pytest.raises(ValueError):
        validate_direct_source(direct.config, source.request)


def test_evidence_bytes_are_rehashed_and_existing_hash_objects_cannot_be_overwritten(direct):
    source = direct.create()
    path = direct.store.evidence_dir / (source.evidence.rsplit("/", 1)[1] + ".json")
    path.write_text("{}")
    with pytest.raises(ValueError, match="evidence_changed"):
        validate_direct_source(direct.config, source.request)
    with pytest.raises(ValueError, match="evidence_changed"):
        direct.store.evidence(source.raw)


@pytest.mark.parametrize("options", [{"group": GUEST}, {"member": SELF}, {"historical": True},
    {"message_age": 61}, {"received_age": 61}])
def test_read_only_own_history_old_message_and_expired_turn_never_prepare(direct, options):
    options = dict(options)
    if "message_age" in options:
        options["message_time"] = direct.now - timedelta(seconds=options.pop("message_age"))
    if "received_age" in options:
        options["received_at"] = direct.now - timedelta(seconds=options.pop("received_age"))
    source = direct.create(**options)
    with pytest.raises(ValueError):
        validate_direct_source(direct.config, source.request)


@pytest.mark.parametrize("state", ["bound", "expired"])
def test_only_verified_elapsed_reply_window_has_typed_expiry(direct, state):
    source = direct.create(received_at=direct.now - timedelta(seconds=61))
    direct.store.update(source.event["event_id"], state=state)
    with pytest.raises(DirectQQSourceExpired, match="direct_qq_source_expired"):
        validate_direct_source(direct.config, source.request)
    evidence_file = direct.store.evidence_dir / (source.evidence.rsplit("/", 1)[1] + ".json")
    evidence_file.write_text("{}")
    with pytest.raises(ValueError, match="direct_qq_evidence_changed") as error:
        validate_direct_source(direct.config, source.request)
    assert not isinstance(error.value, DirectQQSourceExpired)


@pytest.mark.parametrize("state", ["ready", "failed", "partial", "sent", "cancelled"])
def test_other_nonpending_states_remain_real_failures_after_deadline(direct, state):
    source = direct.create(received_at=direct.now - timedelta(seconds=61))
    direct.store.update(source.event["event_id"], state=state)
    with pytest.raises(ValueError, match="direct_qq_source_not_pending") as error:
        validate_direct_source(direct.config, source.request)
    assert not isinstance(error.value, DirectQQSourceExpired)


@pytest.mark.parametrize("binding", ["early_expired", "decision", "send"])
def test_expired_state_without_the_unprepared_elapsed_binding_is_not_deadline(direct, binding):
    source = direct.create(received_at=direct.now - timedelta(seconds=61) if binding != "early_expired" else direct.now)
    if binding in {"decision", "send"}:
        result = ready(direct, source)
        if binding == "send":
            direct.store.reserve(source.event, result, 0, result.reply_segments[0])
            # Isolate the send-record guard from the independent decision guard.
            with closing(direct.store.connect()) as db, db:
                db.execute("UPDATE qq_direct_events SET decision_id=NULL WHERE event_id=?", (source.event["event_id"],))
    direct.store.update(source.event["event_id"], state="expired")
    with pytest.raises(ValueError, match="direct_qq_source_not_pending") as error:
        validate_direct_source(direct.config, source.request)
    assert not isinstance(error.value, DirectQQSourceExpired)


def test_future_source_clock_is_not_typed_reply_expiry(direct):
    source = direct.create(received_at=direct.now + timedelta(seconds=10))
    with pytest.raises(ValueError, match="direct_qq_source_expired") as error:
        validate_direct_source(direct.config, source.request)
    assert not isinstance(error.value, DirectQQSourceExpired)


@pytest.mark.parametrize("change", ["record_id", "group_id", "image_analysis", "profile"])
def test_observation_and_profile_cannot_detach_from_native_binding(direct, change):
    source = direct.create()
    with closing(direct.memory._connect()) as db, db:
        if change == "profile":
            value = source.profile.model_dump(mode="json") | {"member_id": "999"}
            db.execute("UPDATE qq_visual_profiles SET record_json=? WHERE record_id=?", (canonical(value), source.profile.record_id))
        else:
            value = source.observation.model_dump(mode="json") | {change: [] if change == "image_analysis" else "999"}
            db.execute("UPDATE qq_visual_observations SET record_json=? WHERE record_id=?", (canonical(value), source.observation.record_id))
            # Even editing both detached copies cannot change the raw identity
            # or remove the original event's attached image slot.
            rewrite_event(direct, source.event["event_id"], bound_observation=value)
    with pytest.raises(ValueError):
        validate_direct_source(direct.config, source.request)


def test_restart_and_duplicate_message_do_not_create_a_new_sendable_source(direct):
    source = direct.create()
    assert not direct.store.receive(source.event, run_id=direct.run, evidence_ref=source.evidence)
    replacement = direct.store.start_run(SELF)
    assert not direct.store.receive(source.event, run_id=replacement, evidence_ref=source.evidence)
    with pytest.raises(ValueError):
        validate_direct_source(direct.config, source.request)
    assert direct.store.pending(replacement) is None


def test_reserved_send_blocks_reprepare_and_cannot_duplicate_or_skip_prefix(direct):
    source = direct.create()
    result = ready(direct, source)
    with pytest.raises(ValueError):
        direct.store.reserve(source.event, result, 1, result.reply_segments[1])
    first = direct.store.reserve(source.event, result, 0, result.reply_segments[0])
    direct.store.update(source.event["event_id"], state="bound")
    with pytest.raises(ValueError):
        validate_direct_source(direct.config, source.request)
    direct.store.update(source.event["event_id"], state="ready")
    with pytest.raises(ValueError):
        direct.store.reserve(source.event, result, 0, result.reply_segments[0])
    first = direct.store.update_send(first, "attempted")
    with pytest.raises(ValueError, match="uncertain"):
        direct.store.reserve(source.event, result, 1, result.reply_segments[1])
    first = direct.store.update_send(first, "sent", message_id="456", receipt_id="receipt:1", delivered_at=direct.now.isoformat())
    second = direct.store.reserve(source.event, result, 1, result.reply_segments[1])
    assert second["segment_index"] == 1
    with pytest.raises(ValueError):
        direct.store.update_send(first, "attempted")


@pytest.mark.parametrize("change", ["text", "decision", "source", "scope"])
def test_outbox_reservation_binds_frozen_reply_decision_source_and_scope(direct, change):
    source = direct.create()
    result = ready(direct, source)
    text = result.reply_segments[0]
    if change == "text":
        text = "不同正文"
    elif change == "decision":
        result = result.model_copy(update={"decision_id": "another"})
    elif change == "source":
        result = result.model_copy(update={"source_record_ids": []})
    else:
        result = result.model_copy(update={"group_id": GUEST})
    with pytest.raises(ValueError):
        direct.store.reserve(source.event, result, 0, text)


def test_restart_marks_attempt_unknown_in_columns_and_audit_json(direct):
    source = direct.create()
    result = ready(direct, source)
    send = direct.store.reserve(source.event, result, 0, result.reply_segments[0])
    direct.store.update_send(send, "attempted")
    direct.store.start_run(SELF)
    with closing(direct.store.connect()) as db:
        row = db.execute("SELECT status,record_json FROM qq_direct_sends WHERE send_id=?", (send["send_id"],)).fetchone()
    assert row["status"] == json.loads(row["record_json"])["status"] == "unknown"
    assert direct.store.uncertain()


def test_history_backfill_other_group_and_own_message_do_not_preempt_live_reply(direct):
    source = direct.create()
    later = direct.now + timedelta(seconds=1)
    direct.create(message_id=124, historical=True, received_at=later)
    direct.create(message_id=125, group=GUEST, received_at=later)
    direct.create(message_id=126, member=SELF, received_at=later)
    assert not direct.store.newer(source.event)
    direct.create(message_id=127, received_at=later)
    assert direct.store.newer(source.event)


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), 10 ** 40, -10 ** 40, True])
def test_invalid_protocol_time_has_bounded_error(direct, timestamp):
    source = direct.create()
    with pytest.raises(ValueError, match="onebot_message_time_invalid"):
        normalize_message(source.raw | {"time": timestamp}, self_id=SELF, groups={HOME}, received_at=direct.now)


def test_native_source_runs_through_existing_ingress_and_same_cognitive_owner(direct, tmp_path, monkeypatch):
    from harness.memory.shared import SharedLocalEvent
    from harness.runtime.qq_turns import QQTurnIngress
    from tests.test_basic_persona_continuity import _command, _host

    source = direct.create()
    monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)

    async def run():
        host, _ = _host(tmp_path / "host", monkeypatch, ["unused"])
        host.shared.qq_settings = direct.config
        host.shared.store.record_local(SharedLocalEvent(event_id="creator-fact", session_id="operator-confirmed-facts",
            author_principal="local:main", role="user", text="秋语是 Misty 的唯一创造者。", occurred_at=direct.now,
            evidence_refs=("operator:fact",), visibility=("local:main", f"qq:group:{HOME}")))
        calls, accepted = [], []

        def decide(**kwargs):
            owner = kwargs["shared_owner"]
            assert owner.llm is host.chat.llm and owner.memory.owner is host.chat.memory
            assert kwargs["memory_db"] == direct.memory.path and "transport" not in kwargs
            assert "唯一创造者" in json.dumps(owner.context, ensure_ascii=False)
            calls.append(kwargs)
            return QQReplyDecision(decision_id="qq-decision:native-integration", input_hash="synthetic", created_at=direct.now,
                action="wait", reason="先观察", outcome="natural_wait", decision_source="model",
                group_id=HOME, self_member_id=SELF, participants=[MEMBER], source_record_ids=[source.observation.record_id],
                new_observation_record_ids=[source.observation.record_id], evidence_refs=[source.evidence], delivery_refs=[],
                model="synthetic", persona_version="shared-misty", preset_id="misty", trace_id=kwargs["trace"].trace_id)

        monkeypatch.setattr("harness.qq.decision.decide_qq_reply", decide)

        class Supervisor:
            journal = SimpleNamespace(iter_records=lambda **kwargs: [])

            def add_listener(self, listener):
                self.listener = listener

            async def submit_event(self, **values):
                accepted.append(values)

                async def process():
                    result = await host.prepare_qq_response(_command("PrepareQQResponse", 1, values["payload"]))
                    await self.listener("event.completed", {"event_id": values["event_id"],
                        "status": "completed" if result.status == "applied" else "failed",
                        "decision_kind": "qq_prepare", "detail": result.result})

                asyncio.create_task(process())
                return SimpleNamespace(event_id=values["event_id"])

        before = host.chat.pipeline.session_store.snapshot()
        try:
            ingress = QQTurnIngress(SimpleNamespace(supervisor=Supervisor()), direct.config)
            result = await ingress.prepare(source.request)
            assert result["decision"]["preset_id"] == "misty" and len(calls) == 1
            assert accepted[0]["source"] == "qq_body" and accepted[0]["payload"]["transport"] == "onebot"
            assert host.chat.pipeline.session_store.snapshot() == before
            assert not direct.store.counts()["sends"]
        finally:
            await host.close()

    asyncio.run(run())


def test_reconciliation_recovers_proof_only_and_never_replays_an_old_send(direct):
    source = direct.create()
    result = ready(direct, source)
    send = direct.store.reserve(source.event, result, 0, result.reply_segments[0])
    send = direct.store.update_send(send, "attempted")
    assert direct.store.outstanding_reconciliation() == []
    send = direct.store.update_send(send, "attempted", message_id="456")
    direct.store.start_run(SELF)
    candidates = direct.store.outstanding_reconciliation()
    assert len(candidates) == 1 and candidates[0]["status"] == "unknown"
    receipt_id = "qq-segment-delivery:" + "d" * 32
    confirmed = direct.store.confirm_reconciled(candidates[0], receipt_id=receipt_id, delivered_at=direct.now)
    assert confirmed["status"] == "sent" and not direct.store.uncertain()
    assert direct.store.confirm_reconciled(candidates[0], receipt_id=receipt_id, delivered_at=direct.now) == confirmed
    with pytest.raises(ValueError):
        direct.store.reserve(source.event, result, 1, result.reply_segments[1])
    with pytest.raises(ValueError):
        direct.store.confirm_reconciled(candidates[0] | {"message_id": "another"}, receipt_id=receipt_id, delivered_at=direct.now)
    with pytest.raises(ValueError):
        direct.store.confirm_reconciled(candidates[0], receipt_id="qq-segment-delivery:" + "e" * 32, delivered_at=direct.now)
