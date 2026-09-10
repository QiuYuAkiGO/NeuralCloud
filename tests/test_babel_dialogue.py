"""Protocol and collector faults; these deterministic fixtures are not live results."""
from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess

import httpx
import pytest

from eval.basic_runtime import babel
from eval.basic_runtime.babel_evidence import (
    EvidenceStore, archive_journal, copy_call_evidence, export_shareable, fingerprint, read_json, write_json,
)
from gui.presets import load_builtin
from harness.homeostasis.models import HomeostaticProfile, initial_snapshot
from harness.runtime.journal import RuntimeJournal


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class ScriptedRuntime:
    def __init__(self, clock, *, lose_ack=False, background=False, failure=None, pause_after=None, backlog=0, monitor_outage=False):
        self.clock = clock
        self.lose_ack = lose_ack
        self.background = background
        self.failure = failure
        self.pause_after = pause_after
        self.backlog = backlog
        self.monitor_outage = monitor_outage
        self.monitor_failed = False
        self.calls = []
        self.events = []
        self.page_count = 0
        profile = HomeostaticProfile.model_validate(read_json(Path("presets/homeostasis/functional-proxy-v1.json")))
        self.state = initial_snapshot(profile, now=datetime.now(UTC)).model_dump(mode="json")
        self.profile = profile.model_dump(mode="json")
        self.ui = {"mode": "live", "persona": load_builtin("misty").persona.model_dump(mode="json"),
                   "llm": {"model": "fixture", "base_url": "http://example.invalid", "reachable": True, "aux": {}},
                   "inference_config": {"call_log_enabled": True},
                   "session": {"session_id": "sess_misty", "preset_id": "misty"}}

    def status(self):
        return {"runtime_instance_id": "fixture", "lifecycle": "PAUSED" if self.pause_after and len(self.calls) >= self.pause_after else "RUNNING", "health": {}}

    def add(self, kind, data, correlation=None, event_id=None, command_id=None):
        seq = len(self.events) + 1
        payload = {"record_seq": seq, "record_type": kind, "recorded_at": datetime.now(UTC).isoformat(), "data": data,
                   "event_id": event_id, "correlation_id": correlation, "command_id": command_id}
        self.events.append({"seq": seq, "correlation_id": correlation, "event_id": event_id, "topic": "journal." + kind, "payload": payload})

    def increment_state(self):
        self.state = copy.deepcopy(self.state)
        self.state["revision"] += 1
        self.state["interoceptive_summary"]["snapshot_revision"] = self.state["revision"]

    async def get(self, path, **params):
        if path == "/v1/snapshot":
            if self.failure == "drift" and self.calls:
                self.ui["llm"]["model"] = "changed-model"
            return copy.deepcopy({"runtime": self.status(), "cognitive_ui": self.ui, "manifest": {"revision": 3}, "homeostasis": {"payload": {**self.state, "profile": self.profile}}})
        if path == "/v1/status":
            return self.status()
        if path == "/v1/monitor/events":
            if self.monitor_outage and self.calls and not self.monitor_failed:
                self.monitor_failed = True
                raise httpx.ReadTimeout("fixture")
            self.page_count += 1
            if "after_seq" not in params:
                rows = list(reversed(self.events))[:params.get("limit", 500)]
                return {"items": rows, "has_more": len(self.events) > len(rows)}
            rows = [e for e in self.events if e["seq"] > params["after_seq"]]
            return {"items": rows[:500], "has_more": len(rows) > 500}
        raise AssertionError(path)

    async def submit(self, payload):
        self.calls.append((copy.deepcopy(payload), self.clock.now))
        self.clock.now += .1
        index = len(self.calls)
        corr, event_id = payload["correlation_id"], f"evt_{index}"
        event = {**payload, "event_id": event_id, "receive_seq": index, "event_type": "user_message"}
        self.add("event_accepted", {"event": event}, corr, event_id)
        if self.failure == "timeout":
            return event
        for i in range(self.backlog if index == 1 else 0):
            self.add("background", {"index": i}, "background", f"bg_{i}")
        self.add("processing_started", {}, corr, event_id)
        self.increment_state()
        used = copy.deepcopy(self.state)
        summary = used["interoceptive_summary"]
        digest = fingerprint(summary) if self.failure != "hash" else "0" * 64
        prepare_id = f"prep_{index}"
        self.add("domain_command_result", {"service": "homeostasis", "result": {"result": {"snapshot": used}}}, corr, event_id, f"homeo_{index}")
        self.add("homeostatic_interoception_routed", {"target": "PrepareResponse", "homeostasis_revision": used["revision"], "homeostatic_summary_hash": digest}, corr, event_id, f"prepare_{index}")
        self.add("domain_command_intent", {"service": "cognitive", "envelope": {"command_type": "PrepareResponse", "version_vector": {"homeostasis_state": used["revision"]}, "payload": {"homeostatic_interoception": summary}}}, corr, event_id, f"prepare_{index}")
        if self.background:
            self.increment_state()
            self.add("domain_command_result", {"service": "homeostasis", "result": {"result": {"snapshot": self.state}}}, "background", "heartbeat")
        self.add("reply_delivery", {"prepare_id": prepare_id, "result": {"success": True, "delivered_segments": ["我在听。", f"第 {index} 段。"]}}, corr, event_id)
        self.add("domain_command_intent", {"service": "cognitive", "envelope": {"command_type": "CommitInteraction", "payload": {"prepare_id": prepare_id}}}, corr, event_id, f"commit_{index}")
        self.add("domain_command_result", {"service": "cognitive", "result": {"status": "failed" if self.failure == "commit" else "applied"}}, corr, event_id, f"commit_{index}")
        self.add("processing_completed", {"status": "completed", "decision_kind": "reply", "detail": {"prepare_id": prepare_id, "delivered": True}}, corr, event_id)
        if self.lose_ack:
            raise httpx.ReadTimeout("ack lost")
        return event


