"""Bounded evidence selection shared by scheduling and offline diagnostics."""
from __future__ import annotations

from harness.runtime.capabilities import digest


REQUEST_CONTEXT_VERSION = "goal-context-v2"


def select_evidence(*, snapshot, goal, episode, offers, fresh_refs, used_refs):
    """Reserve current bindings, step proofs and appraisal refs before history.

    At least one independent proof per executed step stays available for review.
    The complete proofs remain in the ledger; truncation grants no new authority.
    """
    episode = episode or {}
    results = episode.get("results", [])
    proven = [r for r in results if r.get("status") in {"completed", "committed"}]
    bindings = ["homeostasis-snapshot:" + digest(snapshot), "goal-request:" + digest(goal)]
    step_proofs = list(dict.fromkeys(ref for step in episode.get("steps", [])
        if step.get("status") == "executed" for ref in step.get("evidence_refs", [])[:1]))
    required = list(dict.fromkeys([*bindings, *step_proofs]))
    if len(required) > 128:
        raise ValueError("goal_context_too_large")
    candidates = list(dict.fromkeys([*[ref for r in proven for ref in r.get("evidence_refs", [])], *fresh_refs]))
    appraisals = [r for r in candidates if r not in used_refs][-min(64, 128 - len(required)):] if len(required) < 128 else []
    known = list(dict.fromkeys([*required, *appraisals]))
    optional = [ref for r in reversed(proven) for ref in r.get("evidence_refs", [])]
    optional.extend(ref for offer in offers for ref in offer.observation_refs)
    for ref in optional:
        if len(known) >= 128:
            break
        if ref not in known:
            known.append(ref)
    return known, appraisals


def web_information_feedback(result, previous_sources):
    """Describe observed information differences, never a psychological reward."""
    def signature(source):
        value = source.model_dump(mode="json") if hasattr(source, "model_dump") else source
        return (value.get("url"), digest([value.get("content") or value.get("snippet") or "", value.get("origin")]))

    previous = {signature(s) for s in previous_sources}
    sources = [s.model_dump(mode="json") if hasattr(s, "model_dump") else s for s in result.sources]
    new = [s for s in sources if signature(s) not in previous]
    return {"kind": "external_information", "novelty_assessment": "unassessed",
        "returned_sources": len(sources), "new_or_changed_sources": len(new),
        "repeated_sources": len(sources) - len(new), "empty_result": not sources,
        "sources": [{k: s.get(k) for k in ("source_ref", "title", "url", "origin", "retrieved_at")}
                    | {"excerpt": (s.get("content") or s.get("snippet") or "")[:800]} for s in sources[:4]]}
