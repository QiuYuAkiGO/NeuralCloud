"""Tier2 production triggers with daily safety gates; QQ remains passive."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from tools.selfcheck.common import (
    ApiClient, LaunchContext, ProbeResult, already_ran, journal_items, mark_ran, probe,
    qq_send_count, shanghai_day, wait_for,
)
from tools.selfcheck.probes_readonly import _qq_web_passive


def run_production(ctx: LaunchContext, *, no_active: bool = False, timeout: float = 90,
                   client: ApiClient | None = None) -> list[ProbeResult]:
    owned = client is None
    runtime = client or ApiClient(ctx.runtime_url, ctx.control_token, timeout=timeout)
    before = qq_send_count(ctx.watcher_config)
    results: list[ProbeResult] = []
    try:
        results = [_qq_web_passive(runtime, tier=2)]
        if no_active:
            for name, label in (("chat.reply", "chat"), ("chat.web", "web"), ("diary.write", "diary"),
                                ("goal.submit", "goal")):
                results.append(probe(name, 2, "skipped(disabled)", detail="--no-active"))
            return results
        results.append(_chat_reply(runtime, timeout=timeout))
        results.append(_chat_web(runtime, timeout=timeout))
        results.append(_diary(runtime, timeout=timeout))
        results.append(_goal(runtime, timeout=timeout))
        return results
    finally:
        after = qq_send_count(ctx.watcher_config)
        if after != before:
            results.append(probe("production.qq_sends", 2, "fail",
                                 evidence={"before": before, "after": after},
                                 triage=["Self-check must not create qq_watch_sends rows."]))
        if owned:
            runtime.close()


def _active_session(runtime: ApiClient) -> str | None:
    status, payload = runtime.json("GET", "/v1/snapshot")
    if status != 200 or not isinstance(payload, dict):
        status, payload = runtime.json("GET", "/v1/status")
    if status != 200 or not isinstance(payload, dict):
        return None
    cognitive = payload.get("cognitive") or {}
    inner = cognitive.get("payload") if isinstance(cognitive, dict) else {}
    if isinstance(inner, dict):
        value = inner.get("default_session_id") or inner.get("session_id")
        if isinstance(value, str) and value.startswith("sess_"):
            return value
    ui = payload.get("cognitive_ui") if isinstance(payload.get("cognitive_ui"), dict) else {}
    for key in ("default_session_id", "session_id", "active_session_id"):
        value = ui.get(key) if ui else payload.get(key)
        if isinstance(value, str) and value.startswith("sess_"):
            return value
    return None


def _chat_reply(runtime: ApiClient, *, timeout: float) -> ProbeResult:
    if already_ran("chat"):
        return probe("chat.reply", 2, "skipped(already_ran_today)")
    session_id = _active_session(runtime)
    if not session_id:
        return probe("chat.reply", 2, "unavailable", detail="active chat session_id not found",
                     triage=["GET /v1/snapshot cognitive default_session_id"])
    correlation = f"selfcheck:chat:{uuid4().hex[:12]}"
    status, payload = runtime.json("POST", "/v1/messages", json={
        "text": "自检：请只用一句话确认你在。", "session_id": session_id,
        "speaker": "user", "channel": "chat", "correlation_id": correlation,
    })
    if status in {401, 0}:
        return probe("chat.reply", 2, "unavailable", evidence={"http_status": status, "payload": payload},
                     triage=["Bearer control token and Runtime 8770"])
    if status != 200 or not (payload or {}).get("event_id"):
        return probe("chat.reply", 2, "fail", evidence={"http_status": status, "payload": payload})
    completed = wait_for(lambda: _completed(runtime, correlation), timeout=timeout)
    mark_ran("chat")
    ok = bool(completed) and _kind(completed) in {"reply", None}
    # processing_completed with status completed is enough; decision_kind lives in payload.
    return probe("chat.reply", 2, "pass" if completed else "fail",
                 evidence={"correlation_id": correlation, "event_id": payload.get("event_id"),
                           "session_id": session_id, "completed": bool(completed),
                           "decision_kind": _kind(completed)},
                 triage=["GET /v1/monitor/journal?record_type=processing_completed"])


def _chat_web(runtime: ApiClient, *, timeout: float) -> ProbeResult:
    if already_ran("web"):
        return probe("chat.web", 2, "skipped(already_ran_today)")
    status, web = runtime.json("GET", "/v1/web/status")
    if status != 200 or not isinstance(web, dict):
        return probe("chat.web", 2, "unavailable", evidence={"http_status": status},
                     triage=["GET /v1/web/status; Runtime 8770 must be up."])
    if not web.get("enabled") or not web.get("conversation_enabled"):
        return probe("chat.web", 2, "skipped(disabled)" if not web.get("enabled") else "skipped(not_configured)",
                     evidence={"web": {"enabled": web.get("enabled"), "conversation_enabled": web.get("conversation_enabled")}})
    usage = web.get("usage") or {}
    requests = int(usage.get("requests") or 0)
    limit = int(usage.get("daily_limit") or 100)
    if limit and requests >= 0.8 * limit:
        return probe("chat.web", 2, "skipped(quota_guard)", evidence={"usage": usage})
    correlation = f"selfcheck:web:{uuid4().hex[:12]}"
    session_id = _active_session(runtime)
    if not session_id:
        return probe("chat.web", 2, "unavailable", detail="active chat session_id not found")
    post_status, payload = runtime.json("POST", "/v1/messages", json={
        "text": "请联网查一下今天的公开新闻标题，并附来源链接。", "session_id": session_id,
        "channel": "chat", "correlation_id": correlation,
    })
    if post_status != 200:
        return probe("chat.web", 2, "fail", evidence={"http_status": post_status, "payload": payload})
    web_hit = wait_for(lambda: journal_items(runtime, record_type="web_request_completed",
                                             correlation_id=correlation, window="1h", limit=20),
                       timeout=timeout)
    if not web_hit:
        # Some monitors copy correlation from the user event; fall back to any new web record.
        later, later_web = runtime.json("GET", "/v1/web/status")
        grew = later == 200 and int((later_web or {}).get("usage", {}).get("requests") or 0) > requests
        wait_for(lambda: _completed(runtime, correlation), timeout=min(20, timeout))
        mark_ran("web")
        return probe("chat.web", 2, "pass" if grew else "no_recent_sample",
                     evidence={"correlation_id": correlation, "usage_grew": grew, "usage_before": requests},
                     detail="no web_request_completed tied to correlation_id")
    mark_ran("web")
    return probe("chat.web", 2, "pass", evidence={"correlation_id": correlation, "web_records": len(web_hit)})


def _diary(runtime: ApiClient, *, timeout: float) -> ProbeResult:
    if already_ran("diary"):
        return probe("diary.write", 2, "skipped(already_ran_today)")
    day = shanghai_day()
    before_status, before = runtime.json("GET", "/v1/diary")
    if before_status != 200:
        return probe("diary.write", 2, "unavailable", evidence={"http_status": before_status},
                     triage=["GET /v1/diary; Runtime 8770 must be up."])
    request_id = f"selfcheck:{day}"
    status, payload = runtime.json("POST", "/v1/diary/invite", json={
        "request_id": request_id,
        "text": "自检邀请：如果今天有想留给自己的话，可以写进日记；没有也没关系。",
    })
    if status == 409:
        detail = str((payload or {}).get("detail") or payload)
        skipped = "diary_day_limit" in detail or "diary_paused" in detail
        return probe("diary.write", 2, "skipped(already_ran_today)" if skipped else "fail",
                     evidence={"http_status": status, "detail": detail})
    if status != 200:
        return probe("diary.write", 2, "fail" if status else "unavailable",
                     evidence={"http_status": status, "payload": payload})
    done = wait_for(lambda: _diary_committed(runtime, payload.get("entry_id")), timeout=timeout)
    mark_ran("diary")
    after_status, after = runtime.json("GET", "/v1/diary")
    grew = after_status == 200 and int((after or {}).get("today_count") or 0) >= int((before or {}).get("today_count") or 0)
    return probe("diary.write", 2, "pass" if done or grew else "fail",
                 evidence={"entry_id": payload.get("entry_id"), "committed": bool(done),
                           "today_count": (after or {}).get("today_count")},
                 triage=["GET /v1/diary and diary-turns.sqlite3"])


def _goal(runtime: ApiClient, *, timeout: float) -> ProbeResult:
    if already_ran("goal"):
        return probe("goal.submit", 2, "skipped(already_ran_today)")
    request_id = f"selfcheck-goal:{shanghai_day()}"
    status, payload = runtime.json("POST", "/v1/goals", json={
        "request_id": request_id,
        "statement": "自检：仅观察当前内稳态，不采取身体动作。",
        "success_criteria": ["存在一次 Goal 提交记录"],
    })
    if status == 409:
        detail = str((payload or {}).get("detail") or payload)
        skipped = "disabled" in detail or "goal regulation is disabled" in detail
        return probe("goal.submit", 2, "skipped(disabled)" if skipped else "skipped(already_ran_today)",
                     evidence={"http_status": status, "payload": payload})
    if status == 503:
        return probe("goal.submit", 2, "skipped(disabled)", evidence={"http_status": status, "payload": payload})
    if status != 200 or not isinstance(payload, dict):
        return probe("goal.submit", 2, "unavailable" if status in {0, 401} else "fail",
                     evidence={"http_status": status, "payload": payload},
                     triage=["POST /v1/goals; Runtime 8770 must be up."])
    identity = payload.get("id") or payload.get("goal_id")
    appeared = wait_for(lambda: _goal_present(runtime, identity), timeout=min(30, timeout))
    mark_ran("goal")
    if identity:
        runtime.json("POST", f"/v1/goals/{identity}/cancel", json={"request_id": f"{request_id}:cancel"})
    return probe("goal.submit", 2, "pass" if identity and appeared else "fail",
                 evidence={"goal_id": identity, "in_snapshot": bool(appeared)},
                 triage=["journal production_goal_submitted; cancel the self-check goal"])


def _completed(runtime: ApiClient, correlation: str) -> dict[str, Any] | None:
    items = journal_items(runtime, record_type="processing_completed", correlation_id=correlation,
                          window="1h", limit=20)
    return items[0] if items else None


def _kind(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    payload = item.get("payload") or {}
    data = payload.get("data") or payload
    return data.get("decision_kind") or payload.get("decision_kind")


def _diary_committed(runtime: ApiClient, entry_id: str | None) -> bool:
    status, payload = runtime.json("GET", "/v1/diary")
    if status != 200 or not isinstance(payload, dict):
        return False
    if payload.get("wrote_today"):
        return True
    for turn in payload.get("turns") or []:
        if entry_id and turn.get("entry_id") == entry_id and turn.get("status") == "committed":
            return True
    return False


def _goal_present(runtime: ApiClient, identity: str | None) -> bool:
    if not identity:
        return False
    status, payload = runtime.json("GET", "/v1/goals")
    if status != 200 or not isinstance(payload, dict):
        return False
    return any(goal.get("id") == identity for goal in payload.get("goals") or [])
