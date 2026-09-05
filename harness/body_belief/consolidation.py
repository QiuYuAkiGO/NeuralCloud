"""Versioned, deterministic evidence scoring with weakening and replacement."""

from harness.body_belief.models import BodyCausalBelief, ConsolidationProfile, SensorReliabilityBelief, validate_evidence
from harness.research.grounded_agency.hashes import content_hash


class BodyBeliefConsolidator:
    def __init__(self, store, *, profile: ConsolidationProfile | None = None):
        self.store = store
        self.profile = profile or store.profile
        if self.profile != store.profile:
            raise ValueError("service consolidation profile does not match the frozen store")

    def apply(self, candidate):
        all_episodes = {item.experience_id: item for item in self.store.experiences()}
        validate_evidence(candidate, all_episodes)
        relevant = [item for item in all_episodes.values() if item.eligible
                    and item.context_signature == candidate.context_signature
                    and item.action_signature == candidate.action_signature
                    and item.observation_before.channel_id == candidate.predicted_channel]
        support = tuple(sorted(item.experience_id for item in relevant
                               if item.transition == candidate.predicted_transition and item.outcome_success))
        contrary = tuple(sorted(item.experience_id for item in relevant if item.experience_id not in support))
        if not support:
            raise ValueError("candidate has no independently verified supporting evidence")
        candidate = candidate.model_copy(update={"supporting_experience_refs": support, "contradicting_experience_refs": contrary})
        windows = {int(all_episodes[ref].completed_at.timestamp()) // self.profile.window_seconds for ref in support}
        precision = len(support) / (len(support) + len(contrary))
        enough = (len(support) >= self.profile.minimum_support and len(windows) >= self.profile.minimum_windows
                  and precision >= self.profile.minimum_precision
                  and 1 - precision <= self.profile.maximum_contradiction_ratio)
        identifier = "belief-" + content_hash((candidate.context_signature, candidate.action_signature, candidate.predicted_channel))[:32]
        previous = next((item for item in self.store.beliefs() if item.belief_id == identifier), None)
        candidate = candidate.model_copy(update={"status": "consolidated" if enough else "candidate"})
        self.store.put_candidate(candidate)
        if previous is None and not enough:
            return None
        if previous is not None and previous.predicted_transition != candidate.predicted_transition and not enough:
            return self.refresh(previous)
        dropped = 1 - precision >= self.profile.drop_contradiction_ratio
        status = "dropped" if dropped else "weakened" if contrary else "consolidated"
        if previous is not None and previous.predicted_transition != candidate.predicted_transition and enough:
            status = "revised"
        now = max(item.completed_at for item in relevant)
        if previous is not None:
            unchanged = (previous.predicted_transition == candidate.predicted_transition
                         and previous.supporting_experience_refs == support and previous.contradicting_experience_refs == contrary)
            if unchanged:
                return previous
        belief = BodyCausalBelief(
            **candidate.model_dump(exclude={"candidate_id", "status", "created_at", "updated_at", "confidence"}),
            belief_id=identifier, confidence=0.0 if dropped else precision,
            stability=0.0 if dropped else precision * min(1.0, len(windows) / self.profile.minimum_windows),
            formed_at=previous.formed_at if previous else now, last_revised_at=now,
            revision=previous.revision + 1 if previous else 1, status=status,
        )
        self.store.put_belief(belief)
        previous_reliability = next((item for item in self.store.reliabilities()
                                     if item.context_signature == belief.context_signature
                                     and item.channel_id == belief.predicted_channel), None)
        prior_support = set(previous_reliability.supporting_refs) if previous_reliability else set()
        prior_contrary = set(previous_reliability.contradicting_refs) if previous_reliability else set()
        refreshed = set(support) | set(contrary)
        reliability_support = tuple(sorted((prior_support - refreshed) | set(support)))
        reliability_contrary = tuple(sorted((prior_contrary - refreshed) | set(contrary)))
        self.store.put_reliability(SensorReliabilityBelief(
            channel_id=belief.predicted_channel, context_signature=belief.context_signature,
            reliability=len(reliability_support) / (len(reliability_support) + len(reliability_contrary)),
            calibration_count=len(reliability_support) + len(reliability_contrary),
            supporting_refs=reliability_support, contradicting_refs=reliability_contrary,
            revision=previous_reliability.revision + 1 if previous_reliability else 1,
        ))
        return belief

    def refresh(self, belief):
        from harness.body_belief.models import BodyCausalBeliefCandidate

        return self.apply(BodyCausalBeliefCandidate(
            **belief.model_dump(exclude={"belief_id", "stability", "formed_at", "last_revised_at", "revision", "status"}),
            candidate_id="refresh-" + belief.belief_id, created_at=belief.formed_at, updated_at=belief.last_revised_at,
        ))

    def refresh_all(self):
        return tuple(self.refresh(item) for item in self.store.beliefs())
