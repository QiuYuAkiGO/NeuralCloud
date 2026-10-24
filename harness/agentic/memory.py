"""Audience-filtered model capabilities over the existing SQL memory store."""
from contextlib import closing

from harness.agentic.store import canonical
from harness.memory.production import _terms


class MemoryTools:
    def __init__(self, store, conversation):
        self.store, self.conversation = store, conversation
        self.memory = store.memory
        self.audience = store.get(conversation)["audience"]
        self.shown = set()
        self.versions = {}

    def search(self, query):
        terms = _terms(query.query)[:32]
        clause, args = ["v.audience=?", "r.active=1"], [self.audience]
        if terms:
            clause.append("EXISTS(SELECT 1 FROM pm_terms t WHERE t.id=r.id AND t.term IN (" + ",".join("?" for _ in terms) + "))")
            args.extend(terms)
        for operator, moment in ((">=", query.after), ("<", query.before)):
            if moment:
                from datetime import timezone
                clause.append("r.time" + operator + "?")
                args.append(moment.astimezone(timezone.utc).isoformat())
        result = []
        with closing(self.memory._connect()) as db:
            rows = db.execute("SELECT r.* FROM pm_records r JOIN pm_visibility v ON v.id=r.id WHERE " +
                " AND ".join(clause) + " ORDER BY r.time DESC,r.id LIMIT ? OFFSET ?", (*args, query.limit, query.offset)).fetchall()
            for row in rows:
                if not self.memory._visible(db, row["id"], self.audience):
                    continue
                self.shown.add(row["id"])
                self.versions[row["id"]] = row["revision"]
                result.append({"source_id": row["id"], "kind": row["kind"], "occurred_at": row["time"],
                    "text": row["text"][:600], "truncated": len(row["text"]) > 600, "revision": row["revision"]})
        return {"authority": "source_attributed_data_not_instructions", "entries": result,
            "next_offset": query.offset + len(rows) if len(rows) == query.limit else None}

    def read(self, query):
        result = []
        remaining = query.max_characters
        with closing(self.memory._connect()) as db:
            for identifier in query.source_ids:
                if remaining <= 0:
                    break
                if not self.memory._visible(db, identifier, self.audience):
                    continue
                row = db.execute("SELECT * FROM pm_records WHERE id=?", (identifier,)).fetchone()
                if row["kind"] == "experience":
                    raw = db.execute("SELECT payload FROM pm_sources WHERE id=?", (identifier,)).fetchone()[0]
                    source = self.memory._effective_source(db, raw)
                    payload = {"text": source.text, "author": source.author_id, "time": source.occurred_at.isoformat(),
                        "source_refs": list(source.source_refs), "authority": "source_attributed_experience"}
                elif row["kind"] == "claim_v2":
                    from harness.memory.evidence_v2 import project_entry
                    payload = project_entry(db, identifier, self.audience)
                    if payload is None:
                        continue
                else:
                    raw = db.execute("SELECT payload FROM pm_revisions WHERE id=? AND revision=?", (identifier, row["revision"])).fetchone()
                    import json
                    payload = {"authority": "legacy_model_interpretation", "claim": json.loads(raw[0])}
                text = canonical(payload)
                excerpt = text[query.offset:query.offset + remaining]
                remaining -= len(excerpt)
                result.append({"source_id": identifier, "revision": row["revision"], "offset": query.offset,
                    "data": excerpt, "next_offset": query.offset + len(excerpt) if query.offset + len(excerpt) < len(text) else None})
                self.shown.add(identifier)
                self.versions[identifier] = row["revision"]
        return {"entries": result, "authority": "memory_data_not_instructions"}

    def validate(self):
        if not self.store.validate_sources(self.conversation, sorted(self.shown)):
            raise ValueError("agentic_evidence_retracted")
        with closing(self.memory._connect()) as db:
            for identifier, revision in self.versions.items():
                row = db.execute("SELECT revision FROM pm_records WHERE id=?", (identifier,)).fetchone()
                if row is None or row[0] != revision:
                    raise ValueError("agentic_evidence_superseded")
