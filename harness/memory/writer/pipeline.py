"""Candidate → evidence accumulation → confidence update → semantic consolidation."""

from __future__ import annotations

from datetime import UTC, datetime

from harness.config import Settings, get_settings
from harness.core.exceptions import EvidenceRequiredError, InvalidCuratorOpError
from harness.core.ids import new_id
from harness.core.models import (
    CandidateMemory,
    CuratorOp,
    CuratorOutput,
    EpisodicEvent,
    SemanticFact,
)
from harness.llm.embeddings import EmbeddingClient
from harness.memory.store.protocols import MemoryStores
from harness.memory.writer.confidence import should_consolidate, update_confidence


class WriterPipeline:
    def __init__(
        self,
        stores: MemoryStores,
        embeddings: EmbeddingClient,
        settings: Settings | None = None,
    ) -> None:
        self.stores = stores
        self.embeddings = embeddings
        self.settings = settings or get_settings()

    def apply(
        self,
        output: CuratorOutput,
        *,
        event: EpisodicEvent,
        now: datetime | None = None,
        strict: bool = True,
    ) -> list[CuratorOp]:
        applied: list[CuratorOp] = []
        moment = now or datetime.now(UTC)
        for op in output.ops:
            try:
                self._apply_one(op, event=event, now=moment)
                applied.append(op)
            except (InvalidCuratorOpError, EvidenceRequiredError) as exc:
                if strict:
                    raise
                self.stores.audit.log(
                    op=op.op,
                    target_type=op.type or "none",
                    target_id=op.ref,
                    actor="curator",
                    success=False,
                    detail={"reason": str(exc)},
                )
                applied.append(CuratorOp(op="NO_OP", reason=str(exc)))
        return applied

    def _apply_one(self, op: CuratorOp, *, event: EpisodicEvent, now: datetime) -> None:
        if op.op == "NO_OP":
            self.stores.audit.log(
                op="NO_OP",
                target_type=op.type or "none",
                target_id=op.ref,
                actor="curator",
                success=True,
                detail={"reason": op.reason},
            )
            return
        if op.op == "ADD" and op.type == "candidate":
            self._add_candidate(op, event=event, now=now)
            return
        if op.op == "UPDATE" and op.type == "candidate":
            self._update_candidate(op, event=event, now=now)
            return
        if op.op == "UPDATE" and op.type == "semantic":
            self._update_semantic(op, event=event, now=now)
            return
        if op.op == "ADD" and op.type == "semantic":
            raise EvidenceRequiredError(
                "semantic facts cannot be added directly from model output; use candidate consolidation"
            )
        if op.op == "DELETE" and op.type == "candidate":
            self._drop_candidate(op, now=now)
            return
        if op.op == "ADD" and op.type == "episodic":
            self.stores.audit.log(
                op="ADD",
                target_type="episodic",
                target_id=op.ref or event.event_id,
                actor="curator",
                success=True,
                detail={"importance": op.importance},
            )
            return
        raise InvalidCuratorOpError(f"unsupported op: {op.op} {op.type}")

    def _add_candidate(self, op: CuratorOp, *, event: EpisodicEvent, now: datetime) -> None:
        evidence = list(op.evidence or [event.event_id])
        if not evidence:
            raise EvidenceRequiredError("candidate ADD requires evidence")
        existing = self.stores.candidates.find_similar(
            subject=op.subject,
            predicate=op.predicate,
            value=op.value,
            claim=op.claim or "",
        )
        if existing:
            merged = op.model_copy(update={"op": "UPDATE", "ref": existing.candidate_id, "type": "candidate"})
            self._update_candidate(merged, event=event, now=now)
            return
        candidate = CandidateMemory(
            claim=op.claim or event.summary,
            source=op.source or f"inference_from_{event.event_id}",
            confidence=op.confidence if op.confidence is not None else self.settings.initial_candidate_confidence,
            status="candidate",
            evidence=evidence,
            subject=op.subject,
            predicate=op.predicate,
            value=op.value,
            created_at=now,
            updated_at=now,
        )
        self.stores.candidates.insert(candidate)
        self.stores.audit.log(
            op="ADD",
            target_type="candidate",
            target_id=candidate.candidate_id,
            actor="curator",
            success=True,
            detail={"claim": candidate.claim, "confidence": candidate.confidence},
        )
        self._maybe_consolidate(candidate, now=now)

    def _update_candidate(self, op: CuratorOp, *, event: EpisodicEvent, now: datetime) -> None:
        if not op.ref:
            raise InvalidCuratorOpError("UPDATE candidate requires ref")
        candidate = self.stores.candidates.get(op.ref)
        if candidate is None:
            raise InvalidCuratorOpError(f"unknown candidate {op.ref}")
        new_evidence = [eid for eid in (op.evidence or [event.event_id]) if eid not in candidate.evidence]
        evidence = candidate.evidence + new_evidence
        confidence = candidate.confidence
        if op.confidence_delta is not None:
            confidence = min(1.0, max(0.0, confidence + op.confidence_delta))
        elif new_evidence:
            confidence = update_confidence(confidence, n_new_evidence=len(new_evidence), settings=self.settings)
        updated = candidate.model_copy(
            update={"evidence": evidence, "confidence": confidence, "updated_at": now}
        )
        self.stores.candidates.update(updated)
        self.stores.audit.log(
            op="UPDATE",
            target_type="candidate",
            target_id=updated.candidate_id,
            actor="curator",
            success=True,
            detail={"confidence": updated.confidence, "new_evidence": new_evidence},
        )
        self._maybe_consolidate(updated, now=now)

    def _update_semantic(self, op: CuratorOp, *, event: EpisodicEvent, now: datetime) -> None:
        if not op.ref:
            raise InvalidCuratorOpError("UPDATE semantic requires ref")
        fact = self.stores.semantic.get(op.ref)
        if fact is None:
            raise InvalidCuratorOpError(f"unknown semantic fact {op.ref}")
        supporting = list(fact.supporting_events)
        if event.event_id not in supporting:
            supporting.append(event.event_id)
        confidence = fact.confidence
        if op.confidence_delta is not None:
            confidence = min(1.0, max(0.0, confidence + op.confidence_delta))
        updated = fact.model_copy(update={"supporting_events": supporting, "confidence": confidence})
        self.stores.semantic.update(updated)
        self.stores.audit.log(
            op="UPDATE",
            target_type="semantic",
            target_id=fact.fact_id,
            actor="curator",
            success=True,
            detail={"confidence_delta": op.confidence_delta},
        )

    def _drop_candidate(self, op: CuratorOp, *, now: datetime) -> None:
        if not op.ref:
            raise InvalidCuratorOpError("DELETE candidate requires ref")
        candidate = self.stores.candidates.get(op.ref)
        if candidate is None:
            return
        updated = candidate.model_copy(update={"status": "dropped", "updated_at": now})
        self.stores.candidates.update(updated)
        self.stores.audit.log(
            op="DELETE",
            target_type="candidate",
            target_id=op.ref,
            actor="curator",
            success=True,
            detail={"reason": op.reason},
        )

    def _maybe_consolidate(self, candidate: CandidateMemory, *, now: datetime) -> None:
        if candidate.status != "candidate":
            return
        if not should_consolidate(candidate.confidence, self.settings):
            return
        if not candidate.evidence:
            raise EvidenceRequiredError("cannot consolidate without evidence")
        subject = candidate.subject or "user"
        predicate = candidate.predicate or "fact"
        value = candidate.value or candidate.claim
        existing = self.stores.semantic.find(subject, predicate, value)
        text = f"{subject} {predicate} {value}"
        embedding = self.embeddings.embed_one(text)
        if existing:
            supporting = list(dict.fromkeys(existing.supporting_events + candidate.evidence))
            fact = existing.model_copy(
                update={
                    "confidence": max(existing.confidence, candidate.confidence),
                    "supporting_events": supporting,
                    "embedding": embedding,
                }
            )
            self.stores.semantic.update(fact)
        else:
            fact = SemanticFact(
                fact_id=new_id("fact"),
                subject=subject,
                predicate=predicate,
                value=value,
                confidence=candidate.confidence,
                supporting_events=list(candidate.evidence),
                formed_at=now,
                embedding=embedding,
            )
            self.stores.semantic.insert(fact)
        closed = candidate.model_copy(
            update={"status": "consolidated", "updated_at": now, "consolidated_fact_id": fact.fact_id}
        )
        self.stores.candidates.update(closed)
        self.stores.audit.log(
            op="CONSOLIDATE",
            target_type="semantic",
            target_id=fact.fact_id,
            actor="pipeline",
            success=True,
            detail={"candidate_id": candidate.candidate_id, "confidence": fact.confidence},
        )
