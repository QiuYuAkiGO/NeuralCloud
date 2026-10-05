"""Exercise the actual QQ gate -> optional Kimi tool -> main reply, without QQ sends.

Uses only the public/synthetic benchmark manifest and a fresh temporary database.
Credentials are read from private files and never copied to the report.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4

from harness.qq import decision, direct_images, vision_tool
from harness.qq.memory import (
    QQVerifiedProfileInput,
    QQVisualMemoryStore,
    QQVisualObservationInput,
    protocol_message_evidence_ref,
)
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from tools.production_acceptance.qq_vision_reply_benchmark import key_from_file


async def run_web_plan(plan, key_path, root, deadline):
    """Use the normal web provider/controller, with isolated fixture authority."""
    from harness.embodiment.providers.web import TavilyWebProvider
    from harness.qq.web_plan import QQWebPlan
    from harness.research.manifest import EmbodimentCondition, WebAccessCondition, load_experiment_manifest
    from harness.runtime.models import RuntimeLifecycle
    from harness.runtime.web import READ, SEARCH, WebController
    from harness.runtime.web_config import WebSettings

    plan = QQWebPlan.model_validate(plan or {})
    if not plan.steps:
        return None
    access = WebAccessCondition(enabled=True, conversation_enabled=True)
    manifest = load_experiment_manifest(Path("presets/runtime/shared-misty-v1.json"))
    sup = SimpleNamespace(
        manifest=manifest.model_copy(update={"web_access": access}),
        lifecycle=RuntimeLifecycle.RUNNING,
        runtime_instance_id="runtime:public-fixture",
        capabilities=SimpleNamespace(activity_revision=1),
        _cognitive_snapshot=SimpleNamespace(revision=0),
    )
    settings = WebSettings(daily_requests=12, autonomous_daily_requests=0)
    provider = TavilyWebProvider(
        EmbodimentCondition(
            mode="observe", body_instance_id="web-01", capability_requirements=access.capability_requirements
        ),
        settings.base_url,
        key_path.read_text(encoding="utf-8").strip(),
        "web-01",
    )
    web = WebController(sup, settings, root, provider=provider)
    sup.web = web
    scope, results, failures = "public-fixture", {}, []
    until = datetime.now(timezone.utc) + timedelta(seconds=max(0, min(10, deadline.remaining() - 13)))
    try:
        for step in plan.steps:
            capability = SEARCH if step.action == "search" else READ
            offers = await web.catalog(scope, autonomous=False, deadline=until)
            selected = next(item for item in offers if item.capability_id == capability and item.ready)
            if step.action == "search":
                arguments = step.model_dump(exclude={"step_id", "action"})
            else:
                refs = list(step.source_refs)
                for selection in step.search_results:
                    sources = results.get(selection.step_id, [])
                    refs += [sources[i].source_ref for i in selection.result_indices if i < len(sources)]
                arguments = {"source_refs": list(dict.fromkeys(refs))}
            result = await web.execute("fixture:" + step.step_id, selected, arguments, deadline=until)
            results[step.step_id] = result.sources
            failures.extend(result.failures)
    except Exception:
        failures.append({"code": "fixture_web_unavailable"})
    finally:
        try:
            context = web.context(scope, failures=failures)
        finally:
            await web.close()
    return context


def fixture(root, case, manifest_root):
    cache = root / "images"
    cache.mkdir()
    analyses = []
    for index, name in enumerate(case["images"], 1):
        raw = (manifest_root / name).read_bytes()

        async def api(*args, **kwargs):
            return {"base64": base64.b64encode(raw).decode()}

        loaded = asyncio.run(
            direct_images.load_and_describe_image(
                {"file": "public-fixture"},
                call_api=api,
                cache_dir=cache,
                llm=None,
                deadline=QQDeadline.after(15),
                acquire_only=True,
            )
        )
        if not loaded.evidence_ref:
            raise ValueError("fixture_image_acquisition_failed")
        analyses.append(
            {
                "image_index": index,
                "status": "unknown",
                "error": "unavailable",
                "evidence_ref": loaded.evidence_ref,
                "uncertainties": ["等待模型按需识图"],
            }
        )
    store = QQVisualMemoryStore(root / "memory.sqlite3")
    group, member, own = "private:29999:20002", "20002", "29999"
    event = "qq-watch-event:" + uuid4().hex
    metadata = "qq-protocol://sha256/" + hashlib.sha256(case["user"].encode()).hexdigest()
    identity = protocol_message_evidence_ref(group, member, "123456")
    profile = QQVerifiedProfileInput(
        group_id=group,
        member_id=member,
        display_name="公开样本测试",
        display_name_source="qq_metadata",
        verification="qq_protocol_verified",
        capture_id=event + ":profile",
        observed_at=datetime.now(timezone.utc),
        group_evidence=[metadata],
        member_evidence=[metadata],
    )
    store.record_protocol_message(
        profile,
        lambda p: QQVisualObservationInput(
            group_id=group,
            member_id=member,
            profile_record_id=p.record_id,
            capture_id=event,
            capture_item_id=event,
            observed_at=profile.observed_at,
            text=case["user"] + " [图片]",
            evidence=[metadata, identity, *[a["evidence_ref"] for a in analyses]],
            attribution_evidence=[metadata, identity],
            author_attribution="qq_protocol_verified",
            extraction_method="onebot11_message_array",
            message_id="123456",
            message_id_source="qq_metadata",
            image_analysis=analyses,
        ),
    )
    return store, group, member, own, event, cache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--main-config", type=Path, required=True)
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--vision-key-file", type=Path)
    credentials.add_argument("--vision-config", type=Path)
    parser.add_argument("--web-key-file", type=Path, help="Optional private Tavily key; searches remain model-selected")
    parser.add_argument("--cases", nargs="+", default=["c02", "c04", "c05"])
    parser.add_argument(
        "--ask-search", action="store_true", help="Add an explicit public verification request to the fixture text"
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error("report must be a new file")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in manifest["cases"]}
    cases = [by_id[key] for key in args.cases]
    if args.ask_search:
        cases = [{**case, "user": case["user"] + " 请联网核实官方角色和换装名称。"} for case in cases]
    cases += [
        {**by_id["c07"], "id": "no_vision", "user": "这张图只是先存一下，不用看图，也不用评价。回复一句收到就行。"}
    ]
    main_config = json.loads(args.main_config.read_text(encoding="utf-8"))
    report = {
        "scope": "isolated public/synthetic fixtures; no QQ sends; no production memory",
        "main_model": main_config["model"],
        "vision_model": "kimi-k2.6",
        "thinking": "disabled",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rows": [],
    }
    key = (
        key_from_file(args.vision_key_file)
        if args.vision_key_file
        else vision_tool.read_config(args.vision_config).api_key.get_secret_value()
    )
    original_invoke = vision_tool.QQVisionTool.invoke
    with tempfile.TemporaryDirectory(prefix="qq-vision-smoke-") as folder:
        root = Path(folder)
        for case in cases:
            case_root = root / case["id"]
            case_root.mkdir()
            store, group, member, own, event, cache = fixture(case_root, case, args.manifest.parent)
            config = case_root / "vision.json"
            config.write_text(
                json.dumps({"enabled": True, "api_key": key, "image_cache_dir": str(cache)}), encoding="utf-8"
            )
            os.chmod(config, 0o600)
            os.environ["NC_QQ_VISION_TOOL_CONFIG"] = str(config)
            os.environ["NC_MODEL_REQUEST_LOG_ENABLED"] = "false"
            # Preserve the deployed main provider, persona and helper bindings.
            # Enable the same compact path for this isolated test account.
            replica = {
                **main_config,
                "qq_latency": {
                    **(main_config.get("qq_latency") or {}),
                    "qq_latency_conversations": None,
                    "qq_web_policy": "after_gate",
                },
            }
            model_path = case_root / "main.json"
            model_path.write_text(json.dumps(replica), encoding="utf-8")
            os.chmod(model_path, 0o600)
            visual = []

            def captured(self, *a, **kw):
                start = time.perf_counter()
                result, usage = original_invoke(self, *a, **kw)
                visual.append(
                    {"seconds": round(time.perf_counter() - start, 3), "result": result, "usage": usage.model_dump()}
                )
                return result, usage

            vision_tool.QQVisionTool.invoke = captured
            deadline = QQDeadline.after(60)
            binding = {
                "event_id": event,
                "transport": "direct",
                "group_id": group,
                "self_member_id": own,
                "trigger_member_id": member,
                "runtime_instance_id": "runtime:fixture",
                "manifest_revision": "manifest:fixture",
                "source_digest": "1" * 64,
                "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
            }
            trace = QQTurnTrace()
            start = time.perf_counter()
            kwargs = dict(
                memory_db=store.path, config_path=model_path, preset_path=Path("presets/builtin/misty.json").absolute()
            )
            prepared = decision.prepare_qq_turn(
                **kwargs,
                group_id=group,
                trigger_member_id=member,
                self_member_id=own,
                turn_binding=binding,
                _request_source_event_ids=[event],
                web_capabilities={
                    "enabled": bool(args.web_key_file),
                    "registered_sources": [],
                    "max_searches": 2,
                    "max_reads": 1,
                    "max_read_sources": 3,
                    "max_search_results": 5,
                },
                deadline=deadline,
                trace=trace,
            )
            web_context = None
            if prepared["kind"] == "prepared":
                if args.web_key_file and prepared.get("web_plan"):
                    web_context = asyncio.run(
                        run_web_plan(prepared["web_plan"], args.web_key_file, case_root, deadline)
                    )
                result = decision.resume_qq_response(
                    **kwargs,
                    prepare_id=prepared["prepare_id"],
                    snapshot_hash=prepared["snapshot_hash"],
                    turn_binding=binding,
                    deadline=deadline,
                    trace=trace,
                    runtime_web_context=web_context,
                )
            else:
                result = decision.QQReplyDecision.model_validate(prepared["decision"])
            row = {
                "case": case["id"],
                "question": case["user"],
                "expected": case.get("expected"),
                "seconds": round(time.perf_counter() - start, 3),
                "action": result.action,
                "outcome": result.outcome,
                "reply": result.reply,
                "model_call_counts": result.model_call_counts,
                "web_executed": web_context is not None,
                "web_plan": prepared.get("web_plan"),
                "web_context": web_context,
                "vision": visual,
                "trace": trace.snapshot(),
            }
            report["rows"].append(row)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {k: row[k] for k in ("case", "seconds", "outcome", "reply", "model_call_counts")},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    vision_tool.QQVisionTool.invoke = original_invoke
    return 0 if all(row["outcome"] == "ready" for row in report["rows"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
