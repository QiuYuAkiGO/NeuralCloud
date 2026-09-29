"""Ten serial isolated QQ turns, using production cognition and simulated OneBot.

No external QQ connection is constructed. Only --execute enables model HTTP;
--synthetic runs the same store/source/Runtime/Cognitive/delivery path offline.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch


SELF, GROUP, MEMBER = "29999", "10001", "20002"
PROMPTS = (
    "小雾，下午好，今天想和你简单聊两句。",
    "如果给雨后的照片取个短名字，你会选什么？",
    "刚忙完手头的事，我想放松一下，你有什么简单的主意？",
    "窗外的云有点像一只猫，你会怎么看？",
    "你觉得散步时听音乐和安静看风景，各有什么趣味？",
    "我想把书桌整理一下，你建议先从哪里开始？",
    "今天见到一朵很小的花，突然想拍下来。你会怎么构图？",
    "写一句很短的问候，送给刚结束忙碌一天的人吧。",
    "周末在家看书时，怎样安排一个舒服的下午？",
    "谢谢陪我聊了一会儿，用一句轻松的话结束这段聊天吧。",
)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def percentiles(values):
    ordered = sorted(values)
    return {"samples": len(ordered), "p50_ms": ordered[math.ceil(len(ordered) * .50) - 1] if ordered else None,
            "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1] if ordered else None}


def turn_failure_detail(*, current, terminal_reason, successful_reply, domain_failures=()):
    """Keep failure causes separate from Direct's unchanged terminal behavior."""
    from harness.qq.failures import safe_failure_detail

    if successful_reply:
        return {"failure_category": None, "error_code": None}
    # A typed rejection survives a generic ValueError at the Direct bridge.
    for value in (current, current.get("runtime_detail"), *domain_failures):
        if detail := safe_failure_detail(value):
            return detail
    detail = current.get("runtime_detail")
    decision = detail.get("decision") if isinstance(detail, dict) else None
    decision = decision if isinstance(decision, dict) else {}
    legacy = {
        "deadline_expired": ("timeout", "qq_turn_expired"),
        "expired": ("timeout", "qq_turn_expired"),
        "model_timeout": ("timeout", "qq_model_timeout"),
        "TimeoutError": ("timeout", "qq_model_timeout"),
        "ReadTimeout": ("timeout", "qq_model_timeout"),
        "QQDeadlineExpired": ("timeout", "qq_turn_expired"),
        "model_http": ("model_error", "qq_model_http_error"),
        "model_invalid": ("model_invalid", "qq_model_response_invalid"),
        "model_error": ("model_error", "qq_model_error"),
        "qq_transport_error": ("transport_error", "qq_transport_error"),
        "shared_cognitive_unavailable": ("transport_error", "shared_cognitive_unavailable"),
    }
    # Inspect only fixed telemetry categories/type names, never error prose.
    for value in (decision.get("error_type"), current.get("error_type"),
                  current.get("decision_outcome"), terminal_reason):
        if type(value) is str and value in legacy:
            category, code = legacy[value]
            return {"failure_category": category, "error_code": code}
    if current.get("error_type") or current.get("runtime_status") in {"failed", "rejected"}:
        return {"failure_category": "internal_error", "error_code": "qq_turn_internal_error"}
    return {"failure_category": None, "error_code": None}


def failure_counts(rows):
    return dict(Counter(row["failure_category"] for row in rows if row.get("failure_category")))


async def drain_commands(host):
    # Runtime can time out its shielded domain command while its worker still
    # owns the model call. Await the whole command, including gaps between
    # appraisal/gate/body calls, before checking transport receipts.
    while tasks := tuple(task for task in host.command_tasks if not task.done()):
        await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
    while host.adapter.production_meter.spool.status()["in_flight"]:
        await asyncio.sleep(.1)


