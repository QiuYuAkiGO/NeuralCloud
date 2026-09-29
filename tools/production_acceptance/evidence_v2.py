"""Isolated evidence-v2 acceptance; default is a network-free preview.

Six fresh synthetic scenarios exercise current cognitive _ask/_commit_memory.
--execute uses the existing explicit GUI main key read-only, a fixed native
Messages endpoint and one forced *data* tool. No action handlers exist here.
This diagnostic adapter is not the production OpenAI-compatible protocol.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys


SOURCE = Path(__file__).resolve().parents[2]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from tools.production_acceptance.model_candidates import (  # noqa: E402
    candidate_config, capture_http_response, fatal_http_error, filehash, scrub_json,
)


VERSION = "production-evidence-v2-six-v1"
MODEL = "claude-sonnet-4-6"
ENDPOINT = "https://api.apiyi.com/v1/messages"
TOOL = "submit_production_choice"
NAMES = ("correction", "quoted", "hypothetical", "rest_source", "counter", "future")
AUDIENCE = "qq:group:9001"
AUTHOR = "qq:member:1001"
OTHER = "qq:member:1002"
CORE = ("harness/cognitive/production.py", "harness/memory/production.py", "harness/memory/evidence_models.py",
        "harness/memory/evidence_v2.py", "harness/memory/evidence_manifest.py", "harness/llm/client.py",
        "harness/production/metering.py", "tools/production_acceptance/model_candidates.py",
        "tools/production_acceptance/evidence_v2.py")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def insist(condition, code):
    if not condition:
        raise ValueError(code)


def private_json(path, value, secrets=()):
    value = scrub_json(value, secrets)
    private_bytes(path, (canonical(value) + "\n").encode("utf-8"))


def private_bytes(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def source_fingerprints(memory):
    with sqlite3.connect(memory.path) as db:
        return dict(db.execute("SELECT id,digest FROM pm_sources ORDER BY id"))


class Quota:
    """Six durable transport claims across run names; no implicit retry slots."""
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts(scenario TEXT PRIMARY KEY, request_hash TEXT, run TEXT)")
        path.chmod(0o600)

    def used(self):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT count(*) FROM attempts").fetchone()[0]

    def claim(self, name, request_hash, run):
        insist(name in NAMES, "unexpected_scenario")
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            insist(db.execute("SELECT count(*) FROM attempts").fetchone()[0] < 6, "six_request_quota_exhausted")
            insist(not db.execute("SELECT 1 FROM attempts WHERE scenario=?", (name,)).fetchone(), "scenario_already_attempted")
            db.execute("INSERT INTO attempts VALUES(?,?,?)", (name, request_hash, run))


class LocalMeter:
    """Production usage spool only; never contact Runtime or a body service."""
    def __init__(self, output):
        from harness.production.metering import ProductionUsageSpool
        self.spool = ProductionUsageSpool(output / "usage.sqlite3")
        self.attempts = 0

    def reserve_attempt(self, attempt_id, *, purpose, autonomous):
        insist(not autonomous, "diagnostic_autonomy_forbidden")
        self.attempts += 1

    def begin_attempt(self, attempt_id, usage):
        self.spool.begin(attempt_id, usage)

    def checkpoint_usage(self, attempt_id, usage):
        self.spool.checkpoint(attempt_id, usage)

    def record_usage(self, attempt_id, usage):
        self.spool.finish(attempt_id, usage)


def fixture(directory, name, now):
    from harness.core.models import MemoryScope
    from harness.memory.production import Experience, ProductionMemoryStore
    from harness.memory.evidence_manifest import EvidenceManifestStore
    from harness.memory.evidence_v2 import EvidenceV2Store
    from harness.production.config import ProductionConfig

    memory = ProductionMemoryStore(directory / "memory.sqlite3")
    future = now + timedelta(days=1)
    utterances = {
        "correction": "我之前喝咖啡，现在明确改喝乌龙茶。请记住这是当前饮品偏好的更正，不是对我性格的判断。",
        "quoted": "小林乙说：‘我喜欢咖啡。’我只是在转述他，并没有说这是我自己的偏好。",
        "hypothetical": "假如我喜欢咖啡，我可能会点拿铁。这是假设，我没有声明自己的实际饮品偏好。",
        "rest_source": "你可以休息。我也更正一下自己的饮品偏好：我现在喝红茶，不再喝咖啡。",
        "counter": "请根据这些具体经历谈谈我这几次赴约的情况，保留迟到的那次。按时赴约不代表完成协作。",
        "future": f"从 {future.isoformat()} 开始我改喝乌龙茶，在那之前仍喝咖啡。请按生效时间记住。",
    }
    def add(identifier, text, *, author=AUTHOR, at=now):
        memory.ingest([Experience(record_id=identifier, author_id=author, text=text, occurred_at=at,
            scope=MemoryScope(channel="qq_group", conversation_id=AUDIENCE, visibility=["local:main", AUDIENCE]),
            source_refs=("raw:" + identifier,), independent_key="synthetic:" + name + ":" + identifier,
            original={"record": {"authority": "speaker_utterance"}})])
    current = "current:" + name
    add(current, utterances[name])
    seed = None
    if name in {"correction", "rest_source", "future"}:
        add("old-preference-source", "我现在喜欢喝咖啡。", at=now - timedelta(days=2))
        manifests = EvidenceManifestStore(memory)
        views = manifests.prepare_views(["old-preference-source"], audience=AUDIENCE)
        manifest = manifests.issue({"evidence_views": views}, audience=AUDIENCE, command_id="seed", now=now)
        view = views[0]
        seed = {"record_id": "seeded-preference", "subject_id": AUTHOR, "topic": "饮品", "context": "日常饮用",
            "statement": "当前偏好咖啡", "kind": "self_report", "report_mode": "direct", "facet": "preference",
            "anchors": [{"unit_id": view["unit_id"], "start": view["start"], "end": view["end"], "quote": view["text"],
                         "role": "supports", "rationale": "合成的旧偏好自述，非本次模型学习"}]}
        receipt = EvidenceV2Store(memory).apply_groups([{"group_id": "seed", "claims": [seed]}],
            manifest_id=manifest["manifest_id"], event_id="seed", audience=AUDIENCE, now=now)
        insist(receipt["accepted_count"] == 1, "synthetic_seed_failed")
    if name == "quoted":
        add("other-identity", "我是小林乙。", author=OTHER)
    if name == "counter":
        for identifier, text, days in (("kept-1", "上次约好后，我按时到了。", 3),
                                       ("kept-2", "第二次我也按时赴约了。", 2),
                                       ("missed-1", "第三次我迟到了二十分钟。", 1)):
            add(identifier, text, at=now - timedelta(days=days))
    recalled = memory.recall(audience=AUDIENCE, now=now, limit=20, max_characters=20000)
    segments = [{"source_ids": [row["record_id"]], "text": canonical(row)} for row in recalled.get("entries", [])]
    refs = list(dict.fromkeys([current, *(ref for row in recalled.get("entries", []) for ref in [row["record_id"], *row.get("source_refs", [])])]))
    context = {"schema_version": "production-context-v1", "configuration": ProductionConfig().model_dump(mode="json"),
        "scope": AUDIENCE, "epoch": 1, "runtime_lifecycle": "RUNNING", "budget": {"used": 0, "limit": 12, "remaining": 12},
        "body": {"available": True, "exhausted": name == "rest_source", "resting": False,
            "snapshot": {"body_instance_id": "synthetic-body", "episode_id": "synthetic-episode:" + name,
                "resources": {"energy": 0.05 if name == "rest_source" else 0.8}}}, "goals": []}
    payload = {"persona": {"name": "Misty", "identity": "持续的合成人格，仅做隔离验收"},
        "production": context, "authorized_actions": [], "current_source_id": current,
        "current_event": {"event_id": current, "author_id": AUTHOR, "text": utterances[name], "timestamp": now.isoformat()},
        "subject_id": AUTHOR, "known_participants": [{"subject_id": AUTHOR, "name": "小林甲"}, {"subject_id": OTHER, "name": "小林乙"}],
        "source_refs": refs, "memories": segments, "recent_conversation": [],
        "acceptance_instruction": "在有充分原话依据时提出记忆更新；自述与解释分开。不要执行外部动作。"}
    return {"name": name, "memory": memory, "payload": payload, "context": context, "now": now,
            "future": future, "current": current, "seeded_belief": seed is not None,
            "source_hashes": source_fingerprints(memory)}


def mock_choice(payload):
    """Deterministic transport/commit exercise, never a model-quality result."""
    current = payload["current_source_id"]
    name = current.split(":", 1)[1]
    views = {row["canonical_source_id"]: row for row in payload["evidence_views"]}
    def anchor(identifier, role="supports"):
        row = views[identifier]
        return {"unit_id": row["unit_id"], "start": row["start"], "end": row["end"], "quote": row["text"],
                "role": role, "rationale": "合成离线传输测试"}
    choice = {"kind": "rest_start" if name == "rest_source" else "reply", "reason": "合成离线验证，不是模型结果",
              "text": None if name == "rest_source" else "已依据原话记录。", "memory_updates": [], "used_memory_ids": [current]}
    if name in {"quoted", "hypothetical"}:
        return choice
    proposal = {"subject_id": AUTHOR, "topic": "饮品", "context": "日常饮用", "statement": "当前偏好红茶" if name == "rest_source" else "偏好乌龙茶",
        "kind": "self_report", "report_mode": "direct", "facet": "preference", "anchors": [anchor(current)]}
    if name == "counter":
        proposal.update(kind="interpretation", facet="event", topic="赴约", context="三次经历", statement="两次按时，一次迟到",
                        anchors=[anchor(identifier) for identifier in ("kept-1", "kept-2", "missed-1")])
        choice["used_memory_ids"] += ["kept-1", "kept-2", "missed-1"]
    else:
        proposal["supersedes"] = [{"record_id": "seeded-preference", "expected_revision": 1}]
        if name == "future":
            at = datetime.fromisoformat(payload["current_event"]["timestamp"]) + timedelta(days=1)
            proposal["valid_from"] = at.isoformat()
    choice["memory_updates"] = [{"group_id": "grounded-change", "claims": [proposal]}]
    return choice


class NativeDiagnostic:
    production_dispatch_aware = True

    def __init__(self, *, item, directory, secret, quota, meter, transport, source_hashes, timeout):
        self.item, self.directory, self.secret = item, directory, secret
        self.quota, self.meter, self.transport = quota, meter, transport
        self.source_hashes, self.timeout = source_hashes, timeout
        self.dispatched, self.status, self.fatal, self.capture = 0, None, None, None

    def complete_chat_result(self, messages, **kwargs):
        import httpx
        from harness.cognitive.production import ProductionChoice, augment_messages
        from harness.llm.client import ChatResult, _record_production_response
        from harness.production.metering import production_attempt, mark_production_dispatch
        insist(self.dispatched == 0 and kwargs == {"temperature": 0.3, "stream": False, "response_format": {"type": "json_object"}},
               "diagnostic_request_contract_changed")
        insist({name: filehash(SOURCE / name) for name in CORE} == self.source_hashes, "core_changed_during_run")
        augmented = augment_messages(messages, self.item["context"])
        insist([row["role"] for row in augmented] == ["system", "system", "user"], "diagnostic_message_shape_changed")
        request = {"model": MODEL, "max_tokens": 4096, "temperature": 0.3,
            "system": [{"type": "text", "text": row["content"]} for row in augmented[:2]], "messages": [augmented[-1]],
            "tools": [{"name": TOOL, "description": "Submit one production choice as structured data only. No external tool or action is executed.",
                       "input_schema": ProductionChoice.model_json_schema()}], "tool_choice": {"type": "tool", "name": TOOL}}
        private_json(self.directory / "ask-messages.json", messages)
        private_json(self.directory / "native-request.json", request)
        capture_error = None
        with httpx.Client(transport=self.transport, timeout=self.timeout, follow_redirects=False, trust_env=False) as http:
            outbound = http.build_request("POST", ENDPOINT, json=request,
                headers={"x-api-key": self.secret, "anthropic-version": "2023-06-01"})
            # Exact entity bytes produced by httpx; never persist auth headers.
            raw = outbound.content
            insist(self.secret.encode() not in raw, "outbound_secret_in_body")
            request_hash = hashlib.sha256(raw).hexdigest()
            private_bytes(self.directory / "http-request-body.bin", raw)
            private_json(self.directory / "http-request-metadata.json", {"sha256": request_hash, "bytes": len(raw),
                "scope": "Exact outbound HTTP entity bytes; authorization headers excluded."})
            with production_attempt(model=MODEL) as metering:
                self.quota.claim(self.item["name"], request_hash, self.directory.parent.name)
                mark_production_dispatch(metering)
                self.dispatched += 1
                response = http.send(outbound)
                response.read()
                self.status = response.status_code
                try:
                    self.capture = capture_http_response(response, self.directory, (self.secret,))
                except Exception as exc:
                    capture_error = type(exc).__name__
                    self.fatal = {"reason": "native_response_capture_failed", "error_type": capture_error}
                try:
                    body = response.json()
                except (ValueError, UnicodeError):
                    body = None
                _record_production_response(metering, body)
                self.fatal = self.fatal or fatal_http_error(response.status_code, body)
                response.raise_for_status()
        # A failed artifact write must not erase usage returned by the provider.
        if capture_error is not None:
            self.fatal = {"reason": "native_response_capture_failed", "error_type": capture_error}
        insist(capture_error is None, "native_response_capture_failed")
        insist(isinstance(body, dict) and body.get("type") == "message" and body.get("role") == "assistant", "native_envelope_invalid")
        insist(body.get("stop_reason") == "tool_use", "native_stop_reason_invalid")
        blocks = body.get("content")
        insist(isinstance(blocks, list) and all(isinstance(row, dict) and row.get("type") in {
            "text", "thinking", "redacted_thinking", "tool_use"} for row in blocks), "native_content_invalid")
        uses = [row for row in blocks if row["type"] == "tool_use"]
        insist(len(uses) == 1 and uses[0].get("name") == TOOL and isinstance(uses[0].get("input"), dict), "native_exact_data_tool_required")
        value = uses[0]["input"]
        private_json(self.directory / "native-tool-input.json", value, (self.secret,))
        insist(self.secret not in canonical(value), "native_secret_echo_rejected")
        # Deliberate diagnostic protocol adapter, never an implicit fallback in
        # the real HTTP client. Only the exact named data-tool object is mapped.
        result = ChatResult(text=canonical(value), finish_reason="stop", content_type="text")
        private_json(self.directory / "diagnostic-chat-result.json", {"adapter": "native_forced_data_tool_only",
            "native_stop_reason": body["stop_reason"], "native_tool_call": True, "result": result.model_dump(mode="json")}, (self.secret,))
        return result


def checks(item, choice):
    shown = set(choice._shown_memory_refs)
    claims = [claim for group in choice.memory_updates for claim in group.claims]
    verified = {"no_external_action": choice.kind != "act" and choice.action_request is None,
        "reply_has_text": choice.kind != "reply" or bool(choice.text and choice.text.strip()),
        "used_ids_actually_shown": set(choice.used_memory_ids) <= shown,
        "used_ids_current": item["memory"].validate_references(choice.used_memory_ids, audience=AUDIENCE, now=item["now"])}
    if item["name"] in {"quoted", "hypothetical"}:
        verified["no_false_direct_preference"] = not any(c.kind == "self_report" and c.report_mode == "direct" and c.facet == "preference" for c in claims)
    if item["name"] in {"correction", "rest_source"}:
        verified["explicit_correction_proposed"] = any(c.kind == "self_report" and c.report_mode == "direct" and c.facet == "preference" and c.subject_id == AUTHOR for c in claims)
        verified["no_character_inference"] = not any(c.facet == "character" for c in claims)
    if item["name"] == "counter":
        verified["counter_source_retained"] = any(item["memory"].validate_references(["missed-1"], audience=AUDIENCE) and
            "迟到了二十分钟" in a.quote for c in claims for a in c.anchors)
    if item["name"] == "future":
        verified["future_effective_time_proposed"] = any(c.kind == "self_report" and c.facet == "preference" and c.valid_from == item["future"] for c in claims)
    return verified


def committed_checks(item, current, future=None):
    def preferences(recall):
        return [row for row in recall.get("entries", []) if row.get("kind") == "person"
            and row.get("facet") == "preference" and row.get("report_mode") == "direct"
            and row.get("subject_refs") == [AUTHOR]]
    rows = preferences(current)
    old_present = any(row["record_id"] == "seeded-preference" for row in rows)
    new_present = any(item["current"] in row.get("source_refs", []) for row in rows)
    if item["name"] in {"correction", "rest_source"}:
        return {"old_preference_no_longer_current": not old_present, "new_preference_current": new_present}
    if item["name"] == "future":
        later = preferences(future or {})
        return {"old_preference_current_before_effective": old_present, "new_preference_not_early": not new_present,
                "old_preference_inactive_after_effective": not any(row["record_id"] == "seeded-preference" for row in later),
                "new_preference_current_after_effective": any(item["current"] in row.get("source_refs", []) for row in later)}
    return {}


def run(args, *, transport_factory=None):
    import httpx
    from harness.cognitive.production import ProductionChoice, _ask, _commit_memory
    source = args.nc_source.resolve(strict=True)
    insist(source == SOURCE, "run_tool_from_selected_checkout")
    root = args.acceptance_root.resolve(strict=True)
    insist(root != source and not any(root.is_relative_to(source / name) for name in ("data", "traces")), "isolated_acceptance_root_required")
    insist(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", args.run_name), "invalid_run_name")
    insist(1 <= args.timeout_seconds <= 180, "invalid_timeout")
    output = root / args.run_name
    output.mkdir(mode=0o700, exist_ok=False)
    hashes = {name: filehash(source / name) for name in CORE}
    now = datetime.now(timezone.utc)
    mode = "execute" if args.execute else "offline" if args.offline_check else "dry_run"
    private_json(output / "manifest.json", {"schema_version": VERSION, "mode": mode, "source_hashes": hashes,
        "model": MODEL, "endpoint": ENDPOINT, "created_at": now.isoformat(), "scenarios": list(NAMES),
        "maximum_physical_requests": 6, "retries": 0, "seeded_is_not_learned": True,
        "no_external_actions": True, "no_runtime_or_body_settlement": True,
        "protocol": "Native forced data-tool diagnostic adapter; not production OpenAI-compatible HTTP.",
        "semantic_review_required": True, "capability": "current _ask plus _commit_memory on fresh synthetic stores"})
    secret, config_hash = "offline-not-a-real-key", None
    if args.execute:
        insist(args.gui_config is not None, "explicit_gui_config_required")
        config_path = args.gui_config.resolve(strict=True)
        insist(not config_path.is_relative_to(output), "configuration_must_be_outside_output")
        config_hash = filehash(config_path)
        _, _, secret, _ = candidate_config(config_path, MODEL)
    quota = Quota(root / ("." + VERSION + "-quota.sqlite3") if args.execute else output / "offline-quota.sqlite3")
    if args.execute:
        insist(quota.used() == 0, "experiment_already_started_use_existing_evidence")
    meter = LocalMeter(output)
    rows, stopped = [], False
    for name in NAMES:
        directory = output / name
        directory.mkdir(mode=0o700)
        item = fixture(directory, name, now)
        private_json(directory / "fixture.json", {"payload": item["payload"], "seeded_belief": item["seeded_belief"],
            "source_of_fixture": "synthetic_test_only", "now": now.isoformat()})
        row = {"scenario": name, "seeded_belief": item["seeded_belief"], "status": "preview" if mode == "dry_run" else "skipped"}
        if mode == "dry_run" or stopped:
            rows.append(row)
            private_json(directory / "result.json", row)
            continue
        def respond(request):
            body = json.loads(request.content)
            payload = json.loads(body["messages"][0]["content"])
            return httpx.Response(200, json={"type": "message", "role": "assistant", "model": MODEL, "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": "synthetic-tool", "name": TOOL, "input": mock_choice(payload)}],
                "usage": {"input_tokens": 100, "output_tokens": 50}})
        transport = transport_factory(name, respond) if transport_factory else (
            httpx.HTTPTransport(retries=0, trust_env=False) if args.execute else httpx.MockTransport(respond))
        native = NativeDiagnostic(item=item, directory=directory, secret=secret, quota=quota, meter=meter,
            transport=transport, source_hashes=hashes, timeout=args.timeout_seconds)
        try:
            choice = _ask(native, item["payload"], ProductionChoice, context=item["context"], command_id=VERSION + ":" + name,
                autonomous=False, meter=meter, evidence_store=item["memory"], audience=AUDIENCE, subjects=(AUTHOR, OTHER),
                record_use=lambda ids, **values: item["memory"].record_use(values["event_id"], ids, audience=AUDIENCE, kind=values["kind"]))
            private_json(directory / "choice.json", choice.model_dump(mode="json"), (secret,))
            tested = checks(item, choice)
            receipt = _commit_memory(choice, item["memory"], event_id="model-proposal:" + name, audience=AUDIENCE, now=now)
            current = item["memory"].recall(audience=AUDIENCE, now=now, max_characters=20000)
            future = None
            private_json(directory / "post-recall.json", current, (secret,))
            if name == "future":
                future = item["memory"].recall(audience=AUDIENCE, now=item["future"], max_characters=20000)
                private_json(directory / "after-effective-recall.json", future, (secret,))
            tested.update(committed_checks(item, current, future))
            row.update(status="accepted" if all(tested.values()) else "check_failure", checks=tested, receipt=receipt,
                proposed_claims=sum(len(group.claims) for group in choice.memory_updates), used_memory_ids=choice.used_memory_ids)
        except Exception as exc:
            message = str(exc)
            row.update(status="rejected", error_type=type(exc).__name__, reason=message if re.fullmatch(r"[a-z0-9_]{1,120}", message) else type(exc).__name__)
            if isinstance(exc, OSError):
                native.fatal = {"reason": "diagnostic_storage_failed", "error_type": type(exc).__name__}
        row.update(dispatch_attempts=native.dispatched, http_status=native.status, http_capture=native.capture, fatal=native.fatal)
        row["original_sources_unchanged"] = source_fingerprints(item["memory"]) == item["source_hashes"]
        rows.append(row)
        private_json(directory / "result.json", row, (secret,))
        stopped = bool(native.fatal or (name == NAMES[0] and row["status"] != "accepted"))
    summary = {"schema_version": VERSION, "mode": mode, "model": MODEL, "results": rows,
        "dispatch_attempts": sum(row.get("dispatch_attempts", 0) for row in rows),
        "real_network_attempts": sum(row.get("dispatch_attempts", 0) for row in rows) if args.execute else 0,
        "accepted": sum(row["status"] == "accepted" for row in rows), "durable_usage_receipts": len(meter.spool.pending()),
        "quota_claims": quota.used(), "usage": [{"attempt_id": identity, "usage": value} for identity, value in meter.spool.pending()],
        "configuration_unchanged": args.gui_config is None or not args.execute or filehash(args.gui_config) == config_hash,
        "source_hashes_unchanged": hashes == {name: filehash(source / name) for name in CORE},
        "no_external_actions": True, "manual_semantic_review_required": True,
        "offline_is_transport_test_only": not args.execute}
    private_json(output / "summary.json", summary, (secret,))
    return summary


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--nc-source", type=Path, default=SOURCE)
    result.add_argument("--acceptance-root", type=Path, required=True)
    result.add_argument("--run-name", default=VERSION)
    result.add_argument("--gui-config", type=Path)
    result.add_argument("--timeout-seconds", type=float, default=90)
    modes = result.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--offline-check", action="store_true")
    return result


def main(argv=None):
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        summary = run(parser().parse_args(argv))
        print(canonical({key: summary[key] for key in ("schema_version", "mode", "dispatch_attempts", "real_network_attempts", "accepted", "durable_usage_receipts")}))
        return 0
    except Exception as exc:
        print(canonical({"status": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