def run_fixture(tmp_path, **kwargs):
    clock = Clock()
    runtime = ScriptedRuntime(clock, **kwargs)
    directory = tmp_path / "evidence"
    result = asyncio.run(babel.run_dialogue(runtime, story=babel.load_story(), directory=directory,
        options=babel.RunOptions(turn_timeout_seconds=2, drain_seconds=0, use_stream=False), clock=clock))
    return runtime, clock, directory, result


def test_frozen_story_and_exact_reflection_questions():
    story = babel.load_story()
    assert len(story["turns"]) == 100
    assert [t["text"] for t in story["turns"][90:]] == list(babel.QUESTIONS)
    assert "虚构" in story["introduction"]
    assert "故事" in story["turns"][89]["text"]
    assert all(name in "".join(t["text"] for t in story["turns"][:10]) for name in ("阿珥", "米拉", "沙兰"))


def test_shortened_story_cannot_validate(tmp_path):
    story = babel.load_story()
    story["turns"].pop()
    path = tmp_path / "story.json"
    write_json(path, story)
    with pytest.raises(ValueError, match="exactly 100"):
        babel.load_story(path)


def test_100_rounds_commit_then_ten_second_gap_and_no_false_live_completion(tmp_path):
    runtime, clock, directory, result = run_fixture(tmp_path)
    assert len(runtime.calls) == 100
    assert all(b[1] - a[1] >= 10 for a, b in zip(runtime.calls, runtime.calls[1:]))
    assert clock.now >= 990
    assert result["status"] == "INCOMPLETE_EVIDENCE"  # no raw journal/model files in a synthetic fixture
    assert read_json(directory / "summary.json")["completed_turns"] == 100
    assert len((directory / "turns.jsonl").read_text(encoding="utf-8").splitlines()) == 100
    assert story_intro_once(runtime)


def story_intro_once(runtime):
    return sum("分九十段" in p[0]["text"] for p in runtime.calls) == 1


def test_lost_ack_recovered_by_correlation_without_resending_and_pause_preserved(tmp_path):
    runtime, _, directory, result = run_fixture(tmp_path, lose_ack=True, pause_after=1)
    assert len(runtime.calls) == 1
    row = read_json(directory / "turns/001.json")
    assert row["acceptance_recovered"]
    assert row["status"] == "COMPLETED"
    assert result["status"] == "PAUSED"
    assert read_json(directory / "summary.json")["completed_turns"] == 1


def test_actual_input_revision_survives_interleaved_heartbeat(tmp_path):
    _, _, directory, _ = run_fixture(tmp_path, background=True, pause_after=1)
    row = read_json(directory / "turns/001.json")
    assert row["analysis"]["used_for_response"][0]["revision"] == 1
    assert row["after_commit"]["revision"] == 2
    assert row["analysis"]["state_exact"]
    assert row["analysis"]["reply"] == "我在听。\n\n第 1 段。"