class SimulatedOneBot:
    """Only an in-process receipt fixture; never a socket or QQ account."""
    def __init__(self):
        self.sent = {}
        self.closed = asyncio.Event()
        self.last_error_type = None

    async def call(self, action, params=None, timeout=10):
        params = params or {}
        if action == "get_login_info":
            return {"user_id": int(SELF), "nickname": "Misty"}
        if action == "get_status":
            return {"online": True, "good": True}
        if action == "get_group_list":
            return [{"group_id": int(GROUP), "group_name": "isolated synthetic performance group"}]
        if action == "get_group_member_info":
            return params | {"nickname": "Misty" if str(params.get("user_id")) == SELF else "测试用户", "card": ""}
        if action == "send_group_msg":
            identifier = 90000 + len(self.sent)
            self.sent[str(identifier)] = {"message_id": identifier, "group_id": params["group_id"],
                "sender": {"user_id": int(SELF)}, "message": params["message"]}
            return {"message_id": identifier}
        if action == "get_msg":
            return self.sent[str(params["message_id"])]
        if action == "get_group_msg_history":
            return {"messages": []}
        raise ValueError("unsupported_simulated_protocol_action")

    async def close(self):
        self.closed.set()


def manifest(model):
    from harness.research.manifest import ExperimentManifest
    from harness.production.config import ProductionConfig
    return ExperimentManifest.model_validate({
        "experiment_id": "qq-performance-isolated", "revision": 1, "control_mode": "development_debug",
        "condition": "llm_top3", "persona_version": "misty-builtin", "homeostatic_profile": "passive-v1",
        "heartbeat_seconds": 60, "policy_horizon": 3, "random_seed": 7,
        "initial_snapshot_ref": "synthetic://qq-performance-empty", "model_version": model,
        "prompt_version": "production-evidence-v2", "schema_version": "production-memory-v1",
        "a_version": "A-v1", "b_version": "B-v1", "c_version": "C-v1", "precision_version": "precision-v1",
        "coupling_version": "coupling-v1", "priority_policy": "runtime-v1", "environment": "isolated-performance",
        "retention": "synthetic-only", "watchdog_action": "pause_preserve_state",
        "production": ProductionConfig(autonomous_enabled=False).model_dump(mode="json")})


def model_client(settings, synthetic):
    from harness.llm.client import ChatResult, MockLLM, build_llm_client
    from harness.qq.decision import is_qq_gate_messages
    class Scripted(MockLLM):
        def __init__(self):
            super().__init__(chat_routes={"": "嗯，先从一个轻松的小步骤开始吧。"})
            self.synthetic_calls = 0

        def complete_chat_result(self, messages, **kwargs):
            self.synthetic_calls += 1
            if any("ProductionAppraisal" in str(row.get("content")) for row in messages):
                return ChatResult(text='{"memory_updates":[],"affect":null}', finish_reason="stop", content_type="text")
            if is_qq_gate_messages(messages):
                return ChatResult(text='{"action":"speak","reason":"回应直接对话"}', finish_reason="stop", content_type="text")
            return super().complete_chat_result(messages, **kwargs)
    return Scripted() if synthetic else build_llm_client(settings)


class LocalMeter:
    def __init__(self, directory, runtime):
        from harness.production.metering import ProductionUsageSpool
        self.spool = ProductionUsageSpool(directory / "usage.sqlite3")
        self.runtime = runtime

    def reserve_attempt(self, attempt_id, *, purpose, autonomous):
        if autonomous:
            raise ValueError("performance_autonomy_forbidden")
        return self.runtime.production_runtime.store.reserve_attempt(attempt_id, purpose=purpose, autonomous=False)

    def begin_attempt(self, attempt_id, usage):
        self.spool.begin(attempt_id, usage)

    def checkpoint_usage(self, attempt_id, usage):
        self.spool.checkpoint(attempt_id, usage)

    def record_usage(self, attempt_id, usage):
        self.spool.finish(attempt_id, usage)

    def context(self, scope="owner", **kwargs):
        return self.runtime.production_runtime.context_snapshot(scope, autonomous=False)


