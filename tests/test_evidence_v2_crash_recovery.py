"""Kill the writer around its durable boundary; never reuse live state."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from harness.memory.evidence_manifest import EvidenceManifestStore
from harness.memory.evidence_v2 import EvidenceV2Store
from harness.memory.production import BeliefRevision, ProductionMemoryStore
from tests.test_evidence_manifest import NOW, source
from tests.test_production_evidence_integration import proposal


@pytest.mark.parametrize("crash", ["before_commit", "after_commit"])
def test_process_exit_does_not_lose_sources_or_duplicate_claims(tmp_path, crash):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    memory.ingest([source("raw", author="local:main")])
    manifests = EvidenceManifestStore(memory)
    view = manifests.prepare_views(["raw"], audience="local:main")[0]
    manifest = manifests.issue({"evidence_views": [view]}, audience="local:main", command_id="crash-call")
    group = proposal(view)
    case = {"groups": [group], "manifest_id": manifest["manifest_id"], "event_id": "durable-event", "audience": "local:main"}
    (tmp_path / "input.json").write_text(json.dumps(case), encoding="utf-8")
    script = '''import json,os,sys
from pathlib import Path
from harness.memory.production import ProductionMemoryStore
from harness.memory.evidence_v2 import EvidenceV2Store
root=Path(sys.argv[1]); store=EvidenceV2Store(ProductionMemoryStore(root/'memory.sqlite3'))
if sys.argv[2]=='before_commit':
    original=store._apply_group
    def interrupted(*args,**kwargs):
        original(*args,**kwargs)
        os._exit(17)
    store._apply_group=interrupted
store.apply_groups(**json.loads((root/'input.json').read_text(encoding='utf-8')))
os._exit(17)
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), crash],
        cwd=Path(__file__).resolve().parents[1], env={**os.environ, "PYTHONPATH": "."}, capture_output=True, timeout=25)
    assert result.returncode == 17, result.stderr.decode(errors="replace")
    reopened = ProductionMemoryStore(memory.path)
    with closing(reopened._connect()) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM pm_sources").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM pm_v2_versions").fetchone()[0] == int(crash == "after_commit")
    first = EvidenceV2Store(reopened).apply_groups(**case)
    assert first == EvidenceV2Store(reopened).apply_groups(**case)
    assert first["accepted_count"] == 1
    with closing(reopened._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM pm_v2_versions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM pm_v2_receipts").fetchone()[0] == 1


def test_additive_schema_preserves_legacy_payloads_and_source_bytes(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "migration.sqlite3")
    memory.ingest([source("old", author="local:main")])
    memory.revise(BeliefRevision(record_id="legacy", subject_id="local:main", topic="饮品", statement="历史解释原文",
        kind="self_report", revision=1, supporting_ids=("old",), valid_from=NOW), event_id="legacy-seed")
    def archived_digest(store):
        with closing(store._connect()) as db:
            values = [list(map(tuple, db.execute(sql).fetchall())) for sql in [
                "SELECT * FROM pm_sources ORDER BY id", "SELECT * FROM pm_revisions ORDER BY id,revision",
                "SELECT * FROM pm_records WHERE kind!='claim_v2' ORDER BY id"]]
            return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()
    before = archived_digest(memory)
    with closing(memory._connect()) as db, db:
        # Simulate the pre-v2 schema with real historical rows, no model rewrite.
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pm_v2_%'")]
        for table in tables:
            assert table.startswith("pm_v2_") and table.replace("_", "").isalnum()
            db.execute('DROP TABLE "' + table + '"')
    upgraded = ProductionMemoryStore(memory.path)
    assert archived_digest(upgraded) == before
    manifests = EvidenceManifestStore(upgraded)
    view = manifests.prepare_views(["old"], audience="local:main")[0]
    manifest = manifests.issue({"evidence_views": [view]}, audience="local:main", command_id="upgrade")
    receipt = EvidenceV2Store(upgraded).apply_groups([proposal(view)], manifest_id=manifest["manifest_id"],
        event_id="upgrade", audience="local:main")
    assert receipt["accepted_count"] == 1 and archived_digest(upgraded) == before
    assert upgraded.history("legacy")[0]["statement"] == "历史解释原文"
    assert ProductionMemoryStore(memory.path).recall(audience="local:main")["entries"]