@pytest.mark.parametrize(("failure", "status"), [("commit", "FAILED"), ("timeout", "TIMEOUT"), ("hash", "INCOMPLETE_EVIDENCE"), ("drift", "CONFIGURATION_DRIFT")])
def test_failed_or_incomplete_turn_never_counts_as_completed(tmp_path, failure, status):
    runtime, _, directory, result = run_fixture(tmp_path, failure=failure)
    assert len(runtime.calls) == 1
    assert result["status"] == status
    assert read_json(directory / "summary.json")["completed_turns"] == 0


def test_monitor_more_than_500_and_transient_outage_are_recovered(tmp_path):
    runtime, _, directory, result = run_fixture(tmp_path, backlog=1100, monitor_outage=True, pause_after=2)
    assert result["status"] == "PAUSED"
    values = [json.loads(line) for line in (directory / "monitor.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(v["topic"] == "journal.background" for v in values) == 1100
    assert len({v["seq"] for v in values}) == len(values)
    assert result["monitor"]["poll_errors"] == 1
    assert runtime.page_count > 3


def test_store_rejects_conflicting_duplicate_sequence(tmp_path):
    store = EvidenceStore(tmp_path)
    event = {"seq": 1, "topic": "background", "payload": {}}
    assert store.add(event, 1)
    assert not store.add(event, 2)
    with pytest.raises(ValueError, match="reused"):
        store.add({**event, "payload": {"changed": True}}, 3)
    store.close()


def test_original_journal_contiguous_bytes_and_anchor(tmp_path):
    source = tmp_path / "journal"
    journal = RuntimeJournal(source, runtime_instance_id="test", character_id="character", manifest_revision=1)
    for i in range(5):
        journal.append("sample", data={"index": i, "embedding": [1, 2, 3]})
    path = tmp_path / "copy.jsonl"
    receipt = archive_journal(source, path, 2, 4)
    original = (source / "runtime-000001.jsonl").read_bytes().splitlines(keepends=True)
    assert path.read_bytes() == b"".join(original[:4])
    assert receipt["anchor_included"] and receipt["records"] == 3
    with pytest.raises(ValueError, match="incomplete"):
        archive_journal(source, tmp_path / "bad.jsonl", 2, 7)


def test_report_refresh_keeps_archived_spans_when_live_source_is_gone(tmp_path):
    trace = tmp_path / "source"
    trace.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    value = {"span_id": "span", "status": "success", "correlation_id": "corr"}
    (trace / "model_requests.jsonl").write_text(json.dumps(value) + "\n")
    copy_call_evidence(trace, trace / "calls", out, {"corr"})
    result = copy_call_evidence(tmp_path / "missing", tmp_path / "missing-calls", out, {"corr"})
    assert result["model_spans"] == 1
    assert json.loads((out / "model_calls.jsonl").read_text()) == value


def test_existing_evidence_directory_is_never_overwritten(tmp_path):
    with pytest.raises(ValueError, match="fresh"):
        asyncio.run(babel.run_dialogue(None, story=babel.load_story(), directory=tmp_path))


def test_duplicate_ingress_is_detected_even_after_successful_http_ack(tmp_path):
    class Duplicate(ScriptedRuntime):
        async def submit(self, payload):
            event = await super().submit(payload)
            duplicate = {**event, "event_id": "duplicate", "receive_seq": 999}
            self.add("event_accepted", {"event": duplicate}, payload["correlation_id"], "duplicate")
            return event
    clock = Clock()
    runtime = Duplicate(clock)
    result = asyncio.run(babel.run_dialogue(runtime, story=babel.load_story(), directory=tmp_path / "out", clock=clock,
        options=babel.RunOptions(drain_seconds=0, use_stream=False)))
    assert result["status"] == "INCOMPLETE_EVIDENCE"
    assert len(runtime.calls) == 1


def test_shareable_strips_url_credentials_and_known_keys(tmp_path):
    write_json(tmp_path / "run.json", {"base_url": "https://user:password@model.test/v1?token=query-secret#fragment", "api_key": "private", "message": "contains private"})
    export_shareable(tmp_path, ["private"])
    value = read_json(tmp_path / "shareable/run.json")
    assert value["base_url"] == "https://model.test/v1"
    assert "private" not in json.dumps(value)
    assert "password" not in json.dumps(value)


def test_atomic_report_retries_transient_windows_file_lock(tmp_path, monkeypatch):
    original = Path.replace
    attempts = []
    def replace(path, target):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("fixture indexer lock")
        return original(path, target)
    monkeypatch.setattr(Path, "replace", replace)
    write_json(tmp_path / "report.json", {"status": "RUNNING"})
    assert len(attempts) == 3
    assert read_json(tmp_path / "report.json")["status"] == "RUNNING"


def test_missing_commit_telemetry_cannot_count_as_complete_trace(tmp_path):
    trace, out = tmp_path / "traces", tmp_path / "out"
    out.mkdir()
    write_json(trace / "calls/p.json", {"call_id": "p", "correlation_id": "corr", "kind": "prepare_response", "ok": True,
        "phase": "prepare", "steps": [{"kind": kind} for kind in ("compile", "generate", "final")]})
    span = {"span_id": "span", "correlation_id": "corr", "phase": "prepare", "role": "main", "stage": "generate", "status": "success"}
    (trace / "model_requests.jsonl").write_text(json.dumps(span) + "\n")
    evidence = copy_call_evidence(trace, trace / "calls", out, {"corr"})
    assert evidence["complete_trace_correlations"] == []


def saved_launch_fixture(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    state = root / "data"
    write_json(root / "presets/manifest.json", {"embodiment": {"body_provider": "mistyai"}})
    write_json(root / "presets/profile.json", {"revision": 3})
    write_json(state / "runtime/active-start.json", {"manifest_path": str(root / "presets/manifest.json"), "profile_path": str(root / "presets/profile.json")})
    write_json(state / "gui_settings.json", {"mode": "live", "active_preset_id": "misty"})
    write_json(state / "homeostasis/snapshot.json", {"revision": 12, "preserve": True})
    monkeypatch.setattr(babel, "ROOT", root)
    monkeypatch.setattr(babel, "port_open", lambda port: False)
    return root, state, tmp_path / "backup"


@pytest.mark.skipif(babel.os.name != "nt", reason="Windows launcher integration")
def test_restore_keeps_backup_and_receipt_on_startup_timeout(tmp_path, monkeypatch):
    root, state, backup = saved_launch_fixture(tmp_path, monkeypatch)
    def timeout(command, **kwargs):
        assert command[command.index("-EmbodimentProvider") + 1] == "mistyai"
        raise subprocess.TimeoutExpired(command, 240)
    monkeypatch.setattr(babel.subprocess, "run", timeout)
    with pytest.raises(babel.StopRun, match="启动结果不确定"):
        babel.restore_saved_runtime(state, backup)
    assert (backup / "homeostasis/snapshot.json").read_bytes() == (state / "homeostasis/snapshot.json").read_bytes()
    assert (backup / "babel-start-configuration/manifest.json").read_bytes() == (root / "presets/manifest.json").read_bytes()
    assert read_json(tmp_path / "backup-receipt.json")["status"] == "STARTUP_UNCERTAIN"


@pytest.mark.skipif(babel.os.name != "nt", reason="Windows launcher integration")
def test_restored_paused_instance_is_preserved_and_not_resumed(tmp_path, monkeypatch):
    _, state, backup = saved_launch_fixture(tmp_path, monkeypatch)
    calls = []
    def start(command, **kwargs):
        calls.append(command)
        (state / "control.token").write_text("fixture-control-token")
        return subprocess.CompletedProcess(command, 1)
    class Probe:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            assert url.endswith("/v1/snapshot")
            return httpx.Response(200, json={"runtime": {"lifecycle": "PAUSED"}}, request=httpx.Request("GET", url))
    monkeypatch.setattr(babel.subprocess, "run", start)
    monkeypatch.setattr(babel.httpx, "Client", Probe)
    with pytest.raises(babel.StopRun) as exc:
        babel.restore_saved_runtime(state, backup)
    assert exc.value.status == "PAUSED"
    assert len(calls) == 1
    assert read_json(tmp_path / "backup-receipt.json")["runtime_lifecycle"] == "PAUSED"
    assert (backup / "homeostasis/snapshot.json").read_bytes() == (state / "homeostasis/snapshot.json").read_bytes()
