"""Bounded text-only HTTP replay. No delivery, memory, tools or body backends."""

from __future__ import annotations

import copy
import hashlib
import json
import random
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, Callable
from uuid import uuid4

import httpx

from .audit import audit_spec, preflight
from .budget import BudgetLedger, BudgetStopped
from .fixtures import evaluate_probe, probe_payload, treatments
from .models import DiagnosticRunSpec, append_jsonl, digest, write_json

_INVALID_RESPONSE_STOP_LIMIT = 3


class HTTPReplayFailure(RuntimeError):
    """A fixed error code and safe response evidence, never provider error text."""

    def __init__(self, code: str, evidence: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.evidence = evidence


class DirectHTTPReplayClient:
    def __init__(self, *, endpoint: str, api_key: str, timeout_seconds: float = 90,
                 observer: Callable[[dict[str, Any]], None] | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.endpoint, self.api_key, self.timeout_seconds, self.observer = endpoint, api_key, timeout_seconds, observer
        self.test_transport = isinstance(transport, httpx.MockTransport)
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport, follow_redirects=False)

    def _observe(self, event: dict[str, Any]) -> None:
        from infra.trace.diagnostics import emit_diagnostic
        emit_diagnostic(event, observer=self.observer)

    def complete_payload(self, payload: dict[str, Any], *, request_id: str,
                         metadata: dict[str, Any], exact_body: str | None = None) -> dict[str, Any]:
        if any(self._client.event_hooks.values()) or self._client.auth is not None:
            raise ValueError("HTTP hooks/auth may mutate a budgeted payload; refused")
        body = exact_body.encode("utf-8") if exact_body is not None else json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if json.loads(body) != payload:
            raise ValueError("exact body and parsed payload differ")
        request = self._client.build_request("POST", self.endpoint, content=body,
                                             headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
        common = {"request_id": request_id, "request_body_sha256": hashlib.sha256(request.content).hexdigest(),
                  "endpoint": self.endpoint, "method": "POST", "trace_context": {}, "diagnostic_context": copy.deepcopy(metadata)}
        started = time.perf_counter()
        # Observation failure happens before send: a diagnostic cannot claim verified bytes it failed to capture.
        self._observe({**common, "record_type": "http_request_prepared", "request_body_utf8": request.content.decode("utf-8"),
                       "payload": copy.deepcopy(payload), "payload_verified": False})
        response = None
        result = None
        try:
            response = self._client.send(request)
            actual_body = response.request.content
            actual_sha = hashlib.sha256(actual_body).hexdigest()
            common.update(request_body_sha256=actual_sha)
            try:
                raw = response.json()
                json_valid = True
            except ValueError:
                raw, json_valid = None, False
            from infra.trace.diagnostics import response_evidence
            result = {**common, "record_type": "http_response_received", **response_evidence(raw),
                      "http_status": response.status_code, "elapsed_ms": (time.perf_counter() - started) * 1000,
                      "http_payload_verified": True, "payload_verified": True,
                      "payload_matches_frozen_request": actual_body == body,
                      "response_json_valid": json_valid}
            if not response.is_success:
                # Error bodies can echo private inputs or credentials. Preserve only
                # whitelisted identity/usage and status, never their message/content.
                result["response_content"] = None
            self._observe({**result, "request_body_utf8": actual_body.decode("utf-8"), "payload": json.loads(actual_body)})
            if not response.is_success:
                raise HTTPReplayFailure(f"provider_http_{response.status_code}", result)
            if not json_valid or not isinstance(raw, dict):
                raise HTTPReplayFailure("provider_response_invalid_json", result)
            if not isinstance(raw.get("choices"), list) or len(raw["choices"]) != 1:
                raise HTTPReplayFailure("provider_response_invalid_choices", result)
            return result
        except Exception as exc:
            failure = exc if isinstance(exc, HTTPReplayFailure) else HTTPReplayFailure(
                "http_transport_error" if response is None else "http_response_evidence_error", result)
            error = {**common, "record_type": "http_request_error", "error_type": type(exc).__name__,
                     "error_code": failure.code, "http_status": response.status_code if response is not None else None,
                     "elapsed_ms": (time.perf_counter() - started) * 1000}
            try:
                self._observe(error)
            except Exception:
                # The coordinator still receives the evidence and stops admission
                # when the observer cannot persist the failure event itself.
                failure = HTTPReplayFailure("http_evidence_sink_failed", result)
            if failure is exc:
                raise
            raise failure from exc
        finally:
            if response is not None:
                response.close()

    def close(self) -> None:
        self._client.close()


class OfflineFakeClient:
    """Mechanism exercise only: outputs cannot count as behavioral evidence."""
    def complete_payload(self, payload: dict[str, Any], *, request_id: str,
                         metadata: dict[str, Any], exact_body: str | None = None) -> dict[str, Any]:
        text = "这是离线合成响应，仅用于验证诊断接线，不是模型实验结果。"
        if metadata["kind"] == "probe":
            text = json.dumps({"owner": "角色", "directions": {}, "trends": {}, "unknown_fields": [], "confirmed_causes": []}, ensure_ascii=False)
        return {"response_content": text, "content_type": "text", "response_model": "offline-fake",
                "response_id": request_id, "finish_reason": "stop", "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "has_refusal": False, "has_tool_calls": False, "elapsed_ms": 0, "http_payload_verified": False}


def _validate_payload(payload: dict[str, Any], spec: DiagnosticRunSpec, *, exact_body: str | None = None) -> int:
    allowed = {"model", "messages", "temperature", "top_p", "max_tokens", "max_completion_tokens", "thinking",
               "reasoning_effort", "response_format", "stop", "frequency_penalty", "presence_penalty", "seed", "n", "stream"}
    if set(payload) - allowed:
        raise ValueError("unsupported_or_unbudgeted_payload_fields")
    if "n" in payload and (type(payload["n"]) is not int or payload["n"] != 1):
        raise ValueError("diagnostics require exactly one completion")
    if "max_tokens" in payload and "max_completion_tokens" in payload:
        raise ValueError("ambiguous_output_token_caps")
    thinking = payload.get("thinking")
    if thinking is not None and (not isinstance(thinking, dict) or set(thinking) != {"type"} or thinking["type"] not in {"enabled", "disabled"}):
        raise ValueError("unsupported_thinking_payload")
    if payload.get("model") != spec.generation.model:
        raise ValueError("request_model_differs_from_frozen_generation_model")
    if payload.get("stream"):
        raise ValueError("streaming is not supported; TTFT must remain null")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or any(not isinstance(message, dict) or not isinstance(message.get("content"), str)
            or set(message) - {"role", "content", "name"} or message.get("role") not in {"system", "developer", "user", "assistant"} for message in messages):
        raise ValueError("diagnostics require complete text-only messages")
    # Include every serialized field/schema, not just message content, plus protocol overhead.
    body = exact_body.encode("utf-8") if exact_body is not None else json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if exact_body is not None and json.loads(exact_body) != payload:
        raise ValueError("exact_body_payload_mismatch")
    if len(body) + 64 * len(messages) > spec.budget.max_input_tokens:
        raise ValueError("request exceeds reserved input-token envelope")
    cap = payload.get("max_tokens", payload.get("max_completion_tokens"))
    if type(cap) is not int or not 1 <= cap <= spec.generation.max_tokens:
        raise ValueError("actual request has no compatible bounded output cap; cannot alter A-prod")
    return cap


def run_experiment(spec: DiagnosticRunSpec, output_dir: Path, *, client: Any | None = None,
                   live: bool = False, budget_path: Path | None = None) -> dict[str, Any]:
    spec = DiagnosticRunSpec.model_validate_json(spec.model_dump_json())
    frozen_hash = digest(spec)
    checks = preflight(spec, live=live)
    if not checks["passed"]:
        raise ValueError("preflight_failed: " + ",".join(checks["problems"]))
    if live:
        if not isinstance(client, DirectHTTPReplayClient):
            raise ValueError("live requires the explicit audited HTTP replay client")
        if client.endpoint != spec.generation.endpoint or client.observer is None:
            raise ValueError("live client endpoint/evidence sink differs from the frozen execution contract")
    elif client is not None and not isinstance(client, OfflineFakeClient):
        raise ValueError("offline runs accept only OfflineFakeClient; HTTP calls require live=True")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError("run output must be new or empty; use another directory, never overwrite evidence")
    output_dir.mkdir(parents=True, exist_ok=True)
    client = client or OfflineFakeClient()
    if budget_path is None:
        budget_path = (Path.cwd() / "eval" / "reports" / "emotional_expression_diagnostics" / "private" / "deepseek-budget.json") if live else output_dir / "offline-budget.json"
    ledger = BudgetLedger(budget_path, spec.budget, spec.price)
    effective_live = live and not getattr(client, "test_transport", False)
    manifest = {"schema_version": "expression-run-v1", "spec_sha256": frozen_hash, "spec": spec.model_dump(mode="json"),
                "live": effective_live, "network_opt_in": live,
                "release_eligible": False, "started_at": datetime.now(UTC).isoformat(),
                "validator_executed": False, "delivery_enabled": False, "memory_writeback_enabled": False,
                "budget_path": str(budget_path.resolve()), "preflight": checks,
                "failure_admission_policy": {"http_transport_evidence_or_identity_error": "stop_on_first",
                                             "consecutive_invalid_responses": _INVALID_RESPONSE_STOP_LIMIT,
                                             "automatic_retries": 0, "drain_in_flight": True}}
    write_json(output_dir / "run-manifest.json", manifest)
    audit = audit_spec(spec)
    write_json(output_dir / "visibility-matrix.json", audit)
    blocked = {(case_id, row["condition"]) for row in audit["unobservable_contrasts"] for case_id in row["case_ids"]}
    work = []
    for case in spec.cases:
        for treatment in treatments(spec, case):
            for repeat in range(spec.repeats):
                work.append((case, treatment, repeat, "generation"))
            if treatment.name in {"B0-values", "B1-defined", "C-verbal"}:
                work.append((case, treatment, 0, "probe"))
    random.Random(spec.seed).shuffle(work)
    started = time.perf_counter()
    counts = {"planned": len(work), "attempted": 0, "completed": 0, "failed": 0, "unobservable_skipped": 0}
    stop_reason = None
    invalid_response_streak = 0
    admission_failed = Event()
    response_models: set[str] = set()
    response_fingerprints: set[str] = set()

    def stop_admission(code: str, record: dict[str, Any]) -> None:
        nonlocal stop_reason
        admission_failed.set()
        if stop_reason is None:
            stop_reason = code
            try:
                ledger.stop_new_requests(code)
            except Exception as exc:
                # No further sends in this run; retain reservations if persistence fails.
                record["budget_stop_error_type"] = type(exc).__name__

    def execute(payload, *, request_id, metadata, exact_body):
        try:
            if live and (client.endpoint != spec.generation.endpoint or client.observer is None):
                raise HTTPReplayFailure("live_client_contract_changed")
            return client.complete_payload(payload, request_id=request_id, metadata=metadata, exact_body=exact_body)
        except Exception:
            # A failed worker closes admission immediately, before the coordinator
            # has finished collecting other independent requests already in flight.
            admission_failed.set()
            raise

    def make_record(sequence, case, treatment, repeat, kind):
        record = {"schema_version": "expression-request-v1", "run_id": spec.run_id, "sequence": sequence,
                  "case_id": case.case_id, "cluster_id": case.cluster_id, "family": case.family, "variant": case.variant,
                  "route": case.route, "repeat": repeat, "kind": kind, "condition": treatment.name,
                  "factor": treatment.factor, "baseline_kind": treatment.baseline_kind,
                  "negative_control": case.negative_control, "release_eligible": False,
                  "information_sha256": treatment.information_sha256, "validator_executed": False,
                  "revision": 0, "ttft_ms": None, "live": effective_live, "network_opt_in": live}
        return record

    # Reserve/settle only on the coordinator thread. Network calls are independent, bounded and stateless.
    cursor, pending = 0, {}
    with ThreadPoolExecutor(max_workers=spec.budget.max_parallel_requests) as pool:
        while (cursor < len(work) and not stop_reason) or pending:
            while not stop_reason and not admission_failed.is_set() and cursor < len(work) and len(pending) < spec.budget.max_parallel_requests:
                if any(future.done() for future in pending):
                    break
                sequence = cursor
                case, treatment, repeat, kind = work[cursor]
                cursor += 1
                record = make_record(sequence, case, treatment, repeat, kind)
                if (case.case_id, treatment.name) in blocked and kind == "generation":
                    counts["unobservable_skipped"] += 1
                    append_jsonl(output_dir / "requests.jsonl", {**record, "status": "unobservable_contrast"})
                    continue
                payload = probe_payload(treatment) if kind == "probe" else copy.deepcopy(treatment.payload)
                request_id = "diag_" + uuid4().hex
                record.update(request_id=request_id, payload_sha256=digest(payload))
                try:
                    if digest(spec) != frozen_hash:
                        raise ValueError("frozen_spec_mutated")
                    cap = _validate_payload(payload, spec, exact_body=treatment.exact_body if kind == "generation" else None)
                    record["reservation_cny"] = ledger.reserve(request_id, max_output_tokens=cap)
                except BudgetStopped as exc:
                    stop_reason = str(exc)
                    append_jsonl(output_dir / "requests.jsonl", {**record, "status": "budget_stopped", "error_code": stop_reason})
                    break
                except ValueError as exc:
                    counts["failed"] += 1
                    append_jsonl(output_dir / "requests.jsonl", {**record, "status": "invalid_request", "error_code": str(exc)})
                    if str(exc) == "frozen_spec_mutated":
                        stop_admission("frozen_spec_mutated", record)
                        break
                    continue
                counts["attempted"] += 1
                future = pool.submit(execute, payload, request_id=request_id,
                                     metadata={key: record[key] for key in ("run_id", "case_id", "condition", "kind", "revision")},
                                     exact_body=treatment.exact_body if kind == "generation" else None)
                pending[future] = (case, record)
            if not pending:
                continue
            finished, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in finished:
                case, record = pending.pop(future)
                request_id = record["request_id"]
                try:
                    result = future.result()
                    record["result"] = result
                    settlement = ledger.settle(request_id, result.get("usage"))
                    content = result.get("response_content")
                    valid = isinstance(content, str) and bool(content.strip()) and result.get("finish_reason") not in {"length", "content_filter"} and not result.get("has_refusal") and not result.get("has_tool_calls") and result.get("payload_matches_frozen_request", True)
                    record.update(status="completed" if valid else "invalid_response", result=result, settlement=settlement,
                                  initial_content=content, final_content=content, content_sha256=digest(content),
                                  final_stage="raw_replay_no_validator")
                    if record["kind"] == "probe":
                        record["probe_evaluation"] = evaluate_probe(case, content or "")
                        if not record["probe_evaluation"]["valid"]:
                            valid = False
                            record["status"] = "invalid_probe_response"
                    if result.get("response_model"):
                        response_models.add(result["response_model"])
                    if result.get("system_fingerprint"):
                        response_fingerprints.add(result["system_fingerprint"])
                    if live:
                        identity_error = (
                            "http_payload_identity_unverified" if not result.get("http_payload_verified") or not result.get("payload_matches_frozen_request") else
                            "provider_response_identity_missing" if not result.get("response_model") else
                            "provider_response_identity_changed" if len(response_models) > 1 or len(response_fingerprints) > 1 else None)
                        if identity_error:
                            valid = False
                            record.update(status="invalid_response", error_code=identity_error)
                            stop_admission(identity_error, record)
                        if result.get("finish_reason") != "stop":
                            valid = False
                            record["status"] = "invalid_response"
                    counts["completed" if valid else "failed"] += 1
                    if settlement.get("provider_cap_violation"):
                        stop_admission("provider_exceeded_reserved_token_cap", record)
                    invalid_response_streak = 0 if valid else invalid_response_streak + 1
                    if live and invalid_response_streak >= _INVALID_RESPONSE_STOP_LIMIT:
                        stop_admission("consecutive_invalid_responses", record)
                except Exception as exc:
                    # HTTP errors may still contain trustworthy usage. Otherwise the
                    # whole reservation stays outstanding; never infer zero billing.
                    evidence = exc.evidence if isinstance(exc, HTTPReplayFailure) else None
                    if evidence is not None:
                        record["result"] = evidence
                    record.update(status="request_error", error_type=type(exc).__name__,
                                  error_code=exc.code if isinstance(exc, HTTPReplayFailure) else "request_execution_failed",
                                  settlement={"status": "unsettled"})
                    if evidence is not None:
                        try:
                            record["settlement"] = ledger.settle(request_id, evidence.get("usage"))
                        except Exception as settlement_error:
                            record["budget_settlement_error_type"] = type(settlement_error).__name__
                    stop_admission(record["error_code"], record)
                    counts["failed"] += 1
                append_jsonl(output_dir / "requests.jsonl", record)
    result = {"schema_version": "expression-run-result-v1", "spec_sha256": frozen_hash, "release_eligible": False,
              "status": "stopped" if stop_reason else "completed_with_errors" if counts["failed"] else "completed",
              "stop_reason": stop_reason, "counts": counts, "elapsed_ms": (time.perf_counter() - started) * 1000,
              "failure_admission_policy": manifest["failure_admission_policy"],
              "budget": ledger.snapshot(), "response_models": sorted(response_models),
              "response_system_fingerprints": sorted(response_fingerprints),
              "backend_identity_drift": len(response_models) > 1 or len(response_fingerprints) > 1,
              "live": effective_live, "network_opt_in": live,
              "behavioral_confirmation": "inconclusive", "completed_at": datetime.now(UTC).isoformat()}
    write_json(output_dir / "run-result.json", result)
    return result
