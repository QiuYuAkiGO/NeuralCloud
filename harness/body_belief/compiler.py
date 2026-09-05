"""Neutral objective memory and explicitly uncertain belief context."""

from harness.body_belief.models import BodyExperienceEpisode
from harness.body_belief.retrieval import evidence_bundle
from harness.core.inference import ContextSegment
from harness.research.grounded_agency.hashes import canonical_json
from harness.research.grounded_agency.neutrality import NeutralityScanner


def visible_experience(item: BodyExperienceEpisode):
    return {
        "experience_id": item.experience_id, "context_signature": item.context_signature,
        "action_signature": item.action_signature, "decision": item.decision.model_dump(mode="json"),
        "channel": item.observation_before.channel_id, "before": item.observation_before.reading,
        "after": item.observation_after.reading if item.observation_after else None,
        "outcome_verified": item.outcome_verified, "outcome_success": item.outcome_success,
        "outcome_refs": list(item.action_outcome_refs),
    }


def visible_belief(item, experiences):
    evidence = evidence_bundle(item, experiences)
    return {
        "belief_id": item.belief_id, "revision": item.revision, "status": item.status,
        "context_signature": item.context_signature, "action_signature": item.action_signature,
        "channel": item.predicted_channel, "transition": item.predicted_transition,
        "confidence": item.confidence,
        "support_count": len(evidence["support"]), "counterexample_count": len(evidence["counterexamples"]),
        "support_refs": [entry.experience_id for entry in evidence["support"]],
        "counterexample_refs": [entry.experience_id for entry in evidence["counterexamples"]],
        "control_refs": [entry.experience_id for entry in evidence["controls"]],
        "qualification": "Provisional observation-based hypothesis; subsequent observations may contradict it.",
    }


def compile_body_memory(*, treatment, experiences=(), beliefs=(), max_tokens=4096, objective_history_visible=True):
    if treatment == "none":
        return None
    if treatment not in {"objective_events", "body_causal_beliefs"}:
        raise ValueError("unknown body memory treatment")
    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("body memory budget must be positive")
    if type(objective_history_visible) is not bool:
        raise ValueError("objective_history_visible must be a boolean")
    selected, refs = [], []
    # Reserve identical objective space in both treatments, so removing beliefs
    # cannot also replace the objective examples handed to the decision model.
    groups = [([visible_experience(item) for item in experiences], max_tokens // 2)]
    if treatment == "body_causal_beliefs":
        groups.append(([visible_belief(item, tuple(experiences)) for item in beliefs], max_tokens))
    for rows, budget in groups:
        for row in rows:
            candidate = canonical_json({"body_memory": [*selected, row]})
            if len(candidate.encode("utf-8")) > budget:
                continue
            selected.append(row)
            refs.append(row.get("belief_id") or row["experience_id"])
    if not objective_history_visible:
        selected = [row for row in selected if "belief_id" in row]
        refs = [row["belief_id"] for row in selected]
    if not selected:
        return None
    text = canonical_json({"body_memory": selected})
    NeutralityScanner().assert_neutral([{"role": "system", "content": text}])
    return ContextSegment(kind="body_memory", text=text, source_ids=refs, why="verified_body_experiences", token_estimate=len(text.encode("utf-8")))
