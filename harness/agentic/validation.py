"""Bounded schema diagnostics without private model output or source prose."""
import json

from pydantic_core import ErrorType
from typing import get_args


_ERROR_TYPES = frozenset(get_args(ErrorType))
_REASONS = frozenset({
    'claim_requires_subject_or_unbound_mention', 'claim_revision_requires_id_and_reason',
    'claim_effective_interval_invalid', 'supersession_requires_direct_preference_or_intention',
    'supersession_target_duplicate', 'evidence_anchor_span_invalid',
    'evidence_group_empty', 'evidence_group_dependency_invalid',
})


def _json_type(value):
    # Never expose an arbitrary class name or a representation of private data.
    return {dict: 'object', list: 'array', str: 'string', int: 'number',
        float: 'number', bool: 'boolean', type(None): 'null'}.get(type(value), 'other')


def _container_shape(value):
    """Describe a type mismatch without repairing or persisting its contents."""
    shape = {'input_type': _json_type(value)}
    if isinstance(value, str):
        if len(value) > 24000:
            shape['encoded_json_type'] = 'not_inspected_size_limit'
        else:
            try:
                parsed = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except RecursionError:
                shape['encoded_json_type'] = 'not_inspected_depth_limit'
            except ValueError:
                shape['encoded_json_type'] = 'invalid_json'
            else:
                shape['encoded_json_type'] = _json_type(parsed)
    return shape


class BackgroundModelValidationError(ValueError):
    def __init__(self, schema, error):
        super().__init__('background_model_output_invalid')
        fields = set()

        def visit(value):
            if isinstance(value, dict):
                fields.update(value.get('properties', {}))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(schema.model_json_schema())
        # Inputs are examined only in memory for fixed type/presence labels.
        # They must never be copied into the durable diagnostic or exception.
        errors = error.errors(include_input=True, include_context=True, include_url=False)
        projected = []
        for item in errors[:8]:
            row = {'type': item['type'] if item['type'] in _ERROR_TYPES else 'custom_validation',
                'path': [part if isinstance(part, int) or part in fields else '<field>'
                    for part in item['loc'][:12]]}
            # Only literal repository-owned reason codes are persisted. Never
            # copy arbitrary validator messages, input values, or extra keys.
            reason = str(item.get('ctx', {}).get('error', ''))
            if reason in _REASONS:
                row['reason'] = reason
            if item['type'] in {'list_type', 'dict_type'}:
                row['shape'] = _container_shape(item.get('input'))
            if reason == 'claim_revision_requires_id_and_reason' and type(item.get('input')) is dict:
                value = item['input']
                row['revision_fields'] = {
                    key: _json_type(value[key]) if key in value else 'missing'
                    for key in ('record_id', 'revision_reason')
                }
            projected.append(row)
        self.diagnostic = {'phase': 'model_output_validation',
            'schema': schema.__name__ if schema.__name__ in {'Summary', 'Updates', 'AffectProposal'} else 'unknown',
            'error_count': len(errors), 'errors': projected}
