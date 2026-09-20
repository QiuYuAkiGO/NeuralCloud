"""Tier1 isolated Runtime: real wiring, no production mutation, no QQ sends."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from tools.selfcheck.common import (
    LaunchContext, ProbeResult, fingerprint, listening, probe, qq_send_count, utc_now,
)

BOARD_PORT = 8793


async def drain(supervisor) -> list[Any]:
    results = []
    while (queued := await supervisor.queue.get(timeout=0)) is not None:
        results.append(await supervisor.dispatch(queued))
    return results


async def run_isolated(ctx: LaunchContext, *, supervisor=None, protected: list[Path] | None = None
                       ) -> list[ProbeResult]:
    before_sends = qq_send_count(ctx.watcher_config)
    hashes = {str(path): fingerprint(path) for path in (protected or _production_paths(ctx))}
    owned = supervisor is None
    cleanup = None
    results: list[ProbeResult] = []
    try:
        if owned:
            supervisor, cleanup, skipped = await _start_live_isolated(ctx)
            if skipped:
                results.extend(skipped)
                return results
        results.append(await scenario_chat(supervisor))
        results.append(await scenario_chat_web(supervisor))
        results.append(await scenario_qq_web(supervisor))
        results.append(await scenario_diary(supervisor))
        results.append(await scenario_goal(supervisor))
        results.append(await scenario_homeostasis(supervisor))
        results.append(await scenario_memory(supervisor))
        results.append(_emotion(supervisor))
        if owned:
            results.extend(await _board_acceptance(ctx))
    except Exception as exc:
        results.append(probe("isolated.runtime", 1, "fail", detail=f"{type(exc).__name__}: {exc}",
                             triage=["Inspect isolated traces under the temp destination."]))
    finally:
        if cleanup is not None:
            await cleanup()
        after = qq_send_count(ctx.watcher_config)
        unchanged = all(fingerprint(path) == old for path, old in hashes.items())
        zero_sends = after == before_sends
        results.append(probe("isolated.safety", 1,
                             "pass" if unchanged and zero_sends else "fail",
                             evidence={"qq_send_before": before_sends, "qq_send_after": after,
                                       "production_sources_unchanged": unchanged},
                             triage=["Self-check must not send QQ or rewrite production persona/homeostasis."]))
    return results


def _production_paths(ctx: LaunchContext) -> list[Path]:
    paths = []
    if ctx.gui_config:
        paths.append(ctx.gui_config)
    state = ctx.bindings.get("NC_HOMEOSTASIS_STATE_DIR")
    if state:
        root = Path(state)
        paths.extend([root / "snapshot.json", root / "command-ledger.sqlite3"])
    return [path for path in paths if path.exists()]


async def _start_live_isolated(ctx: LaunchContext):
    import importlib.util
    verify_path = ctx.root / "tools" / "verify_capabilities.py"
    if not verify_path.is_file():
        return None, None, _skipped_live("verify_capabilities missing")
    spec = importlib.util.spec_from_file_location("neuralcloud_verify_capabilities", verify_path)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        return None, None, _skipped_live("verify_capabilities loader missing")
    spec.loader.exec_module(module)
    environment, isolated_owner, read_json = module.environment, module.isolated_owner, module.read_json

    launch = {"NC_GUI_CONFIG": str(ctx.gui_config or ""), "NC_GUI_PRESETS": ctx.bindings.get("NC_GUI_PRESETS", ""),
              "NC_SHARED_MEMORY_DB": ctx.bindings.get("NC_SHARED_MEMORY_DB", ""),
              "NC_BOARD_CONFIG": ctx.bindings.get("NC_BOARD_CONFIG", ""),
              "NC_QQ_WATCHER_CONFIG": str(ctx.watcher_config) if ctx.watcher_config else "",
              "NC_HOMEOSTASIS_STATE_DIR": ctx.bindings.get("NC_HOMEOSTASIS_STATE_DIR", "")}
    if not ctx.gui_config or not Path(ctx.gui_config).is_file():
        return None, None, _skipped_live("gui config missing")
    try:
        gui = read_json(ctx.gui_config)
        if gui.get("mode") != "live":
            return None, None, _skipped_live("saved_live_model_required")
    except (OSError, ValueError) as exc:
        return None, None, _skipped_live(str(exc))
    destination = Path(tempfile.mkdtemp(prefix="neuralcloud-selfcheck-"))
    try:
        gui_path, values, _preset = isolated_owner(launch, destination)
    except ValueError as exc:
        return None, None, _skipped_live(str(exc))

    from harness.cognitive.service import CognitiveServiceHost
    from harness.diary.config import DiarySettings
    from harness.homeostasis import HomeostasisEngine, load_profile
    from harness.research.manifest import WebAccessCondition, load_experiment_manifest
    from harness.runtime.diary import DiaryController
    from harness.runtime.supervisor import RuntimeSupervisor
    from tests.test_web_runtime import rig as web_rig

    host = None
    supervisor = None

    async def cleanup():
        if supervisor is not None:
            with suppress(Exception):
                await supervisor.stop(reason="self-check isolated complete")
            diary = getattr(supervisor, "diary", None)
            if diary is not None:
                with suppress(Exception):
                    diary.close()
            web = getattr(supervisor, "web", None)
            if web is not None:
                with suppress(Exception):
                    await web.close()
        elif host is not None:
            with suppress(Exception):
                await host.close()

    try:
        with environment(values):
            host = CognitiveServiceHost(config_path=gui_path, presets_dir=destination / "presets",
                                        state_path=destination / "cognitive.json")
            profile = load_profile(ctx.root / "presets/homeostasis/qq-social-drive-v1.json")
            engine = HomeostasisEngine(profile, state_dir=destination / "homeostasis")
            manifest_path = ctx.manifest_path or (ctx.root / "presets/runtime/shared-misty-regulation-v1.json")
            base = load_experiment_manifest(manifest_path)
            manifest = base.model_copy(update={
                "control_mode": "development_debug",
                "web_access": WebAccessCondition(enabled=True, conversation_enabled=True),
                "autonomous_chat_enabled": False,
            })
            supervisor = RuntimeSupervisor(manifest=manifest, cognitive=host, homeostasis=engine,
                                           state_dir=destination / "runtime")
            supervisor.diary = DiaryController(supervisor, DiarySettings(autonomous_enabled=False, max_entries_per_day=8),
                                               destination / "runtime")
            fake, _ = web_rig(destination / "web-state")
            _attach_web(supervisor, fake)
            await supervisor.boot_and_recover()
            supervisor.scheduler.suspend()
            await drain(supervisor)
            _refresh_web_revision(supervisor)
            return supervisor, cleanup, None
    except Exception:
        await cleanup()
        raise


def _skipped_live(reason: str) -> list[ProbeResult]:
    return [probe("isolated.live_owner", 1, "skipped(not_configured)", detail=reason,
                  triage=["Tier1 live isolation needs GUI mode=live and a saved persona."])]


def _attach_web(supervisor, fake) -> None:
    supervisor.manifest = supervisor.manifest.model_copy(update={"web_access": fake.manifest.web_access})
    supervisor.web = fake.web
    supervisor.web.supervisor = supervisor
    _refresh_web_revision(supervisor)


def _refresh_web_revision(supervisor) -> None:
    web = getattr(supervisor, "web", None)
    if web is None:
        return
    web.supervisor = supervisor
    web._revision = web._configuration()


async def scenario_chat(supervisor) -> ProbeResult:
    from harness.runtime.models import RuntimeLifecycle
    if supervisor.lifecycle != RuntimeLifecycle.RUNNING:
        return probe("chat.reply", 1, "fail", detail=f"lifecycle={supervisor.lifecycle}")
    event = await supervisor.submit_user_message(
        "自检：请只用一句话确认你在。", session_id="sess_selfcheck", correlation_id="selfcheck:chat")
    queued = await supervisor.queue.get(timeout=0)
    if queued is None or queued.event_id != event.event_id:
        return probe("chat.reply", 1, "fail", detail="user message was not queued")
    result = await supervisor.dispatch(queued)
    await drain(supervisor)
    ok = result.status == "completed" and result.decision_kind == "reply"
    return probe("chat.reply", 1, "pass" if ok else "fail",
                 evidence={"event_id": event.event_id, "status": result.status,
                           "decision_kind": result.decision_kind},
                 triage=["processing_completed and InMemory delivery; inspect isolated journal."])


async def scenario_chat_web(supervisor) -> ProbeResult:
    web = getattr(supervisor, "web", None)
    if web is None or not web.conversation_enabled:
        return probe("chat.web", 1, "skipped(not_configured)", detail="web conversation is off")
    before = (await web.status())["usage"]["requests"]
    event = await supervisor.submit_user_message(
        "请联网查一下 selfcheck evidence 并引用来源。", session_id="sess_selfcheck",
        correlation_id="selfcheck:chat-web")
    queued = await supervisor.queue.get(timeout=0)
    result = await supervisor.dispatch(queued)
    await drain(supervisor)
    after = (await web.status())["usage"]["requests"]
    round_row = web.calls.round(event.event_id)
    journaled = any(record.record_type == "web_request_completed" for record in supervisor.journal.iter_records(reverse=True))
    ok = after > before and round_row and round_row.get("status") == "completed" and journaled
    # A real model may finish without searching; wiring is still proven if the round completed.
    if round_row and round_row.get("status") == "completed" and after == before:
        return probe("chat.web", 1, "no_recent_sample",
                     evidence={"round": round_row.get("status"), "requests": after,
                               "decision_kind": result.decision_kind},
                     detail="model finished without a web call")
    return probe("chat.web", 1, "pass" if ok else "fail",
                 evidence={"requests_before": before, "requests_after": after,
                           "round": (round_row or {}).get("status"), "journaled": journaled,
                           "decision_kind": result.decision_kind},
                 triage=["web/calls.sqlite3 round status and journal web_request_completed"])


async def scenario_qq_web(supervisor) -> ProbeResult:
    from harness.qq.shared import QQSharedPrepareRequest
    from harness.runtime.models import RuntimeEventPriority, RuntimeEventType
    from harness.runtime.web_dialogue import prepare_web_context

    web = getattr(supervisor, "web", None)
    if web is None or not web.conversation_enabled:
        return probe("qq.web", 1, "skipped(not_configured)")
    _refresh_web_revision(supervisor)
    # prepare_web_context reserves the last 30s for the QQ reply; a 20s
    # deadline therefore never searches. The stub must look like a RuntimeEvent
    # so _invoke_domain can read event_type / receive_seq.
    stub = SimpleNamespace(
        event_id=f"selfcheck-qq-web:{uuid4().hex[:12]}",
        receive_seq=1,
        correlation_id=f"selfcheck:qq-web:{uuid4().hex[:12]}",
        event_type=RuntimeEventType.USER_MESSAGE,
        payload={"query": "帮我查一下 selfcheck evidence", "text": "帮我查一下 selfcheck evidence"},
    )
    context, _deadline = await prepare_web_context(
        supervisor, stub, channel="qq", deadline=utc_now() + timedelta(seconds=90))
    sources = (context or {}).get("sources") or []
    turn_kind = None
    if getattr(supervisor.cognitive, "prepare_qq_response", None):
        try:
            now = utc_now()
            request = QQSharedPrepareRequest.model_validate({
                "transport": "onebot", "group_id": "10001", "self_member_id": "29999",
                "trigger_member_id": "20001", "request": "帮我查一下 selfcheck evidence",
                "query": "selfcheck evidence", "deadline_at": now + timedelta(seconds=59),
                "trace": {"trace_id": f"qq-trace:{uuid4().hex}",
                          "event_id": f"qq-watch-event:{uuid4().hex}",
                          "first_seen_at": now, "first_seen_mono": 0},
            })
            queued = await supervisor.submit_event(
                event_type=RuntimeEventType.QQ_TURN, priority=RuntimeEventPriority.USER, source="selfcheck",
                source_time=request.trace.first_seen_at, correlation_id=request.trace.trace_id,
                payload=request.model_dump(mode="json"))
            dispatched = await supervisor.queue.get(timeout=0)
            if dispatched is not None:
                outcome = await supervisor.dispatch(dispatched)
                turn_kind = outcome.decision_kind
                await drain(supervisor)
                round_row = web.calls.round(queued.event_id)
                turn_sources = ((round_row or {}).get("context") or {}).get("sources") or []
                if turn_sources:
                    sources = turn_sources
                    context = (round_row or {}).get("context") or context
        except Exception as exc:
            turn_kind = type(exc).__name__
    if not sources:
        return probe("qq.web", 1, "no_recent_sample" if context else "fail",
                     evidence={"context": context, "qq_prepare": turn_kind},
                     detail="prepare_web_context(channel=qq) returned no sources",
                     triage=["QQ deadline must leave ~30s for web steps; check web catalog ready offers."])
    return probe("qq.web", 1, "pass",
                 evidence={"sources": len(sources), "qq_prepare": turn_kind,
                           "first_title": sources[0].get("title") if sources else None})


async def scenario_diary(supervisor) -> ProbeResult:
    diary = getattr(supervisor, "diary", None)
    if diary is None:
        return probe("diary.write", 1, "skipped(not_configured)")
    before = diary.store.count(diary._day())
    accepted = await diary.request({"request_id": f"selfcheck:{diary._day()}:{uuid4().hex[:8]}",
                                    "text": "自检邀请：如果今天有想留给自己的话，可以写进日记。"})
    await drain(supervisor)
    after = diary.store.count(diary._day())
    entry_id = accepted.get("entry_id")
    turn = diary.turns.get(entry_id) if entry_id else None
    status = (turn or {}).get("status")
    ok = after > before and status == "committed"
    return probe("diary.write", 1, "pass" if ok else "fail",
                 evidence={"before": before, "after": after, "turn_status": status,
                           "autonomous_enabled": diary.settings.autonomous_enabled},
                 detail="user-invite path; expression.diary.write.v1 is not catalogued",
                 triage=["diary.sqlite3 count and diary-turns status committed"])


async def scenario_goal(supervisor) -> ProbeResult:
    if not supervisor.goals.enabled:
        return probe("goal.submit", 1, "skipped(disabled)")
    identity = None
    try:
        submitted = await supervisor.goals.submit({
            "request_id": f"selfcheck-goal:{uuid4().hex[:12]}",
            "statement": "自检：仅观察当前内稳态，不采取身体动作。",
            "success_criteria": ["存在一次 Goal 提交记录"],
        })
        identity = submitted.get("id") or submitted.get("goal_id")
        recorded = any(record.record_type == "production_goal_submitted"
                       for record in supervisor.journal.iter_records(reverse=True))
        snapshot = supervisor.goals.snapshot()
        found = any(goal.get("id") == identity for goal in snapshot.get("goals") or [])
        ok = recorded and found
        return probe("goal.submit", 1, "pass" if ok else "fail",
                     evidence={"goal_id": identity, "journaled": recorded, "in_snapshot": found},
                     triage=["journal production_goal_submitted and GET /v1/goals"])
    finally:
        if identity:
            with suppress(Exception):
                await supervisor.goals.operate(identity, "cancel", f"selfcheck-cancel:{uuid4().hex[:8]}")


async def scenario_homeostasis(supervisor) -> ProbeResult:
    from harness.runtime.models import RuntimeEventPriority, RuntimeEventType
    before = supervisor.status().health.last_heartbeat_at
    before_rev = supervisor._homeostasis_snapshot.revision
    event = await supervisor.submit_event(
        event_type=RuntimeEventType.HEARTBEAT, priority=RuntimeEventPriority.HEARTBEAT,
        source="selfcheck", payload={})
    queued = await supervisor.queue.get(timeout=0)
    if queued is None:
        return probe("homeostasis.heartbeat", 1, "fail", detail="heartbeat was not queued")
    result = await supervisor.dispatch(queued)
    await drain(supervisor)
    after = supervisor.status().health.last_heartbeat_at
    after_rev = supervisor._homeostasis_snapshot.revision
    ok = result.status == "completed" and after is not None and (before is None or after >= before)
    return probe("homeostasis.heartbeat", 1, "pass" if ok else "fail",
                 evidence={"before": None if before is None else before.isoformat(),
                           "after": None if after is None else after.isoformat(),
                           "revision_before": before_rev, "revision_after": after_rev})


async def scenario_memory(supervisor) -> ProbeResult:
    recorded = False
    for record in supervisor.journal.iter_records(reverse=True):
        envelope = ((record.data or {}).get("envelope") or {})
        result = ((record.data or {}).get("result") or {})
        if envelope.get("command_type") == "CommitInteraction" or result.get("command_type") == "CommitInteraction":
            recorded = True
            break
    calls = getattr(supervisor.cognitive, "calls", None)
    scripted = isinstance(calls, list) and "CommitInteraction" in calls
    return probe("memory.commit", 1, "pass" if recorded or scripted else "no_recent_sample",
                 evidence={"journal_commit": recorded, "scripted_commit": scripted})


def _emotion(supervisor) -> ProbeResult:
    affect = supervisor.status().affect
    if not affect:
        return probe("emotion.frame", 1, "skipped(not_configured)")
    return probe("emotion.frame", 1, "pass", evidence={"affect": [item.model_dump(mode="json") for item in affect]})


async def _board_acceptance(ctx: LaunchContext) -> list[ProbeResult]:
    if not listening(BOARD_PORT):
        return [probe("board.live_opportunity", 1, "skipped(not_configured)",
                      detail="board body is not listening on 8793")]
    # Live board acceptance is delegated; it must not start a QQ watcher.
    python = ctx.root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = ctx.root / ".venv" / "bin" / "python"
    proc = await asyncio.create_subprocess_exec(
        str(python),
        str(ctx.root / "tools" / "verify_capabilities.py"),
        "--mode", "board", "--launch-config", str(ctx.launch_config),
        cwd=str(ctx.root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except TimeoutError:
        proc.kill()
        return [probe("board.live_opportunity", 1, "fail", detail="verify_capabilities timed out")]
    text = stdout.decode("utf-8", errors="replace")
    ok = proc.returncode == 0
    return [probe("board.live_opportunity", 1, "pass" if ok else "fail",
                  evidence={"returncode": proc.returncode, "stdout_tail": text[-1500:]},
                  detail=stderr.decode("utf-8", errors="replace")[-400:],
                  triage=["See tools/verify_capabilities.py --mode board"])]
