"""Strict main/aux model appraisal of objective observations and counterexamples."""

from __future__ import annotations

from harness.llm.json_output import model_json_value

import json
from datetime import UTC, datetime

from pydantic import Field

from harness.body_belief.compiler import visible_belief, visible_experience
from harness.body_belief.models import BodyCausalBeliefCandidate, FrozenModel, _Proposition, validate_evidence
from harness.cognitive.decision_parser import _reject_constant, _unique_object
from harness.research.grounded_agency.hashes import canonical_json, content_hash
from harness.research.grounded_agency.neutrality import NeutralityScanner


class _Proposals(FrozenModel):
    candidates: tuple[_Proposition, ...] = Field(max_length=8)


class AppraisalResult(FrozenModel):
    candidates: tuple[BodyCausalBeliefCandidate, ...] = ()
    success: bool
    errors: tuple[str, ...] = ()
    messages: tuple[dict[str, str], ...]
    input_hash: str
    raw_output_hash: str | None = None


class BodyBeliefAppraiser:
    def __init__(self, llm):
        self.llm = llm

    def appraise(self, experiences, *, existing_beliefs=(), persona=()) -> AppraisalResult:
        episodes = tuple(item for item in experiences if item.eligible)
        payload = {
            "mode": "body_causal_appraisal",
            "instruction": (
                "Return one JSON object with candidates. Each hypothesis must cite observed supporting and "
                "contradicting experiences. Include contrary observations and controls. Empty candidates is valid. "
                "Use the supplied context_signature, action_signature, channel, and Mx->My transition exactly. "
                "Describe a provisional association without instructions for choosing an action."
            ),
            "schema": _Proposals.model_json_schema(),
            "experiences": [visible_experience(item) for item in episodes],
            "existing_beliefs": [visible_belief(item, episodes) for item in existing_beliefs],
            "general_traits": list(persona),
        }
        messages = [{"role": "system", "content": canonical_json(payload)}]
        report = NeutralityScanner().assert_neutral(messages)
        raw_hash = None
        try:
            method = getattr(self.llm, "complete_chat_result", None)
            sent = json.loads(canonical_json(messages))
            from harness.llm.structured import helper_options
            raw = (method(sent, stream=False, **helper_options(self.llm, _Proposals, "body_appraisal")).text
                   if callable(method) else self.llm.complete_chat(sent, stream=False))
            raw_hash = content_hash(raw)
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32_768:
                raise ValueError("bounded appraisal JSON required")
            decoded = model_json_value(raw, schema=_Proposals.model_json_schema(), legacy=lambda source: json.loads(source, object_pairs_hook=_unique_object, parse_constant=_reject_constant))
            proposals = _Proposals.model_validate(decoded)
            now = max((item.completed_at for item in episodes), default=datetime.now(UTC))
            candidates = []
            for proposal in proposals.candidates:
                validate_evidence(proposal, {item.experience_id: item for item in episodes})
                candidates.append(BodyCausalBeliefCandidate(
                    **proposal.model_dump(), candidate_id="candidate-" + content_hash(proposal)[:32],
                    created_at=now, updated_at=now,
                ))
            return AppraisalResult(candidates=tuple(candidates), success=True, messages=tuple(messages),
                                   input_hash=report.message_hash, raw_output_hash=raw_hash)
        except Exception:
            return AppraisalResult(success=False, errors=("appraisal_validation_or_model_failure",),
                                   messages=tuple(messages), input_hash=report.message_hash, raw_output_hash=raw_hash)
