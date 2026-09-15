"""Bounded real capability acceptance using isolated cognition and homeostasis.

Default preflight is read-only and never starts services. Explicit ``board`` or
``desktop`` mode permits one live model-owned opportunity on an already running
body. A valid wait remains a wait. This tool never starts a QQ watcher or sends
through QQ; its before/after send-ledger count must remain identical.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, contextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def fingerprint(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def listening(port):
    with socket.socket() as connection:
        connection.settimeout(0.3)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def qq_send_count(launch):
    watcher = read_json(launch["NC_QQ_WATCHER_CONFIG"])
    path = Path(watcher["state_db"]).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        return database.execute("SELECT count(*) FROM qq_watch_sends").fetchone()[0]


def preflight(launch):
    from harness.board.config import BoardSettings

    gui = read_json(launch["NC_GUI_CONFIG"])
    board = BoardSettings.model_validate(read_json(launch["NC_BOARD_CONFIG"]))
    return {"mode": "preflight", "model": gui.get("model"), "model_mode": gui.get("mode"),
        "active_preset_id": gui.get("active_preset_id"), "thinking_override": False,
        "ports": {str(port): listening(port) for port in (8770, 8771, 8772, 8787, 8790, 8793)},
        "board": {"body_instance_id": board.body_instance_id, "board_id": board.board_id,
                  "model_max_tokens": board.model_max_tokens},
        "qq_send_count": qq_send_count(launch), "services_started": False, "model_calls": 0}


@contextmanager
def environment(values):
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def isolated_owner(launch, destination):
    """Copy personality/memory, keep credentials only in process environment."""
    gui = read_json(launch["NC_GUI_CONFIG"])
    if gui.get("mode") != "live":
        raise ValueError("saved_live_model_required")
    active = gui.get("active_preset_id", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", active):
        raise ValueError("invalid_active_preset_identity")
    preset = Path(launch["NC_GUI_PRESETS"]) / f"{active}.json"
    if not preset.is_file() or preset.is_symlink():
        raise ValueError("saved_persona_required")
    (destination / "presets").mkdir()
    shutil.copyfile(preset, destination / "presets" / preset.name)
    gui_path = destination / "gui.json"
    credentials = {}
    for field, variable in (("api_key", "NC_LLM_API_KEY"), ("aux_api_key", "NC_AUX_LLM_API_KEY")):
        value = gui.pop(field, None)
        if value:
            credentials[variable] = value
    gui_path.write_text(json.dumps(gui, ensure_ascii=False), encoding="utf-8")
    source = Path(launch["NC_SHARED_MEMORY_DB"]).resolve()
    copied = destination / "shared-memory.sqlite3"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as before:
        with closing(sqlite3.connect(copied)) as after:
            before.backup(after)
    values = {"NC_CONTROL_MODE": "development_debug", "NC_ISOLATED_DEVELOPMENT": "1", "NC_MODE": gui["mode"],
        "NC_QQ_SESSION_CONFIG": None, "NC_QQ_WATCHER_CONFIG": None,
        "NC_RUNTIME_MANIFEST_PATH": None, "NC_COGNITIVE_MANIFEST_PATH": None, "NC_EXPERIMENT_MANIFEST": None,
        "NC_SHARED_MEMORY_DB": copied, "NC_BOARD_CONFIG": launch["NC_BOARD_CONFIG"],
        "NC_GUI_CONFIG": gui_path, "NC_GUI_PRESETS": destination / "presets",
        "NC_TRACE_DIR": destination / "traces", "NC_CALL_LOG_ENABLED": "true", **credentials}
    return gui_path, values, preset


async def live_opportunity(args, launch, destination):
    from harness.board.config import BoardSettings
    from harness.cognitive.service import CognitiveServiceHost
    from harness.embodiment.capabilities import CapabilityRequirement
    from harness.embodiment.manager import EmbodimentManager
    from harness.embodiment.providers.board import BOARD_CAPABILITIES, BoardProvider
    from harness.embodiment.providers.mistyai import (MistyAIActionBackend, MistyAICapabilityProvider,
        MistyAIClient, MistyAIPerceptionBackend)
    from harness.homeostasis import HomeostasisEngine, load_profile
    from harness.memory.shared import SharedReadScope
    from harness.research.manifest import AutonomousCapabilitiesCondition, EmbodimentCondition, load_experiment_manifest
    from harness.runtime.action import ActionCoordinator
    from harness.runtime.board import BoardController
    from harness.runtime.models import RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle, utc_now
    from harness.runtime.perception import PerceptionGuard
    from harness.runtime.supervisor import RuntimeSupervisor

    if not listening(8793 if args.mode == "board" else 8787):
        raise RuntimeError("selected_body_not_running")
    gui_path, values, preset = isolated_owner(launch, destination)
    protected = [Path(launch["NC_GUI_CONFIG"]), preset,
        Path(launch["NC_HOMEOSTASIS_STATE_DIR"]) / "snapshot.json",
        Path(launch["NC_HOMEOSTASIS_STATE_DIR"]) / "command-ledger.sqlite3"]
    hashes = {str(path): fingerprint(path) for path in protected}
    before_qq = qq_send_count(launch)
    report = {"mode": args.mode, "isolation": str(destination), "opportunities": 0,
        "started_at": utc_now().isoformat(), "thinking_override": False, "qq_send_before": before_qq,
        "simulation_boundaries": ["elapsed heartbeat source times replayed from latest observed state, independent of wall clock", "initial quiet timer elapsed",
                                  "private copies of personality and shared memory", "fresh isolated runtime and homeostasis"]}
    host, supervisor, desktop_client = None, None, None
    try:
        with environment(values):
            host = CognitiveServiceHost(config_path=gui_path, presets_dir=destination / "presets",
                                        state_path=destination / "cognitive.json")
            report.update(model=host.chat.settings.llm_model, persona_sha256=hashlib.sha256(
                host.chat.memory.get_persona().model_dump_json().encode()).hexdigest())
            started = utc_now() - timedelta(hours=8)
            profile = load_profile(ROOT / "presets/homeostasis/qq-social-drive-v1.json")
            engine = HomeostasisEngine(profile, state_dir=destination / "homeostasis", now=started, clock=utc_now)
            base = load_experiment_manifest(ROOT / "presets/runtime/shared-misty-v1.json")
            channels = {"board": "off", "qq": "off", "desktop": "off"}
            channels[args.mode] = "shared"
            allowed = (("expression.board.read.v1", "expression.board.append.v1") if args.mode == "board" else
                       ("app.open.v1", "control.observe.v1"))
            changes = {"control_mode": "development_debug", "autonomous_chat_enabled": False,
                "autonomous_capabilities": AutonomousCapabilitiesCondition(enabled=True, owners=channels,
                    allowed_capabilities=allowed, qq_send_enabled=False)}
            kwargs = {}
            if args.mode == "desktop":
                token = (args.desktop_token_file.read_text().strip() if args.desktop_token_file else
                         os.environ.get("NC_ACCEPTANCE_DESKTOP_TOKEN", "").strip())
                if not token:
                    raise ValueError("desktop_token_required")
                desktop_client = MistyAIClient("http://127.0.0.1:8787", token=token)
                token = None
                native = await desktop_client.capabilities()
                # This acceptance targets the existing isolated Linux body,
                # never a native Windows desktop or QQ session.
                if native.body_instance_id != "misty-body-01":
                    raise ValueError("isolated_body_identity_mismatch")
                report["desktop_declared_skills"] = native.skills
                changes["embodiment"] = EmbodimentCondition(mode="observe", body_provider="mistyai",
                    body_instance_id=native.body_instance_id, capability_requirements=[CapabilityRequirement(capability=name,
                        required=True) for name in ("action.execute.v1", "action.verify.v1", "observation.snapshot.v1", "evidence.reference.v1")])
                manager = EmbodimentManager(changes["embodiment"], MistyAICapabilityProvider(desktop_client))
                kwargs = {"embodiment_manager": manager, "action_coordinator": ActionCoordinator(
                    MistyAIActionBackend(desktop_client), capability_check=manager.supports)}
            manifest = base.model_copy(update=changes)
            supervisor = RuntimeSupervisor(manifest=manifest, cognitive=host, homeostasis=engine,
                                            state_dir=destination / "runtime", maintenance_drain_seconds=25, **kwargs)
            report["isolated_shutdown_drain_seconds"] = 25
            if desktop_client is not None:
                supervisor.attach_perception_guard(PerceptionGuard(MistyAIPerceptionBackend(desktop_client),
                    supervisor.submit_event, interval_seconds=60))
            settings = BoardSettings.from_environment()
            supervisor.board = None
            if args.mode == "board":
                provider = BoardProvider(EmbodimentCondition(mode="grounded", body_instance_id=settings.body_instance_id,
                    capability_requirements=[CapabilityRequirement(capability=name, required=True) for name in BOARD_CAPABILITIES]),
                    settings.client(), settings.scope)
                supervisor.board = BoardController(supervisor, settings, destination / "runtime", provider=provider)
                append_call = provider.append

                async def observed_append(request, *, pre_effect_guard=None):
                    try:
                        return await append_call(request, pre_effect_guard=pre_effect_guard)
                    except Exception as exc:
                        code = getattr(exc, "code", None)
                        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,96}", code):
                            report["board_append_error_code"] = code
                        raise

                provider.append = observed_append
            memory_scope = SharedReadScope(audience="local:main", kinds=("board_entry",))
            memory_before = len(host.shared.store.query(memory_scope).records)
            await supervisor.boot_and_recover()
            supervisor.scheduler.suspend()
            if supervisor._perception_guard:
                await supervisor._perception_guard.stop()
            while (queued := await supervisor.queue.get(timeout=0)) is not None:
                await supervisor.dispatch(queued)
            if supervisor.lifecycle != RuntimeLifecycle.RUNNING:
                raise RuntimeError("isolated_runtime_not_running")
            supervisor.capabilities.last_activity = time.monotonic() - 61
            if supervisor.board:
                supervisor.board.last_activity = time.monotonic() - 61
                supervisor.board.next_opportunity = 0
            initial_social = engine.snapshot.dimensions["social_connection"].value
            # A real initial perception can advance the state beyond its birth
            # time. Replay elapsed ticks from that observed state; stale source
            # times would be clamped and would never produce natural drift.
            # The live perception guard may finish its initial capture just
            # after this queue drain. Start at current wall time as well so that
            # the next +90s tick cannot be older than that in-flight capture.
            drift_origin = max(utc_now(), engine.snapshot.updated_at)
            pending = None
            for step in range(1, 241):
                await supervisor.submit_event(event_type=RuntimeEventType.HEARTBEAT,
                    priority=RuntimeEventPriority.HEARTBEAT, source="isolated-natural-elapsed-replay",
                    source_time=drift_origin + timedelta(seconds=step * profile.max_heartbeat_step_seconds))
                while (queued := await supervisor.queue.get(timeout=0)) is not None:
                    if queued.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY:
                        if pending is not None:
                            raise RuntimeError("multiple_isolated_opportunities")
                        pending = queued
                        continue
                    result = await supervisor.dispatch(queued)
                    if result.status != "completed":
                        raise RuntimeError("isolated_event_failed")
                if pending is not None:
                    break
            else:
                raise RuntimeError("no_ready_capability_after_natural_drift")
            report["natural_drift"] = {"source_time_origin": drift_origin.isoformat(), "heartbeat_count": step, "elapsed_seconds": step * profile.max_heartbeat_step_seconds,
                "initial_social": initial_social, "final_social": engine.snapshot.dimensions["social_connection"].value,
                "summary": supervisor._response_homeostatic_interoception()}
            event = pending
            report["opportunities"] = 1
            opportunity_id = event.payload["opportunity_id"]
            await supervisor.dispatch(event)
            if supervisor._action_tasks:
                await asyncio.wait_for(asyncio.gather(*list(supervisor._action_tasks.values())), timeout=65)
            while (queued := await supervisor.queue.get(timeout=0)) is not None:
                await supervisor.dispatch(queued)
            await supervisor.capabilities.reconcile()
            row = supervisor.capabilities.calls.get(opportunity_id)
            report["opportunity"] = {key: row.get(key) for key in ("opportunity_id", "status", "event_id", "action_id", "entry_id", "error_type")}
            choice = (row.get("decision") or {}).get("choice") or {}
            offer = row.get("offer") or {}
            report["selected"] = {"kind": choice.get("kind"), "offer_id": choice.get("offer_id"),
                **{key: offer.get(key) for key in ("channel", "capability_id", "provider_id", "body_instance_id")}}
            report["selection_timing"] = row.get("timing", {})
            report["observation_evidence_refs"] = row.get("evidence_refs", [])
            if row.get("outcome"):
                report["outcome_evidence_refs"] = row["outcome"].get("evidence_refs", [])
            if desktop_client is not None and row.get("action_id"):
                token = (args.desktop_token_file.read_text().strip() if args.desktop_token_file else
                         os.environ.get("NC_ACCEPTANCE_DESKTOP_TOKEN", "").strip())
                independent = MistyAIClient("http://127.0.0.1:8787", token=token)
                token = None
                try:
                    identity = await independent.capabilities()
                    if identity.body_instance_id != "misty-body-01":
                        raise RuntimeError("independent_body_identity_mismatch")
                    # get_action verifies every returned evidence hash through
                    # this fresh client's empty cache. Never re-execute an action.
                    observed = await independent.get_action(row["action_id"])
                    report["independent_desktop_readback"] = {"action_id": observed.action_id, "state": observed.state,
                        "success": observed.result.success if observed.result else None,
                        "unknown": observed.result.unknown if observed.result else None,
                        "evidence_refs": observed.result.evidence_refs if observed.result else [],
                        "evidence_sha256_verified": bool(observed.result and observed.result.evidence_refs)}
                    view = await independent.observe()
                    target_app = choice.get("arguments", {}).get("app_id")
                    windows = [window for window in view.windows if target_app and window.app_id == target_app and window.visible]
                    report["independent_desktop_observation"] = {"observation_id": view.observation_id,
                        "captured_at": view.captured_at.isoformat(), "after_action_result": view.captured_at >= observed.updated_at,
                        "body_instance_id": view.body_instance_id, "target_app_id": target_app,
                        "target_app_present": bool(windows),
                        "target_app_running": any(app.app_id == target_app for app in view.running_apps),
                        "target_windows": [{"window_id": window.window_id, "app_id": window.app_id,
                            "visible": window.visible, "focused": window.focused} for window in windows],
                        "frame_evidence_ref": view.frame.evidence_ref, "frame_sha256": view.frame.sha256,
                        "evidence_sha256_verified": True}
                finally:
                    await independent.close()
            if row.get("entry_id") and supervisor.board:
                turn = supervisor.board.turns.get(row["entry_id"])
                report["board_turn"] = {key: turn.get(key) for key in ("status", "receipt", "timing", "error_type", "error_code")}
                if turn.get("receipt"):
                    from harness.embodiment.board_contracts import BoardEvidenceRequest
                    client = settings.client()
                    try:
                        entry = await client.evidence(BoardEvidenceRequest(scope=settings.scope, entry_id=row["entry_id"],
                            requested_at=utc_now(), deadline=utc_now() + timedelta(seconds=5)))
                        report["independent_board_readback"] = entry.receipt.model_dump(mode="json")
                    finally:
                        await client.close()
            report["isolated_memory_delta"] = len(host.shared.store.query(memory_scope).records) - memory_before
            observational = offer.get("capability_id") in {"control.wait.v1", "control.observe.v1", "expression.board.read.v1"}
            report["effect_verified"] = bool(report.get("independent_board_readback") or
                (not observational and row["status"] == "completed" and report.get("outcome_evidence_refs")
                 and report.get("independent_desktop_readback", {}).get("success")
                 and report.get("independent_desktop_readback", {}).get("evidence_sha256_verified")
                 and report.get("independent_desktop_observation", {}).get("target_app_present")
                 and report.get("independent_desktop_observation", {}).get("after_action_result")))
            report["body_observation_verified"] = bool(report.get("observation_evidence_refs") or
                (observational and row["status"] == "completed" and report.get("outcome_evidence_refs")))
            report["acceptance"] = ("effect_verified" if report["effect_verified"] else
                "body_observation_verified" if report["body_observation_verified"] else
                "valid_wait_effect_pending" if row["status"] in {"wait", "defer", "no_action"} else row["status"])
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        code = getattr(exc, "code", str(exc))
        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,96}", code):
            report["error_code"] = code
        report["acceptance"] = "failed"
    finally:
        cleanup = []
        if supervisor is not None:
            cleanup.append(supervisor.stop)
            if supervisor.board:
                cleanup.append(supervisor.board.close)
        elif host is not None:
            cleanup.append(host.close)
        if desktop_client is not None:
            cleanup.append(desktop_client.close)
        for close in cleanup:
            try:
                await close()
            except Exception as exc:
                report.setdefault("cleanup_error_types", []).append(type(exc).__name__)
        report["qq_send_after"] = qq_send_count(launch)
        report["qq_zero_new_sends"] = report["qq_send_after"] == before_qq
        report["production_sources_unchanged"] = all(fingerprint(path) == old for path, old in hashes.items())
        if not report["qq_zero_new_sends"] or not report["production_sources_unchanged"] or report.get("cleanup_error_types"):
            report["acceptance"] = "failed"
        report["completed_at"] = utc_now().isoformat()
        (destination / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "board", "desktop"), default="preflight")
    parser.add_argument("--launch-config", type=Path, default=ROOT / "data/shared-misty-launch.json")
    parser.add_argument("--desktop-token-file", type=Path)
    args = parser.parse_args()
    launch = read_json(args.launch_config)
    if args.mode == "preflight":
        result = preflight(launch)
    else:
        destination = Path(tempfile.mkdtemp(prefix="neuralcloud-capability-acceptance-"))
        result = asyncio.run(live_opportunity(args, launch, destination))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("acceptance") in {"failed", "error", "uncertain", "expired"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
