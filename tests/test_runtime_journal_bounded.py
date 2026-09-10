"""Journal history stays durable while resident payloads and reads stay bounded."""

from __future__ import annotations

import gc
import json
import weakref
from pathlib import Path

import pytest

from harness.runtime.journal import JournalCorruptionError, JournalUnavailableError, RuntimeJournal


def _journal(path, **options):
    return RuntimeJournal(
        path, runtime_instance_id="basic-runtime", character_id="misty",
        manifest_revision=1, fsync=False, **options,
    )


def _append(journal, count, *, payload_size=512):
    for index in range(count):
        journal.append("fixture", data={"index": index, "payload": "x" * payload_size})


def test_history_exceeds_tail_and_segments_without_retaining_old_payloads(tmp_path):
    path = tmp_path / "journal"
    journal = _journal(path, max_segment_bytes=64 * 1024)
    first = journal.append("first", data={"payload": "x" * 2048})
    first_ref = weakref.ref(first)
    del first
    _append(journal, 1499, payload_size=2048)
    gc.collect()
    assert first_ref() is None, "Evicted payload objects must not remain in hidden full-history containers."
    assert journal.retained_records == 512
    assert journal.last_record_seq == 1500
    assert len(list(path.glob("runtime-*.jsonl"))) > 2
    assert len(journal._index) <= 1500 // 128 + len(list(path.glob("runtime-*.jsonl"))) + 1
    assert [item.record_seq for item in journal.records_after(5, limit=7)] == list(range(6, 13))
    assert [item.record_seq for item in journal.records_after(1497, limit=9)] == [1498, 1499, 1500]
    assert journal.records_after(1500) == []
    assert [item.record_seq for item in journal.iter_records()] == list(range(1, 1501))
    assert [item.record_seq for item in journal.iter_records(reverse=True)] == list(range(1500, 0, -1))
    assert len(journal.records()) == 1500  # Explicit compatibility operation.


def test_restart_streams_files_and_rebuilds_old_page_index(tmp_path, monkeypatch):
    path = tmp_path / "journal"
    journal = _journal(path, recent_record_limit=16, index_stride=8, max_segment_bytes=4096)
    _append(journal, 90)
    previous_hash = journal.last_hash

    def no_whole_file_read(*args, **kwargs):
        raise AssertionError("Journal recovery must not materialize complete segment files.")

    monkeypatch.setattr(Path, "read_bytes", no_whole_file_read)
    monkeypatch.setattr(Path, "read_text", no_whole_file_read)
    resumed = _journal(path, recent_record_limit=16, index_stride=8, max_segment_bytes=4096)
    assert resumed.retained_records == 16
    assert resumed.last_record_seq == 90
    assert resumed.last_hash == previous_hash
    assert [item.record_seq for item in resumed.records_after(2, limit=50)] == list(range(3, 53))
    new = resumed.append("after_restart")
    assert new.record_seq == 91
    assert new.prev_hash == previous_hash
    assert len(resumed.records()) == 91


@pytest.mark.parametrize("reverse", [False, True])
def test_iteration_has_a_fixed_prefix_while_new_events_are_appended(tmp_path, reverse):
    journal = _journal(tmp_path / "journal", recent_record_limit=16)
    _append(journal, 600, payload_size=0)
    iterator = journal.iter_records(reverse=reverse)
    first = next(iterator)
    journal.append("concurrent")
    sequences = [first.record_seq, *(record.record_seq for record in iterator)]
    assert sequences == (list(range(600, 0, -1)) if reverse else list(range(1, 601)))
    assert journal.last_record_seq == 601


def test_recent_reverse_lookup_does_not_read_old_disk_pages(tmp_path, monkeypatch):
    journal = _journal(tmp_path / "journal")
    _append(journal, 1500, payload_size=0)

    def no_disk_read(*args):
        raise AssertionError("A recent match should be answered from the bounded tail.")

    monkeypatch.setattr(journal, "_read_disk_page", no_disk_read)
    assert next(journal.iter_records(reverse=True)).record_seq == 1500


def test_final_partial_tail_is_truncated_without_losing_old_segments(tmp_path):
    path = tmp_path / "journal"
    journal = _journal(path, recent_record_limit=8, max_segment_bytes=2048)
    _append(journal, 35)
    partial = b'{"record_seq":36,"unfinished":"'
    with journal.path.open("ab") as handle:
        handle.write(partial)
    recovered = _journal(path, recent_record_limit=8, max_segment_bytes=2048)
    assert recovered.truncated_tail_bytes == len(partial)
    assert recovered.last_record_seq == 35
    assert len(recovered.records()) == 35
    assert recovered.records_after(0, limit=1)[0].record_seq == 1
    recovered.append("after_partial_tail")
    assert _journal(path).last_record_seq == 36


