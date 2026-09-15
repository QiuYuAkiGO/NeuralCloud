"""Memory Curator: structured ADD/UPDATE/DELETE/NO_OP (01 §6.1)."""

from __future__ import annotations

from typing import Protocol

import httpx
from pydantic import ValidationError

from harness.core.exceptions import InvalidCuratorOpError
from harness.core.models import (
    CandidateMemory,
    CuratorOp,
    CuratorOutput,
    EpisodicEvent,
    PersonaCore,
)
from harness.llm.client import LLMClient, MockLLM


class CuratorBackend(Protocol):
    def propose(
        self,
        event: EpisodicEvent,
        *,
        persona: PersonaCore | None,
        open_candidates: list[CandidateMemory],
    ) -> CuratorOutput: ...


_CURATOR_SYSTEM = """You are Memory Curator for a character AI.
Output JSON only: {"ops":[{"op":"ADD|UPDATE|DELETE|NO_OP","type":"episodic|semantic|candidate|relationship",...}]}
Rules:
- Never write semantic facts without evidence event ids.
- Never modify Persona Core.
- Prefer candidate memories; consolidation happens downstream.
- UPDATE/DELETE must include ref of an existing row. If you do not know the id, output NO_OP.
- Subagent output is structured judgment, not dialogue.
"""


class LLMCurator:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def propose(
        self,
        event: EpisodicEvent,
        *,
        persona: PersonaCore | None,
        open_candidates: list[CandidateMemory],
    ) -> CuratorOutput:
        user = {
            "event": event.model_dump(mode="json", exclude={"embedding"}),
            "persona_name": persona.identity.name if persona else None,
            "open_candidates": [
                c.model_dump(mode="json") for c in open_candidates
            ],
        }
        try:
            raw = self.llm.complete_json(system=_CURATOR_SYSTEM, user=str(user))
            return sanitize_curator_output(
                _coerce_curator_output(raw),
                open_candidates=open_candidates,
            )
        except (ValueError, ValidationError, InvalidCuratorOpError, httpx.HTTPError):
            return CuratorOutput(ops=[CuratorOp(op="NO_OP", reason="curator_parse_failed")])


class RuleCurator:
    """Deterministic curator for tests and as LLM fallback."""

    def propose(
        self,
        event: EpisodicEvent,
        *,
        persona: PersonaCore | None,
        open_candidates: list[CandidateMemory],
    ) -> CuratorOutput:
        text = " ".join(
            part
            for part in [event.summary, event.objective_fact, event.character_action or ""]
            if part
        )
        if _mentions_cat(text):
            existing = _find_cat_candidate(open_candidates)
            if existing:
                return CuratorOutput(
                    ops=[
                        CuratorOp(
                            op="UPDATE",
                            type="candidate",
                            ref=existing.candidate_id,
                            confidence_delta=0.35,
                            evidence=[event.event_id],
                            reason="additional_cat_evidence",
                        )
                    ]
                )
            return CuratorOutput(
                ops=[
                    CuratorOp(
                        op="ADD",
                        type="candidate",
                        claim="用户喜欢猫",
                        source=f"inference_from_{event.event_id}",
                        confidence=0.15,
                        evidence=[event.event_id],
                        subject="user",
                        predicate="preference",
                        value="喜欢猫",
                    )
                ]
            )
        if _mentions_interruption(text):
            return CuratorOutput(
                ops=[
                    CuratorOp(
                        op="ADD",
                        type="candidate",
                        claim="用户不喜欢被突然打扰",
                        source=f"inference_from_{event.event_id}",
                        confidence=0.15,
                        evidence=[event.event_id],
                        subject="user",
                        predicate="preference",
                        value="不喜欢被突然打扰",
                    )
                ]
            )
        return CuratorOutput(ops=[CuratorOp(op="NO_OP", reason="no_structured_claim")])


def _mentions_cat(text: str) -> bool:
    return any(token in text.lower() for token in ("猫", "cat", "kitten"))


def _mentions_interruption(text: str) -> bool:
    return any(token in text for token in ("打扰", "突然", "别烦"))


def _find_cat_candidate(candidates: list[CandidateMemory]) -> CandidateMemory | None:
    for candidate in candidates:
        blob = f"{candidate.claim} {candidate.value or ''}"
        if _mentions_cat(blob) and candidate.status == "candidate":
            return candidate
    return None


def sanitize_curator_output(
    output: CuratorOutput,
    *,
    open_candidates: list[CandidateMemory] | None = None,
) -> CuratorOutput:
    """Drop or repair ops the writer cannot apply. Missing refs become NO_OP."""
    open_candidates = open_candidates or []
    repaired: list[CuratorOp] = []
    for op in output.ops:
        if op.op in {"UPDATE", "DELETE"} and op.type == "candidate" and not op.ref:
            match = _match_open_candidate(op, open_candidates)
            if match is not None:
                repaired.append(op.model_copy(update={"ref": match.candidate_id}))
            else:
                repaired.append(CuratorOp(op="NO_OP", reason="update_candidate_missing_ref"))
            continue
        if op.op in {"UPDATE", "DELETE"} and not op.ref:
            repaired.append(CuratorOp(op="NO_OP", reason=f"{op.op.lower()}_missing_ref"))
            continue
        repaired.append(op)
    return CuratorOutput(ops=repaired or [CuratorOp(op="NO_OP", reason="empty_ops")])


def _match_open_candidate(op: CuratorOp, candidates: list[CandidateMemory]) -> CandidateMemory | None:
    for candidate in candidates:
        if candidate.status != "candidate":
            continue
        if op.subject and op.predicate and op.value:
            if (
                candidate.subject == op.subject
                and candidate.predicate == op.predicate
                and candidate.value == op.value
            ):
                return candidate
        if op.value and (candidate.value == op.value or op.value in candidate.claim):
            return candidate
        if op.claim and op.claim == candidate.claim:
            return candidate
        if op.ref and op.ref == candidate.candidate_id:
            return candidate
    return None


def _coerce_curator_output(raw: dict) -> CuratorOutput:
    if "ops" not in raw and isinstance(raw.get("curator_output"), dict):
        raw = raw["curator_output"]
    try:
        output = CuratorOutput.model_validate(raw)
    except ValidationError as exc:
        raise InvalidCuratorOpError(str(exc)) from exc
    if not output.ops:
        raise InvalidCuratorOpError("empty ops")
    return output


def default_curator(llm: LLMClient | None = None) -> CuratorBackend:
    if llm is None or isinstance(llm, MockLLM):
        return RuleCurator()
    return LLMCurator(llm)
