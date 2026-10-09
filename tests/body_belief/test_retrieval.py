import json

from harness.body_belief.appraisal import BodyBeliefAppraiser
from harness.body_belief.compiler import compile_body_memory
from harness.body_belief.consolidation import BodyBeliefConsolidator
from harness.body_belief.retrieval import evidence_bundle, retrieve_experiences
from harness.llm.client import ChatResult
from tests.body_belief.conftest import candidate, experience


class ObservationalModel:
    def __init__(self):
        self.messages = []

    def complete_chat_result(self, messages, *, stream=False):
        self.messages.append(messages)
        supplied = json.loads(messages[0]["content"])
        episodes = supplied["experiences"]
        first = episodes[0]
        transition = first["before"] + "->" + first["after"]
        matching = [item for item in episodes if item["context_signature"] == first["context_signature"]
                    and item["action_signature"] == first["action_signature"]]
        supports = [item["experience_id"] for item in matching
                    if item["before"] + "->" + item["after"] == transition and item["outcome_success"]]
        contrary = [item["experience_id"] for item in matching if item["experience_id"] not in supports]
        return ChatResult(text=json.dumps({"candidates": [{
            "proposition": f"Observed Q7 transition {transition} after this action.",
            "context_signature": first["context_signature"], "action_signature": first["action_signature"],
            "predicted_channel": first["channel"], "predicted_transition": transition,
            "confidence": .8, "supporting_experience_refs": supports, "contradicting_experience_refs": contrary,
        }]}))


def test_appraisal_receives_counterexamples_controls_and_no_private_mapping(store):
    episodes = [experience(index) for index in range(3)]
    for item in episodes:
        store.append_experience(item)
    belief = BodyBeliefConsolidator(store).apply(candidate(episodes))
    episodes += [experience(3, after="M1"), experience(4, action="station_b")]
    bundle = evidence_bundle(belief, tuple(episodes))
    assert len(bundle["support"]) == 3 and len(bundle["counterexamples"]) == 1 and len(bundle["controls"]) == 1
    model = ObservationalModel()
    result = BodyBeliefAppraiser(model).appraise(episodes, existing_beliefs=(belief,))
    assert result.success
    assert result.candidates[0].contradicting_experience_refs == ("exp-3",)
    visible = json.dumps(model.messages)
    for forbidden in ("available_ratio", "transform_ref", "seed", "truth", "policy_rank"):
        assert forbidden not in visible
    assert result.input_hash


def test_memory_treatments_have_distinct_auditable_bounded_context(store):
    episodes = [experience(index) for index in range(3)]
    for item in episodes:
        store.append_experience(item)
    belief = BodyBeliefConsolidator(store).apply(candidate(episodes))
    assert compile_body_memory(treatment="none", experiences=episodes, beliefs=(belief,)) is None
    objective = compile_body_memory(treatment="objective_events", experiences=episodes, beliefs=(belief,), max_tokens=5000)
    epistemic = compile_body_memory(treatment="body_causal_beliefs", experiences=episodes, beliefs=(belief,), max_tokens=5000)
    assert "belief_id" not in objective.text and "belief_id" in epistemic.text
    assert '"revision":1' in epistemic.text and "counterexample_count" in epistemic.text
    assert belief.belief_id in epistemic.source_ids
    objective_rows = json.loads(objective.text)["body_memory"]
    epistemic_objectives = [row for row in json.loads(epistemic.text)["body_memory"] if "experience_id" in row]
    assert objective_rows == epistemic_objectives
    assert compile_body_memory(treatment="body_causal_beliefs", experiences=episodes, beliefs=(belief,), max_tokens=10) is None


def test_appraiser_rejects_model_invented_evidence_and_reasoning_text_legacy_mode(legacy_json_mode, ):
    class BadModel:
        def complete_chat_result(self, messages, *, stream=False):
            return ChatResult(text='I know the answer. {"candidates":[]}')

    result = BodyBeliefAppraiser(BadModel()).appraise([experience(1)])
    assert not result.success and not result.candidates


def test_recent_agreement_cannot_hide_older_counterexamples_within_budget():
    episodes = [experience(0, after="M1"), *[experience(index) for index in range(1, 30)]]
    selected = retrieve_experiences(episodes, context_signature=episodes[0].context_signature,
                                   action_signature=episodes[0].action_signature, limit=6)
    assert len(selected) == 6
    assert any(item.transition == "M2->M1" for item in selected)
    assert any(item.transition == "M2->M4" for item in selected)
