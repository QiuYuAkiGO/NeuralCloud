import json
from types import SimpleNamespace

import pytest

from neuralcloud.contracts import SourceEvent
from neuralcloud.rebuild_batch import (
    BudgetedModel,
    BudgetExhausted,
    initialize_jobs,
    chunks,
    validate_facts,
    verify_candidates,
    run,
    queue_tasks,
)
from neuralcloud.evidence_index import EvidenceIndex


def test_budget_reservation_blocks_call_before_spending_and_charges_unknown(tmp_path):
    db = initialize_jobs(tmp_path / "jobs.db", {"version": "test"})
    calls = []

    def request(*args):
        calls.append(args)
        raise TimeoutError()

    model = BudgetedModel(db, {}, 10000, request=request)
    with pytest.raises(TimeoutError):
        model.call("extract", [{"role": "user", "content": "text"}], 100)
    spent = model.spent()
    assert spent > 100
    model.maximum = spent
    with pytest.raises(BudgetExhausted):
        model.call("extract", [])
    assert len(calls) == 1
    db.close()


def test_long_records_have_complete_overlapping_coverage():
    text = "字" * 32768
    ranges = list(chunks(text))
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(a[1] - b[0] == 200 for a, b in zip(ranges, ranges[1:]))


def test_quotes_offsets_and_verification_ids_are_strict():
    root = {
        "id": "r@1:100:120",
        "record_id": "r",
        "revision": 1,
        "entity": "qq:12345",
        "start": 100,
        "content": "我喜欢摄影",
    }
    p = {"id": "7", "root": root, "context": []}
    raw = {
        "entity": "qq:12345",
        "predicate": "likes",
        "value": "摄影",
        "basis": "direct",
        "confidence": 0.9,
        "references": [{"source_id": root["id"], "quote": "喜欢摄影"}],
    }
    facts, rejected = validate_facts([raw], p)
    assert not rejected and facts[0]["references"][0]["start"] == 101
    with pytest.raises(ValueError, match="VERIFICATION_IDS"):
        verify_candidates({"accepted": [1], "rejected": []}, facts)
    verify_candidates({"accepted": ["7:0"], "rejected": []}, facts)
    assert facts[0]["verified"]
    raw["entity"] = "qq:67890"
    facts, rejected = validate_facts([raw], p)
    assert not facts and rejected[0]["reason"] == "ENTITY_NOT_GROUNDED"


def test_full_job_is_idempotent_and_does_not_activate_model_facts(store, tmp_path):
    store.ingest(
        SourceEvent(
            character_id="misty-test",
            source_instance="qq",
            event_id="1",
            session_id="s",
            occurred_at=1,
            content="我喜欢摄影",
            participants=["qq:12345"],
        )
    )
    config = tmp_path / "model.json"
    config.write_text(json.dumps({"model": {"id": "test"}}))
    args = SimpleNamespace(
        source=store.path,
        index=tmp_path / "index.db",
        jobs=tmp_path / "jobs.db",
        model_config=config,
        report=tmp_path / "report.json",
        sample=0,
        authorized_model_run=True,
        full_budget=True,
        max_tokens=100000,
        batch_size=16,
        pause_file=None,
        self_account=[],
        follow=False,
    )
    requests = []

    def request(config, messages, max_tokens):
        data = json.loads(messages[1]["content"])
        requests.append(data)
        if "candidates" in data:
            return {"accepted": [f["id"] for f in data["candidates"]], "rejected": []}, {"total_tokens": 10}, 0.01
        p = data["tasks"][0]
        return (
            {
                "results": [
                    {
                        "id": p["id"],
                        "facts": [
                            {
                                "entity": "qq:12345",
                                "predicate": "likes",
                                "value": "摄影",
                                "basis": "direct",
                                "confidence": 1,
                                "references": [{"source_id": p["root"]["id"], "quote": "喜欢摄影"}],
                            }
                        ],
                    }
                ]
            },
            {"total_tokens": 10},
            0.01,
        )

    report = run(args, request=request)
    assert report["tasks"] == {"COMPLETE": 1}
    assert report["facts"]["independently_verified"] == 1
    assert report["index"]["facts"] == 0
    assert run(args, request=request)["calls"] == {"COMPLETE": 2}
    assert len(requests) == 2


def test_failed_batch_retries_at_most_twice(store, tmp_path):
    store.ingest(
        SourceEvent(
            character_id="misty-test",
            source_instance="qq",
            event_id="1",
            session_id="s",
            occurred_at=1,
            content="hello",
            participants=["qq:12345"],
        )
    )
    config = tmp_path / "model.json"
    config.write_text('{"model":{"id":"test"}}')
    args = SimpleNamespace(
        source=store.path,
        index=tmp_path / "index.db",
        jobs=tmp_path / "jobs.db",
        model_config=config,
        report=tmp_path / "report.json",
        sample=0,
        authorized_model_run=True,
        full_budget=True,
        max_tokens=100000,
        batch_size=16,
        pause_file=None,
        self_account=[],
        follow=False,
    )
    result = run(args, request=lambda *args: (None, {"total_tokens": 10}, 0.01))
    assert result["tasks"] == {"FAILED": 1} and result["calls"] == {"COMPLETE": 3}


def test_incremental_queue_reprocesses_invalidated_dependencies_but_not_empty_selection(store, tmp_path):
    saved = store.ingest(
        SourceEvent(
            character_id="misty-test",
            source_instance="qq",
            event_id="1",
            session_id="s",
            occurred_at=1,
            content="我喜欢摄影",
            participants=["qq:12345"],
        )
    )
    index = EvidenceIndex(store.path, tmp_path / "index.db", writable=True)
    db = initialize_jobs(tmp_path / "jobs.db", {"version": "test"})
    try:
        index.sync_batch()
        queue_tasks(index, db, [])
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        queue_tasks(index, db)
        db.execute("UPDATE tasks SET status='COMPLETE',attempts=1,result='{}'")
        db.commit()
        with index.db:
            index.db.execute("UPDATE jobs SET status='COMPLETE'")
        queue_tasks(index, db)
        assert db.execute("SELECT status FROM tasks").fetchone()[0] == "COMPLETE"
        with index.db:
            index.db.execute("UPDATE jobs SET status='PENDING'")
        queue_tasks(index, db, [(saved["record_id"], 1, "incremental")])
        assert tuple(db.execute("SELECT status,attempts,result FROM tasks").fetchone()) == ("PENDING", 0, None)
    finally:
        db.close()
        index.close()