@pytest.mark.parametrize("corruption", ["old_checksum", "old_partial", "complete_invalid_tail", "hash_chain"])
def test_corrupt_history_is_rejected_instead_of_trimmed(tmp_path, corruption):
    path = tmp_path / "journal"
    journal = _journal(path, recent_record_limit=8, max_segment_bytes=2048)
    _append(journal, 35)
    first_path = sorted(path.glob("runtime-*.jsonl"))[0]
    if corruption == "old_checksum":
        lines = first_path.read_bytes().splitlines(keepends=True)
        payload = json.loads(lines[0])
        payload["data"]["index"] = -1
        lines[0] = json.dumps(payload).encode() + b"\n"
        first_path.write_bytes(b"".join(lines))
    elif corruption == "old_partial":
        first_path.write_bytes(first_path.read_bytes().rstrip(b"\n")[:-3])
    elif corruption == "hash_chain":
        lines = first_path.read_bytes().splitlines(keepends=True)
        payload = json.loads(lines[0])
        payload["prev_hash"] = "f" * 64
        lines[0] = json.dumps(payload).encode() + b"\n"
        first_path.write_bytes(b"".join(lines))
    else:
        with journal.path.open("ab") as handle:
            handle.write(b"invalid but complete\n")
    before = {item: item.stat().st_size for item in path.glob("runtime-*.jsonl")}
    with pytest.raises(JournalCorruptionError):
        _journal(path, recent_record_limit=8)
    assert {item: item.stat().st_size for item in path.glob("runtime-*.jsonl")} == before


def test_partial_emergency_flush_retries_only_unwritten_records(tmp_path, monkeypatch):
    path = tmp_path / "journal"
    journal = _journal(path, recent_record_limit=2, emergency_buffer_size=3, max_segment_bytes=2048)
    _append(journal, 20)
    original_write = journal._write_record
    failed_sequence = [21]

    def fail_selected_record(record):
        if record.record_seq == failed_sequence[0]:
            raise OSError("injected disk outage")
        original_write(record)

    monkeypatch.setattr(journal, "_write_record", fail_selected_record)
    with pytest.raises(JournalUnavailableError, match="append failed"):
        journal.append("buffered_first")
    journal.append("buffered_second")
    journal.append("buffered_third")
    before = (journal.last_record_seq, journal.last_hash)
    with pytest.raises(JournalUnavailableError, match="buffer is full"):
        journal.append("must_not_consume_a_sequence")
    assert (journal.last_record_seq, journal.last_hash) == before
    assert [record.record_seq for record in journal.records_after(18, limit=5)] == [19, 20, 21, 22, 23]

    failed_sequence[0] = 22
    assert journal.try_recover_writes() is False
    assert journal.buffered_records == 2
    assert journal._disk_last_record_seq == 21
    assert [record.record_seq for record in journal.records_after(18, limit=5)] == [19, 20, 21, 22, 23]
    failed_sequence[0] = None
    assert journal.try_recover_writes() is True
    assert journal.buffered_records == 0
    assert not journal.degraded
    assert journal.append("recovered").record_seq == 24
    restarted = _journal(path, recent_record_limit=2)
    assert [record.record_seq for record in restarted.records()] == list(range(1, 25))


def test_fsync_failure_and_failed_rollback_are_repaired_before_buffer_replay(tmp_path, monkeypatch):
    import harness.runtime.journal as journal_module

    path = tmp_path / "journal"
    journal = _journal(path, recent_record_limit=2)
    _append(journal, 5)
    journal.fsync = True
    original_fsync = journal_module.os.fsync
    calls = [0]

    def fail_first_two_fsyncs(fd):
        calls[0] += 1
        if calls[0] <= 2:
            raise OSError("injected fsync and rollback failure")
        original_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", fail_first_two_fsyncs)
    with pytest.raises(JournalUnavailableError):
        journal.append("must_be_replayed_once")
    assert journal._pending_truncation is not None
    assert journal.buffered_records == 1
    assert journal.try_recover_writes()
    assert journal._pending_truncation is None
    assert len(_journal(path).records()) == 6


def test_page_arguments_and_buffer_sizes_are_validated(tmp_path):
    journal = _journal(tmp_path / "journal")
    _append(journal, 5, payload_size=0)
    with pytest.raises(ValueError, match="non-negative"):
        journal.records_after(-1)
    assert len(journal.records_after(0, limit=0)) == 1
    for field in ("recent_record_limit", "emergency_buffer_size", "index_stride"):
        with pytest.raises(ValueError, match="positive"):
            _journal(tmp_path / field, **{field: 0})
