"""Tier0 read-only probes against the shared production Runtime."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from tools.selfcheck.common import (
    ApiClient, LaunchContext, ProbeResult, fingerprint, journal_fetch, listening,
    probe, qq_send_count, readonly_connect, utc_now,
)

QQ_AGENT_PORT = 8766
NAPCAT_ONEBOT_PORT = 6301
WATCHER_MAX_AGE_SECONDS = 120
HOMEOSTASIS_MAX_AGE_SECONDS = 90


def run_readonly(ctx: LaunchContext) -> list[ProbeResult]:
    runtime = ApiClient(ctx.runtime_url, ctx.control_token)
    cognitive = ApiClient(ctx.cognitive_url, ctx.service_token)
    homeostasis = ApiClient(ctx.homeostasis_url, ctx.service_token)
    gui = ApiClient(ctx.gui_url, None)
    try:
        return list(_collect(ctx, runtime, cognitive, homeostasis, gui))
    finally:
        runtime.close()
        cognitive.close()
        homeostasis.close()
        gui.close()


def _collect(ctx, runtime, cognitive, homeostasis, gui) -> list[ProbeResult]:
    results = []
    results.append(_port(ctx, "runtime", ctx.ports["runtime"], 8770))
    results.append(_port(ctx, "cognitive", ctx.ports["cognitive"], 8771))
    results.append(_port(ctx, "homeostasis", ctx.ports["homeostasis"], 8772))
    results.append(_port(ctx, "gui", ctx.ports["gui"], 8790))
    results.append(_qq_body(ctx))
    results.append(_service_health("infra.runtime_health", runtime, "/v1/health", lifecycle=True))
    results.append(_service_health("infra.cognitive_health", cognitive, "/v1/health"))
    results.append(_service_health("infra.homeostasis_health", homeostasis, "/v1/health"))
    gui_status, _ = gui.json("GET", "/")
    gui_ok = gui_status in {200, 401}
    results.append(probe("infra.gui", 0, "pass" if gui_ok else "unavailable",
                         evidence={"http_status": gui_status},
                         triage=["Linux GUI may require auth (401 still means the process is up). Never stop 8766."]))
    status_code, status = runtime.json("GET", "/v1/status")
    lifecycle = (status.get("lifecycle") if isinstance(status, dict) else None) or (
        (status.get("health") or {}).get("lifecycle") if isinstance(status, dict) else None)
    results.append(probe("infra.runtime_lifecycle", 0,
                         "pass" if status_code == 200 and lifecycle == "RUNNING" else "unavailable",
                         evidence={"http_status": status_code, "lifecycle": lifecycle},
                         triage=["GET /v1/status on 8770; if FAULTED inspect journal."]))
    results.extend(_capabilities(runtime))
    results.extend(_web(runtime, ctx))
    results.extend(_diary(runtime))
    results.extend(_goals(runtime))
    results.extend(_board(runtime))
    results.extend(_voice(runtime))
    results.extend(_server(runtime))
    results.extend(_homeostasis(runtime, status if isinstance(status, dict) else {}))
    results.extend(_emotion(status_code, status if isinstance(status, dict) else {}))
    results.extend(_journal_evidence(runtime))
    results.extend(_qq_channel(ctx))
    results.append(_qq_web_passive(runtime))
    results.append(_capability_store(ctx))
    return results


def _port(ctx, name, port, expected) -> ProbeResult:
    ok = listening(port)
    return probe(f"infra.port.{name}", 0, "pass" if ok else "unavailable",
                 evidence={"port": port, "listening": ok, "launch_expected": expected},
                 triage=[f"Start shared {name}; never free a GUI port by stopping 8766."])


def _qq_body(ctx: LaunchContext) -> ProbeResult:
    agent = listening(QQ_AGENT_PORT)
    napcat = listening(NAPCAT_ONEBOT_PORT)
    direct = ctx.bindings.get("NC_QQ_DIRECT_CONFIG")
    direct_path = Path(direct) if direct else None
    linux_qq = bool(direct_path and direct_path.is_file()) or bool(ctx.bindings.get("NC_QQ_SESSION_CONFIG"))
    if agent:
        status = "pass"
        detail = "legacy QQ Agent on 8766; self-check never stops it"
    elif napcat:
        status = "pass"
        detail = "NapCat OneBot on 6301; 8766 is unused on this Linux instance"
    elif linux_qq:
        status = "unavailable"
        detail = "QQ Direct/session is configured but 6301/8766 are not listening"
    else:
        status = "unavailable"
        detail = "port 8766 must keep its owner; self-check never stops it"
    return probe("infra.qq_agent", 0, status, detail=detail,
                 evidence={"listening_8766": agent, "listening_6301": napcat,
                           "qq_direct_config": str(direct_path) if direct_path else None},
                 triage=["Do not stop 8766. On Linux check neuralcloud-qq-direct and NapCat 6301."])


def _service_health(path, client, route, *, lifecycle=False) -> ProbeResult:
    status, payload = client.json("GET", route)
    healthy = status == 200
    if isinstance(payload, dict) and "lifecycle" in payload:
        healthy = status == 200 and payload.get("lifecycle") == "RUNNING"
    return probe(path, 0, "pass" if healthy else "unavailable",
                 evidence={"http_status": status, "payload": _small(payload)},
                 triage=["Confirm bearer token files and that the domain process is up."])


def _capabilities(runtime) -> list[ProbeResult]:
    status, payload = runtime.json("GET", "/v1/capabilities")
    if status != 200 or not isinstance(payload, dict):
        return [probe("capabilities.catalog", 0, "unavailable", evidence={"http_status": status},
                      triage=["GET /v1/capabilities with control token."])]
    owners = payload.get("owners") or {}
    catalog = payload.get("catalog") or []
    ready = [item.get("capability_id") for item in catalog if item.get("ready")]
    enabled = bool(payload.get("enabled"))
    return [probe("capabilities.catalog", 0, "pass" if enabled else "skipped(disabled)",
                  evidence={"enabled": enabled, "owners": owners,
                            "qq_send_enabled": payload.get("qq_send_enabled"),
                            "catalog_count": len(catalog), "ready": ready[:20],
                            "recent_calls": len(payload.get("calls") or [])},
                  detail="shared catalog snapshot")]


def _web(runtime, ctx) -> list[ProbeResult]:
    status, payload = runtime.json("GET", "/v1/web/status")
    if status == 404:
        return [probe("web.status", 0, "skipped(not_configured)", detail="web routes missing")]
    if status != 200 or not isinstance(payload, dict):
        return [probe("web.status", 0, "unavailable", evidence={"http_status": status},
                      triage=["GET /v1/web/status; check NC_WEB_CONFIG."])]
    enabled = bool(payload.get("enabled"))
    conversation = bool(payload.get("conversation_enabled"))
    usage = payload.get("usage") or {}
    state = "pass" if enabled and conversation else ("skipped(disabled)" if not enabled else "skipped(not_configured)")
    return [probe("web.status", 0, state,
                  evidence={"enabled": enabled, "conversation_enabled": conversation,
                            "autonomous_enabled": payload.get("autonomous_enabled"), "usage": usage})]


def _diary(runtime) -> list[ProbeResult]:
    status, payload = runtime.json("GET", "/v1/diary")
    if status != 200 or not isinstance(payload, dict):
        return [probe("diary.snapshot", 0, "unavailable", evidence={"http_status": status},
                      triage=["GET /v1/diary"])]
    return [probe("diary.snapshot", 0, "pass",
                  evidence={"autonomous_enabled": payload.get("autonomous_enabled"),
                            "wrote_today": payload.get("wrote_today"),
                            "today_count": payload.get("today_count"),
                            "paused": payload.get("paused")},
                  detail="expression.diary.write.v1 is not in the shared catalog")]


def _goals(runtime) -> list[ProbeResult]:
    status, payload = runtime.json("GET", "/v1/goals")
    if status != 200 or not isinstance(payload, dict):
        return [probe("goals.snapshot", 0, "unavailable", evidence={"http_status": status},
                      triage=["GET /v1/goals"])]
    enabled = bool(payload.get("enabled"))
    return [probe("goals.snapshot", 0, "pass" if enabled else "skipped(disabled)",
                  evidence={"enabled": enabled, "goal_count": len(payload.get("goals") or []),
                            "active_episode": bool(payload.get("active_episode")),
                            "budget": payload.get("budget")})]


def _board(runtime) -> list[ProbeResult]:
    status, payload = runtime.json("GET", "/v1/board")
    if status == 503:
        return [probe("board.snapshot", 0, "skipped(not_configured)", detail="board_not_configured")]
    if status != 200:
        return [probe("board.snapshot", 0, "unavailable", evidence={"http_status": status})]
    return [probe("board.snapshot", 0, "pass", evidence=_small(payload if isinstance(payload, dict) else {}))]


def _voice(runtime) -> list[ProbeResult]:
    status, payload = runtime.json("GET", "/v1/voice")
    if status in {503, 404}:
        return [probe("voice.status", 0, "skipped(disabled)")]
    if status != 200:
        return [probe("voice.status", 0, "unavailable", evidence={"http_status": status})]
    enabled = bool((payload or {}).get("enabled")) if isinstance(payload, dict) else False
    return [probe("voice.status", 0, "pass" if enabled else "skipped(disabled)",
                  evidence=_small(payload if isinstance(payload, dict) else {}))]


def _server(runtime) -> list[ProbeResult]:
    status, payload = runtime.json("GET", "/v1/server/status")
    if status in {503, 404}:
        return [probe("server.status", 0, "skipped(disabled)")]
    if status != 200:
        return [probe("server.status", 0, "unavailable", evidence={"http_status": status})]
    enabled = bool((payload or {}).get("enabled")) if isinstance(payload, dict) else False
    return [probe("server.status", 0, "pass" if enabled else "skipped(disabled)",
                  evidence=_small(payload if isinstance(payload, dict) else {}))]


def _homeostasis(runtime, status_payload) -> list[ProbeResult]:
    code, payload = runtime.json("GET", "/v1/homeostasis/snapshot")
    if code != 200 or not isinstance(payload, dict):
        return [probe("homeostasis.snapshot", 0, "unavailable", evidence={"http_status": code},
                      triage=["GET /v1/homeostasis/snapshot on 8770 or 8772"])]
    inner = payload.get("payload") or payload.get("snapshot") or payload
    updated = inner.get("updated_at") or (inner.get("snapshot") or {}).get("updated_at")
    age = None
    if updated:
        try:
            age = (utc_now() - datetime_from(updated)).total_seconds()
        except ValueError:
            age = None
    fresh = age is not None and age <= HOMEOSTASIS_MAX_AGE_SECONDS
    last_hb = (status_payload.get("health") or {}).get("last_heartbeat_at")
    return [probe("homeostasis.snapshot", 0, "pass" if fresh or last_hb else "fail",
                  evidence={"updated_at": updated, "age_seconds": age, "last_heartbeat_at": last_hb},
                  triage=["Check 8772 health and Runtime heartbeat_seconds."])]


def _emotion(status_code, status_payload) -> list[ProbeResult]:
    if status_code != 200:
        return [probe("emotion.affect", 0, "unavailable", evidence={"http_status": status_code},
                      triage=["GET /v1/status; affect is projected from the Runtime snapshot."])]
    affect = status_payload.get("affect") or []
    if not affect:
        return [probe("emotion.affect", 0, "skipped(not_configured)",
                      detail="emotion_mode off or affect worker idle")]
    return [probe("emotion.affect", 0, "pass", evidence={"affect": affect})]


def _journal_evidence(runtime) -> list[ProbeResult]:
    wanted = {
        "web.journal": "web_request_completed",
        "chat.journal": "processing_completed",
        "goal.journal": "production_goal_submitted",
        "capability.journal": "action_proposed",
    }
    results = []
    for path, record_type in wanted.items():
        status, items = journal_fetch(runtime, record_type=record_type, window="24h", limit=20)
        if status != 200:
            results.append(probe(path, 0, "unavailable", evidence={"http_status": status, "record_type": record_type},
                                 triage=["GET /v1/monitor/journal; Runtime 8770 must be up."]))
            continue
        results.append(probe(path, 0, "pass" if items else "no_recent_sample",
                             evidence={"count": len(items), "record_type": record_type},
                             detail="recent 24h journal projection"))
    return results


def _qq_channel(ctx: LaunchContext) -> list[ProbeResult]:
    count = qq_send_count(ctx.watcher_config)
    age = watcher_age(ctx.watcher_config)
    status = "pass" if ctx.watcher_config and ctx.watcher_config.is_file() else "skipped(not_configured)"
    if status == "pass" and age is not None and age > WATCHER_MAX_AGE_SECONDS:
        status = "fail"
    return [
        probe("qq.watcher", 0, status,
              evidence={"watcher_config": str(ctx.watcher_config) if ctx.watcher_config else None,
                        "status_age_seconds": age, "send_count": count},
              triage=["Read watcher status file; do not send. Never stop 8766."]),
        probe("qq.send_ledger", 0, "pass" if count is not None else "skipped(not_configured)",
              evidence={"qq_watch_sends": count}),
    ]


def _qq_web_passive(runtime, *, tier: int = 0) -> ProbeResult:
    turn_status, turns = journal_fetch(runtime, record_type="event_accepted", window="24h", limit=100)
    if turn_status != 200:
        return probe("qq.web.passive", tier, "unavailable", evidence={"http_status": turn_status},
                     triage=["GET /v1/monitor/journal; Runtime 8770 must be up."])
    qq_turns = [item for item in turns if _event_type(item) == "qq_turn"]
    web_status, web = journal_fetch(runtime, record_type="web_request_completed", window="24h", limit=50)
    if web_status != 200:
        web = []
    matched = []
    web_ids = {_correlation(item) for item in web}
    for item in qq_turns:
        corr = _correlation(item)
        event_id = _event_id(item)
        if corr and corr in web_ids:
            matched.append(corr)
        elif event_id and any(event_id in str(w.get("payload") or {}) for w in web):
            matched.append(event_id)
    if matched:
        return probe("qq.web.passive", tier, "pass",
                     evidence={"matched": matched[:8], "qq_turns": len(qq_turns), "web_records": len(web)})
    if qq_turns:
        return probe("qq.web.passive", tier, "no_recent_sample",
                     evidence={"qq_turns": len(qq_turns), "web_records": len(web)},
                     detail="recent QQ turns exist but none associated with web_request_completed")
    return probe("qq.web.passive", tier, "no_recent_sample",
                 detail="no qq_turn in the last 24h; isolation tier covers the wiring")


def _capability_store(ctx: LaunchContext) -> ProbeResult:
    if ctx.runtime_state is None:
        return probe("capabilities.store", 0, "skipped(not_configured)")
    path = ctx.runtime_state / "capability-calls.sqlite3"
    if not path.is_file():
        return probe("capabilities.store", 0, "skipped(not_configured)", evidence={"path": str(path)})
    try:
        with readonly_connect(path) as database:
            tables = database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            names = [item[0] for item in tables]
            count = 0
            if "calls" in names:
                count = database.execute("SELECT count(*) FROM calls").fetchone()[0]
            elif names:
                # CapabilityCalls stores JSON rows; tolerate schema name.
                try:
                    count = database.execute(f"SELECT count(*) FROM {names[0]}").fetchone()[0]
                except sqlite3.Error:
                    count = 0
    except sqlite3.Error as exc:
        return probe("capabilities.store", 0, "fail", detail=type(exc).__name__,
                     triage=["Open capability-calls.sqlite3 read-only."])
    return probe("capabilities.store", 0, "pass" if count else "no_recent_sample",
                 evidence={"path": str(path), "rows": count, "fingerprint": fingerprint(path)})


def watcher_age(watcher_config: Path | None) -> float | None:
    if watcher_config is None or not watcher_config.is_file():
        return None
    try:
        payload = __import__("json").loads(watcher_config.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    status_path = Path(payload.get("status_file") or payload.get("watcher_status_path") or "")
    if not status_path.is_file():
        sibling = watcher_config.with_name("watcher-status.json")
        status_path = sibling if sibling.is_file() else watcher_config
    try:
        mtime = datetime_from_timestamp(status_path.stat().st_mtime)
        return (utc_now() - mtime).total_seconds()
    except OSError:
        return None


def datetime_from(value: str):
    from datetime import datetime
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def datetime_from_timestamp(value: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _event_type(item: dict[str, Any]) -> str | None:
    payload = item.get("payload") or {}
    if isinstance(payload, dict):
        inner = payload.get("data") or payload
        event = inner.get("event") if isinstance(inner, dict) else None
        if isinstance(event, dict):
            return event.get("event_type")
        return inner.get("event_type") if isinstance(inner, dict) else None
    return None


def _correlation(item: dict[str, Any]) -> str | None:
    if item.get("correlation_id"):
        return item["correlation_id"]
    payload = item.get("payload") or {}
    if isinstance(payload, dict):
        return payload.get("correlation_id")
    return None


def _event_id(item: dict[str, Any]) -> str | None:
    if item.get("event_id"):
        return item["event_id"]
    payload = item.get("payload") or {}
    if isinstance(payload, dict):
        return payload.get("event_id")
    return None


def _small(payload: dict[str, Any]) -> dict[str, Any]:
    skip = {"catalog", "calls", "entries", "turns", "bodies", "episodes", "evaluations", "goals"}
    return {key: value for key, value in payload.items() if key not in skip}
