import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from neuralcloud.api import create_app
from neuralcloud.contracts import digest
from neuralcloud.migration import LegacyImporter, file_hash
from neuralcloud.storage import MemoryStore, Refusal


def legacy_snapshot(path):
    """Synthetic fixture with the schemas observed in the frozen Linux baseline."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE pm_identity(singleton INTEGER,owner TEXT,preset TEXT);
        INSERT INTO pm_identity VALUES(1,'misty-test','test');
        CREATE TABLE pm_records(id TEXT PRIMARY KEY,kind TEXT,subject TEXT,text TEXT,time TEXT,revision INTEGER,active INTEGER,confidence REAL,pinned INTEGER,important INTEGER);
        CREATE TABLE pm_sources(id TEXT,payload TEXT,digest TEXT,revoked INTEGER);
        CREATE TABLE pm_revisions(id TEXT,revision INTEGER,payload TEXT);
        CREATE TABLE pm_visibility(id TEXT,audience TEXT);
        CREATE TABLE pm_source_alias(alias TEXT,id TEXT);
        CREATE TABLE pm_lineage(parent TEXT,child TEXT);
        CREATE TABLE pm_legacy_valid(id TEXT,complete INTEGER);
        CREATE TABLE pm_legacy_presence(id TEXT,present INTEGER);
        CREATE TABLE pm_evidence(id TEXT,source TEXT,stance TEXT);
        CREATE TABLE pm_evidence_revision(id TEXT,source TEXT,revision INTEGER);
        CREATE TABLE pm_v2_versions(id TEXT,revision INTEGER,payload TEXT,event_id TEXT);
        CREATE TABLE pm_v2_relations(id TEXT,revision INTEGER,unit_key TEXT,source TEXT,source_revision INTEGER,role TEXT,payload TEXT);
        CREATE TABLE pm_v2_supersessions(prior_id TEXT,prior_revision INTEGER,replacement_id TEXT,replacement_revision INTEGER,effective_from TEXT);
    """)
    for key, revoked in (("old-source", 0), ("withdrawn", 1)):
        source = {
            "record_id": key,
            "kind": "observation",
            "author_id": "小明",
            "text": "小明说喜欢星空",
            "occurred_at": "2020-01-01T00:00:00+00:00",
            "scope": {"conversation_id": "private-a", "channel": "qq", "visibility": ["private-a"]},
            "source_refs": ["platform:message:1"],
            "original": {"immutable_raw": True},
        }
        db.execute("INSERT INTO pm_sources VALUES(?,?,?,?)", (key, json.dumps(source), digest(source), revoked))
        db.execute(
            "INSERT INTO pm_records VALUES(?,'experience',NULL,?,?,0,1,NULL,0,0)",
            (key, source["text"], source["occurred_at"]),
        )
        db.execute("INSERT INTO pm_visibility VALUES(?,'private-a')", (key,))
    db.execute("INSERT INTO pm_source_alias VALUES('阿明','old-source')")
    for identifier, source in (("claim", "old-source"), ("invalidated", "withdrawn")):
        db.execute(
            "INSERT INTO pm_records VALUES(?,'claim_v2','小明','历史认识','2020-01-01T00:00:00+00:00',2,1,NULL,0,1)",
            (identifier,),
        )
        for revision in (1, 2):
            claim = {
                "record_id": identifier,
                "revision": revision,
                "kind": "interpretation",
                "statement": "曾经认为小明喜欢天文学",
                "subject_id": "小明",
                "valid_from": "2020-01-01T00:00:00+00:00",
                "topic": "兴趣",
                "context": "过去的判断",
                "source_channel": "qq",
                "source_conversation_id": "group-b",
                "supersedes": [{"record_id": "earlier-claim", "expected_revision": 1}],
            }
            relation = {
                "source_id": source,
                "source_revision": 0,
                "role": "challenges" if revision == 1 else "supports",
                "start": 0,
                "end": 2,
            }
            db.execute(
                "INSERT INTO pm_v2_versions VALUES(?,?,?,?)", (identifier, revision, json.dumps(claim), "claim-event")
            )
            db.execute(
                "INSERT INTO pm_v2_relations VALUES(?,?,?,?,?,?,?)",
                (identifier, revision, "unit", source, 0, relation["role"], json.dumps(relation)),
            )
    db.commit()
    db.close()


def test_snapshot_import_is_bounded_resumable_and_preserves_semantics(tmp_path):
    source, target = tmp_path / "source.sqlite3", tmp_path / "new.sqlite3"
    legacy_snapshot(source)
    sha = file_hash(source)

    def open_importer():
        return LegacyImporter(source, target, character="misty-test", expected_sha256=sha, expected_preset="test")

    importer = open_importer()
    importer.step(1)
    importer.close()
    with pytest.raises(Refusal, match="MIGRATION_NOT_COMPLETE"):
        MemoryStore(target, "misty-test")
    importer = open_importer()
    while importer.step(1)["stage"] != "COMPLETE":
        pass
    report = importer.report()
    assert report["records"] == 4 and report["revisions"] == 6
    assert report["old_actions_replayed"] == 0
    assert importer.step(1) == report
    importer.close()
    assert file_hash(source) == sha
    store = MemoryStore(target, "misty-test")
    try:
        assert store.read("withdrawn")["status"] == "RETRACTED"
        assert store.read("invalidated")["status"] == "SOURCE_CHANGED"
        found = store.read("old-source")
        assert found["source"]["provenance"]["original_revision"] == 0
        assert found["revision"] == 1
        assert store.recall("阿明")["items"][0]["record_id"] == "old-source"
        claim = store.read("claim")
        assert claim["source"]["provenance"]["semantic_status"] == "historical_interpretation"
        assert claim["source"]["provenance"]["evidence"][0]["original_revision"] == 0
        assert claim["source"]["pinned"] is True
        old = json.loads(
            store.db.execute("SELECT payload FROM revisions WHERE record_id='claim' AND revision=1").fetchone()[0]
        )
        assert old["provenance"]["evidence"][0]["role"] == "challenges"
        client = TestClient(create_app(store, "x" * 32))
        assert client.get("/v1/records/claim", headers={"Authorization": "Bearer " + "x" * 32}).status_code == 200
    finally:
        store.close()


def test_migration_failed_batch_rolls_back_and_changed_snapshot_is_rejected(tmp_path):
    source, target = tmp_path / "source.sqlite3", tmp_path / "new.sqlite3"
    legacy_snapshot(source)
    sha = file_hash(source)
    importer = LegacyImporter(source, target, character="misty-test", expected_sha256=sha, expected_preset="test")
    importer.target.db.execute(
        "CREATE TRIGGER test_failure BEFORE INSERT ON records BEGIN SELECT RAISE(ABORT,'simulated IO failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        importer.step(2)
    assert importer.target.generation() == 0
    assert importer.target.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
    importer.target.db.execute("DROP TRIGGER test_failure")
    importer.step(1)
    importer.close()
    with sqlite3.connect(source) as db:
        db.execute("UPDATE pm_records SET active=0 WHERE id='old-source'")
    with pytest.raises(Refusal, match="SNAPSHOT_HASH_MISMATCH"):
        LegacyImporter(source, target, character="misty-test", expected_sha256=sha, expected_preset="test")
    with pytest.raises(Refusal, match="MIGRATION_SOURCE_CHANGED"):
        LegacyImporter(
            source, target, character="misty-test", expected_sha256=file_hash(source), expected_preset="test"
        )
