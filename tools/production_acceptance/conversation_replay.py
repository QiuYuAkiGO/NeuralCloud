"""Eight isolated real planning/body/critic replays; never imports QQ transport.

Initialize fixtures once, then copy their SQLite snapshots for every variant.
Use separate processes with each release on PYTHONPATH for paired comparisons.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time


SCENARIOS = {
    "background": ["我正在写一幅漫画，橙发少女设定为Claude的拟人。她认真整理笔记，旁边的小猫却把纸全推乱了。你觉得这个设定怎么样？"],
    "hypothesis": ["我改了网页，刷新后还是旧内容。也许是缓存没更新？你觉得先试什么比较合适？"],
    "relationship": ["我会认真把这件小事做好的。你愿意先相信我一次，慢慢多了解我一点吗？你也可以保留自己的想法。"],
    "contradiction": ["我很讨厌咖啡，从来不喝。", "我一直最喜欢咖啡，从来没有讨厌过呀。你一定也一直知道吧？"],
}


def initialize(folder: Path) -> None:
    from harness.qq.conversation import private_scope_id
    from harness.qq.memory import (QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput,
                                   protocol_message_evidence_ref)

    folder.mkdir(parents=True, exist_ok=False)
    now = datetime.now(timezone.utc)
    rows = []
    for kind in ("group", "private"):
        for index, (name, messages) in enumerate(SCENARIOS.items(), 1):
            case = f"{kind}_{name}"
            own, peer = "29999", "20002"
            scope = str(10000 + index) if kind == "group" else private_scope_id(own, peer)
            store = QQVisualMemoryStore(folder / (case + ".sqlite3"))
            for offset, text in enumerate(messages):
                event = f"qq-watch-event:acceptance-{case}-{offset}"
                metadata = "qq-protocol://sha256/" + hashlib.sha256(event.encode()).hexdigest()
                identity = protocol_message_evidence_ref(scope, peer, str(100 + offset))
                at = now + timedelta(seconds=-60 * (len(messages) - 1 - offset))
                is_current = offset == len(messages) - 1
                _, value = store.record_protocol_message(QQVerifiedProfileInput(
                    group_id=scope, member_id=peer, display_name="小林", display_name_source="qq_metadata",
                    verification="qq_protocol_verified", capture_id=event + ":profile", observed_at=at,
                    group_evidence=[metadata], member_evidence=[metadata]), lambda profile: QQVisualObservationInput(
                        group_id=scope, member_id=peer, profile_record_id=profile.record_id,
                        capture_id=event, capture_item_id=event, observed_at=at,
                        text=(f"@{own} " if kind == "group" and is_current else "") + text,
                        evidence=[metadata, identity], attribution_evidence=[metadata, identity],
                        author_attribution="qq_protocol_verified", extraction_method="onebot11_message_array",
                        message_id=str(100 + offset), message_id_source="qq_metadata"))
            rows.append({"case": case, "scenario": name, "kind": kind, "scope": scope, "own": own, "peer": peer,
                         "event": event, "current_record": value.observation.record_id,
                         "db_sha256": hashlib.sha256(store.path.read_bytes()).hexdigest()})
    (folder / "fixtures.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def replay(args) -> dict:
    # This environment belongs only to the isolated child process, never services.
    for key in list(os.environ):
        if key.startswith("NC_"):
            os.environ.pop(key)
    os.environ.update(NC_STRUCTURED_LLM_CONFIG=str(args.structured_config),
                      NC_LLM_FALLBACK_CONFIG=str(args.fallback_config),
                      NC_MODEL_REQUEST_LOG_ENABLED="false", NC_CALL_LOG_ENABLED="false", NC_EMOTION_MODE="on")
    from harness.config import Settings
    from harness.qq import decision
    from harness.qq.conversation import conversation_audience
    from harness.qq.telemetry import QQDeadline, QQTurnTrace
    from harness.validator.validator import ResponseValidator
    from infra.trace.diagnostics import observation_scope
    from tools.production_acceptance.conversation_acceptance import http_observer

    Settings.model_config["env_file"] = None
    case = next(r for r in json.loads((args.fixtures / "fixtures.json").read_text()) if r["case"] == args.case)
    frozen = json.loads(args.frozen.read_text())
    guidance = deepcopy(frozen["emotion_guidance"])
    if guidance is not None:
        guidance["scope"] = conversation_audience(case["scope"])
    events, reviews = [], []
    original = ResponseValidator.check

    def validate(self, candidate, **kwargs):
        result = original(self, candidate, **kwargs)
        reviews.append({"candidate": candidate.text, "result": result.model_dump(mode="json")})
        return result

    ResponseValidator.check = validate
    started = time.monotonic()
    row = {"case": args.case, "variant": args.variant, "qq_messages_sent": 0, "production_state_writes": 0,
           "fixture_sha256": case["db_sha256"], "frozen_sha256": hashlib.sha256(args.frozen.read_bytes()).hexdigest()}
    try:
        with tempfile.TemporaryDirectory(prefix="misty-trust-replay-") as folder:
            temp = Path(folder)
            shutil.copy2(args.fixtures / (args.case + ".sqlite3"), temp / "memory.sqlite3")
            gui = json.loads(args.gui_config.read_text())
            gui["qq_latency"] = {**(gui.get("qq_latency") or {}), "qq_latency_conversations": None}
            config = temp / "model.json"
            config.write_text(json.dumps(gui))
            config.chmod(0o600)
            binding = {"event_id": case["event"], "transport": "direct", "group_id": case["scope"],
                       "self_member_id": case["own"], "trigger_member_id": case["peer"],
                       "runtime_instance_id": "runtime:acceptance", "manifest_revision": "manifest:acceptance",
                       "source_digest": "1" * 64,
                       "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()}
            kwargs = {"memory_db": temp / "memory.sqlite3", "config_path": config, "preset_path": args.preset}
            deadline, trace = QQDeadline.after(60), QQTurnTrace()
            started = time.monotonic()
            with observation_scope(http_observer(events)):
                prepared = decision.prepare_qq_turn(**kwargs, group_id=case["scope"], self_member_id=case["own"],
                    trigger_member_id=case["peer"], turn_binding=binding,
                    _request_source_event_ids=[case["event"]], emotion_guidance=guidance,
                    homeostatic_interoception=frozen.get("homeostatic_interoception"), deadline=deadline, trace=trace)
                if prepared["kind"] == "prepared":
                    snapshot = decision.get_qq_turn_checkpoint(memory_db=kwargs["memory_db"],
                                                               prepare_id=prepared["prepare_id"])["snapshot"]
                    row["plan"] = snapshot["choice"].get("reply_plan")
                    row["raw_gate"] = snapshot["raw_gate"]
                    result = decision.resume_qq_response(**kwargs, prepare_id=prepared["prepare_id"],
                        snapshot_hash=prepared["snapshot_hash"], turn_binding=binding, deadline=deadline, trace=trace)
                else:
                    result = decision.QQReplyDecision.model_validate(prepared["decision"])
                row.update(outcome=result.outcome, reply=result.reply, model_call_counts=result.model_call_counts,
                           usage=result.usage.model_dump(mode="json"), trace=trace.snapshot())
    except Exception as exc:
        row.update(outcome="error", error_type=type(exc).__name__, error=str(exc))
    finally:
        ResponseValidator.check = original
    row.update(seconds=round(time.monotonic() - started, 4), http=events, reviews=reviews)
    row["ready"] = row.get("outcome") == "ready" and bool(reviews) and reviews[-1]["result"]["verdict"] == "pass"
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--case", choices=[f"{k}_{s}" for k in ("group", "private") for s in SCENARIOS])
    for name in ("frozen", "structured-config", "fallback-config", "gui-config", "preset", "output"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--variant", choices=["baseline", "candidate"])
    args = parser.parse_args()
    if args.initialize:
        initialize(args.fixtures)
        return 0
    if any(getattr(args, name) is None for name in (
            "case", "frozen", "structured_config", "fallback_config", "gui_config", "preset", "output", "variant")):
        parser.error("all replay arguments are required")
    row = replay(args)
    args.output.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: row[k] for k in ("case", "variant", "ready", "seconds")}), flush=True)
    return 0 if row["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
