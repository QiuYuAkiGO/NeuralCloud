from concurrent.futures import ThreadPoolExecutor

from harness.memory.revision.queue import DailyRevisionQueue


def enqueue(queue, **kwargs):
    return queue.enqueue(owner_id="misty", preset_id="misty", day="2026-09-14",
        partition="local", sources=[{"source_id": "one", "text": "frozen"}], basis={"revision": 1}, **kwargs)


def test_idempotent_enqueue_single_claim_receipt_recovery(tmp_path):
    queue = DailyRevisionQueue(tmp_path / "jobs.sqlite3")
    job = enqueue(queue)
    assert enqueue(queue)["run_id"] == job["run_id"]
    with ThreadPoolExecutor(2) as workers:
        claims = list(workers.map(lambda _: queue.claim(), range(2)))
    assert sum(item is not None for item in claims) == 1
    queue.checkpoint(job["run_id"], "proposal", {"memories": ["saved"]})
    restored = DailyRevisionQueue(queue.path)
    restored.recover(set())
    assert restored.claim()["run_id"] == job["run_id"]
    assert restored.stage(job["run_id"], "proposal") == {"memories": ["saved"]}
    # Preset receipt survived while queue acknowledgement was lost.
    restored.recover({job["run_id"]})
    assert restored.get(job["run_id"])["status"] == "applied"
    assert restored.claim() is None


def test_budget_reservation_is_atomic_and_survives_unknown_transmission(tmp_path):
    queue = DailyRevisionQueue(tmp_path / "jobs.sqlite3")
    job = enqueue(queue)
    with ThreadPoolExecutor(2) as workers:
        reserved = list(workers.map(lambda key: queue.reserve(job["run_id"], key, "2026-09-15", 60, 100), ["a", "b"]))
    assert sum(reserved) == 1
    key = "a" if reserved[0] else "b"
    queue.account(key, None, status="transmission_unknown")
    restarted = DailyRevisionQueue(queue.path)
    assert not restarted.reserve(job["run_id"], "c", "2026-09-15", 50, 100)
    restarted.account(key, {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20})
    assert restarted.reserve(job["run_id"], "c", "2026-09-15", 80, 100)
    assert restarted.status()["usage"][0]["charged_tokens"] == 100


def test_rollback_tombstone_prevents_rescheduling_under_changed_model(tmp_path):
    queue = DailyRevisionQueue(tmp_path / "jobs.sqlite3")
    job = enqueue(queue)
    queue.finish(job["run_id"], "applied", {"commit_seq": 1})
    assert queue.tombstone(job["run_id"])
    assert not queue.retry(job["run_id"])
    assert enqueue(queue, config_digest="new-model")["run_id"] == job["run_id"]
    queue.recover({job["run_id"]})
    assert queue.get(job["run_id"])["status"] == "tombstoned"
    preview = enqueue(queue, dry_run=True)
    assert queue.claim()["run_id"] == preview["run_id"]


def test_rebase_only_invalidates_final_proposal_and_claim_is_owner_scoped(tmp_path):
    queue = DailyRevisionQueue(tmp_path / "jobs.sqlite3")
    job = enqueue(queue)
    queue.checkpoint(job["run_id"], "cache:clean", {"cleaned": ["one"]})
    queue.checkpoint(job["run_id"], "proposal", {"old": True})
    queue.checkpoint(job["run_id"], "diff", {"old": True})
    queue.rebase(job["run_id"], {"revision": 2})
    assert queue.stage(job["run_id"], "cache:clean") == {"cleaned": ["one"]}
    assert queue.stage(job["run_id"], "proposal") is None
    assert queue.stage(job["run_id"], "diff") is None
    assert queue.get(job["run_id"])["basis"] == {"revision": 2}
    assert queue.claim(owner_id="someone_else") is None
    assert queue.claim(owner_id="misty", preset_id="misty")["run_id"] == job["run_id"]


def test_invalidated_dependent_revision_gets_a_new_run_with_immutable_old_history(tmp_path):
    queue = DailyRevisionQueue(tmp_path / "jobs.sqlite3")
    old = enqueue(queue)
    queue.checkpoint(old["run_id"], "cache:clean", {"cleaned": ["one"]})
    queue.checkpoint(old["run_id"], "proposal", {"old": True})
    queue.finish(old["run_id"], "applied", {"commit_seq": 5})
    revised = queue.requeue_invalidated(old["run_id"], {"revision": 3})
    assert revised["run_id"] != old["run_id"] and revised["status"] == "pending"
    assert revised["sources"] == old["sources"]
    assert queue.stage(revised["run_id"], "cache:clean") == {"cleaned": ["one"]}
    assert queue.stage(revised["run_id"], "proposal") is None
    assert queue.stage(old["run_id"], "proposal") == {"old": True}
    assert queue.get(old["run_id"])["status"] == "invalidated"
    assert enqueue(queue)["run_id"] == revised["run_id"]
    assert queue.requeue_invalidated(old["run_id"], {"revision": 3})["run_id"] == revised["run_id"]


def test_invalid_proposal_preserves_clean_cache_and_archives_other_stages(tmp_path):
    queue = DailyRevisionQueue(tmp_path / "jobs.sqlite3")
    job = enqueue(queue)
    queue.checkpoint(job["run_id"], "cache:clean", {"stage": "clean:0", "response": {"cleaned": []}})
    queue.checkpoint(job["run_id"], "cache:synth", {"stage": "synthesize", "response": {"wrong": True}})
    queue.checkpoint(job["run_id"], "cache:review", {"stage": "review:0", "response": {"approved": True}})
    queue.checkpoint(job["run_id"], "proposal", {"wrong": True})
    queue.reject_proposal(job["run_id"], "daily_revision_subject_not_authenticated")
    stages = queue.get(job["run_id"])["stages"]
    assert "cache:clean" in stages
    assert not {"cache:synth", "cache:review", "proposal"} & stages.keys()
    assert len([key for key in stages if key.startswith("invalid:")]) == 3
