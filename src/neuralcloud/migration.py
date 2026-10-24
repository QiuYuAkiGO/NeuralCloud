"""Explicit, resumable import of a verified production-memory SQLite snapshot.

No legacy runtime imports, models, channels, state feedback or action replay.
The immutable source snapshot remains the lossless maintenance asset, including
retired execution ledgers. This converter is an online projection, not permission
to delete that asset. Original IDs/revisions and raw payload hashes are retained.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .contracts import EvidenceReference, LegacyProvenance, SourceEvent, canonical, digest
from .storage import MemoryStore, Refusal, initialize

REQUIRED = {
    "pm_identity",
    "pm_records",
    "pm_sources",
    "pm_revisions",
    "pm_visibility",
    "pm_source_alias",
    "pm_lineage",
    "pm_legacy_valid",
    "pm_legacy_presence",
    "pm_evidence",
    "pm_evidence_revision",
    "pm_v2_versions",
    "pm_v2_relations",
    "pm_v2_supersessions",
}


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def timestamp(value):
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.utcoffset() is None or moment.timestamp() < 0:
        raise Refusal("LEGACY_TIME_INVALID")
    return moment.timestamp()


class LegacyImporter:
    def __init__(self, source: Path, target: Path, *, character: str, expected_sha256: str, expected_preset: str):
        self.source_path, self.target_path = source.resolve(), target.resolve()
        self.source = self.target = None
        self.sha = expected_sha256
        if self.source_path == self.target_path or not source.is_file():
            raise Refusal("ISOLATED_SNAPSHOT_REQUIRED")
        if any(
            Path(str(source) + suffix).exists() and Path(str(source) + suffix).stat().st_size
            for suffix in ("-wal", "-journal")
        ):
            raise Refusal("STANDALONE_CONSISTENT_SNAPSHOT_REQUIRED")
        if file_hash(source) != expected_sha256:
            raise Refusal("SNAPSHOT_HASH_MISMATCH")
        try:
            self.source = sqlite3.connect(source.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            self.source.row_factory = sqlite3.Row
            self.source.execute("PRAGMA cache_size=-8192")
            self.source.execute("PRAGMA mmap_size=0")
            self.source.execute("PRAGMA temp_store=FILE")
            tables = {r[0] for r in self.source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not REQUIRED <= tables:
                raise Refusal("LEGACY_FORMAT_UNSUPPORTED")
            # The source's alias primary key is (alias,id), so id lookups otherwise
            # scan the table once per record. Build a disk-backed TEMP index only;
            # the frozen main database stays immutable and hash-identical.
            self.source.execute("CREATE TEMP TABLE migration_aliases AS SELECT alias,id FROM pm_source_alias")
            self.source.execute("CREATE INDEX migration_aliases_by_id ON migration_aliases(id,alias)")
            identities = self.source.execute("SELECT owner,preset FROM pm_identity").fetchall()
            if len(identities) != 1 or tuple(identities[0]) != (character, expected_preset):
                raise Refusal("LEGACY_IDENTITY_MISMATCH")
            if self.source.execute(
                "SELECT 1 FROM pm_records WHERE kind NOT IN ('experience','belief','claim_v2') LIMIT 1"
            ).fetchone():
                raise Refusal("LEGACY_KIND_UNSUPPORTED")
            if not target.exists():
                initialize(
                    target,
                    character,
                    migration_marker={"sha256": expected_sha256, "stage": "RECORDS", "cursor": "", "version": "1"},
                )
            self.target = MemoryStore(target, character, maintenance=True)
            with self.target.transaction() as db:
                if not db.execute("SELECT 1 FROM metadata WHERE key='index_format'").fetchone():
                    # Explicit maintenance of development's old derived index;
                    # never invoked by normal service startup.
                    db.execute("DELETE FROM search")
                    db.execute(
                        "UPDATE metadata SET value='' WHERE key='migration.cursor' AND EXISTS(SELECT 1 FROM metadata WHERE key='migration.stage' AND value IN ('INDEX','COMPLETE'))"
                    )
                    db.execute("UPDATE metadata SET value='INDEX' WHERE key='migration.stage' AND value='COMPLETE'")
                    db.execute("INSERT INTO metadata VALUES('index_format','record-rowid-v1')")
                prior = db.execute("SELECT value FROM metadata WHERE key='migration.sha256'").fetchone()
                if prior and prior[0] != expected_sha256:
                    raise Refusal("MIGRATION_SOURCE_CHANGED")
                if not prior:
                    if db.execute("SELECT 1 FROM records LIMIT 1").fetchone():
                        raise Refusal("EMPTY_STAGING_TARGET_REQUIRED")
                    db.executemany(
                        "INSERT INTO metadata VALUES(?,?)",
                        [
                            ("migration.sha256", expected_sha256),
                            ("migration.stage", "RECORDS"),
                            ("migration.cursor", ""),
                            ("migration.version", "1"),
                        ],
                    )
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.source is not None:
            self.source.close()
            self.source = None
        if self.target is not None:
            self.target.close()
            self.target = None

    def _rows(self, sql, args=(), limit=128):
        rows = self.source.execute(sql, args).fetchmany(limit + 1)
        if len(rows) > limit:
            raise Refusal("LEGACY_RECORD_EXCEEDS_BATCH_BOUNDS")
        return rows

    def _references(self, row, raw, revision):
        identifier = row["id"]
        if row["kind"] == "experience":
            rows = self._rows("SELECT parent FROM pm_lineage WHERE child=? ORDER BY parent", (identifier,))
            return [
                EvidenceReference(record_id=r[0], original_revision=self._source_revision(r[0]), role="derived_from")
                for r in rows
            ]
        if row["kind"] == "belief":
            return [
                EvidenceReference(record_id=ref, original_revision=self._belief_source_revision(row, ref), role=role)
                for key, role in (("supporting_ids", "supports"), ("counter_ids", "challenges"))
                for ref in raw[key]
            ]
        rows = self._rows(
            "SELECT * FROM pm_v2_relations WHERE id=? AND revision=? ORDER BY unit_key", (identifier, revision)
        )
        result = []
        for relation in rows:
            detail = json.loads(relation["payload"])
            if (detail["source_id"], detail["source_revision"], detail["role"]) != (
                relation["source"],
                relation["source_revision"],
                relation["role"],
            ):
                raise Refusal("LEGACY_RELATION_MISMATCH")
            result.append(
                EvidenceReference(
                    record_id=relation["source"],
                    original_revision=relation["source_revision"],
                    role=relation["role"],
                    start=detail.get("start"),
                    end=detail.get("end"),
                )
            )
        return result

    def _belief_source_revision(self, row, ref):
        binding = self.source.execute(
            "SELECT revision FROM pm_evidence_revision WHERE id=? AND source=?", (row["id"], ref)
        ).fetchone()
        if binding is None:
            raise Refusal("LEGACY_BELIEF_REVISION_BINDING_MISSING")
        return binding[0]

    def _source_revision(self, identifier):
        row = self.source.execute("SELECT revision FROM pm_records WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise Refusal("LEGACY_SOURCE_MISSING")
        return row[0]

    def _versions(self, row):
        if row["kind"] == "experience":
            values = self._rows("SELECT payload,digest FROM pm_sources WHERE id=?", (row["id"],), limit=1)
            if len(values) != 1 or digest(json.loads(values[0]["payload"])) != values[0]["digest"]:
                raise Refusal("LEGACY_SOURCE_HASH_MISMATCH")
            return [(row["revision"], values[0]["payload"])]
        table = "pm_revisions" if row["kind"] == "belief" else "pm_v2_versions"
        return [
            (r[0], r[1])
            for r in self._rows(
                f"SELECT revision,payload FROM {table} WHERE id=? ORDER BY revision", (row["id"],), limit=512
            )
        ]

    def _event(self, row, revision, serialized):
        if len(serialized.encode()) > 1024 * 1024:
            raise Refusal("LEGACY_PAYLOAD_EXCEEDS_BOUNDS")
        raw = json.loads(serialized)
        if raw["record_id"] != row["id"] or ("revision" in raw and raw["revision"] != revision):
            raise Refusal("LEGACY_VERSION_IDENTITY_MISMATCH")
        scope = raw.get("scope", {})
        is_source = row["kind"] == "experience"
        evidence = self._references(row, raw, revision)
        source_ids = sorted({ref.record_id for ref in evidence})
        status = {
            "observation": "observation",
            "self_expression": "self_expression",
            "legacy_episode": "derived_summary",
        }.get(raw["kind"], "historical_interpretation")
        provenance = LegacyProvenance(
            snapshot_sha256=self.sha,
            payload_sha256=digest(raw),
            original_id=row["id"],
            original_revision=revision,
            semantic_status=status,
            valid_from=raw.get("valid_from"),
            valid_until=raw.get("valid_until"),
            topic=raw.get("topic"),
            context=raw.get("context"),
            source_channel=scope.get("channel") or raw.get("source_channel"),
            visibility=[
                r[0]
                for r in self._rows("SELECT audience FROM pm_visibility WHERE id=? ORDER BY audience", (row["id"],))
            ],
            external_refs=raw.get("source_refs", []),
            evidence=evidence,
            supersedes=[{"record_id": item} if isinstance(item, str) else item for item in raw.get("supersedes", [])],
            original_pinned=bool(row["pinned"]),
            original_important=bool(row["important"]),
        )
        participant = raw.get("author_id") or raw.get("subject_id")
        return SourceEvent(
            character_id=self.target.character_id,
            source_instance="legacy:" + self.sha[:32],
            event_id=row["id"] + ":" + str(revision),
            session_id=scope.get("conversation_id") or raw.get("source_conversation_id") or "legacy:unspecified",
            conversation_id=scope.get("conversation_id") or raw.get("source_conversation_id"),
            occurred_at=timestamp(raw["occurred_at"] if is_source else raw["valid_from"]),
            content=raw["text"] if is_source else raw["statement"],
            kind=("summary" if raw["kind"] == "legacy_episode" else "observation") if is_source else "relationship",
            privacy="public" if scope.get("visibility") == "public" else "private",
            participants=[participant] if participant else [],
            aliases=[
                r[0]
                for r in self._rows(
                    "SELECT alias FROM migration_aliases WHERE id=? ORDER BY alias", (row["id"],), limit=64
                )
            ]
            if is_source
            else [],
            source_ids=source_ids,
            pinned=bool(row["pinned"] or row["important"]),
            provenance=provenance,
        )

    def _initial_status(self, row):
        if self.source.execute("SELECT 1 FROM pm_sources WHERE id=? AND revoked=1", (row["id"],)).fetchone():
            return "RETRACTED"
        if not row["active"]:
            return "INACTIVE"
        if self.source.execute("SELECT 1 FROM pm_legacy_valid WHERE id=? AND complete=0", (row["id"],)).fetchone():
            return "SOURCE_CHANGED"
        if self.source.execute("SELECT 1 FROM pm_legacy_presence WHERE id=? AND present=0", (row["id"],)).fetchone():
            return "SOURCE_CHANGED"
        return "ONLINE"

    def step(self, batch_size=128):
        if not 1 <= batch_size <= 256:
            raise Refusal("MIGRATION_BATCH_BOUNDS")
        with self.target.transaction() as db:
            meta = dict(db.execute("SELECT key,value FROM metadata WHERE key LIKE 'migration.%'"))
            stage, cursor = meta["migration.stage"], meta["migration.cursor"]
            if stage == "COMPLETE":
                return self.report()
            if stage == "RECORDS":
                rows = self.source.execute(
                    "SELECT * FROM pm_records WHERE id>? ORDER BY id LIMIT ?", (cursor, batch_size)
                ).fetchall()
                for row in rows:
                    versions = self._versions(row)
                    if not versions or versions[-1][0] != row["revision"]:
                        raise Refusal("LEGACY_HEAD_MISMATCH")
                    event = None
                    for index, (revision, serialized) in enumerate(versions, 1):
                        event = self._event(row, revision, serialized)
                        payload = event.model_dump(mode="json")
                        db.execute(
                            "INSERT INTO revisions VALUES(?,?,?,?)",
                            (row["id"], index, canonical(payload), digest(payload)),
                        )
                        self.target._commit_event(db, event, row["id"], index, "LEGACY_IMPORT")
                    status = self._initial_status(row)
                    db.execute(
                        "INSERT INTO records VALUES(?,?,?,?,?,?,NULL)",
                        (row["id"], len(versions), status, event.occurred_at, event.kind, int(event.pinned)),
                    )
                    # Index only after all source relationships have been checked.
                if rows:
                    db.execute("UPDATE metadata SET value=? WHERE key='migration.cursor'", (rows[-1]["id"],))
                else:
                    db.execute("UPDATE metadata SET value='LINKS' WHERE key='migration.stage'")
                    db.execute("UPDATE metadata SET value='' WHERE key='migration.cursor'")
            elif stage == "LINKS":
                rows = db.execute(
                    "SELECT * FROM records WHERE id>? ORDER BY id LIMIT ?", (cursor, batch_size)
                ).fetchall()
                for row in rows:
                    current = None
                    for revision, serialized in db.execute(
                        "SELECT revision,payload FROM revisions WHERE record_id=? ORDER BY revision", (row["id"],)
                    ).fetchall():
                        payload = json.loads(serialized)
                        for source in payload["source_ids"]:
                            if not db.execute("SELECT 1 FROM records WHERE id=?", (source,)).fetchone():
                                raise Refusal("LEGACY_SOURCE_MISSING")
                            db.execute("INSERT OR IGNORE INTO sources VALUES(?,?)", (row["id"], source))
                        if revision == row["revision"]:
                            current = payload
                    for evidence in current["provenance"]["evidence"]:
                        source = db.execute(
                            "SELECT r.status,v.payload FROM records r JOIN revisions v ON v.record_id=r.id AND v.revision=r.revision WHERE r.id=?",
                            (evidence["record_id"],),
                        ).fetchone()
                        source_payload = json.loads(source[1])
                        if (
                            source[0] != "ONLINE"
                            or source_payload["provenance"]["original_revision"] != evidence["original_revision"]
                        ):
                            db.execute(
                                "UPDATE records SET status='SOURCE_CHANGED' WHERE id=? AND status='ONLINE'",
                                (row["id"],),
                            )
                if rows:
                    db.execute("UPDATE metadata SET value=? WHERE key='migration.cursor'", (rows[-1]["id"],))
                else:
                    db.execute("UPDATE metadata SET value='VERIFY' WHERE key='migration.stage'")
                    db.execute("UPDATE metadata SET value='' WHERE key='migration.cursor'")
            elif stage == "VERIFY":
                # Propagate withdrawal transitively before publishing any projection.
                db.execute("""WITH RECURSIVE bad(id) AS (
                    SELECT id FROM records WHERE status<>'ONLINE' UNION
                    SELECT s.record_id FROM sources s JOIN bad b ON s.source_id=b.id)
                    UPDATE records SET status='SOURCE_CHANGED' WHERE status='ONLINE' AND id IN (SELECT id FROM bad)""")
                count = self.source.execute("SELECT count(*) FROM pm_records").fetchone()[0]
                if count != db.execute("SELECT count(*) FROM records").fetchone()[0]:
                    raise Refusal("MIGRATION_COUNT_MISMATCH")
                expected_versions = sum(
                    self.source.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("pm_sources", "pm_revisions", "pm_v2_versions")
                )
                if expected_versions != db.execute("SELECT count(*) FROM revisions").fetchone()[0]:
                    raise Refusal("MIGRATION_VERSION_COUNT_MISMATCH")
                if (
                    db.execute("PRAGMA foreign_key_check").fetchone()
                    or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                ):
                    raise Refusal("MIGRATION_INTEGRITY_FAILED")
                if file_hash(self.source_path) != self.sha:
                    raise Refusal("SNAPSHOT_CHANGED_DURING_MIGRATION")
                db.execute("UPDATE metadata SET value='INDEX' WHERE key='migration.stage'")
            elif stage == "INDEX":
                rows = db.execute(
                    "SELECT r.id,v.payload FROM records r JOIN revisions v ON v.record_id=r.id AND v.revision=r.revision WHERE r.id>? AND r.status='ONLINE' ORDER BY r.id LIMIT ?",
                    (cursor, batch_size),
                ).fetchall()
                for row in rows:
                    self.target._index(db, row["id"], json.loads(row["payload"]))
                if rows:
                    db.execute("UPDATE metadata SET value=? WHERE key='migration.cursor'", (rows[-1]["id"],))
                else:
                    db.execute("UPDATE metadata SET value='COMPLETE' WHERE key='migration.stage'")
            else:
                raise Refusal("MIGRATION_STAGE_UNSUPPORTED")
        return self.report()

    def report(self):
        db = self.target.db
        return {
            "format": "production-memory-import-v1",
            "source_sha256": self.sha,
            "stage": db.execute("SELECT value FROM metadata WHERE key='migration.stage'").fetchone()[0],
            "records": db.execute("SELECT count(*) FROM records").fetchone()[0],
            "revisions": db.execute("SELECT count(*) FROM revisions").fetchone()[0],
            "statuses": dict(db.execute("SELECT status,count(*) FROM records GROUP BY status")),
            "source_snapshot_retention_required": True,
            "old_actions_replayed": 0,
            "historical_interpretations_are_current_facts": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--character", required=True)
    parser.add_argument("--legacy-preset", required=True)
    parser.add_argument("--max-batches", type=int, default=100000)
    parser.add_argument("--batch-size", type=int, default=128)
    args = parser.parse_args()
    importer = LegacyImporter(
        args.snapshot,
        args.target,
        character=args.character,
        expected_sha256=args.sha256,
        expected_preset=args.legacy_preset,
    )
    started = time.monotonic()
    try:
        for number in range(args.max_batches):
            report = importer.step(args.batch_size)
            if number % 50 == 0 or report["stage"] == "COMPLETE":
                print(json.dumps({**report, "elapsed_seconds": round(time.monotonic() - started, 3)}), flush=True)
            if report["stage"] == "COMPLETE":
                break
    finally:
        importer.close()


if __name__ == "__main__":
    main()
