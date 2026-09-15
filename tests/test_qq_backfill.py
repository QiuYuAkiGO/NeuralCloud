from datetime import timedelta
import json

import pytest

from gui.presets import CharacterPreset, load_builtin
from harness.memory import qq_backfill
from harness.memory.qq_assimilation import QQAssimilationQueue, QQMemoryProposal
from harness.memory.shared import SharedMemoryStore
from tests.test_qq_assimilation import NOW, add_message


class Proposer:
    def __init__(self, *, fail=False, impressions=True):
        self.calls = []
        self.fail = fail
        self.impressions = impressions

    def propose(self, job, *, old_impressions, persona):
        self.calls.append((job.job_id, old_impressions))
        if self.fail:
            raise RuntimeError("SECRET_TOKEN and private message must never enter output")
        source_ids = [source.source_id for source in job.sources]
        impressions = []
        if self.impressions:
            impressions.append({"kind": "person", "subject_refs": ["qq:member:200"],
                "topic": "互助", "context": "认真求助时", "statement": "暂时觉得这位成员愿意帮忙。",
                "stance": "support", "source_ids": source_ids,
                "target_impression_id": old_impressions[0]["impression_id"] if old_impressions else None})
        return QQMemoryProposal.model_validate({"episodes": [{"source_ids": source_ids,
            "summary": "成员认真回答了问题", "insight": "暂时觉得他愿意解释。", "importance": 0.3,
            "valence": 0.1}], "impressions": impressions})


@pytest.fixture
def inputs(tmp_path):
    source = SharedMemoryStore(tmp_path / "live.sqlite3")
    for i in range(2):
        add_message(source, str(i), seconds=i * 100, message_id=str(i + 1))
    preset = tmp_path / "live-preset.json"
    preset.write_text(load_builtin("misty").model_dump_json(), encoding="utf-8")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"mode": "live", "base_url": "http://127.0.0.1:12345/v1",
                                   "model": "test-model", "api_key": "SECRET_TOKEN"}), encoding="utf-8")
    return dict(source_db=source.path, preset=preset, settings=settings, output=tmp_path / "isolated",
                self_id="999", group_ids=["100"], now=NOW + timedelta(days=1))


def checkpoint(inputs):
    return CharacterPreset.model_validate_json((inputs["output"] / "preset.json").read_bytes())


def test_backfill_freezes_sources_and_preserves_live_inputs(inputs):
    original_db, original_preset = inputs["source_db"].read_bytes(), inputs["preset"].read_bytes()
    proposer, lines = Proposer(), []
    report = qq_backfill.run_backfill(**inputs, proposer=proposer, progress=lines.append)
    assert report["completed"] and report["failures_this_run"] == 0
    assert report["raw_observations"] == 2 and report["source_records"] == 4
    assert report["episodes"] == 2 and report["impressions"] == 1
    assert report["queue"]["applied"] == 2
    assert len(proposer.calls) == 2 and proposer.calls[1][1]
    assert inputs["source_db"].read_bytes() == original_db
    assert inputs["preset"].read_bytes() == original_preset
    assert (inputs["output"] / "input-preset.json").read_bytes() == original_preset
    manifest = json.loads((inputs["output"] / "source_manifest.json").read_text())
    assert len(manifest["sources"]) == 2
    assert all(len(source["original_sha256"]) == 64 for source in manifest["sources"])
    assert all("original_record_id" in source for source in manifest["sources"])
    assert all(isinstance(value, (int, float)) for line in lines for value in json.loads(line).values())
    for file in ("source_manifest.json", "report.json"):
        assert "SECRET_TOKEN" not in (inputs["output"] / file).read_text()
    assert checkpoint(inputs).qq_memory_state["last_review_at"] == inputs["now"].isoformat()


def test_checkpoint_resume_uses_frozen_data_and_previous_impression_context(inputs):
    first = Proposer()
    report = qq_backfill.run_backfill(**inputs, proposer=first, max_jobs=1, progress=lambda _: None)
    assert not report["completed"] and len(first.calls) == 1
    add_message(SharedMemoryStore(inputs["source_db"]), "new-live", seconds=300, message_id="3")
    changed = load_builtin("misty").model_copy(update={"name": "New live name"})
    inputs["preset"].write_text(changed.model_dump_json(), encoding="utf-8")
    second = Proposer()
    report = qq_backfill.run_backfill(**inputs, proposer=second, progress=lambda _: None)
    assert report["completed"] and report["raw_observations"] == 2
    assert len(second.calls) == 1 and second.calls[0][1]
    assert checkpoint(inputs).name != "New live name"
    assert report["episodes"] == 2
    repeated = Proposer(fail=True)
    again = qq_backfill.run_backfill(**inputs, proposer=repeated, progress=lambda _: None)
    assert again["completed"] and not repeated.calls
    assert again["episodes"] == report["episodes"] and again["committed_windows"] == 2


