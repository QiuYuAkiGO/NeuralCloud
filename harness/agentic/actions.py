"""Native proposals retain Runtime's offer, evidence and action authority."""
from __future__ import annotations

from datetime import datetime, timezone

from harness.agentic.models import SubmitAction


class RestActions:
    terminal = 'finish_turn'

    def __init__(self, state):
        self.state = state

    def schema(self):
        schema = SubmitAction.model_json_schema()
        schema['properties'].pop('goal', None)
        schema['properties']['rest'] = {'$ref': '#/$defs/RestArguments'}
        schema['required'] = ['rest']
        schema['$defs'] = {'RestArguments': schema['$defs']['RestArguments']}
        return schema

    def validate(self, proposal, memory):
        memory.validate()
        if proposal.goal is not None or proposal.rest is None:
            raise ValueError('only_rest_is_offered_in_dialogue')
        if not self.state.runtime.get('autonomous_enabled'):
            raise ValueError('native_actions_disabled')


class GoalActions(RestActions):
    terminal = 'submit_action'

    def __init__(self, state, request, ready):
        super().__init__(state)
        self.request, self.ready = request, ready
        self.catalog_at = datetime.now(timezone.utc)

    def schema(self):
        schema = SubmitAction.model_json_schema()
        schema['$defs']['GoalChoice']['properties'].pop('appraisal', None)
        schema['$defs']['CapabilityChoice']['properties'].pop('attention_assessment', None)
        return schema

    def validate(self, proposal, memory):
        if proposal.rest is not None:
            return super().validate(proposal, memory)
        memory.validate()
        from harness.cognitive.goals import _validate_episode
        from harness.cognitive.capabilities import _bound_choice
        _validate_episode(proposal.goal, self.request)
        if proposal.goal.appraisal is not None or proposal.goal.choice.attention_assessment is not None:
            raise ValueError('use_source_bound_affect_propose_for_appraisal')
        if self.state.runtime.get('rest', {}).get('resting') and proposal.goal.choice.kind in {'act','observe'}:
            raise ValueError('end_rest_before_external_activity')
        if proposal.goal.choice.offer_id is not None:
            offer = next((o for o in self.ready if o.offer_id == proposal.goal.choice.offer_id), None)
            if offer is None:
                raise ValueError('goal_unknown_offer')
            # Execution still requires Runtime's fresh catalog and deadline.
            _bound_choice(proposal.goal.choice, offer, now=self.catalog_at)
