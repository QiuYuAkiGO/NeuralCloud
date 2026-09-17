"""QQ requests to the single Runtime/Cognitive owner; no local model fallback."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import math
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from harness.homeostasis.models import HomeostaticInteroceptiveSummary
from harness.core.error_notice import ERROR_FALLBACK_SEND_SECONDS
from harness.qq.decision import QQReplyDecision, QQVerifiedSelfAlias
from harness.qq.idle import QQIdleIntent
from harness.qq.memory import QQID
from harness.qq.conversation import QQConversationID, conversation_scope
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace


class QQSharedTraceOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    trace_id: str = Field(pattern=r"^qq-trace:[0-9a-f]{32}$")
    event_id: str = Field(pattern=r"^qq-watch-event:[0-9a-f]{32}$")
    first_seen_at: AwareDatetime
    first_seen_mono: float = Field(ge=0)
    observation_sequence: int | None = Field(default=None, ge=0)


class QQSharedPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    transport: Literal["desktop", "onebot"] = "desktop"
    group_id: QQConversationID
    self_member_id: QQID
    trigger_member_id: QQID | None = None
    request: str | None = Field(default=None, min_length=1, max_length=4096)
    query: str | None = Field(default=None, min_length=1, max_length=4096)
    member_ids: list[QQID] | None = Field(default=None, max_length=20)
    verified_self_aliases: list[QQVerifiedSelfAlias] = Field(default_factory=list, max_length=16)
    idle_intent: QQIdleIntent | None = None
    homeostatic_interoception: HomeostaticInteroceptiveSummary | None = None
    deadline_at: AwareDatetime
    trace: QQSharedTraceOrigin
    request_id: str | None = Field(default=None, pattern=r"^qq-reply-request:[0-9a-f]{32}$")
    revision: int | None = Field(default=None, ge=1, strict=True)
    source_event_ids: list[str] | None = Field(default=None, min_length=1, max_length=128)
    authority_anchor: str | None = Field(default=None, pattern=r"^qq-watch-event:[0-9a-f]{32}$")

    @model_validator(mode="after")
    def source_scope(self):
        batch = (self.request_id, self.revision, self.source_event_ids, self.authority_anchor)
        if any(value is not None for value in batch):
            import re
            if (not all(value is not None for value in batch) or self.transport != "onebot"
                    or self.idle_intent is not None or self.authority_anchor != self.trace.event_id
                    or self.source_event_ids[0] != self.authority_anchor
                    or len(set(self.source_event_ids)) != len(self.source_event_ids)
                    or any(not isinstance(item, str) or re.fullmatch(r"qq-watch-event:[0-9a-f]{32}", item) is None
                           for item in self.source_event_ids)):
                raise ValueError("shared_qq_request_binding_invalid")
        scope = conversation_scope(self.group_id)
        if scope["kind"] == "private":
            if (self.transport != "onebot" or self.self_member_id != scope["self_member_id"]
                    or self.trigger_member_id != scope["peer_id"] or self.idle_intent is not None
                    or any(member not in {scope["self_member_id"], scope["peer_id"]}
                           for member in self.member_ids or [])):
                raise ValueError("shared_qq_private_scope_mismatch")
        if any(x.group_id != self.group_id or x.self_member_id != self.self_member_id
               for x in self.verified_self_aliases):
            raise ValueError("shared_qq_alias_scope_mismatch")
        expected_verification = "qq_protocol_verified" if self.transport == "onebot" else "qq_ui_verified"
        if any(x.verification != expected_verification for x in self.verified_self_aliases):
            raise ValueError("shared_qq_alias_transport_mismatch")
        if self.idle_intent is not None and (
            self.idle_intent.group_id != self.group_id
            or self.idle_intent.self_member_id != self.self_member_id
            or self.trigger_member_id is not None
        ):
            raise ValueError("shared_qq_idle_scope_mismatch")
        if self.deadline_at > self.trace.first_seen_at + timedelta(seconds=60):
            raise ValueError("shared_qq_deadline_extended")
        return self


class SharedQQUnavailable(ConnectionError):
    """A shared service failure, never an autonomous wait or a send retry."""


class QQSharedCognitiveClient:
    def __init__(self, runtime_url: str, token_file: Path, *, client=None):
        parts = urlsplit(runtime_url)
        if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1"}
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError("shared_runtime_requires_loopback")
        token = Path(token_file).read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("shared_runtime_token_required")
        self._owns_client = client is None
        self.client = client or httpx.Client(base_url=runtime_url.rstrip("/"), trust_env=False,
                                             headers={"Authorization": "Bearer " + token})

    def __call__(self, *, deadline: QQDeadline, trace: QQTurnTrace, **kwargs: Any) -> QQReplyDecision:
        remaining = deadline.check()
        origin = QQSharedTraceOrigin(trace_id=trace.trace_id, event_id=trace.event_id,
            first_seen_at=trace.first_seen_at, first_seen_mono=trace.first_seen_mono,
            observation_sequence=trace.observation_sequence)
        values = {key: kwargs[key] for key in (
            "group_id", "self_member_id", "trigger_member_id", "request", "query", "member_ids",
            "verified_self_aliases", "idle_intent", "homeostatic_interoception", "transport",
            "request_id", "revision", "source_event_ids", "authority_anchor") if key in kwargs}
        # Wall deadline is an additional cross-service bound; the caller's
        # original monotonic deadline is checked both before and after RPC.
        deadline_at = min(datetime.now(timezone.utc) + timedelta(seconds=remaining),
                          trace.first_seen_at + timedelta(seconds=60))
        request = QQSharedPrepareRequest(**values, trace=origin, deadline_at=deadline_at)
        started = trace.clock()
        try:
            response = self.client.post("/v1/qq/decisions", json=request.model_dump(mode="json"),
                                        timeout=remaining + ERROR_FALLBACK_SEND_SECONDS, follow_redirects=False)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            if deadline.expired:
                raise QQDeadlineExpired("Shared cognition exceeded the original turn deadline") from exc
            raise SharedQQUnavailable("shared_cognitive_timeout") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SharedQQUnavailable("shared_cognitive_unavailable") from exc
        ended = trace.clock()
        if not isinstance(payload, dict) or not isinstance(payload.get("decision"), dict):
            raise SharedQQUnavailable("shared_cognitive_invalid_result")
        result = QQReplyDecision.model_validate(payload["decision"])
        if (result.group_id != request.group_id or result.self_member_id != request.self_member_id
                or result.trace_id != trace.trace_id):
            raise SharedQQUnavailable("shared_cognitive_result_scope_mismatch")
        _import_remote_trace(trace, payload.get("trace"), started=started, ended=ended)
        trace.record("shared_cognitive.rpc", started_mono=started)
        trace.bind_decision(result.decision_id)
        if result.outcome != "error_fallback":
            deadline.check()
        return result

    def close(self):
        if self._owns_client:
            self.client.close()


def _import_remote_trace(trace: QQTurnTrace, remote: dict, *, started: float, ended: float):
    """Retain server measurements separately, inside the measured RPC interval.

    No remote monotonic value is assumed to share the client's clock epoch.
    End alignment is a visualization bound, not a measured network latency.
    Original server offsets and the RTT residual remain explicit diagnostics.
    """
    if not isinstance(remote, dict) or remote.get("trace_id") != trace.trace_id:
        raise SharedQQUnavailable("shared_cognitive_trace_mismatch")
    stages, counters = remote.get("stages"), remote.get("counters")
    elapsed = remote.get("elapsed_ms")
    if (not isinstance(stages, list) or len(stages) > 256 or not isinstance(counters, dict)
            or len(counters) > 128 or type(elapsed) not in {int, float}
            or not math.isfinite(elapsed) or elapsed < 0 or elapsed > (ended-started)*1000+1):
        raise SharedQQUnavailable("shared_cognitive_trace_invalid")
    validated = []
    aligned_elapsed = min(elapsed, (ended-started)*1000)
    shift = (ended-trace.first_seen_mono)*1000-aligned_elapsed
    for stage in stages:
        if not isinstance(stage, dict):
            raise SharedQQUnavailable("shared_cognitive_trace_invalid")
        name = trace._name(stage.get("name"))
        a, b = stage.get("start_offset_ms"), stage.get("end_offset_ms")
        if (type(a) not in {int, float} or type(b) not in {int, float}
                or not all(math.isfinite(x) for x in (a,b)) or not 0 <= a <= b <= elapsed+1):
            raise SharedQQUnavailable("shared_cognitive_trace_invalid")
        if stage.get("outcome") not in {"ok","error","expired","cancelled","skipped","late_result"}:
            raise SharedQQUnavailable("shared_cognitive_trace_invalid")
        validated.append({**stage, "name": name, "start_offset_ms": round(shift+min(a,aligned_elapsed),3),
            "end_offset_ms": round(shift+min(b,aligned_elapsed),3), "remote_start_offset_ms": a,
            "remote_end_offset_ms": b, "timing_origin": "shared_cognitive",
            "rpc_residual_ms": round(max(0,(ended-started)*1000-elapsed),3)})
    for name, amount in counters.items():
        trace._name(name)
        if type(amount) is not int or amount < 0:
            raise SharedQQUnavailable("shared_cognitive_trace_invalid")
    configuration = remote.get("effective_configuration")
    if configuration is not None:
        from harness.qq.policy import EFFECTIVE_FIELDS, QQLatencyOptions
        try:
            if (not isinstance(configuration, dict)
                    or set(configuration) != {"schema_version", "version", "policy_version", "values"}
                    or configuration["schema_version"] != "qq-effective-config-v1"
                    or not isinstance(configuration["values"], dict)
                    or set(configuration["values"]) != set(EFFECTIVE_FIELDS)):
                raise ValueError
            values = configuration["values"]
            for name, entry in values.items():
                scope_list = (isinstance(entry, dict) and name == "qq_latency_conversations"
                    and isinstance(entry.get("value"), list) and len(entry["value"]) <= 128
                    and all(isinstance(item, str) and len(item) <= 128 for item in entry["value"]))
                if (not isinstance(entry, dict) or set(entry) != {"value", "source"}
                        or entry["source"] not in {"constructor", "environment", "dotenv", "default", "persisted",
                                                  "runtime", "frozen_request", "conversation_rollout"}
                        or (type(entry["value"]) not in {type(None), bool, int, float, str} and not scope_list)
                        or (isinstance(entry["value"], str) and len(entry["value"]) > 256)
                        or (isinstance(entry["value"], float) and not math.isfinite(entry["value"]))):
                    raise ValueError
            policy = QQLatencyOptions.model_validate({name: values[name]["value"] for name in QQLatencyOptions.model_fields})
            digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            if configuration["version"] != "qq-config:" + digest or configuration["policy_version"] != policy.version:
                raise ValueError
            configuration = json.loads(json.dumps(configuration, allow_nan=False))
        except (ValueError, TypeError, KeyError):
            raise SharedQQUnavailable("shared_cognitive_trace_invalid") from None
    with trace._lock:
        if configuration is not None:
            if trace._effective_configuration is not None and trace._effective_configuration != configuration:
                raise SharedQQUnavailable("shared_cognitive_trace_invalid")
            if trace._effective_configuration is None:
                trace._effective_configuration = configuration
        if len(set(trace._counters) | set(counters)) > 128:
            raise SharedQQUnavailable("shared_cognitive_trace_invalid")
        for name, amount in counters.items():
            trace.add_counter(name, amount)
        room = trace.max_events-len(trace._events)
        trace._events.extend(validated[:room])
        if len(validated) > room:
            trace.add_counter("events_dropped",len(validated)-room)