def test_saved_proposal_survives_failed_preset_save_without_second_model_call(inputs, monkeypatch):
    original = qq_backfill.atomic_write_json
    def fail_commit(path, payload):
        if path.name == "preset.json" and payload.get("qq_memory_state", {}).get("receipts"):
            raise OSError("injected disk failure")
        return original(path, payload)
    monkeypatch.setattr(qq_backfill, "atomic_write_json", fail_commit)
    proposer = Proposer()
    report = qq_backfill.run_backfill(**inputs, proposer=proposer, progress=lambda _: None)
    assert not report["completed"] and report["failures_this_run"] == 1
    assert checkpoint(inputs).episodic == []
    monkeypatch.setattr(qq_backfill, "atomic_write_json", original)
    resumed = Proposer()
    report = qq_backfill.run_backfill(**inputs, proposer=resumed, progress=lambda _: None)
    assert report["completed"] and report["episodes"] == 2
    assert len(resumed.calls) == 1  # Second job only; first proposal is durable.


def test_saved_receipt_recovers_ack_failure_without_duplicate_effects(inputs, monkeypatch):
    original = QQAssimilationQueue.acknowledge
    def fail_ack(*args, **kwargs):
        raise OSError("injected ack failure")
    monkeypatch.setattr(QQAssimilationQueue, "acknowledge", fail_ack)
    proposer = Proposer()
    report = qq_backfill.run_backfill(**inputs, proposer=proposer, progress=lambda _: None)
    assert report["failures_this_run"] == 1 and len(checkpoint(inputs).episodic) == 1
    monkeypatch.setattr(QQAssimilationQueue, "acknowledge", original)
    resumed = Proposer()
    report = qq_backfill.run_backfill(**inputs, proposer=resumed, progress=lambda _: None)
    assert report["completed"] and report["episodes"] == 2 and len(resumed.calls) == 1


def test_failures_are_counted_without_leaking_model_errors_or_secrets(inputs):
    lines = []
    report = qq_backfill.run_backfill(**inputs, proposer=Proposer(fail=True), progress=lines.append)
    assert report["failures_this_run"] == 1 and report["queue"]["retry"] == 1
    assert not report["completed"] and report["episodes"] == 0
    assert "SECRET_TOKEN" not in "".join(lines)
    assert "SECRET_TOKEN" not in (inputs["output"] / "report.json").read_text()
    resumed = qq_backfill.run_backfill(**inputs, proposer=Proposer(), progress=lambda _: None)
    assert resumed["completed"]


def test_resume_rejects_changed_scope_and_tampered_snapshot(inputs):
    qq_backfill.run_backfill(**inputs, proposer=Proposer(), max_jobs=1, progress=lambda _: None)
    with pytest.raises(ValueError, match="scope_changed"):
        qq_backfill.run_backfill(**{**inputs, "group_ids": ["101"]}, proposer=Proposer())
    snapshot = inputs["output"] / "source.sqlite3"
    snapshot.write_bytes(snapshot.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="frozen_input_changed"):
        qq_backfill.run_backfill(**inputs, proposer=Proposer())


def test_output_cannot_target_live_directory(inputs):
    with pytest.raises(ValueError, match="isolated_output"):
        qq_backfill.run_backfill(**{**inputs, "output": inputs["source_db"].parent}, proposer=Proposer())


def test_final_aging_does_not_make_historical_greetings_fresh(inputs):
    report = qq_backfill.run_backfill(**{**inputs, "now": NOW + timedelta(days=365)},
                                    proposer=Proposer(impressions=False), progress=lambda _: None)
    assert report["completed"] and report["tiers"].get("short", 0) == 0
    assert all(event.time < inputs["now"] for event in checkpoint(inputs).episodic)


def test_cli_reports_exception_type_without_message(monkeypatch, capsys):
    def fail(**kwargs):
        raise ValueError("SECRET_TOKEN")
    monkeypatch.setattr(qq_backfill, "run_backfill", fail)
    result = qq_backfill.main(["--source-db", "source", "--preset", "preset", "--settings", "settings",
                              "--output", "out", "--self-id", "999", "--group-id", "100"])
    assert result == 1
    assert json.loads(capsys.readouterr().out) == {"failed": 1, "error_type": "ValueError"}
