"""Execute a frozen QQ gate's bounded web intent with fresh Runtime authority."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from harness.embodiment.web_contracts import WebError, WebResult
from harness.qq.policy import QQLatencyOptions
from harness.qq.web_plan import QQWebPlan
from harness.runtime.models import RuntimeLifecycle, utc_now
from harness.runtime.qq_sources import verified_qq_source
from harness.runtime.web import READ, SEARCH, _hash


class QQWebBindingError(ValueError):
    """Invalid scope or preparation is never treated as a recoverable web outage."""


async def web_capabilities(supervisor, event, request, *, policy):
    scope = "qq:" + event.event_id
    web = getattr(supervisor, "web", None)
    value = {"enabled": False, "scope": scope, "registered_sources": [],
             "configuration_version": getattr(web, "_revision", None)}
    if (policy.qq_web_policy != "after_gate" or web is None or not web.conversation_enabled
            or supervisor.manifest.grounded_agency is not None):
        return value
    # The query field is a retrieval hint, not the user's source message.
    source = await asyncio.to_thread(verified_qq_source, request)
    web.register_urls(scope, source["text"])
    value.update(enabled=True, source_digest=source["digest"],
        registered_sources=[{"source_ref": item.source_ref, "url": item.url, "title": item.title}
                            for item in web.calls.sources(scope) if item.origin == "user_link"],
        max_searches=2, max_reads=1, max_read_sources=3,
        max_search_results=web.settings.max_results)
    return value


def _empty(code):
    return {"status": "unavailable", "sources": [], "failures": [{"code": code}], "evidence_refs": []}


async def execute_qq_web(supervisor, event, request, prepared, *, capabilities):
    policy = QQLatencyOptions.model_validate(prepared["policy"])
    if policy.version != prepared["policy_version"]:
        raise QQWebBindingError("qq_web_policy_changed")
    plan = QQWebPlan.model_validate(prepared.get("web_plan") or {})
    if not plan.steps:
        return None
    scope = "qq:" + event.event_id
    if capabilities.get("scope") != scope:
        raise QQWebBindingError("qq_web_scope_changed")
    web = getattr(supervisor, "web", None)
    if not capabilities.get("enabled") or web is None or policy.qq_web_policy != "after_gate":
        return _empty("web_unavailable")
    allowed_refs = {item["source_ref"] for item in capabilities.get("registered_sources", [])}
    if any(ref not in allowed_refs for step in plan.steps if step.action == "read" for ref in step.source_refs):
        raise QQWebBindingError("qq_web_unregistered_source")
    if any(web.calls.source(scope, ref) is None for ref in allowed_refs):
        raise QQWebBindingError("qq_web_source_scope_changed")
    identity = "qq-web-round:" + prepared["prepare_id"]
    binding = _hash([scope, prepared["snapshot_hash"], plan.digest, prepared["policy_version"], capabilities])
    row = web.calls.round(identity)
    if row is not None and row["binding"] != binding:
        raise QQWebBindingError("qq_web_preparation_changed")
    if row is not None and row["status"] == "completed":
        return row["context"]
    remaining = (request.deadline_at - utc_now()).total_seconds()
    budget = policy.stage_budget("web", remaining)
    if row is None:
        if budget <= 0:
            return _empty("web_budget_insufficient")
        row = {"binding": binding, "scope": scope, "status": "started", "results": {}, "failures": [],
               "runtime_instance_id": supervisor.runtime_instance_id,
               "deadline_at": min(request.deadline_at, utc_now() + timedelta(seconds=budget)).isoformat()}
        web.calls.put_round(identity, row)
    if row["runtime_instance_id"] != supervisor.runtime_instance_id:
        row["failures"].append({"code": "web_round_interrupted"})
    else:
        deadline = min(request.deadline_at, datetime.fromisoformat(row["deadline_at"]))

        def invalid():
            return (supervisor.lifecycle != RuntimeLifecycle.RUNNING or not web.conversation_enabled
                    or web._configuration() != capabilities.get("configuration_version"))

        try:
            for step in plan.steps:
                if step.step_id in row["results"]:
                    continue
                # Revoked QQ scope, edited sources and revised requests are hard failures.
                try:
                    current = await asyncio.to_thread(verified_qq_source, request)
                except ValueError as exc:
                    raise QQWebBindingError("qq_web_source_authority_changed") from exc
                if current["digest"] != capabilities.get("source_digest"):
                    raise QQWebBindingError("qq_web_trigger_changed")
                if invalid():
                    raise WebError("web_call_superseded")
                if utc_now() >= deadline:
                    raise WebError("web_deadline_exceeded")
                capability = SEARCH if step.action == "search" else READ
                offers = await web.catalog(scope, autonomous=False, deadline=deadline)
                offer = next((item for item in offers if item.capability_id == capability and item.ready), None)
                if offer is None:
                    raise WebError("web_capability_unavailable")
                if step.action == "search":
                    arguments = step.model_dump(exclude={"step_id", "action"})
                    arguments["max_results"] = min(arguments["max_results"], web.settings.max_results)
                else:
                    refs = list(step.source_refs)
                    for selected in step.search_results:
                        sources = row["results"].get(selected.step_id, {}).get("sources", [])
                        refs.extend(sources[index]["source_ref"] for index in selected.result_indices if index < len(sources))
                    refs = list(dict.fromkeys(refs))
                    if not refs:
                        row["failures"].append({"code": "web_no_search_results"})
                        continue
                    if any(web.calls.source(scope, ref) is None for ref in refs):
                        raise QQWebBindingError("qq_web_search_source_missing")
                    arguments = {"source_refs": refs}
                call_id = plan.execution_id(prepared["prepare_id"], step.step_id)
                previous = web.calls.call(call_id)
                # An uncertain transport result is never retried. Completed results
                # remain available even if the process died before the round commit.
                if previous is not None:
                    if previous["scope"] != scope or previous["capability"] != capability:
                        raise QQWebBindingError("qq_web_call_scope_changed")
                    if previous["status"] != "completed":
                        raise WebError("web_result_uncertain")
                    result = WebResult.model_validate_json(previous["result"])
                else:
                    result = await web.execute(call_id, offer, arguments, deadline=deadline, guard=invalid)
                row["results"][step.step_id] = result.model_dump(mode="json")
                row["failures"].extend(result.failures[:max(0, 6 - len(row["failures"]))])
                if result.status == "partial" and not result.failures:
                    row["failures"].append({"code": "web_partial_result"})
                if not result.sources:
                    row["failures"].append({"code": "web_no_results"})
                web.calls.put_round(identity, row)
        except QQWebBindingError:
            raise
        except (WebError, TimeoutError) as exc:
            row["failures"].append({"code": exc.code if isinstance(exc, WebError) else "web_timeout"})
        except Exception:
            row["failures"].append({"code": "web_request_failed"})
    row["status"] = "completed"
    row["context"] = web.context(scope, failures=row["failures"][:6])
    web.calls.put_round(identity, row)
    return row["context"]
