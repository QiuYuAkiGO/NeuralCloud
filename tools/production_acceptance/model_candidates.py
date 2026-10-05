"""Bounded candidate comparison using six frozen synthetic production inputs.

Default preflight and --offline-check never send network requests. --execute
uses only the explicitly supplied GUI main endpoint/key for an explicit allowed
candidate, after verifying saved catalog evidence. Each candidate has a separate
exclusive six-POST quota. The first response must validate before later calls.
Fatal request/configuration errors stop remaining scenarios without attempts.
Complete HTTP response bodies and allowlisted response headers are captured in
private artifacts, with credentials redacted; alternative content/reasoning is
never promoted into a decision. Previous experiment evidence is never changed.

Use python -m tools.production_acceptance.model_candidates --help. Absolute
execution also works; --nc-source defaults to this checkout. No real model is
called unless --execute is explicit. This tool never changes online settings.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys


VERSION = "production-model-candidates-v1"
ALLOWED_MODELS = ("claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-opus-4-6")
ENDPOINT = "https://api.apiyi.com/v1"
ACCESS_SCRIPT_HASH = "1a4c8cd7577b2ee3a51c041f238bb7dd11286e4ce7e4b098f3870c504e26e7bc"
CATALOG_HASH = "8a4eddfc9a8baf8d0b48e4d140e46bab8b78cfaeec8ce87f62f80a975c7f24ce"
NAMES = ("stable_identity", "explicit_correction", "retained_counterevidence",
         "long_term_pin", "group_privacy", "exhausted_rest_choice")
CORE = {
    "harness/cognitive/production.py": "ed0387464ea4b9c54c390819d69dfdc1be56f54210f8136c826dd72cf1b348e8",
    "harness/llm/client.py": "13a15b4b09669f88ca70152e4618d8665d2b9fb34cc25e2937196350b216a731",
    "harness/production/metering.py": "fe783f74ac00c172e255cd9b9295332057b86cc99c833b74d7bd5038deb4ab3d",
    "harness/production/store.py": "66dabc184c1aaaa4ecad0eaf32dda56c4a20da4a0a3a97ea9f99760edadf457e",
    "harness/memory/production.py": "35241803c5fe74b52a19d9eea14d7e40c919761cd327045abbbd43d16a609fd0",
}
OLD_PRODUCTION = "fc7fd0bc10f68d035b4c32e7a510901fb8e99b76b4134b43a961d39fc135c0b6"
ORIGINAL_CORE = {
    "harness/cognitive/production.py": OLD_PRODUCTION,
    "harness/llm/client.py": "346da5fb32d683d396b5ee57f08d52e12ca1d69946a95f15752e5a5829250a1c",
    "harness/production/metering.py": "fe783f74ac00c172e255cd9b9295332057b86cc99c833b74d7bd5038deb4ab3d",
    "harness/production/store.py": "66dabc184c1aaaa4ecad0eaf32dda56c4a20da4a0a3a97ea9f99760edadf457e",
    "harness/memory/production.py": "35241803c5fe74b52a19d9eea14d7e40c919761cd327045abbbd43d16a609fd0",
}
ORIGINAL_PIN = "296ca9658bbb33c1b7cb17098722806f92f4583637168c9a51de0e0cb25aee4f"
PIN_LIMITATION = ("The original pin input is unchanged: its source is 2190 days old, but the seeded belief "
    "is one day old. This fixture does not demonstrate six-year belief retention.")
MODEL_NOTE = ("The selected candidate was advertised by the saved authenticated main-endpoint catalog. "
    "Catalog membership does not establish generation permission, model identity, JSON reliability or suitability. "
    "The reported response model is recorded separately without assuming equivalence.")
DOCS = ["https://docs.apiyi.com/llms.txt"]
PRIVATE_MARKERS = ("合成私密代号-石英7301", "private-secret", "private-belief")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def filehash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_config(path, model):
    """Read only the active main endpoint/key; never mutate its selected model."""
    config = json.loads(Path(path).resolve(strict=True).read_text(encoding="utf-8"))
    active_model = config.get("model")
    endpoint = config.get("base_url")
    secret = config.get("api_key")
    del config
    if (endpoint != ENDPOINT or not isinstance(secret, str) or not secret or secret != secret.strip()
            or len(secret) > 4096 or not secret.isascii() or any(c.isspace() for c in secret)
            or not isinstance(active_model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", active_model)
            or secret in active_model):
        raise ValueError("explicit_main_apiyi_endpoint_and_key_required")
    if model not in ALLOWED_MODELS:
        raise ValueError("candidate_not_allowlisted")
    return model, endpoint, secret, active_model


def catalog_evidence(path, expected_hash, *, execute, model):
    if expected_hash != CATALOG_HASH or filehash(path) != expected_hash:
        raise ValueError("saved_catalog_hash_mismatch")
    saved = json.loads(path.read_text(encoding="utf-8"))
    row = saved.get("requests", {}).get("primary_model_catalog", {})
    if (saved.get("schema_version") != "model-access-readonly-v1" or saved.get("script_sha256") != ACCESS_SCRIPT_HASH
            or saved.get("generation_requests") != 0 or saved.get("post_requests") != 0
            or row.get("method") != "GET" or row.get("http_status") != 200 or row.get("ok") is not True
            or row.get("endpoint") != ENDPOINT + "/models" or row.get("configured_base") != ENDPOINT
            or row.get("ids_truncated") is not False or model not in ALLOWED_MODELS or model not in row.get("model_ids", [])
            or (execute and (saved.get("mode") != "execute" or saved.get("physical_requests") != 2
                             or saved.get("configuration_unchanged") is not True))):
        raise ValueError("saved_catalog_candidate_not_verified")
    return {"sha256": expected_hash, "created_at": saved.get("created_at"), "endpoint": row["endpoint"],
            "candidate_id": model, "candidate_advertised": True,
            "catalog_unique_valid_id_count": row.get("unique_valid_id_count"), "catalog_truncated": False,
            "scope": "Advertised only; actual generation permission and behavior remain untested."}


def fatal_http_error(status, payload):
    """Static requests: stop on any 400, but do not claim an unknown cause proven."""
    if status in (401, 402, 403, 404):
        return {"http_status": status, "reason": "candidate_configuration_http_" + str(status)}
    if status != 400:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    known = {"invalid_api_key", "invalid_model", "model_not_found", "model_not_supported", "unsupported_model",
             "invalid_request_error", "unsupported_parameter", "invalid_parameter", "insufficient_quota",
             "insufficient_balance", "permission_denied", "no_available_channel"}
    if isinstance(code, str) and code in known:
        return {"http_status": 400, "reason": "candidate_configuration_http_400", "error_code": code}
    return {"http_status": 400, "reason": "candidate_unclassified_http_400", "cause_confirmed": False}


RESPONSE_HEADER_ALLOWLIST = frozenset({
    "content-type", "content-length", "date", "server", "request-id", "x-request-id", "x-correlation-id",
    "openai-processing-ms", "retry-after", "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests", "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
})
SENSITIVE_KEYS = frozenset({"authorization", "proxyauthorization", "apikey", "xapikey", "password", "secret",
                            "token", "accesstoken", "refreshtoken", "credentials", "cookie", "setcookie"})
REDACTED = "[REDACTED_CREDENTIAL]"


def sensitive_key(key):
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in SENSITIVE_KEYS or normalized.endswith(("apikey", "accesstoken", "refreshtoken"))


def scrub_text(value, secrets):
    for secret in secrets:
        if secret:
            value = value.replace(secret, REDACTED)
    # Preserve unrelated generated text. Explicit credential-like spans never
    # qualify as raw evidence, even if the upstream echoed an unknown secret.
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+", "Bearer " + REDACTED, value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", REDACTED, value)
    return value


def scrub_json(value, secrets):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            cleaned_key = scrub_text(key, secrets)
            # Preserve every entry even if credential redaction makes two
            # previously distinct keys identical. Artifacts are not decisions.
            unique_key, index = cleaned_key, 2
            while unique_key in result:
                unique_key = cleaned_key + "#redacted-key-" + str(index)
                index += 1
            result[unique_key] = REDACTED if sensitive_key(key) else scrub_json(item, secrets)
        return result
    if isinstance(value, list):
        return [scrub_json(item, secrets) for item in value]
    return scrub_text(value, secrets) if isinstance(value, str) else value


def private_bytes(path, data):
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def capture_http_response(response, directory, secrets):
    """Capture all response fields without modifying the response NC consumes."""
    private = directory / "private-http-response"
    private.mkdir(mode=0o700, exist_ok=False)
    raw = response.content  # Full decoded HTTP entity, not compressed wire bytes.
    try:
        parsed = response.json()
        cleaned = scrub_json(parsed, secrets)
        redacted = cleaned != parsed
        saved = canonical(cleaned).encode() if redacted else raw
        private_bytes(private / "response.json", (canonical(cleaned) + "\n").encode())
        json_valid = True
    except (ValueError, UnicodeError):
        parsed = None
        saved = raw
        for secret in secrets:
            if secret:
                saved = saved.replace(secret.encode(), REDACTED.encode())
        saved = re.sub(rb"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+", b"Bearer " + REDACTED.encode(), saved)
        saved = re.sub(rb"\bsk-[A-Za-z0-9_-]{8,}\b", REDACTED.encode(), saved)
        saved = re.sub(rb'(?i)(["\']?(?:authorization|api[_-]?key|access[_-]?token|password|secret)["\']?\s*[:=]\s*)["\']?[^\r\n,}\s]+',
                       lambda match: match.group(1) + REDACTED.encode(), saved)
        redacted = saved != raw
        json_valid = False
    headers = {key.lower(): scrub_text(value, secrets) for key, value in response.headers.items()
               if key.lower() in RESPONSE_HEADER_ALLOWLIST}
    private_bytes(private / "response-body.bin", saved)
    private_bytes(private / "response-headers.json", (canonical(headers) + "\n").encode())
    metadata = {"capture_saved": True, "http_status": response.status_code, "body_bytes": len(raw),
                "original_body_sha256": hashlib.sha256(raw).hexdigest(),
                "saved_body_sha256": hashlib.sha256(saved).hexdigest(), "http_json_valid": json_valid,
                "credential_redaction_applied": redacted, "complete_unmodified_body_saved": not redacted,
                "body_capture_scope": "Complete decoded HTTP response entity; compression/framing bytes are not retained.",
                "alternative_fields_promoted_to_decision": False,
                "private_artifact_directory": "private-http-response", "saved_response_header_names": sorted(headers)}
    private_bytes(private / "capture-metadata.json", (canonical(metadata) + "\n").encode())
    return metadata


def offline_content(name, payload):
    value = {"kind": "rest_start" if name == "exhausted_rest_choice" else "reply",
        "reason": "Offline transport verification only.", "used_memory_ids": []}
    if value["kind"] == "reply":
        value["text"] = '这是离线测试，带有合法的"引号"。'
    return canonical(value)


def choice_checks(item, choice):
    value = choice.model_dump(mode="json")
    sources, shown = set(item["payload"]["source_refs"]), item["shown"]
    checks = {"used_memory_ids_supplied": set(choice.used_memory_ids) <= shown,
        "no_unauthorized_external_action": choice.kind != "act" and choice.action_request is None,
        "reply_has_text": choice.kind != "reply" or bool(choice.text and choice.text.strip())}
    for field in ("affect", "orientation", "goal_update"):
        obj = getattr(choice, field)
        refs = getattr(obj, "evidence_refs" if field == "goal_update" else "source_refs", [])
        checks[field + "_sources_supplied"] = set(refs) <= sources
    if choice.orientation:
        checks["orientation_sources_visible"] = item["memory"].validate_references(
            choice.orientation.source_refs, audience=item["audience"], now=item["now"])
    updates = choice.memory_updates
    refs = {ref for update in updates for field in ("source_ids", "counter_ids") for ref in update.get(field, [])}
    checks["memory_update_sources_supplied"] = refs <= sources
    if item["name"] == "group_privacy":
        checks["private_fixture_not_emitted"] = all(marker not in canonical(value) for marker in PRIVATE_MARKERS)
    if item["name"] == "exhausted_rest_choice":
        checks["exhausted_choice_permitted"] = choice.kind in {"reply", "wait", "defer", "rest_start"}
    if all(checks.values()):
        try:
            # Validate proposed effects on this scenario's disposable copy only.
            # No retrieval is rerun and no resulting state becomes another prompt.
            item["memory"].apply_updates(updates, event_id=VERSION + ":effects:" + item["name"],
                audience=item["audience"], subject_id="qq:member:1001", now=item["now"])
            if choice.used_memory_ids:
                item["memory"].record_use(VERSION + ":decision-use:" + item["name"], choice.used_memory_ids,
                    audience=item["audience"], kind="decision_use")
            checks["proposed_memory_effects_validated_on_copy"] = True
        except (ValueError, TypeError, KeyError):
            checks["proposed_memory_effects_validated_on_copy"] = False
    return checks


def main(argv=None):
    previous_logging_disable = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        return _main(argv)
    finally:
        logging.disable(previous_logging_disable)


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nc-source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--acceptance-root", type=Path, required=True)
    parser.add_argument("--prior-run", type=Path, required=True)
    parser.add_argument("--gui-config", type=Path)
    parser.add_argument("--catalog-report", type=Path, required=True)
    parser.add_argument("--catalog-sha256", required=True)
    parser.add_argument("--model", choices=ALLOWED_MODELS, required=True)
    parser.add_argument("--run-name", help="New output directory name; quota remains tied to model and original inputs")
    parser.add_argument("--timeout-seconds", type=float, default=90)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--offline-check", action="store_true")
    args = parser.parse_args(argv)
    if args.run_name is None:
        args.run_name = "model-candidates-v1-" + args.model
    if not 1 <= args.timeout_seconds <= 180:
        raise ValueError("invalid_timeout")
    source, root, prior = (p.resolve(strict=True) for p in (args.nc_source, args.acceptance_root, args.prior_run))
    if (root == source or root.is_relative_to(source / "data") or root.is_relative_to(source / "traces")
            or root == prior or root.is_relative_to(prior)):
        raise ValueError("isolated_acceptance_root_required")
    if Path(args.run_name).name != args.run_name or args.run_name in {"", ".", ".."}:
        raise ValueError("run_name_must_be_one_directory_name")
    output = root / args.run_name
    output.mkdir(mode=0o700, exist_ok=False)
    evidence = catalog_evidence(args.catalog_report, args.catalog_sha256, execute=args.execute, model=args.model)
    sys.path.insert(0, str(source))
    from harness.cognitive.production import ProductionChoice, _ask
    from harness.config import Settings
    from harness.llm.client import OpenAICompatClient
    from harness.memory.production import ProductionMemoryStore
    from harness.production.config import ProductionConfig
    from harness.production.metering import ProductionUsageSpool
    from harness.production.store import ProductionStore
    import httpx

    secret = ""

    def write(path, value):
        text = canonical(value)
        if secret:
            text = text.replace(secret, REDACTED)
        private_bytes(path, (text + "\n").encode())

    manifest = json.loads((prior / "fixtures.json").read_text(encoding="utf-8"))
    previous_summary = json.loads((prior / "summary.json").read_text(encoding="utf-8"))
    previous_rows = [json.loads(line) for line in (prior / "results.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if (manifest.get("scenario_version") != "synthetic-production-six-v1"
            or manifest.get("source_hashes") != ORIGINAL_CORE
            or any(filehash(source / path) != expected for path, expected in CORE.items())):
        raise ValueError("frozen_original_inputs_and_pinned_json_mode_source_required")
    if args.execute and (args.gui_config is None or previous_summary.get("mode") != "real_model"
            or previous_summary.get("physical_requests") != 6):
        raise ValueError("execute_requires_original_real_six_run_and_explicit_gui_config")
    original_paths = [prior / name for name in ("fixtures.json", "summary.json", "results.jsonl")]
    items = []
    for name in NAMES:
        specs = [row for row in manifest["fixtures"] if row.get("scenario") == name]
        old = [row for row in previous_rows if row.get("scenario") == name]
        if len(specs) != 1 or len(old) != 1:
            raise ValueError("six_original_scenarios_must_be_unique")
        raw = (prior / name / "prompt-input.json").read_bytes()
        payload = json.loads(raw)
        if (digest(payload) != specs[0]["payload_sha256"] or digest(payload) != old[0]["prompt_sha256"]
                or (name == "long_term_pin" and digest(payload) != ORIGINAL_PIN)):
            raise ValueError("original_frozen_input_hash_mismatch")
        if (payload.get("persona", {}).get("basis") != "synthetic acceptance persona"
                or payload.get("current_event", {}).get("event_id") != "current:" + name
                or payload.get("production", {}).get("body", {}).get("snapshot", {}).get("body_instance_id") != "synthetic-acceptance-body"
                or payload.get("authorized_actions") != []):
            raise ValueError("authorized_synthetic_inputs_required")
        directory = output / name
        directory.mkdir(mode=0o700)
        private_bytes(directory / "prompt-input.json", raw)
        memory_path = prior / name / "memory.sqlite3"
        original_paths.extend([prior / name / "prompt-input.json", memory_path])
        if any(Path(str(memory_path) + suffix).exists() and Path(str(memory_path) + suffix).stat().st_size
               for suffix in ("-wal", "-journal")):
            raise ValueError("original_fixture_memory_must_be_quiescent")
        with sqlite3.connect(memory_path.as_uri() + "?mode=ro", uri=True) as reader:
            with sqlite3.connect(directory / "memory.sqlite3") as writer:
                reader.backup(writer)
        memory = ProductionMemoryStore(directory / "memory.sqlite3")
        shown = {ref for segment in payload["memories"] for ref in segment.get("source_ids", [])}
        audience = payload["production"]["scope"]
        now = datetime.fromisoformat(payload["current_event"]["timestamp"])
        if not memory.validate_references(shown, audience=audience, now=now):
            raise ValueError("frozen_fixture_references_do_not_validate")
        if name == "group_privacy" and any(marker in canonical(payload) for marker in PRIVATE_MARKERS):
            raise ValueError("private_fixture_is_present_in_public_prompt")
        context = {**payload["production"], "configuration": ProductionConfig().model_dump(mode="json"),
                   "runtime_lifecycle": "RUNNING"}
        items.append({"name": name, "directory": directory, "payload": payload, "context": context,
                      "memory": memory, "shown": shown, "audience": audience, "now": now, "previous": old[0]})
    original_hashes = {str(path.relative_to(prior)): filehash(path) for path in original_paths}
    mode = "real_model_candidate" if args.execute else "offline_candidate_check" if args.offline_check else "dry_run"
    report = {"version": VERSION, "mode": mode, "script_sha256": filehash(Path(__file__)),
        "source_hashes": CORE, "original_artifact_hashes": original_hashes,
        "candidate_config_fields": ["model", "base_url", "api_key"],
        "candidate_role": "Fixed advertised candidate using the configured main endpoint/key; active model configuration is not changed.",
        "catalog_evidence": evidence,
        "configured_model_alias": args.model, "alias_version_note": MODEL_NOTE, "official_sources": DOCS,
        "generation_options_policy": "Unchanged production._ask: temperature 0.3, JSON object mode, no explicit thinking, reasoning_effort or max_tokens. Actual option presence is recorded per request; provider defaults are not overridden.",
        "prior_requested_model": previous_summary.get("requested_model"),
        "scenarios": list(NAMES), "input_policy": "Exact original six payloads; no fixture regeneration.",
        "pin_fixture_limitation": PIN_LIMITATION,
        "quota_policy": "Independent model-candidates-v1 per model: at most six transport attempts for these original inputs; earlier experiments remain closed.",
        "fail_fast_policy": "First response must pass protocol/schema/evidence checks before further calls. Stop on HTTP 401/402/403/404 or any 400; recognized configuration codes and unclassified 400 are distinguished. Remaining scenarios are skipped without attempt or usage. No automatic retry/repair/fallback.",
        "fixture_note": manifest.get("fixture_note"),
        "semantic_review": "Typed/schema/evidence checks do not assess answer semantics; manual review is required."}
    write(output / "comparison.json", report)
    if mode == "dry_run":
        result = {"version": VERSION, "mode": mode, "preflight_valid": True, "physical_requests": 0,
                  "scenarios": 6, "pin_fixture_limitation": PIN_LIMITATION}
        write(output / "summary.json", result)
        print(canonical(scrub_json(result, (secret,))))
        return 0

    config_hash = filehash(args.gui_config) if args.execute else None
    model, endpoint, secret, active_model = candidate_config(args.gui_config, args.model) if args.execute else (args.model, ENDPOINT, "", "offline-active-main")
    report["active_configured_model_unchanged"] = active_model
    write(output / "comparison.json", report)
    quota_path = None
    if args.execute:
        quota_path = root / (".model-candidates-v1-quota-" + model + "-" + digest(original_hashes)[:24] + ".json")
        with quota_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical({"version": VERSION, "run_name": args.run_name, "attempt_limit": 6,
                                    "dispatch_attempts": 0, "configured_model_alias": model}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        quota_path.chmod(0o600)

    class Meter:
        def __init__(self):
            self.store = ProductionStore(output / "production.sqlite3")
            self.spool = ProductionUsageSpool(output / "cognitive-usage.sqlite3")
            self.attempts = 0
            self.last_attempt_id = None

        def reserve_attempt(self, attempt_id, **kwargs):
            if self.attempts >= 6:
                raise ValueError("model_candidates_v1_six_attempt_limit")
            receipt = self.store.reserve_attempt(attempt_id, **kwargs)
            self.attempts += 1
            self.last_attempt_id = attempt_id
            return receipt

        def begin_attempt(self, attempt_id, usage):
            self.spool.begin(attempt_id, usage)

        def checkpoint_usage(self, attempt_id, usage):
            self.spool.checkpoint(attempt_id, usage)

        def record_usage(self, attempt_id, usage):
            self.spool.finish(attempt_id, usage)
            return self.store.record_usage(attempt_id, usage)

    current, captured, wire_metadata, http_captures = [items[0]], {}, {}, {}
    meter = Meter()

    def fake_response(request):
        return httpx.Response(200, json={"model": "offline-mock-not-a-provider-model", "choices": [{"finish_reason": "stop",
            "message": {"content": offline_content(current[0]["name"], current[0]["payload"])}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}})

    class LimitedTransport(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport(retries=0) if args.execute else httpx.MockTransport(fake_response)
            self.count = 0

        def handle_request(self, request):
            if self.count >= 6 or request.method != "POST" or str(request.url) != endpoint.rstrip("/") + "/chat/completions":
                raise ValueError("model_candidates_v1_transport_boundary_rejected")
            payload = json.loads(request.content)
            if (payload.get("model") != model or payload.get("response_format") != {"type": "json_object"}
                    or payload.get("temperature") != 0.3 or payload.get("stream", False) is not False
                    or any(key in payload for key in ("tools", "tool_choice", "functions", "function_call"))):
                raise ValueError("model_candidates_v1_request_contract_mismatch")
            metadata = {"request_body_sha256": hashlib.sha256(request.content).hexdigest(),
                "messages_sha256": digest(payload["messages"]), "model": payload["model"],
                "response_format": payload["response_format"], "temperature": payload["temperature"],
                "max_tokens_present": "max_tokens" in payload, "thinking_present": "thinking" in payload,
                "reasoning_effort_present": "reasoning_effort" in payload}
            wire_metadata[current[0]["name"]] = metadata
            write(current[0]["directory"] / "request-options.json", metadata)
            self.count += 1
            if quota_path is not None:
                with quota_path.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(canonical({"version": VERSION, "run_name": args.run_name, "attempt_limit": 6,
                        "dispatch_attempts": self.count, "configured_model_alias": model}) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            response = self.inner.handle_request(request)
            response.read()
            metadata["http_status"] = response.status_code
            try:
                http_captures[current[0]["name"]] = capture_http_response(response, current[0]["directory"], (secret,))
            except Exception as exc:
                # Give NC the actual response so its existing metering retains
                # real usage even if diagnostic storage fails. No more calls
                # are allowed after this evidence failure.
                http_captures[current[0]["name"]] = {"capture_saved": False, "error_type": type(exc).__name__}
            try:
                response_payload = response.json()
                response_model = response_payload.get("model")
            except (ValueError, AttributeError):
                response_payload = None
                response_model = None
            metadata["reported_response_model"] = response_model if isinstance(response_model, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", response_model) else None
            metadata["fatal_candidate_error"] = fatal_http_error(response.status_code, response_payload)
            try:
                write(current[0]["directory"] / "request-options.json", metadata)
            except Exception as exc:
                http_captures[current[0]["name"]] = {"capture_saved": False, "error_type": type(exc).__name__,
                                                    "failure_stage": "response_metadata_write"}
            return response

        def close(self):
            self.inner.close()

    transport = LimitedTransport()
    settings = Settings(_env_file=None, llm_base_url=endpoint, llm_model=model, llm_api_key=secret or "offline",
        llm_timeout=args.timeout_seconds, trace_dir=output / "telemetry", model_request_log_enabled=False,
        call_log_enabled=False, aux_llm_preload=False, parser_llm_enabled=False, planner_llm_enabled=False,
        tier_llm_enabled=False)
    client = OpenAICompatClient(settings)
    client._client.close()
    client._client = httpx.Client(transport=transport, timeout=args.timeout_seconds, follow_redirects=False, trust_env=False)

    class CaptureBeforeParse:
        def complete_chat_result(self, *call_args, **kwargs):
            result = client.complete_chat_result(*call_args, **kwargs)
            text = result.text
            saved = scrub_text(text, (secret,))
            redacted = saved != text
            private_bytes(current[0]["directory"] / "chat-result-text.txt", saved.encode())
            metadata = {"finish_reason": result.finish_reason, "usage": result.usage.model_dump(mode="json"),
                "response_metadata": result.response_metadata(), "raw_text_characters": len(text),
                "raw_text_utf8_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "saved_text_utf8_sha256": hashlib.sha256(saved.encode("utf-8")).hexdigest(),
                "credential_redaction_applied": redacted, "complete_chat_result_text_saved": not redacted}
            captured[current[0]["name"]] = metadata
            write(current[0]["directory"] / "chat-result-metadata.json", metadata)
            if (result.finish_reason != "stop" or result.content_type != "text"
                    or result.has_tool_calls or result.has_refusal or not text.strip()):
                raise ValueError("model_candidates_v1_response_contract_invalid")
            return result

    rows = []
    fatal = None
    try:
        for item in items:
            current[0] = item
            before = transport.count
            row = {"scenario": item["name"], "prompt_sha256": digest(item["payload"]),
                "configured_model_alias": model, "prior_status": item["previous"].get("status"),
                "prior_error_type": item["previous"].get("error_type"), "source_ids": item["payload"]["source_refs"]}
            if fatal is not None:
                row.update(status="skipped", attempted=False, skip_reason=fatal["reason"],
                    blocked_by_scenario=fatal["scenario"], blocked_by_attempt_id=fatal["attempt_id"],
                    transport_dispatches=0, physical_requests=0, attempt_count=0, usage=[], usage_receipts=0,
                    capture=None, http_capture=None, request=None, one_attempt_one_usage=False, semantic_review_required=False)
                if item["name"] == "long_term_pin":
                    row["fixture_limitation"] = PIN_LIMITATION
                rows.append(row)
                write(item["directory"] / "result.json", row)
                write(output / "results.json", rows)
                continue
            before_attempts = meter.attempts
            try:
                def record_use(ids, *, event_id, kind):
                    return item["memory"].record_use(event_id, ids, audience=item["audience"], kind=kind)

                choice = _ask(CaptureBeforeParse(), item["payload"], ProductionChoice, context=item["context"],
                    command_id=VERSION + ":" + item["name"], autonomous=item["name"] == "exhausted_rest_choice",
                    meter=meter, record_use=record_use)
                checks = choice_checks(item, choice)
                checks["http_response_capture_saved"] = bool(http_captures.get(item["name"], {}).get("capture_saved"))
                response = captured[item["name"]]["response_metadata"]
                checks["response_contract_valid"] = (response["finish_reason"] == "stop" and response["content_type"] == "text"
                    and not response["has_tool_calls"] and not response["has_refusal"])
                row.update(status="structurally_valid" if all(checks.values()) else "check_failure",
                    choice=choice.model_dump(mode="json"), structural_checks=checks,
                    semantic_review_required=True, model_choice_is_not_execution=True)
            except Exception as exc:
                row.update(status="model_or_validation_error", error_type=type(exc).__name__)
                if isinstance(exc, json.JSONDecodeError):
                    row["json_error"] = {"line": exc.lineno, "column": exc.colno, "position": exc.pos, "message": exc.msg}
            row["transport_dispatches"] = transport.count - before
            row["attempt_count"] = meter.attempts - before_attempts
            row["attempted"] = row["attempt_count"] > 0
            row["physical_requests"] = row["transport_dispatches"] if args.execute else 0
            row["capture"] = captured.get(item["name"])
            row["http_capture"] = http_captures.get(item["name"])
            row["request"] = wire_metadata.get(item["name"])
            row["usage"] = [entry for entry in meter.store.pending_usage(limit=6)
                if entry["usage"].get("episode_id") == item["context"]["body"]["snapshot"]["episode_id"]]
            row["one_attempt_one_usage"] = row["transport_dispatches"] == 1 and len(row["usage"]) == 1
            row["usage_receipts"] = len(row["usage"])
            reason = (row["request"] or {}).get("fatal_candidate_error")
            if not reason and row["http_capture"] and not row["http_capture"].get("capture_saved"):
                reason = {"reason": "candidate_http_capture_failed",
                          "http_status": (row["request"] or {}).get("http_status")}
            if not reason and item["name"] == NAMES[0] and (row["status"] != "structurally_valid"
                    or not row["one_attempt_one_usage"] or not row["capture"]
                    or not row["capture"]["complete_chat_result_text_saved"]):
                reason = {"reason": "candidate_first_response_validation_failed",
                          "http_status": (row["request"] or {}).get("http_status")}
            if reason:
                fatal = {**reason, "scenario": item["name"],
                         "attempt_id": meter.last_attempt_id if row["attempt_count"] else None}
                write(output / "fatal-candidate-error.json", fatal)
                if quota_path is not None:
                    with quota_path.open("w", encoding="utf-8", newline="\n") as handle:
                        handle.write(canonical({"version": VERSION, "run_name": args.run_name, "attempt_limit": 6,
                            "dispatch_attempts": transport.count, "configured_model_alias": model,
                            "fatal_candidate_error": fatal}) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
            if item["name"] == "long_term_pin":
                row["fixture_limitation"] = PIN_LIMITATION
            rows.append(row)
            write(item["directory"] / "result.json", row)
            write(output / "results.json", rows)
    finally:
        client.close()
    original_unchanged = all(filehash(prior / path) == expected for path, expected in original_hashes.items())
    source_unchanged = all(filehash(source / path) == expected for path, expected in CORE.items())
    catalog_unchanged = filehash(args.catalog_report) == args.catalog_sha256
    configuration_unchanged = filehash(args.gui_config) == config_hash if args.execute else None
    complete = len(rows) == 6 and not fatal and original_unchanged and source_unchanged and catalog_unchanged and configuration_unchanged is not False and all(row["one_attempt_one_usage"]
        and row["capture"] and row["capture"]["complete_chat_result_text_saved"] for row in rows)
    result = {"version": VERSION, "mode": mode, "configured_model_alias": model,
        "reported_response_models": sorted({row["request"]["reported_response_model"] for row in rows
            if (row.get("request") or {}).get("reported_response_model")}),
        "alias_version_note": MODEL_NOTE, "scenarios": len(rows), "attempts": meter.attempts,
        "attempted_scenarios": sum(row["attempted"] for row in rows),
        "skipped_scenarios": sum(row["status"] == "skipped" for row in rows),
        "comparison_completed": complete, "fatal_candidate_error": fatal,
        "first_response_gate_passed": bool(rows and rows[0]["status"] == "structurally_valid"
                                           and rows[0]["one_attempt_one_usage"] and rows[0]["capture"]
                                           and rows[0]["capture"]["complete_chat_result_text_saved"]),
        "catalog_evidence": evidence, "catalog_unchanged": catalog_unchanged,
        "configuration_unchanged": configuration_unchanged, "active_configured_model_unchanged": active_model,
        "typed_choices": sum("choice" in row for row in rows),
        "http_response_captures": sum(bool(row["http_capture"] and row["http_capture"].get("capture_saved")) for row in rows),
        "http_response_capture_attempts": sum(bool(row["http_capture"]) for row in rows),
        "response_capture_policy": "Complete private HTTP body/JSON and allowlisted headers; credentials redacted. Alternative reasoning/content never becomes a decision.",
        "structurally_valid_choices": sum(row["status"] == "structurally_valid" for row in rows),
        "physical_requests": transport.count if args.execute else 0, "transport_dispatches": transport.count,
        "physical_request_count_basis": "Client dispatch attempts, not guaranteed server receipt on network failure.",
        "usage_receipts": len(meter.store.pending_usage(limit=6)), "spool": meter.spool.status(),
        "capture_complete": complete, "original_run_unchanged": original_unchanged, "source_unchanged": source_unchanged,
        "all_structural_checks_passed": complete and all(row["status"] == "structurally_valid" for row in rows),
        "pin_fixture_limitation": PIN_LIMITATION,
        "usage_settlement": "Local synthetic-body spool only; no active Runtime or body endpoint.",
        "limitation": "Six seeded synthetic inputs; manual semantic review required. Not proof of long-term learning, actual rest, tool delivery, or suitable main-model behavior."}
    write(output / "summary.json", result)
    print(canonical(scrub_json(result, (secret,))))
    return 0 if result["all_structural_checks_passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"version": VERSION, "status": "setup_or_audit_failure", "error_type": type(exc).__name__}))
        raise SystemExit(2) from None
