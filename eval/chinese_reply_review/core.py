"""Frozen prompts, a durable attempt ledger, and single-send HTTP sampling.

This module never imports or starts the production character pipeline.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4

import httpx


BASE_URL = "https://api.apiyi.com/v1"
SCHEMA = "chinese-reply-review-v1"
SCENARIO_IDS = ("s08_strong_emotion", "s04_stance_followups")
MAX_ATTEMPTS = 42
DIMENSIONS = ("naturalness", "relevance", "persona", "emotion", "preference")


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def write_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open("x", encoding="utf-8", newline="\n") as target:
            target.write(canonical(value) + "\n")
            target.flush()
            os.fsync(target.fileno())
        return
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as target:
        target.write(canonical(value) + "\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def run_lock(run_dir: Path):
    """An OS-owned lock releases on process exit, including a crash."""
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / ".operation.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("another_review_operation_is_running") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_models(models: list[dict]) -> None:
    if not 1 <= len(models) <= 21:
        raise ValueError("model_count_out_of_bounds")
    seen: set[str] = set()
    counts: dict[str, int] = {}
    allowed_providers = {"openai", "anthropic", "deepseek", "qwen", "kimi", "glm", "minimax"}
    paths = {"chat": "/chat/completions", "responses": "/responses", "anthropic": "/messages"}
    for model in models:
        mid, provider = model["id"], model["provider"]
        if mid in seen or not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", mid):
            raise ValueError("duplicate_or_invalid_model")
        if provider not in allowed_providers:
            raise ValueError("unknown_provider")
        seen.add(mid)
        counts[provider] = counts.get(provider, 0) + 1
        if counts[provider] > 3:
            raise ValueError("more_than_three_models_per_provider")
        if paths.get(model["protocol"]) != model["path"]:
            raise ValueError("unapproved_endpoint")
        options = model["options"]
        allowed = {"max_tokens", "max_completion_tokens", "max_output_tokens", "thinking",
                   "reasoning_effort", "reasoning", "enable_thinking", "output_config",
                   "reasoning_split", "temperature"}
        if set(options) - allowed:
            raise ValueError("unapproved_generation_option")
        caps = [options[key] for key in ("max_tokens", "max_completion_tokens", "max_output_tokens") if key in options]
        if len(caps) != 1 or type(caps[0]) is not int or not 1 <= caps[0] <= 8192:
            raise ValueError("invalid_output_cap")
        canonical(model)


def _system_text(persona: dict, scenario_id: str) -> str:
    text = ("你是小雾（Misty），正在与对方进行中文聊天。请按下列人格与事实边界回应。"
            "只输出准备发给对方的回复正文，不输出分析、角色说明或内部机制。\n\n"
            "已知人格：\n" + json.dumps(persona, ensure_ascii=False, sort_keys=True, indent=2))
    if scenario_id == "s04_stance_followups":
        text += "\n\n本条为日常观点聊天，尽量用 1–2 句、20–60 字自然回答；这是简洁表达的软目标。"
    return text


def build_requests(manifest: dict) -> list[dict]:
    requests = []
    for model in manifest["models"]:
        for scenario in manifest["scenarios"]:
            system = _system_text(manifest["persona"], scenario["id"])
            payload = {"model": model["id"], "stream": False, **copy.deepcopy(model["options"])}
            if model["protocol"] == "chat":
                payload["messages"] = [{"role": "system", "content": system},
                                       {"role": "user", "content": scenario["user_text"]}]
            elif model["protocol"] == "anthropic":
                payload["system"] = system
                payload["messages"] = [{"role": "user", "content": scenario["user_text"]}]
            else:
                payload["instructions"] = system
                payload["input"] = [{"role": "user", "content": scenario["user_text"]}]
            requests.append({"model_id": model["id"], "scenario_id": scenario["id"],
                             "protocol": model["protocol"], "endpoint": BASE_URL + model["path"],
                             "payload": payload, "request_sha256": digest(payload)})
    return requests


def prepare_run(run_dir: Path, project_root: Path, *, models: list[dict] | None = None) -> dict:
    from eval.qq_brief.common import frozen_scenarios
    from .catalog import EXCLUDED, MODELS

    run_dir, project_root = Path(run_dir), Path(project_root)
    selected = copy.deepcopy(MODELS if models is None else models)
    _validate_models(selected)
    with run_lock(run_dir):
        if (run_dir / "manifest.json").exists():
            existing = read_manifest(run_dir)
            if selected != existing["models"]:
                raise ValueError("frozen_model_catalog_changed")
            return existing
        persona_path = project_root / "presets" / "builtin" / "misty.json"
        persona = read_json(persona_path)["persona"]
        source = {item["id"]: item for item in frozen_scenarios()}
        labels = {"s08_strong_emotion": "比赛失利后的情绪支持", "s04_stance_followups": "长寿与有意思的立场表达"}
        scenarios = [{"id": sid, "label": labels[sid], "user_text": source[sid]["turns"][0]["messages"][0],
                      "source": "eval/qq_brief/common.py", "source_turn": 1} for sid in SCENARIO_IDS]
        manifest = {"schema_version": SCHEMA, "run_id": "zh-reply-" + uuid4().hex,
                    "created_at": now(), "base_url": BASE_URL, "models": selected,
                    "excluded": EXCLUDED, "persona": persona, "persona_sha256": digest(persona),
                    "scenarios": scenarios, "scenario_sha256": digest(scenarios),
                    "limits": {"per_model": 2, "global": MAX_ATTEMPTS, "planned": len(selected) * 2},
                    "policy": {"retries": 0, "auxiliary_calls": 0, "concurrency": 1,
                               "thinking": "disabled_where_supported_else_lowest", "currency_cap": None,
                               "reviewers": 1, "release_eligible": False},
                    "source_hashes": {"presets/builtin/misty.json": hashlib.sha256(persona_path.read_bytes()).hexdigest(),
                                      "eval/qq_brief/common.py": hashlib.sha256((project_root / "eval/qq_brief/common.py").read_bytes()).hexdigest()}}
        requests = build_requests(manifest)
        manifest["requests_sha256"] = digest(requests)
        manifest["manifest_sha256"] = digest(manifest)
        write_json(run_dir / "private" / "requests.json", requests, exclusive=True)
        write_json(run_dir / "manifest.json", manifest, exclusive=True)
        Ledger(run_dir / "private" / "ledger.sqlite3", manifest)
        return manifest


def read_manifest(run_dir: Path) -> dict:
    manifest = read_json(Path(run_dir) / "manifest.json")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("schema_version") != SCHEMA or digest(unsigned) != manifest.get("manifest_sha256"):
        raise ValueError("manifest_integrity_failure")
    _validate_models(manifest["models"])
    if tuple(s["id"] for s in manifest["scenarios"]) != SCENARIO_IDS:
        raise ValueError("scenario_set_changed")
    if digest(manifest["persona"]) != manifest["persona_sha256"] or digest(manifest["scenarios"]) != manifest["scenario_sha256"]:
        raise ValueError("prompt_integrity_failure")
    requests = read_json(Path(run_dir) / "private" / "requests.json")
    if digest(requests) != manifest["requests_sha256"] or requests != build_requests(manifest):
        raise ValueError("request_integrity_failure")
    if manifest["base_url"] != BASE_URL or manifest["limits"] != {"per_model": 2, "global": MAX_ATTEMPTS, "planned": len(manifest["models"]) * 2}:
        raise ValueError("frozen_limits_changed")
    return manifest


class Ledger:
    def __init__(self, path: Path, manifest: dict):
        self.path, self.manifest = Path(path), manifest
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.allowed = {(r["model_id"], r["scenario_id"]): r["request_sha256"] for r in build_requests(manifest)}
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS attempts (model_id TEXT NOT NULL, scenario_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, reserved_at TEXT NOT NULL, result_json TEXT, PRIMARY KEY(model_id, scenario_id))")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('manifest_sha256', ?)", (manifest["manifest_sha256"],))
            if db.execute("SELECT value FROM metadata WHERE key='manifest_sha256'").fetchone()[0] != manifest["manifest_sha256"]:
                raise ValueError("ledger_manifest_mismatch")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, model_id: str, scenario_id: str, request_sha256: str) -> bool:
        if self.allowed.get((model_id, scenario_id)) != request_sha256:
            raise ValueError("request_not_in_frozen_plan")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM attempts WHERE model_id=? AND scenario_id=?", (model_id, scenario_id)).fetchone():
                return False
            total = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM attempts WHERE model_id=?", (model_id,)).fetchone()[0]
            if total >= MAX_ATTEMPTS or count >= 2:
                raise ValueError("attempt_limit_exceeded")
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, NULL)", (model_id, scenario_id, request_sha256, now()))
        return True

    def finish(self, model_id: str, scenario_id: str, result: dict) -> None:
        with self.connect() as db:
            changed = db.execute("UPDATE attempts SET result_json=? WHERE model_id=? AND scenario_id=? AND result_json IS NULL",
                                 (canonical(result), model_id, scenario_id)).rowcount
            if changed != 1:
                raise ValueError("attempt_result_is_immutable_or_unreserved")

    def rows(self) -> list[dict]:
        with self.connect() as db:
            records = db.execute("SELECT model_id,scenario_id,request_sha256,reserved_at,result_json FROM attempts ORDER BY rowid").fetchall()
        return [{"model_id": mid, "scenario_id": sid, "request_sha256": sha, "reserved_at": ts,
                 **(json.loads(result) if result else {"status": "uncertain", "response_text": None})}
                for mid, sid, sha, ts, result in records]


def read_key(key_file: Path) -> str:
    secret = Path(key_file).read_text(encoding="utf-8-sig").strip()
    if not secret or any(char.isspace() for char in secret):
        raise ValueError("key_file_must_contain_one_token")
    return secret


def _client(transport: httpx.BaseTransport | None, timeout_seconds: float) -> httpx.Client:
    return httpx.Client(transport=transport or httpx.HTTPTransport(retries=0),
                        timeout=httpx.Timeout(timeout_seconds, connect=30), follow_redirects=False, trust_env=False)


def preflight(run_dir: Path, key_file: Path, *, transport: httpx.BaseTransport | None = None) -> dict:
    run_dir = Path(run_dir)
    with run_lock(run_dir):
        manifest = read_manifest(run_dir)
        secret = read_key(key_file)
        result = {"checked_at": now(), "manifest_sha256": manifest["manifest_sha256"],
                  "generation_requests": 0, "catalogue_is_not_generation_proof": True,
                  "key_fingerprint": hashlib.sha256(secret.encode()).hexdigest(),
                  "credential_source": str(Path(key_file).resolve()), "ready": False}
        try:
            with _client(transport, 30) as client:
                response = client.get(BASE_URL + "/models", headers={"Authorization": "Bearer " + secret})
            result["http_status"] = response.status_code
            if response.status_code == 200:
                raw = response.json()
                entries = raw.get("data", []) if isinstance(raw, dict) else []
                model_ids = sorted({entry["id"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)})
                result["listed_model_ids"] = model_ids
                result["missing_models"] = [model["id"] for model in manifest["models"] if model["id"] not in model_ids]
                result["document_blocked_models"] = [model["id"] for model in manifest["models"] if model.get("blocked_reason")]
                result["ready"] = not result["missing_models"] and not result["document_blocked_models"]
            else:
                result["error_code"] = "catalogue_http_error"
        except Exception as exc:
            result["error_code"] = "catalogue_read_failed"
            result["error_type"] = type(exc).__name__
        result = _redact_secret(result, secret)
        write_json(run_dir / "private" / "preflight.json", result)
        return {key: value for key, value in result.items() if key not in {"key_fingerprint", "listed_model_ids"}}


def _safe_usage(value: Any) -> dict:
    allowed = {"input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
               "cache_creation_input_tokens", "cache_read_input_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
               "reasoning_tokens", "cached_tokens", "completion_tokens_details", "prompt_tokens_details",
               "input_tokens_details", "output_tokens_details", "cost", "input_cost", "output_cost"}
    if not isinstance(value, dict):
        return {}
    output = {}
    for key, number in value.items():
        if key not in allowed:
            continue
        if type(number) in (int, float) and math.isfinite(number) and number >= 0:
            output[key] = number
        elif isinstance(number, dict):
            output[key] = _safe_usage(number)
    return output


def _strip_inline_thinking(text: str) -> tuple[str, bool]:
    # Some compatible providers put reasoning in content despite separate-field settings.
    if "<think>" not in text and "</think>" not in text:
        return text, False
    if text.count("<think>") != text.count("</think>"):
        return "", True
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip(), True


def parse_response(raw: Any, protocol: str) -> dict:
    result: dict = {"status": "invalid_response", "response_text": None, "usage": {}, "finish_reason": None}
    if not isinstance(raw, dict):
        return result
    for source, target in (("model", "response_model"), ("id", "response_id")):
        value = raw.get(source)
        if isinstance(value, str) and len(value) <= 256:
            result[target] = value
    result["usage"] = _safe_usage(raw.get("usage"))
    refusal = False
    tool_calls = False
    if protocol == "chat":
        choices = raw.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            return result
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            return result
        text, finish = message.get("content"), choice.get("finish_reason")
        refusal = bool(message.get("refusal"))
        tool_calls = bool(message.get("tool_calls") or message.get("function_call"))
        completed, truncated = finish == "stop", finish == "length"
    elif protocol == "anthropic":
        content = raw.get("content")
        if not isinstance(content, list):
            return result
        text = "".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str))
        tool_calls = any(isinstance(item, dict) and item.get("type") == "tool_use" for item in content)
        finish = raw.get("stop_reason")
        refusal = finish == "refusal"
        completed, truncated = finish in {"end_turn", "stop_sequence"}, finish == "max_tokens"
    elif protocol == "responses":
        output = raw.get("output")
        if not isinstance(output, list):
            return result
        parts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message" and item.get("role") == "assistant":
                for part in item.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        parts.append(part["text"])
                    elif isinstance(part, dict) and part.get("type") == "refusal":
                        refusal = True
            elif item.get("type") not in {"reasoning", "message"}:
                tool_calls = True
        text = "".join(parts)
        finish = raw.get("status")
        completed = finish == "completed"
        details = raw.get("incomplete_details")
        truncated = finish == "incomplete" and isinstance(details, dict) and details.get("reason") == "max_output_tokens"
    else:
        raise ValueError("unknown_protocol")
    result["finish_reason"] = finish if isinstance(finish, str) and len(finish) < 64 else None
    if isinstance(text, str):
        text, had_thinking = _strip_inline_thinking(text)
        result["inline_thinking_removed"] = had_thinking
        result["response_text"] = text
    result["status"] = ("refusal" if refusal else "unexpected_tool_call" if tool_calls else "truncated" if truncated
                        else "empty" if not isinstance(text, str) or not text.strip() else "complete" if completed else "invalid_response")
    return result


def _redact_secret(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact_secret(item, secret) for key, item in value.items()}
    return value


def _write_result_export(run_dir: Path, ledger: Ledger) -> None:
    path = run_dir / "private" / "results.jsonl"
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as target:
        for row in ledger.rows():
            target.write(canonical(row) + "\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def summarize(run_dir: Path) -> dict:
    manifest = read_manifest(run_dir)
    ledger = Ledger(Path(run_dir) / "private" / "ledger.sqlite3", manifest)
    rows = ledger.rows()
    statuses: dict[str, int] = {}
    counts: dict[str, int] = {}
    for row in rows:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        counts[row["model_id"]] = counts.get(row["model_id"], 0) + 1
    sealed = (Path(run_dir) / "private" / "ratings" / "first-seal.json").exists()
    return {"run_id": manifest["run_id"], "planned": len(manifest["models"]) * 2,
            "attempted": len(rows), "remaining": len(manifest["models"]) * 2 - len(rows),
            "statuses": statuses, "per_model_attempts": counts,
            "human_review_status": "sealed" if sealed else "waiting_for_human", "release_eligible": False}


def run_samples(run_dir: Path, key_file: Path, *, transport: httpx.BaseTransport | None = None,
                timeout_seconds: float = 300, progress: Callable[[dict], None] | None = None) -> dict:
    run_dir = Path(run_dir)
    with run_lock(run_dir):
        manifest = read_manifest(run_dir)
        secret = read_key(key_file)
        precheck = read_json(run_dir / "private" / "preflight.json")
        if (not precheck.get("ready") or precheck["manifest_sha256"] != manifest["manifest_sha256"]
                or precheck.get("key_fingerprint") != hashlib.sha256(secret.encode()).hexdigest()):
            raise ValueError("preflight_not_ready_or_credentials_changed")
        ledger = Ledger(run_dir / "private" / "ledger.sqlite3", manifest)
        requests = read_json(run_dir / "private" / "requests.json")
        with _client(transport, timeout_seconds) as client:
            for spec in requests:
                if not ledger.reserve(spec["model_id"], spec["scenario_id"], spec["request_sha256"]):
                    continue
                started = time.perf_counter()
                if progress:
                    progress({"event": "request_started", "model_id": spec["model_id"], "scenario_id": spec["scenario_id"]})
                headers = {"Authorization": "Bearer " + secret, "Content-Type": "application/json"}
                if spec["protocol"] == "anthropic":
                    headers.update({"x-api-key": secret, "anthropic-version": "2023-06-01"})
                result = {"status": "uncertain", "response_text": None, "started_at": now(),
                          "protocol": spec["protocol"], "http_status": None}
                try:
                    response = client.post(spec["endpoint"], headers=headers, content=canonical(spec["payload"]).encode("utf-8"))
                    result["http_status"] = response.status_code
                    if response.is_success:
                        try:
                            result.update(parse_response(response.json(), spec["protocol"]))
                        except (ValueError, TypeError, KeyError):
                            result.update(status="invalid_response", response_text=None)
                    else:
                        # Do not persist error bodies or exception messages: either can echo the key.
                        result["status"] = "http_error"
                    response.close()
                except Exception as exc:
                    result.update(status="transport_error", error_type=type(exc).__name__)
                result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
                result["completed_at"] = now()
                result = _redact_secret(result, secret)
                ledger.finish(spec["model_id"], spec["scenario_id"], result)
                _write_result_export(run_dir, ledger)
                if progress:
                    progress({"event": "request_finished", "model_id": spec["model_id"], "scenario_id": spec["scenario_id"],
                              "status": result["status"], "http_status": result["http_status"], "elapsed_ms": result["elapsed_ms"]})
        _write_result_export(run_dir, ledger)
        result = summarize(run_dir)
        write_json(run_dir / "private" / "sampling-summary.json", result)
        return result