async def run(args):
    from harness.cognitive.service import CognitiveServiceHost
    from harness.cognitive.shared_memory import SharedMemoryCompiler
    from harness.config import Settings
    from harness.qq.policy import QQLatencyOptions
    from harness.qq import decision
    from harness.qq.direct import DirectQQActor
    from harness.qq.direct_config import DirectQQSettings
    from harness.qq.session import QQSessionSettings
    from harness.qq.shared import QQSharedPrepareRequest, QQSharedTraceOrigin, _import_remote_trace
    from harness.runtime.adapters import DomainSnapshot
    from harness.runtime.models import RuntimeEvent, RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle
    from harness.runtime.supervisor import RuntimeSupervisor
    from infra.trace.diagnostics import observation_scope

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    # All runtime paths and NC settings are process-local. Never load the live
    # manifest, state, QQ credentials or a running background service.
    for key in list(os.environ):
        if key.startswith("NC_"):
            os.environ.pop(key)
    os.environ.update(NC_MODE="demo", NC_ISOLATED_DEVELOPMENT="1", NC_CONTROL_MODE="development_debug",
                      NC_SHARED_MEMORY_DB=str(output / "shared-memory.sqlite3"),
                      NC_CALL_LOG_DIR=str(output / "calls"))
    settings = (decision._load_configuration(args.config, args.source_root / "presets/builtin/misty.json")[0]
                if args.execute else Settings(llm_model="scripted-offline", validator_enabled=False))
    settings.production_memory_enabled = True
    settings.aux_llm_preload = False
    settings.call_log_enabled = False
    settings.trace_dir = output / "traces"
    settings.qq_latency_conversations = [SELF + "/" + GROUP]
    if args.synthetic:
        settings.validator_enabled = False
    # The real QQ facade itself disables interaction recording/continuation.
    # No maintenance worker, network embedding preload or autonomy is started.
    token = output / "synthetic.token"
    token.write_text("isolated-synthetic-token-" + "1" * 32, encoding="utf-8")
    token.chmod(0o600)
    session = QQSessionSettings(memory_db=output / "qq.sqlite3", token_file=token,
        allowed_group_ids=[GROUP], self_member_id=SELF)
    session_path = output / "session.json"
    write_json(session_path, session.model_dump(mode="json"))
    write_json(output / "direct-control.json", {"paused": False, "stop": False})
    write_json(output / "desktop-status.json", {"running": False, "heartbeat_at": datetime.now(timezone.utc).isoformat()})
    direct_settings = DirectQQSettings(enabled=True, token_file=token, self_member_id=SELF,
        group_roster=[{"group_id": GROUP, "label": "synthetic", "observe": True, "send": True}],
        session_config_path=session_path, state_db=output / "direct.sqlite3", status_file=output / "direct-status.json",
        control_file=output / "direct-control.json", evidence_dir=output / "protocol-evidence",
        image_cache_dir=output / "images", desktop_status_file=output / "desktop-status.json",
        shared_control_token_file=token, qq_image_mode="off", qq_reply_scheduler="legacy")
    write_json(output / "direct.json", direct_settings.model_dump(mode="json"))
    os.environ["NC_QQ_SESSION_CONFIG"] = str(session_path)
    os.environ["NC_QQ_DIRECT_CONFIG"] = str(output / "direct.json")
    with patch("gui.service.probe_llm", return_value=False):
        host = CognitiveServiceHost(config_path=output / "gui.json", presets_dir=output / "presets",
                                    state_path=output / "cognitive.json")
    client = model_client(settings, args.synthetic)
    host.chat.settings = host.chat.pipeline.settings = settings
    host.chat.llm = host.chat.pipeline.llm = client
    host.chat.pipeline.compiler = SharedMemoryCompiler(settings)
    runtime = RuntimeSupervisor(manifest=manifest(settings.llm_model), cognitive=host, homeostasis=object(),
                                state_dir=output / "runtime")
    runtime.lifecycle = RuntimeLifecycle.RUNNING
    host.adapter.production_meter = LocalMeter(output, runtime)
    runtime._cognitive_snapshot = await host.get_snapshot()
    runtime._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=0, payload={})
    loop = asyncio.get_running_loop()
    current = {}
    http_events = []
    http_path = output / "http.jsonl"
    http_path.touch(mode=0o600)

    def capture(value):
        http_events.append(value)
        with http_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
            stream.flush()

    async def cognitive(request):
        now = datetime.now(timezone.utc)
        event = RuntimeEvent(event_id="perf-turn:" + str(current["index"]), receive_seq=current["index"],
            correlation_id=request.trace.trace_id, source="qq_body", event_type=RuntimeEventType.QQ_TURN,
            priority=RuntimeEventPriority.USER, source_time=now, received_at=now,
            idempotency_key="perf-turn:" + str(current["index"]), manifest_revision=runtime.manifest.revision,
            payload=request.model_dump(mode="json"))
        started = time.perf_counter()
        try:
            with observation_scope(capture):
                result = await runtime._prepare_qq_turn(event, current["index"], request)
            current["runtime_status"] = result.status
            current["runtime_detail"] = result.detail
            if result.status != "completed" or not isinstance(result.detail, dict) or "decision" not in result.detail:
                raise ValueError("isolated_runtime_turn_failed")
            current["decision_outcome"] = result.detail["decision"].get("outcome")
            return result.detail
        except Exception as exc:
            from harness.qq.failures import safe_failure_detail
            current.update(safe_failure_detail(getattr(exc, "failure_detail", None)))
            current["error_type"] = type(exc).__name__
            raise
        finally:
            current["cognitive_ms"] = round((time.perf_counter() - started) * 1000, 3)

    def bridge(*, deadline, trace, **kwargs):
        request = QQSharedPrepareRequest(**kwargs, trace=QQSharedTraceOrigin(
            trace_id=trace.trace_id, event_id=trace.event_id, first_seen_at=trace.first_seen_at,
            first_seen_mono=trace.first_seen_mono), deadline_at=min(
                datetime.now(timezone.utc) + timedelta(seconds=deadline.check()),
                trace.first_seen_at + timedelta(seconds=60)))
        started = trace.clock()
        # Direct actor runs this in a worker. Wait to actual completion; no
        # cancellation wrapper leaves a model call running into the next turn.
        payload = asyncio.run_coroutine_threadsafe(cognitive(request), loop).result()
        ended = trace.clock()
        _import_remote_trace(trace, payload["trace"], started=started, ended=ended)
        result = decision.QQReplyDecision.model_validate(payload["decision"])
        trace.bind_decision(result.decision_id)
        return result

    actor = DirectQQActor(direct_settings, cognition=bridge)
    actor.client = SimulatedOneBot()
    actor.connected = True
    await actor.verify_connection()
    provenance = {"schema_version": "qq-latency-ten-v1", "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": "synthetic" if args.synthetic else "live-model-simulated-qq", "model": settings.llm_model,
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest() if args.execute else None,
        "source_root": str(args.source_root), "turns": 10, "warmup_model_calls": 0,
        "simulation_only": True, "external_qq_messages": 0,
        "percentile_method": "nearest-rank (n=10: p50=5th, p95=10th)",
        "boundaries": {"total": "Direct on_event ingress through durable simulated protocol receipt",
            "cognitive": "Runtime _prepare_qq_turn including real Cognitive prepare/resume; in-process transport",
            "body": "unavailable/unknown, user-triggered restricted dialogue", "memory": "fresh isolated SQLite; progressive same session",
            "excluded": ["real QQ network", "cross-process HTTP RPC", "background maintenance", "autonomous work", "external embedding"]},
        "policy_overrides": {"production_memory_enabled": True, "aux_llm_preload": False,
            "call_log_enabled": False, "direct_image_mode": "off", "direct_reply_scheduler": "legacy",
            "qq_latency_conversations": [SELF + "/" + GROUP]},
        "effective_policy": QQLatencyOptions.from_settings(settings).for_conversation(group_id=GROUP, self_member_id=SELF).model_dump(),
        "source_sha256": {str(path.relative_to(args.source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for base in ("harness", "gui") for path in sorted((args.source_root / base).rglob("*.py"))},
        "prompts": list(PROMPTS)}
    write_json(output / "manifest.json", provenance)
    rows = []
    try:
        for index, prompt in enumerate(PROMPTS, 1):
            spacing_started = time.perf_counter()
            while actor.store.cooldown(GROUP, 10):
                await asyncio.sleep(.1)
            cooldown_wait = round((time.perf_counter() - spacing_started) * 1000, 3)
            current.clear()
            current["index"] = index
            before = len(http_events)
            prior_commands = set(host.adapter._ledger)
            scripted_before = getattr(client, "synthetic_calls", 0)
            started = time.perf_counter()
            payload = {"post_type": "message", "message_type": "group", "self_id": int(SELF),
                "group_id": int(GROUP), "user_id": int(MEMBER), "message_id": index,
                "time": datetime.now(timezone.utc).timestamp(), "sender": {"user_id": int(MEMBER), "nickname": "测试用户"},
                "message": [{"type": "at", "data": {"qq": SELF}}, {"type": "text", "data": {"text": prompt}}]}
            await actor.on_event(payload)
            pending = actor.store.pending(actor.run_id)
            if pending is None:
                raise RuntimeError("isolated_input_not_queued")
            await actor.process(pending)
            total = round((time.perf_counter() - started) * 1000, 3)
            drain_started = time.perf_counter()
            await drain_commands(host)
            drain_wait = round((time.perf_counter() - drain_started) * 1000, 3)
            saved = actor.store.get(pending["event_id"])
            trace = json.loads(direct_settings.state_db.with_suffix(".trace.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            events = http_events[before:]
            counters = trace.get("counters", {})
            from harness.qq.failures import safe_failure_detail
            domain_failures = [safe_failure_detail(result.result) for key, result in host.adapter._ledger.items()
                if key not in prior_commands and result.error is not None]
            domain_failures = [value for value in domain_failures if value]
            row = {"index": index, "status": saved["state"], "terminal_reason": saved.get("terminal_reason"),
                "decision_outcome": current.get("decision_outcome"), "total_ms": total,
                "cooldown_wait_ms": cooldown_wait, "post_result_model_drain_ms": drain_wait,
                "error_type": current.get("error_type"),
                "cognitive_ms": current.get("cognitive_ms"), "runtime_status": current.get("runtime_status"),
                "successful_reply": saved["state"] == "sent" and current.get("decision_outcome") == "ready",
                "first_simulated_verified_ms": counters.get("protocol_first_verified_segment_ms"),
                "fully_simulated_verified_ms": counters.get("protocol_fully_verified_ms"),
                "http_requests": sum(event.get("record_type") == "http_request_prepared" for event in events),
                "response_models": sorted({event["response_model"] for event in events if event.get("response_model")}),
                "model_usage": [event.get("usage") for event in events if event.get("usage")],
                "synthetic_calls": getattr(client, "synthetic_calls", 0) - scripted_before,
                "domain_errors": [result.error for key, result in host.adapter._ledger.items()
                    if key not in prior_commands and result.error is not None],
                "domain_failures": domain_failures,
                "stages": trace.get("stages", []), "runtime_detail": current.get("runtime_detail")}
            row.update(turn_failure_detail(current=current, terminal_reason=row["terminal_reason"],
                successful_reply=row["successful_reply"], domain_failures=domain_failures))
            write_json(output / f"turn-{index:02d}.json", row)
            rows.append(row)
            print(json.dumps({key: row[key] for key in ("index", "status", "successful_reply", "decision_outcome", "total_ms", "cognitive_ms", "http_requests")}), flush=True)
        successful = [row for row in rows if row["successful_reply"]]
        summary = {"schema_version": "qq-latency-ten-v1", "mode": provenance["mode"], "model": settings.llm_model,
            "diagnostic_schema_version": "qq-failure-diagnostics-v1",
            "simulation_only": True, "attempts": len(rows), "successful_replies": len(successful),
            "outcomes": dict(Counter(row["decision_outcome"] or row["terminal_reason"] for row in rows)),
            "failure_categories": failure_counts(rows),
            "http_requests": sum(row["http_requests"] for row in rows), "percentile_method": provenance["percentile_method"],
            "all_attempts_total": percentiles([row["total_ms"] for row in rows]),
            "successful_reply_total": percentiles([row["total_ms"] for row in successful]),
            "all_attempts_cognitive": percentiles([row["cognitive_ms"] for row in rows if row["cognitive_ms"] is not None]),
            "successful_reply_cognitive": percentiles([row["cognitive_ms"] for row in successful]),
            "rows": rows, "boundaries": provenance["boundaries"]}
        write_json(output / "summary.json", summary)
    finally:
        await host.close()
        close = getattr(client, "close", None)
        if callable(close):
            close()
        await actor.client.close()
        runtime.instance_lock.release()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--synthetic", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    args.source_root = args.source_root.resolve()
    if args.execute and (args.config is None or not args.config.is_file()):
        parser.error("--execute requires an existing --config")
    sys.path.insert(0, str(args.source_root))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = asyncio.run(run(args))
    print(json.dumps({"summary": str(args.output.resolve() / "summary.json"),
                      "attempts": result["attempts"], "successful_replies": result["successful_replies"]}), flush=True)


if __name__ == "__main__":
    main()
