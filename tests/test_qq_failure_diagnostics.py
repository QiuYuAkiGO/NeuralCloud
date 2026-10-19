"""Fixed rejection metadata at the real Cognitive host and isolated report boundary."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest

from harness.cognitive import qq_turn
from harness.cognitive.shared_memory import ScopedQQMemoryView, SharedQQBinding
from harness.llm.client import ChatResult
from harness.memory.evidence_v2 import EvidenceV2Store
from harness.production.store import ProductionStore
from harness.qq import decision
from harness.qq.conversation import conversation_audience
from harness.qq.failures import failure_detail, safe_failure_detail
from harness.qq.policy import QQLatencyOptions
from harness.qq.telemetry import QQDeadlineExpired, QQTurnTrace
from tests.test_basic_persona_continuity import _command, _host
from tests.test_production_cognition import context
from tests.test_qq_decision import setup as setup
from tools.production_acceptance.qq_latency_ten import failure_counts, turn_failure_detail


@pytest.mark.parametrize("error, category, code", [
    (ValueError("production_memory_proposals_rejected"), "evidence_rejected", "production_memory_proposals_rejected"),
    (TimeoutError("private body, key=secret"), "timeout", "qq_model_timeout"),
    (httpx.ReadTimeout("https://private.invalid/?key=secret"), "timeout", "qq_model_timeout"),
    (QQDeadlineExpired("private source"), "timeout", "qq_turn_expired"),
    (ValueError("production_response_contract_invalid"), "model_invalid", "production_response_contract_invalid"),
    (ValueError("production_memory_proposals_rejected: private source"), "model_invalid", "qq_model_response_invalid"),
    (RuntimeError("private source"), "internal_error", "qq_turn_internal_error"),
])
def test_failure_diagnostics_never_copy_exception_prose(error, category, code):
    assert failure_detail(error) == {"failure_category": category, "error_code": code}


@pytest.mark.parametrize("value", [None, "timeout", {"failure_category": "timeout", "error_code": "secret"},
    {"failure_category": "evidence_rejected", "error_code": "qq_model_timeout"},
    {"failure_category": [], "error_code": []}])
def test_unknown_or_mismatched_diagnostics_are_not_propagated(value):
    assert safe_failure_detail(value) == {}


@pytest.mark.parametrize("failure, category, code, legacy_error", [
    ("evidence", "evidence_rejected", "production_memory_proposals_rejected", "qq_turn_rejected:ValueError"),
    ("timeout", "timeout", "qq_model_timeout", "qq_turn_rejected:TimeoutError"),
    ("invalid", "model_invalid", "qq_model_response_invalid", "qq_turn_rejected:JSONOutputError"),
])
def test_real_host_rejection_is_durable_safe_and_not_a_wait(setup, tmp_path, monkeypatch,
                                                          failure, category, code, legacy_error):
    async def run():
        raw, client, _, args, records = setup
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: False)
        host, _ = _host(tmp_path, monkeypatch, [])
        settings, preset = decision._load_configuration(args["config_path"], args["preset_path"])
        settings.production_memory_enabled = True
        host.chat.settings, host.chat.llm = settings, client
        host.shared.qq_settings = SimpleNamespace(memory_db=raw.path, allowed_group_ids=["10001"],
            allowed_private_peer_ids=[], self_member_id="29999")
        host.adapter.production_meter = ProductionStore(tmp_path / "budget.sqlite3")
        host.adapter.production_meter.context = lambda **kwargs: context()
        owner = SharedQQBinding(settings, preset.model_copy(update={"persona": host.chat.memory.get_persona()}),
            client, ScopedQQMemoryView(host.chat.memory, group_id="10001", member_id="20002"), {}, (),
            production_store=host.shared.production_store)
        monkeypatch.setattr(host.shared, "qq_binding", lambda **kwargs: owner)
        source = records[1]
        monkeypatch.setattr(qq_turn, "verified_qq_source", lambda *args: {
            "digest": "a" * 64, "text": source.text, "source": source.model_dump(mode="json")})
        calls = []

        def model(messages, **kwargs):
            calls.append(messages)
            assert "ProductionAppraisal" in messages[0]["content"]
            if failure == "timeout":
                raise TimeoutError("PRIVATE_ERROR_SENTINEL api_key=fixture-secret")
            if failure == "invalid":
                return ChatResult(text="PRIVATE_ERROR_SENTINEL not JSON", finish_reason="stop", content_type="text")
            payload = json.loads(messages[-1]["content"])
            view = payload["evidence_views"][0]
            # Real schema/apply_groups rejects a missing subject and retains the
            # exact proposal as a pending task; no manual exception substitution.
            proposal = {"group_id": "missing-subject", "claims": [{
                "topic": "preference", "statement": "PRIVATE_ERROR_SENTINEL", "kind": "self_report",
                "report_mode": "direct", "facet": "preference", "anchors": [{
                    "unit_id": view["unit_id"], "start": view["start"], "end": view["end"],
                    "quote": view["text"], "role": "supports", "rationale": "fixture"}]}]}
            return ChatResult(text=json.dumps({"affect": None, "memory_updates": [proposal]}),
                finish_reason="stop", content_type="text")

        monkeypatch.setattr(client, "complete_chat_result", model)
        now = datetime.now(timezone.utc)
        trace = QQTurnTrace(event_id="qq-watch-event:" + "a" * 32, first_seen_at=now)
        payload = {"group_id": "10001", "self_member_id": "29999", "trigger_member_id": "20002",
            "deadline_at": (now + timedelta(seconds=59)).isoformat(),
            "trace": {"trace_id": trace.trace_id, "event_id": trace.event_id,
                "first_seen_at": now.isoformat(), "first_seen_mono": trace.first_seen_mono},
            "qq_frozen_policy": QQLatencyOptions.from_settings(settings).model_dump(),
            "qq_turn_runtime": {"runtime_instance_id": "runtime_basic_chat", "manifest_revision": 1},
            "qq_web_capabilities": {"enabled": False}, "production_context": context()}
        command = _command("PrepareQQTurn", 1, payload)
        try:
            result = await host.prepare_qq_turn(command)
            assert result.status == "rejected"
            assert result.error == legacy_error
            assert safe_failure_detail(result.result) == {"failure_category": category, "error_code": code}
            assert "decision" not in result.result
            assert "PRIVATE_ERROR_SENTINEL" not in result.model_dump_json()
            assert "fixture-secret" not in result.model_dump_json()
            replay = await host.prepare_qq_turn(command)
            assert replay.status == "rejected" and replay.result == result.result and len(calls) == 1
            assert not client.gate_messages and not client.chat_calls
            if failure == "evidence":
                pending = EvidenceV2Store(host.shared.production_store).pending_tasks(audience=conversation_audience("10001"))
                assert len(pending) == 1 and pending[0]["reason"] == "evidence_group_schema_invalid"
            # Read the persisted command through a new adapter, not only the
            # in-memory replay cache. No additional model call is made.
            await host.close()
            reloaded, _ = _host(tmp_path, monkeypatch, [])
            try:
                stored = reloaded.adapter._lookup_replay(command)
                assert stored.status == "rejected"
                assert safe_failure_detail(stored.result) == safe_failure_detail(result.result)
            finally:
                await reloaded.close()
        finally:
            await host.close()
    asyncio.run(run())


def test_report_distinguishes_evidence_and_timeout_from_unchanged_direct_terminal():
    rows = []
    for category, code in [("evidence_rejected", "production_memory_proposals_rejected"),
                           ("timeout", "qq_model_timeout"), ("model_invalid", "qq_model_response_invalid")]:
        row = {"terminal_reason": "model_invalid", "successful_reply": False, "decision_outcome": None}
        row.update(turn_failure_detail(current={"error_type": "ValueError"}, terminal_reason=row["terminal_reason"],
            successful_reply=False, domain_failures=[{"failure_category": category, "error_code": code,
                                                    "text": "PRIVATE_ERROR_SENTINEL"}]))
        assert row["failure_category"] == category
        assert row["terminal_reason"] == "model_invalid" and not row["successful_reply"]
        assert "PRIVATE_ERROR_SENTINEL" not in json.dumps(row)
        rows.append(row)
    assert failure_counts(rows) == {"evidence_rejected": 1, "timeout": 1, "model_invalid": 1}


def test_report_keeps_success_and_natural_wait_separate_from_failures():
    assert turn_failure_detail(current={"decision_outcome": "ready"}, terminal_reason="sent", successful_reply=True) == {
        "failure_category": None, "error_code": None}
    assert turn_failure_detail(current={"decision_outcome": "natural_wait"}, terminal_reason="natural_wait",
                               successful_reply=False)["failure_category"] is None
    assert turn_failure_detail(current={"runtime_detail": {"decision": {"error_type": "model_timeout"}},
        "decision_outcome": "model_error"}, terminal_reason="model_invalid", successful_reply=False)["failure_category"] == "timeout"


def test_report_accepts_only_known_runtime_metadata_and_preserves_old_report(tmp_path):
    from tools.production_acceptance.qq_latency_ten import run
    from harness.runtime.qq_pipeline import QQTurnRejected

    safe = {"failure_category": "timeout", "error_code": "qq_stage_deadline_exhausted"}
    error = QQTurnRejected("qq_turn_stage_deadline", **safe)
    propagated = safe_failure_detail(getattr(error, "failure_detail", None))
    assert propagated == safe
    assert turn_failure_detail(current=propagated, terminal_reason="model_invalid", successful_reply=False) == safe
    assert turn_failure_detail(current={"failure_category": "timeout", "error_code": "PRIVATE_ERROR_SENTINEL",
        "error_type": "ValueError"}, terminal_reason="model_invalid", successful_reply=False) == {
            "failure_category": "model_invalid", "error_code": "qq_model_response_invalid"}
    output = tmp_path / "existing"
    output.mkdir()
    report = output / "summary.json"
    report.write_text("previous report", encoding="utf-8")
    with pytest.raises(FileExistsError):
        asyncio.run(run(SimpleNamespace(output=output)))
    assert report.read_text(encoding="utf-8") == "previous report"
