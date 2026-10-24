"""Native autonomy reserves every physical model attempt at Runtime."""
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

import httpx

_native = ContextVar('native_autonomy', default=None)


@contextmanager
def command_scope(host, command):
    settings = getattr(host.chat.settings, 'agentic_regulation', None)
    payload = command.payload
    opportunity = (payload.get('opportunity_id') or
        (payload.get('runtime_goal_context') or {}).get('opportunity_id') or
        (payload.get('idle_intent') or {}).get('opportunity_ref'))
    if not opportunity and str(payload.get('request_id', '')).startswith('capability:'):
        opportunity = payload['request_id']
    if not opportunity and payload.get('trigger') == 'homeostasis' and payload.get('entry_id'):
        opportunity = 'entry:' + payload['entry_id']
    if not settings or settings.mode != 'on' or not opportunity:
        yield
        return
    token = _native.set({'opportunity_id': opportunity, 'runtime_instance_id': command.runtime_instance_id,
        'manifest_revision': command.manifest_revision})
    try:
        yield
    finally:
        _native.reset(token)


def reserve_attempt():
    context = _native.get()
    if context is None:
        return
    from harness.agentic.internal_state import runtime_endpoint
    url, headers = runtime_endpoint()
    # Reserve before transport, including retries and vision. Lost responses
    # conservatively consume a permit; failures never refund compute budget.
    with httpx.Client(timeout=1) as client:
        response = client.post(url + '/v1/internal/agentic/model-admission', headers=headers,
            json={**context, 'attempt_id': 'native-call:' + uuid4().hex})
        response.raise_for_status()
        if response.json().get('admitted') is not True:
            raise ValueError('native_model_not_admitted')
