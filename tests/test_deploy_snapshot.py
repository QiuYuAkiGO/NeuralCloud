from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from harness.deploy import snapshot as deploy_snapshot
from harness.deploy.snapshot import (
    SnapshotError,
    SnapshotSourceChanged,
    SourceSpec,
    build_sources_manifest,
    discover_shared_sources,
    export_snapshot,
    main,
    restore_snapshot,
    rewrite_config_paths,
    verify_snapshot,
)
from harness.runtime.journal import RuntimeJournalRecord, _payload_checksum


def _journal(path: Path, count: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = ""
    rows = []
    for sequence in range(1, count + 1):
        payload = {
            "schema_version": "runtime-journal-v1", "record_seq": sequence,
            "record_type": "migration_test", "runtime_instance_id": "test-runtime",
            "character_id": "misty", "manifest_revision": 1, "recorded_at": "2026-09-15T00:00:00Z",
            "event_id": None, "receive_seq": None, "process_seq": None, "correlation_id": None,
            "command_id": None, "idempotency_key": None, "data": {"historic_path": "C:\\old\\state"},
            "prev_hash": previous,
        }
        item = RuntimeJournalRecord(**payload, checksum=_payload_checksum(payload))
        assert item.verify_checksum()
        previous = item.checksum
        rows.append(item.model_dump_json() + "\n")
    path.write_text("".join(rows), encoding="utf-8")


def _export(source: Path, destination: Path, **kwargs):
    return export_snapshot([SourceSpec("state", source, "state")], destination,
                           stopped_ack=True, desktop_status="stopped", **kwargs)


def _reseal(snapshot: Path, relative: str | None = None) -> None:
    """Adjust transport checksums so semantic corruption reaches offline checks."""
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if relative:
        source = snapshot / "data" / relative
        for item in manifest["files"]:
            if item["path"] == relative:
                item["size"] = source.stat().st_size
                item["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (snapshot / "manifest.sha256").write_text(hashlib.sha256(manifest_path.read_bytes()).hexdigest() + "\n")


def test_committed_wal_is_merged_and_restore_preserves_journal(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    database = source / "memory.sqlite3"
    # Keep the idle connection open so committed pages remain in WAL; there is
    # no writer while the export runs. A raw copy of the main DB loses the row.
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE messages (message TEXT)")
        writer.execute("INSERT INTO messages VALUES ('committed-wal-row')")
        writer.commit()
        assert Path(str(database) + "-wal").stat().st_size > 0
        journal = source / "journal/runtime-000001.jsonl"
        _journal(journal)
        journal_bytes = journal.read_bytes()
        for name in ("runtime.lock", "direct.status.json", ".neuralcloud.pids.json", "pending-start.json", "noise.tmp"):
            (source / name).write_text("not durable")
        snapshot = tmp_path / "snapshot"
        manifest = _export(source, snapshot)
    names = {item["path"] for item in manifest["files"]}
    assert names == {"state/memory.sqlite3", "state/journal/runtime-000001.jsonl"}
    report = verify_snapshot(snapshot)
    assert report["sqlite_databases"] == 1
    assert report["journals"]["state/journal"]["records"] == 2
    before = {str(p): p.read_bytes() for p in snapshot.rglob("*") if p.is_file()}
    verify_snapshot(snapshot)
    assert before == {str(p): p.read_bytes() for p in snapshot.rglob("*") if p.is_file()}
    restored = tmp_path / "restored"
    restore_snapshot(snapshot, restored)
    with closing(sqlite3.connect(restored / "state/memory.sqlite3")) as connection:
        assert connection.execute("SELECT message FROM messages").fetchone()[0] == "committed-wal-row"
    assert (restored / "state/journal/runtime-000001.jsonl").read_bytes() == journal_bytes


@pytest.mark.parametrize("ack,desktop", [(False, "stopped"), (True, "running"), (True, None)])
def test_export_requires_explicit_stop_and_desktop_declaration(tmp_path, ack, desktop):
    source = tmp_path / "source.json"
    source.write_text("{}")
    with pytest.raises(SnapshotError, match="all writers"):
        export_snapshot([SourceSpec("config", source, "config.json")], tmp_path / "out",
                        stopped_ack=ack, desktop_status=desktop)
    assert not (tmp_path / "out").exists()


def test_source_changed_during_export_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("first")
    original_copy = deploy_snapshot.shutil.copyfile

    def change_after_copy(src, dest):
        result = original_copy(src, dest)
        source.write_text("changed by another writer")
        return result

    monkeypatch.setattr(deploy_snapshot.shutil, "copyfile", change_after_copy)
    with pytest.raises(SnapshotSourceChanged, match="source changed") as raised:
        _export(source, tmp_path / "snapshot")
    assert raised.value.changes == [{"roles": ["state"], "path": str(source), "change": "modified"}]
    assert not (tmp_path / "snapshot").exists()
    assert not list(tmp_path.glob("*.incomplete"))


def test_closed_wal_database_backup_ignores_its_own_empty_wal_sidecar(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    database = source / "closed.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE remembered(value TEXT)")
        connection.execute("INSERT INTO remembered VALUES ('preserved')")
        connection.commit()
    assert not Path(str(database) + "-wal").exists()
    before = database.read_bytes()
    snapshot = tmp_path / "snapshot"
    _export(source, snapshot)
    assert database.read_bytes() == before
    assert verify_snapshot(snapshot)["sqlite_databases"] == 1
    assert not list((snapshot / "data").rglob("*-wal"))
    with closing(sqlite3.connect(snapshot / "data/state/closed.sqlite3")) as connection:
        assert connection.execute("SELECT value FROM remembered").fetchone() == ("preserved",)


def test_nonempty_wal_change_during_backup_still_rejects_export(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    database = source / "memory.sqlite3"
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE changes(value INTEGER)")
        writer.commit()
        original_backup = deploy_snapshot._backup_sqlite

        def backup_then_write(src, target):
            original_backup(src, target)
            writer.execute("INSERT INTO changes VALUES(1)")
            writer.commit()

        monkeypatch.setattr(deploy_snapshot, "_backup_sqlite", backup_then_write)
        with pytest.raises(SnapshotSourceChanged) as raised:
            _export(source, tmp_path / "snapshot")
        assert any(row["path"].endswith("-wal") and row["change"] == "modified" for row in raised.value.changes)


def test_only_configuration_path_fields_are_rewritten(tmp_path):
    source = tmp_path / "session.json"
    config = {"memory_db": "C:\\old\\qq\\memory.sqlite3", "token_file": "c:/OLD/token",
              "prompt": "C:\\old\\memory.sqlite3", "api_key": "C:\\old\\not-a-path",
              "allowed_local_image_roots": ["C:\\old\\images"],
              "nested": {"model_config_path": "C:\\older\\config.json"}}
    source.write_text(json.dumps(config), encoding="utf-8")
    original = source.read_bytes()
    snapshot = tmp_path / "snapshot"
    export_snapshot([SourceSpec("session", source, "config/session.json", rewrite_config=True)], snapshot,
                    stopped_ack=True, desktop_status="not-configured", config_rewrites={"C:\\old": "/srv/neuralcloud"})
    rewritten = json.loads((snapshot / "data/config/session.json").read_text(encoding="utf-8"))
    assert rewritten["memory_db"] == "/srv/neuralcloud/qq/memory.sqlite3"
    assert rewritten["token_file"] == "/srv/neuralcloud/token"
    assert rewritten["allowed_local_image_roots"] == ["/srv/neuralcloud/images"]
    assert rewritten["prompt"] == config["prompt"]
    assert rewritten["api_key"] == config["api_key"]
    assert rewritten["nested"] == config["nested"]
    assert source.read_bytes() == original
    assert rewrite_config_paths({"memory_db": "/a/file"}, {"/a": "/b"}) == {"memory_db": "/b/file"}


def test_corrupt_journal_tail_is_reported_without_repair(tmp_path):
    source = tmp_path / "source"
    _journal(source / "journal/runtime-000001.jsonl")
    snapshot = tmp_path / "snapshot"
    _export(source, snapshot)
    journal = snapshot / "data/state/journal/runtime-000001.jsonl"
    with journal.open("ab") as handle:
        handle.write(b'{"incomplete":')
    _reseal(snapshot, "state/journal/runtime-000001.jsonl")
    corrupted = journal.read_bytes()
    with pytest.raises(SnapshotError, match="incomplete journal tail"):
        verify_snapshot(snapshot)
    assert journal.read_bytes() == corrupted


def test_journal_hash_chain_and_checkpoint_are_verified(tmp_path):
    source = tmp_path / "source"
    journal = source / "journal/runtime-000001.jsonl"
    _journal(journal)
    last = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    (source / "checkpoint.json").write_text(json.dumps({"journal_record_seq": 2, "journal_hash": last["checksum"]}))
    snapshot = tmp_path / "snapshot"
    _export(source, snapshot)
    checkpoint = snapshot / "data/state/checkpoint.json"
    checkpoint.write_text(json.dumps({"journal_record_seq": 2, "journal_hash": "0" * 64}))
    _reseal(snapshot, "state/checkpoint.json")
    with pytest.raises(SnapshotError, match="checkpoint does not match"):
        verify_snapshot(snapshot)
    checkpoint.write_text(json.dumps({"journal_record_seq": 2, "journal_hash": last["checksum"]}))
    _reseal(snapshot, "state/checkpoint.json")
    frozen_journal = snapshot / "data/state/journal/runtime-000001.jsonl"
    lines = frozen_journal.read_text(encoding="utf-8").splitlines()
    changed = json.loads(lines[0])
    changed["data"]["altered"] = True
    lines[0] = json.dumps(changed)
    frozen_journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _reseal(snapshot, "state/journal/runtime-000001.jsonl")
    before = frozen_journal.read_bytes()
    with pytest.raises(SnapshotError, match="journal hash chain"):
        verify_snapshot(snapshot)
    assert frozen_journal.read_bytes() == before


def test_plain_file_corruption_is_detected_before_restore(tmp_path):
    source = tmp_path / "source"
    source.write_text("original")
    snapshot = tmp_path / "snapshot"
    _export(source, snapshot)
    (snapshot / "data/state").write_text("tampered")
    with pytest.raises(SnapshotError, match="hash or size mismatch"):
        restore_snapshot(snapshot, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_corrupt_sqlite_is_detected_after_transport_reseal(tmp_path):
    source = tmp_path / "memory.sqlite3"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE entries(id INTEGER)")
        connection.commit()
    snapshot = tmp_path / "snapshot"
    _export(source, snapshot)
    (snapshot / "data/state").write_bytes(b"damaged database")
    _reseal(snapshot, "state")
    with pytest.raises(SnapshotError, match="SQLite"):
        verify_snapshot(snapshot)


@pytest.mark.parametrize("target", ["../escape", "/absolute", "C:/windows", "a\\b", "a/../b", "a//b", "a/CON"])
def test_source_target_traversal_rejected(tmp_path, target):
    source = tmp_path / "source"
    source.write_text("ok")
    with pytest.raises(SnapshotError):
        export_snapshot([SourceSpec("state", source, target)], tmp_path / "snapshot",
                        stopped_ack=True, desktop_status="stopped")


def test_malicious_manifest_and_unlisted_files_are_rejected(tmp_path):
    source = tmp_path / "source"
    source.write_text("ok")
    snapshot = tmp_path / "snapshot"
    _export(source, snapshot)
    unexpected = snapshot / "unlisted.txt"
    unexpected.write_text("extra")
    with pytest.raises(SnapshotError, match="unlisted"):
        verify_snapshot(snapshot)
    unexpected.unlink()
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../escape"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _reseal(snapshot)
    with pytest.raises(SnapshotError, match="unsafe component"):
        restore_snapshot(snapshot, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_existing_restore_destination_and_source_overlap_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "value").write_text("ok")
    with pytest.raises(SnapshotError, match="overlaps"):
        _export(source, source / "snapshot")
    snapshot = tmp_path / "snapshot"
    _export(source, snapshot)
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "sentinel").write_text("keep")
    with pytest.raises(SnapshotError, match="already exists"):
        restore_snapshot(snapshot, destination)
    assert (destination / "sentinel").read_text() == "keep"


def test_symlink_source_and_restore_parent_rejected(tmp_path):
    source = tmp_path / "source"
    source.write_text("ok")
    link = tmp_path / "link"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("Windows account lacks symlink privilege")
    with pytest.raises(SnapshotError, match="symlink"):
        _export(link, tmp_path / "snapshot")
    snapshot = tmp_path / "snapshot"
    _export(source, snapshot)
    directory = tmp_path / "target"
    directory.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(SnapshotError, match="symlink"):
        restore_snapshot(snapshot, parent_link / "restored")


def test_discovery_includes_private_references_and_explicit_board_extra(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    direct = tmp_path / "private/direct.json"
    direct.parent.mkdir()
    token = direct.parent / "onebot.token"
    database = direct.parent / "direct.sqlite3"
    evidence = direct.parent / "evidence"
    direct.write_text(json.dumps({"token_file": str(token), "state_db": str(database),
                                  "evidence_dir": str(evidence)}), encoding="utf-8")
    board = tmp_path / "wsl-export/board.sqlite3"
    sources = discover_shared_sources(repo, {"NC_QQ_DIRECT_CONFIG": str(direct)},
                                      extras=[SourceSpec("board-state", board, "state/board/board.sqlite3")])
    paths = {Path(item.path) for item in sources}
    assert {direct, token, database, evidence, board, repo / "data/cognitive"}.issubset(paths)
    with pytest.raises(SnapshotError, match="absolute"):
        discover_shared_sources(repo, {"NC_RUNTIME_STATE_DIR": "relative/path"})


@pytest.mark.parametrize("binding,filename,role", [
    ("NC_LLM_FALLBACK_CONFIG", "main-fallback.json", "main-fallback-config"),
    ("NC_QQ_VISION_TOOL_CONFIG", "qq-vision-tool.json", "qq-vision-tool-config"),
])
def test_private_model_config_is_discovered_and_its_binding_is_rewritten(tmp_path,binding,filename,role):
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = tmp_path / "private" / filename
    profile.parent.mkdir()
    profile.write_text(json.dumps({"base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash", "api_key": "private-fixture"}))
    sources = discover_shared_sources(repo, {binding: str(profile)})
    fallback = next(item for item in sources if item.role == role)
    assert Path(fallback.path) == profile and fallback.rewrite_config
    assert fallback.target == "config/" + filename
    rewritten = rewrite_config_paths({binding: str(profile)},
        {str(profile): "/etc/neuralcloud/" + filename})
    assert rewritten[binding] == "/etc/neuralcloud/" + filename


def test_reference_only_status_and_parent_paths_are_mapped_but_not_copied(tmp_path):
    source = tmp_path / "source"
    domain = source / "homeostasis/domain"
    domain.mkdir(parents=True)
    (domain / "snapshot.json").write_text("{}")
    status = source / "direct.status.json"
    status.write_text('{"running":true}')
    specs = [SourceSpec("homeostasis", domain, "state/homeostasis/domain"),
             SourceSpec("homeostasis-parent", domain.parent, "state/homeostasis", required=False, reference_only=True),
             SourceSpec("direct-status", status, "state/qq-direct/direct.status.json", required=False, reference_only=True)]
    source_manifest = build_sources_manifest(specs)
    rewritten = rewrite_config_paths({"homeostasis_state_dir": str(domain.parent), "direct_status_file": str(status)},
                                     source_manifest["path_rewrites"])
    assert rewritten == {"homeostasis_state_dir": "/var/lib/neuralcloud/homeostasis",
                         "direct_status_file": "/var/lib/neuralcloud/qq-direct/direct.status.json"}
    snapshot = tmp_path / "snapshot"
    manifest = export_snapshot(specs, snapshot, stopped_ack=True, desktop_status="stopped")
    assert [row["path"] for row in manifest["files"]] == ["state/homeostasis/domain/snapshot.json"]
    assert sum(row["reference_only"] for row in manifest["sources"]) == 2


def test_inventory_cli_expands_shared_launch_and_does_not_export_data(tmp_path):
    from tools.deploy.export_shared import main as inventory_main

    repo = tmp_path / "repo"
    repo.mkdir()
    homeostasis = tmp_path / "private/homeostasis/domain"
    (repo / "launch.json").write_text(json.dumps({"profile": "shared", "profiles": {"shared": {
        "manifest_path": "presets/runtime/live.json", "profile_path": "presets/homeostasis/live.json",
        "bindings": {"NC_HOMEOSTASIS_STATE_DIR": str(homeostasis), "NC_GUI_CONFIG": "data/gui_settings.json"}}}}))
    output = tmp_path / "sources.json"
    assert inventory_main(["inventory", "--repo", str(repo), "--output", str(output),
                           "--extra-source", f"board-state={tmp_path / 'wsl-export'}=state/board"]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["path_rewrites"][str(homeostasis)] == "/var/lib/neuralcloud/homeostasis/domain"
    assert payload["path_rewrites"][str(homeostasis.parent)] == "/var/lib/neuralcloud/homeostasis"
    assert any(item["role"] == "board-state" for item in payload["sources"])
    assert not homeostasis.exists()
    assert inventory_main(["inventory", "--repo", str(repo), "--output", str(output)]) == 2


def test_direct_explicit_status_control_and_desktop_bindings_are_preserved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    direct = tmp_path / "direct.json"
    control = tmp_path / "custom-control.json"
    status = tmp_path / "custom-status.json"
    desktop = tmp_path / "windows-watcher.status.json"
    control.write_text('{"paused":false,"stop":false}')
    payload = {"state_db": str(tmp_path / "direct.sqlite3"), "status_file": str(status),
               "control_file": str(control), "desktop_status_file": str(desktop)}
    direct.write_text(json.dumps(payload))
    sources = discover_shared_sources(repo, {"NC_QQ_DIRECT_CONFIG": str(direct)})
    inventory = build_sources_manifest(sources)
    rewritten = rewrite_config_paths(payload, inventory["path_rewrites"])
    assert rewritten["status_file"] == "/var/lib/neuralcloud/qq-direct/direct.status.json"
    assert rewritten["control_file"] == "/var/lib/neuralcloud/qq-direct/direct.control.json"
    assert rewritten["desktop_status_file"] == "/var/lib/neuralcloud/qq-desktop/desktop.status.json"
    by_path = {Path(row.path): row for row in sources}
    assert by_path[status].reference_only and by_path[desktop].reference_only
    assert not by_path[control].reference_only


def test_external_web_bundle_and_optional_revision_history_are_discovered(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    web = tmp_path / "external-web/config.json"
    web.parent.mkdir()
    web.write_text(json.dumps({"schema_version": "web-config-index-v1", "revision": 1,
                               "revision_file": "revisions/web-r1.json"}))
    sources = discover_shared_sources(repo, {"NC_WEB_CONFIG": str(web)})
    by_role = {row.role: row for row in sources}
    assert Path(by_role["web-revisions"].path) == web.parent / "revisions"
    assert Path(by_role["web-keys"].path) == web.parent / "keys"
    assert by_role["web-client-config"].target == "config/web/config.json"
    assert Path(by_role["manifest-revisions"].path) == repo / "data/runtime/manifests"
    assert not by_role["homeostasis-revisions"].required


@pytest.mark.parametrize("explicit_trace", [False, True])
def test_watcher_uses_configured_trace_or_real_default_and_keeps_sibling_evidence(tmp_path, explicit_trace):
    repo = tmp_path / "repo"
    repo.mkdir()
    watcher = tmp_path / "watcher.json"
    status = tmp_path / "status-dir/custom-status.json"
    control = tmp_path / "controls/custom-control.json"
    trace = tmp_path / "evidence/operator-trace.jsonl" if explicit_trace else status.with_name("watcher-trace.jsonl")
    trace.parent.mkdir(parents=True)
    for item in (trace, trace.with_suffix(".observation.jsonl"), trace.with_suffix(".gaps.jsonl")):
        item.write_bytes(b'{"opaque_historical_path":"C:\\\\source"}\n')
    baseline = tmp_path / "baseline-observation.json"
    baseline.write_text("{}")
    payload = {"state_db": str(tmp_path / "db/custom-ledger.sqlite3"), "status_file": str(status),
               "control_file": str(control), "initial_baseline_observation": str(baseline)}
    if explicit_trace:
        payload["trace_file"] = str(trace)
    watcher.write_text(json.dumps(payload))
    sources = discover_shared_sources(repo, {"NC_QQ_WATCHER_CONFIG": str(watcher)})
    by_role = {row.role: row for row in sources}
    assert Path(by_role["qq-watcher-trace"].path) == trace
    assert Path(by_role["qq-watcher-control"].path) == control
    assert Path(by_role["qq-watcher-status"].path) == status
    assert all(Path(row.path).name != "watcher.trace.jsonl" for row in sources)
    inventory = build_sources_manifest(sources)
    rewritten = rewrite_config_paths(payload, inventory["path_rewrites"])
    if explicit_trace:
        assert rewritten["trace_file"] == f"/var/lib/neuralcloud/qq-watcher/{trace.name}"
    assert rewritten["initial_baseline_observation"] == "/var/lib/neuralcloud/qq-watcher/baseline-observation.json"
    snapshot = tmp_path / "snapshot"
    traces = [row for row in sources if row.role.startswith("qq-watcher-trace")]
    export_snapshot(traces, snapshot, stopped_ack=True, desktop_status="stopped")
    for row in traces:
        assert (snapshot / "data" / row.target).read_bytes() == Path(row.path).read_bytes()


def test_cli_sources_manifest_backup_verify_restore(tmp_path, capsys):
    source = tmp_path / "source.json"
    source.write_text("{}")
    spec = tmp_path / "sources.json"
    spec.write_text(json.dumps({"sources": [{"role": "source", "path": str(source), "target": "source.json"}]}))
    snapshot = tmp_path / "snapshot"
    assert main(["backup", "--sources-manifest", str(spec), "--output", str(snapshot),
                 "--writers-stopped", "--desktop-status", "not-configured"]) == 0
    assert main(["verify", "--snapshot", str(snapshot)]) == 0
    assert main(["restore", "--snapshot", str(snapshot), "--destination", str(tmp_path / "restored")]) == 0
    assert main(["verify"]) == 2
    assert "offline verify requires --snapshot" in capsys.readouterr().err
