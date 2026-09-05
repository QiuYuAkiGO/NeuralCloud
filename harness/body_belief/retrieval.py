"""Retrieve support, counterexamples, and controls together within mapping scope."""

from harness.body_belief.models import BodyCausalBelief, BodyExperienceEpisode


def retrieve_experiences(experiences, *, context_signature, action_signature=None, limit=64):
    matched = [item for item in experiences if item.context_signature == context_signature and item.eligible]
    matched.sort(key=lambda item: (item.completed_at, item.experience_id), reverse=True)
    # Reserve half the budget for same-action results and half for controls.
    same = [item for item in matched if action_signature is None or item.action_signature == action_signature]
    other = [item for item in matched if action_signature is not None and item.action_signature != action_signature]
    groups = {}
    for item in same:
        groups.setdefault((item.transition, item.outcome_success), []).append(item)
    diverse = []
    while groups:
        for key in tuple(groups):
            diverse.append(groups[key].pop(0))
            if not groups[key]:
                del groups[key]
    selected = diverse[:max(1, limit // 2)] + other[:limit // 2]
    seen = {item.experience_id for item in selected}
    selected += [item for item in matched if item.experience_id not in seen][:max(0, limit - len(selected))]
    return tuple(selected[:limit])


def retrieve_beliefs(beliefs, *, context_signature, action_signatures=(), limit=4):
    return tuple(sorted(
        (item for item in beliefs if item.context_signature == context_signature and item.status != "dropped"
         and (not action_signatures or item.action_signature in action_signatures)),
        key=lambda item: (-item.confidence, -item.revision, item.belief_id),
    )[:limit])


def evidence_bundle(belief: BodyCausalBelief, experiences: tuple[BodyExperienceEpisode, ...]):
    relevant = retrieve_experiences(experiences, context_signature=belief.context_signature,
                                   action_signature=belief.action_signature)
    return {
        "support": tuple(item for item in relevant if item.action_signature == belief.action_signature
                         and item.transition == belief.predicted_transition and item.outcome_success),
        "counterexamples": tuple(item for item in relevant if item.action_signature == belief.action_signature
                                 and (item.transition != belief.predicted_transition or not item.outcome_success)),
        "controls": tuple(item for item in relevant if item.action_signature != belief.action_signature),
    }
