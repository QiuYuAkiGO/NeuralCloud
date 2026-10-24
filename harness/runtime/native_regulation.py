"""Runtime-owned state, rest receipts and native appraisal delivery.

HTTP handlers only queue interpretations or commit scheduling state. They never
call Cognitive back while it is preparing a turn, avoiding a nested RPC lock.
Homeostasis applies the durable queue on its existing passive-state clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from harness.agentic.store import digest
from harness.homeostasis.native_appraisal import NativeAppraisalReceipt
from harness.runtime.models import RuntimeLifecycle, utc_now


class NativeFeedbackBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    runtime_instance_id: str
    manifest_revision: int
    receipts: list[NativeAppraisalReceipt] = Field(max_length=64)

    @model_validator(mode='after')
    def unique_receipts(self):
        if len({r.receipt_id for r in self.receipts}) != len(self.receipts):
            raise ValueError('native_duplicate_receipt_identity')
        return self


class NativeRestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=256)
    runtime_instance_id: str
    manifest_revision: int
    expected_rest_revision: int = Field(ge=0)
    expires_at: AwareDatetime
    resting: bool
    review_seconds: int = Field(default=300, ge=60, le=3600)
    source_refs: list[str] = Field(default_factory=list, max_length=32)


class NativeModelAdmission(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attempt_id: str = Field(min_length=1, max_length=256)
    opportunity_id: str = Field(min_length=1, max_length=256)
    runtime_instance_id: str
    manifest_revision: int


class NativeRegulationController:
    def __init__(self, supervisor, *, clock=utc_now):
        self.supervisor, self.store, self.clock = supervisor, supervisor.goals.store, clock
        self.last_error = None
        with self.store.objects._lock:
            self.store.objects._db.execute("CREATE INDEX IF NOT EXISTS native_feedback_pending_v1 ON agency_objects(kind,json_extract(payload,'$.status')) WHERE kind='regulation_native_feedback'")

    @property
    def settings(self):
        return self.supervisor.manifest.agentic_regulation

    def rest(self):
        return self.store.get('native_self', 'rest') or {'revision': 0, 'resting': False, 'review_at': None}

    def contact_enabled(self):
        return (self.settings.mode == 'on' and self.settings.autonomous_enabled and
            self.supervisor.lifecycle == RuntimeLifecycle.RUNNING and not self.rest()['resting'])

    def authorize_contact(self, opportunity_id, selection_ref, group_id):
        if not self.contact_enabled():
            raise PermissionError('native_contact_disabled')
        cap = self.supervisor.capabilities
        row = cap.calls.get(opportunity_id)
        if (not row or row['status'] not in {'dispatching','pending'} or not row.get('goal_binding') or
                cap.invalid_reason(row) or row.get('goal_decision', {}).get('decision_id') != selection_ref or
                row.get('decision', {}).get('choice', {}).get('kind') != 'act' or
                row.get('offer', {}).get('capability_id') != 'communication.qq.send.v1' or
                row.get('offer', {}).get('scope', {}).get('group_id') != group_id):
            raise PermissionError('native_contact_unbound')
        return row

    def model_budget(self):
        now = self.clock()
        calls = [c for c in (self.store.get('native_self', 'calls') or {}).get('attempts', [])
                 if (now - datetime.fromisoformat(c['at'])).total_seconds() < 3600]
        maximum = min(24, self.supervisor.manifest.goal_regulation.maximum_model_calls_per_hour)
        return {'attempts': calls, 'calls_last_hour': len(calls), 'maximum_calls_per_hour': maximum,
            'next_available_at': (datetime.fromisoformat(calls[0]['at']) + timedelta(hours=1)).isoformat()
                if len(calls) >= maximum and calls else now.isoformat()}

    def admit_model(self, request):
        self.check_fence(request)
        if not self.settings.autonomous_enabled:
            raise PermissionError('native_actions_disabled')
        cap = self.supervisor.capabilities
        row = cap.calls.get(request.opportunity_id)
        if request.opportunity_id.startswith('entry:'):
            row = next((r for r in cap.calls.all() if r.get('entry_id') == request.opportunity_id.removeprefix('entry:')), None)
        if not row or row['status'] not in {'selecting', 'dispatching', 'pending'} or not row.get('goal_binding'):
            raise PermissionError('native_opportunity_unavailable')
        if cap.invalid_reason(row):
            raise PermissionError('native_opportunity_superseded')
        rest = self.rest()
        if rest['resting'] and (row['status'] != 'selecting' or self.clock() < datetime.fromisoformat(rest['review_at'])):
            raise PermissionError('native_resting')
        with self.store.objects.atomic():
            budget = self.model_budget()
            if any(c['id'] == request.attempt_id for c in budget['attempts']):
                raise ValueError('native_attempt_already_reserved')
            if budget['calls_last_hour'] >= budget['maximum_calls_per_hour']:
                raise PermissionError('native_hourly_model_budget')
            budget['attempts'].append({'id':request.attempt_id, 'at':self.clock().isoformat(),
                'opportunity_id':request.opportunity_id})
            self.store.put('native_self', 'calls', {'attempts':budget['attempts']})
        return {'admitted':True, 'attempt_id':request.attempt_id}

    def check_fence(self, request):
        s = self.supervisor
        if (s.lifecycle != RuntimeLifecycle.RUNNING or request.runtime_instance_id != s.runtime_instance_id
                or request.manifest_revision != s.manifest.revision or self.settings.mode != 'on'):
            raise PermissionError('native_state_fence_changed')

    def snapshot(self):
        s = self.supervisor
        observed = getattr(s, '_homeostasis_snapshot', None)
        raw = observed.payload if observed is not None else {}
        state = raw.get('snapshot', raw)
        dimensions, dynamics = state.get('dimensions', {}), state.get('dynamics') or {}
        energy = dimensions.get('energy_reserve') or {}
        captured = state.get('updated_at')
        if isinstance(captured, str):
            captured = datetime.fromisoformat(captured)
        fresh = captured is not None and 0 <= (self.clock()-captured).total_seconds() <= 300
        body = {'status': 'observed_functional_proxy' if fresh else 'stale_or_unknown',
            'revision': observed.revision if observed is not None else None,
            'as_of': captured.isoformat() if captured else None,
            'load': dimensions.get('cognitive_load', {}).get('value') if fresh else None,
            'fatigue': dynamics.get('fatigue') if fresh else None,
            'energy': energy.get('value') if fresh and energy.get('precision', 0) >= .2 else None,
            'psychological': {key: dimensions.get(key, {}).get('value') if fresh else None
                for key in ('safety_predictability', 'competence', 'autonomy', 'social_connection')},
            'pressure': dynamics.get('regulation_pressure', {}).get('level') if fresh else 'unknown'}
        rest = self.rest()
        return {'schema_version': 'native-internal-state-v1', 'mode': self.settings.mode,
            'running': s.lifecycle == RuntimeLifecycle.RUNNING, 'runtime_instance_id': s.runtime_instance_id,
            'manifest_revision': s.manifest.revision, 'autonomous_enabled': self.settings.autonomous_enabled,
            'body': body, 'rest': {key:rest[key] for key in ('revision','resting','review_at')},
            'last_error': self.last_error}

    def queue_feedback(self, request):
        self.check_fence(request)
        accepted = []
        for receipt in request.receipts:
            if receipt.kind == 'control':
                raise ValueError('body_control_is_not_an_affect_proposal')
            if receipt.occurred_at > self.clock() + timedelta(seconds=5):
                raise ValueError('native_appraisal_future_event')
            previous = self.store.get('native_feedback', receipt.receipt_id)
            if previous is not None and previous['hash'] != receipt.receipt_hash:
                raise ValueError('native_appraisal_identity_conflict')
        for receipt in request.receipts:
            if self.store.get('native_feedback', receipt.receipt_id) is None:
                self.store.put('native_feedback', receipt.receipt_id,
                    {'id': receipt.receipt_id, 'hash': receipt.receipt_hash,
                     'receipt': receipt.model_dump(mode='json'), 'status': 'queued'})
            accepted.append(receipt.receipt_id)
        return {'accepted': accepted}

    def set_rest(self, request):
        self.check_fence(request)
        if not self.settings.autonomous_enabled:
            raise PermissionError('native_actions_disabled')
        binding = digest(request.model_dump(mode='json'))
        previous = self.store.get('native_control', request.request_id)
        if previous is not None:
            if previous['binding'] != binding:
                raise ValueError('native_rest_identity_conflict')
            return previous['receipt']
        if not 0 < (request.expires_at - self.clock()).total_seconds() <= 60:
            raise ValueError('native_rest_request_expired')
        prior = self.rest()
        if prior['revision'] != request.expected_rest_revision:
            raise ValueError('native_rest_revision_changed')
        now = self.clock()
        rest = {'revision': prior['revision'] + 1, 'resting': request.resting,
            'review_at': (now + timedelta(seconds=request.review_seconds)).isoformat() if request.resting else None,
            'updated_at': now.isoformat()}
        receipt = {'request_id': request.request_id, 'request_hash': binding, 'status': 'committed', **rest,
            'evidence_refs': ['native-rest:' + request.request_id]}
        interpretation = NativeAppraisalReceipt(receipt_id='control:' + request.request_id,
            cause_id='rest', kind='control', occurred_at=now, resting=request.resting)
        with self.store.objects.atomic():
            self.store.put('native_self', 'rest', rest)
            self.store.put('native_control', request.request_id, {'binding': binding, 'receipt': receipt}, immutable=True)
            self.store.put('native_feedback', interpretation.receipt_id, {'id': interpretation.receipt_id,
                'hash': interpretation.receipt_hash, 'receipt': interpretation.model_dump(mode='json'), 'status':'queued'})
        if request.resting:
            bridge = getattr(self.supervisor, 'qq_capability_bridge', None)
            if bridge is not None:
                bridge.revoke_contacts()
        return receipt

    async def drain(self, event):
        if self.settings.mode != 'on':
            return
        # This explicit migration expires the historical unbounded intent once;
        # it does not reset the body's accumulated load, fatigue, or baselines.
        if self.store.get('native_self', 'activated') is None:
            now = self.clock()
            receipt = NativeAppraisalReceipt(receipt_id='native-intent-expiry-v1', cause_id='migration',
                kind='control', occurred_at=now, resting=self.rest()['resting'])
            with self.store.objects.atomic():
                self.store.put('native_feedback', receipt.receipt_id, {'id': receipt.receipt_id, 'hash': receipt.receipt_hash,
                    'receipt': receipt.model_dump(mode='json'), 'status': 'queued'})
                self.store.put('native_self', 'activated', {'at':now.isoformat()})
        s = self.supervisor
        with self.store.objects._lock:
            rows = [json.loads(r[0]) for r in self.store.objects._db.execute(
                "SELECT payload FROM agency_objects WHERE kind='regulation_native_feedback' AND json_extract(payload,'$.status')='queued' ORDER BY rowid LIMIT 64")]
        for row in rows:
            # The triggering event supplies journal sequencing; it cannot
            # supply model-written body variables or bypass the source receipt.
            native_event = event.model_copy(update={'payload': {'native_appraisal': row['receipt']}})
            command = s._make_command(native_event, command_type='ApplyEvent',
                idempotency_key='native-affect:' + row['id'], expected_revision=None,
                payload={'event':native_event.model_dump(mode='json')})
            try:
                result = await s._invoke_domain(service='homeostasis', envelope=command,
                    method=s.homeostasis.apply_event, event=native_event)
                if result.status not in {'applied', 'idempotent_replay'}:
                    raise ValueError('native_appraisal_not_applied')
                await s._update_homeostasis(result, event=native_event, schedule_marked=False)
            except Exception as exc:
                self.last_error = type(exc).__name__
                break
            self.store.put('native_feedback', row['id'], {**row, 'status':'applied'})
            self.last_error = None
