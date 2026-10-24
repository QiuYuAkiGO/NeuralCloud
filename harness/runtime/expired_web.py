"""Settle old expired web preparations without replaying inference or effects."""
from datetime import datetime, timedelta

from harness.contracts.envelope import DomainCommandEnvelope
from harness.runtime.models import RuntimeEvent, utc_now


def expired_source(supervisor, record):
    raw = record.data.get("envelope")
    if record.data.get("service") != "cognitive" or not isinstance(raw, dict) or raw.get("command_type") != "PrepareResponse":
        return None
    payload = raw.get("payload")
    if not isinstance(payload, dict) or not payload.get("runtime_web_deadline_at"):
        return None
    deadline = datetime.fromisoformat(str(payload["runtime_web_deadline_at"]).replace("Z", "+00:00"))
    if deadline.tzinfo is None or deadline > utc_now():
        return None
    # Old code used model_copy to cap the transport window after constructing
    # the command, so an already-expired web round could make it invalid.
    # Validate all stable identity/payload/hash fields with a temporary, valid
    # transport window. This copy is never dispatched or saved over the log.
    transport_deadline = datetime.fromisoformat(str(raw["deadline"]).replace("Z", "+00:00"))
    envelope = DomainCommandEnvelope[dict].model_validate({**raw,
        "issued_at": (transport_deadline - timedelta(microseconds=1)).isoformat()})
    if (envelope.command_id != record.command_id or envelope.idempotency_key != record.idempotency_key
            or envelope.event_id != record.event_id):
        raise ValueError("expired_web_command_identity_mismatch")
    rows = list(supervisor.recovery.rows("SELECT seq FROM events WHERE id=?", (envelope.event_id,)))
    if len(rows) != 1:
        raise ValueError("expired_web_command_source_missing")
    source = RuntimeEvent.model_validate(supervisor.recovery.read(rows[0][0]).data["event"])
    if source.receive_seq != envelope.receive_seq or source.correlation_id != envelope.correlation_id:
        raise ValueError("expired_web_command_source_mismatch")
    return source
