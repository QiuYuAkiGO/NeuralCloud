"""Independent review regressions; synthetic SQLite only, no model calls."""
from contextlib import closing
from datetime import datetime, timezone
import json

from harness.memory.evidence_manifest import EvidenceManifestStore
from harness.memory.evidence_v2 import EvidenceV2Store, ensure_schema
from harness.memory.production import ProductionMemoryStore
from tests.test_evidence_manifest import NOW, source


def test_repeated_same_future_judgment_requires_revision_not_new_id(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    memory.ingest([source()])
    manifests, writer = EvidenceManifestStore(memory), EvidenceV2Store(memory)
    future = datetime(2090, 1, 1, tzinfo=timezone.utc)
    receipts = []
    for index in range(2):
        views = manifests.prepare_views(["raw-a"], audience="local:main")
        manifest = manifests.issue({"evidence_views": views}, audience="local:main", command_id=f"future:{index}", now=NOW)
        view = views[0]
        claim = {"subject_id": "user:a", "topic": "饮品", "statement": "从约定日期起喝茶",
            "kind": "self_report", "report_mode": "direct", "facet": "preference", "valid_from": future.isoformat(),
            "anchors": [{"unit_id": view["unit_id"], "start": view["start"], "end": view["end"],
                         "quote": view["text"], "role": "supports", "rationale": "same original future report"}]}
        receipts.append(writer.apply_groups([{"group_id": "same", "claims": [claim]}],
            manifest_id=manifest["manifest_id"], event_id=f"event:{index}", audience="local:main", now=NOW))
    assert receipts[0]["accepted_count"] == 1
    assert receipts[1]["rejected_count"] == 1
    assert receipts[1]["groups"][0]["reason"] == "existing_claim_requires_revision"


def test_reopening_v2_schema_does_not_rescan_version_history(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    EvidenceV2Store(memory)
    with closing(memory._connect()) as db:
        sql = []
        db.set_trace_callback(sql.append)
        ensure_schema(db)
    history_scans = [statement for statement in sql if statement.lstrip().upper().startswith("SELECT")
                     and "pm_v2_versions" in statement and "pm_v2_claim_keys" in statement]
    assert history_scans == [], json.dumps(history_scans)


def test_schema_invalid_group_without_id_keeps_original_proposal_for_repair(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    memory.ingest([source()])
    manifests = EvidenceManifestStore(memory)
    views = manifests.prepare_views(["raw-a"], audience="local:main")
    manifest = manifests.issue({"evidence_views": views}, audience="local:main", command_id="invalid-group", now=NOW)
    raw_invalid = {"claims": [{"statement": "preserve this rejected proposal", "missing_other_required_fields": True}]}
    receipt = EvidenceV2Store(memory).apply_groups([raw_invalid], manifest_id=manifest["manifest_id"],
        event_id="invalid-group:event", audience="local:main", now=NOW)
    assert receipt["rejected_count"] == 1
    with closing(memory._connect()) as db:
        saved = json.loads(db.execute("SELECT payload FROM pm_v2_tasks WHERE event_id='invalid-group:event'").fetchone()[0])
    assert saved == raw_invalid
