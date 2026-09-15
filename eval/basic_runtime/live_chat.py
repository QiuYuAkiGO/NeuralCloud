"""Repeatable BR-1 live Cognitive probe, with private state and shareable receipts.

This exercises the production host, not Runtime/GUI or the Linux desktop. Even
20 successful rounds require a separate human persona review and live GUI run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

from gui.persist import load_gui_settings, save_gui_settings
from gui.presets import load_builtin
from harness.cognitive.service import CognitiveServiceHost
from harness.files import replace_path
from harness.contracts.decisions import DeliveryEvidence, ReplyDecision
from harness.contracts.envelope import DomainCommandEnvelope
from harness.research.manifest import load_experiment_manifest

PROMPTS = (
    "你好，可以介绍自己吗？你叫什么、今年几岁，生日是哪一天？",
    "我拍了一张雨后窗边的照片，我们把它取名《雨后微光》，好吗？",
    "刚才照片最后定的名字是什么？就用那个名字吧。",
    "今天想安静地聊聊，你平时有什么爱好？",
    "如果不想出门，你会怎样安排一个轻松的下午？",
    "我说的照片是雨后窗边的那张，不是夜景。这一点记住了吗？",
    "我们把下一本摄影笔记叫《窗边拾光》，先记下这个约定。",
    "下一本摄影笔记叫什么？",
    "今天先不讨论设备参数，说说怎样观察光线吧。",
    "再确认一下，你的名字、年龄和生日。",
    "前面说的摄影笔记名字是什么？",
    "以后我说‘微光计划’，就是整理那张雨后窗边的照片。",
    "‘微光计划’具体指什么？",
    "今天有点忙，我们只选一件最小的事来做。",
    "我想先选照片，再写配文。这个顺序怎样？",
    "就按刚才那个顺序。你能复述一下吗？",
    "明天聊天时我想接着这个话题，不用安排提醒。",
    "你觉得摄影笔记里可以加哪一种简短记录？",
    "谢谢，今天的聊天让我把事情理清楚了。",
    "结束前再介绍一次自己：名字、年龄和生日分别是什么？",
)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def safe_endpoint(value: str | None) -> str | None:
    if not value:
        return value
    parts = urlsplit(value)
    # Userinfo and query strings can carry credentials.
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


class EvidenceWriter:
    def __init__(self, path: Path, secrets: list[str]):
        self.path, self.secrets = path, secrets

    def write(self, value) -> None:
        encoded = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        for secret in sorted(set(filter(None, self.secrets)), key=len, reverse=True):
            # Replace JSON-escaped values as well as plain keys.
            encoded = encoded.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "<redacted>")
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{os.urandom(3).hex()}.tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        replace_path(temporary, self.path)


def command(kind: str, index: int, payload: dict, *, timeout: float, revision: int, run_id: str):
    now = datetime.now(UTC)
    return DomainCommandEnvelope[dict](
        command_type=kind, event_id=f"evt_br1_{run_id}_{index}", receive_seq=index,
        correlation_id=f"corr_br1_{index}", runtime_instance_id=f"br1-chat-{run_id}",
        manifest_revision=revision, idempotency_key=f"{kind}:{index}",
        issued_at=now, deadline=now + timedelta(seconds=timeout), payload=payload,
    )


class ContextRecorder:
    def __init__(self):
        self.stages, self.contexts = [], []

    def on_stage(self, kind, result, payload):
        self.stages.append({"stage": kind, "elapsed_ms": payload.get("latency_ms")})
        if kind == "compile":
            text = "\n".join(item.text for item in result.segments)
            identity = load_builtin("misty").persona.identity
            self.contexts.append({
                "sha256": fingerprint(result.render_messages()),
                "fixed_anchors_present": all(v in text for v in (identity.name, "年龄=19岁", "生日=5月20日")),
            })


async def run_probe(*, state_dir: Path, out_dir: Path, config_path: Path | None, manifest_path: Path, rounds: int) -> dict:
    if not 1 <= rounds <= len(PROMPTS):
        raise ValueError("rounds must be between 1 and 20")
    state_dir, out_dir = state_dir.resolve(), out_dir.resolve()
    if state_dir == out_dir or state_dir in out_dir.parents or out_dir in state_dir.parents:
        raise ValueError("private state and shareable evidence directories must be separate")
    if state_dir.exists() or out_dir.exists():
        raise ValueError("use fresh state and output directories; previous attempts must be preserved")
    manifest = load_experiment_manifest(manifest_path)
    selected = load_gui_settings(config_path)
    if selected.mode == "demo" or not selected.base_url:
        raise ValueError("configure a live/ollama model in GUI settings before running this probe")
    selected = selected.model_copy(update={"active_preset_id": "misty", "open_preset_ids": ["misty"], "character_name": ""})
    state_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    save_gui_settings(selected, state_dir / "gui.json")
    secrets = [selected.api_key or "", selected.aux_api_key or ""]
    writer = EvidenceWriter(out_dir / "summary.json", secrets)
    report = {
        "schema_version": "basic-live-chat-evidence-v2", "status": "RUNNING",
        "started_at": datetime.now(UTC).isoformat(), "requested_rounds": rounds,
        "scope": "isolated production CognitiveServiceHost; actual configured model; local receipt delivery",
        "runtime_http_gui_delivery_tested": False, "real_linux_tested": False,
        "full_milestone_verdict": "NOT_EVALUATED", "persona_review": "PENDING",
        "command_timeout_seconds": manifest.command_timeout_seconds,
        "manifest_id": manifest.experiment_id, "manifest_revision": manifest.revision,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "attempts": [], "restarts": [], "configurations": [],
        "health_probe": {"samples": 0, "failures": 0, "maximum_elapsed_seconds": 0.0},
    }
    writer.write(report)
    run_id = uuid.uuid4().hex[:12]
    host = None
    probe = None
    env = {
        "NC_MODE": selected.mode, "NC_TRACE_DIR": str(state_dir / "traces"),
        "NC_COGNITIVE_AUTONOMOUS_LOG_DIR": str(state_dir / "autonomous"),
        "NC_COGNITIVE_MANIFEST_PATH": str(manifest_path.resolve()),
    }

    async def make_host():
        created = await asyncio.to_thread(
            CognitiveServiceHost, config_path=state_dir / "gui.json",
            presets_dir=state_dir / "presets", state_path=state_dir / "cognitive.json",
        )
        settings = created.chat.settings
        secrets.extend((settings.llm_api_key or "", settings.aux_llm_api_key or ""))
        safe = {key: getattr(settings, key) for key in (
            "llm_model", "aux_llm_model", "llm_timeout", "parser_llm_enabled", "planner_llm_enabled",
            "tier_llm_enabled", "validator_sample_rate", "context_token_budget", "session_window",
        )}
        safe["llm_base_url"] = safe_endpoint(settings.llm_base_url)
        safe["aux_llm_base_url"] = safe_endpoint(settings.aux_llm_base_url)
        safe["embedding"] = {k: safe_endpoint(v) if k == "base_url" else v for k, v in created.chat.embedding_info.items()}
        safe["persona_sha256"] = fingerprint(created.chat.memory.get_persona().model_dump(mode="json"))
        report["configurations"].append({"values": safe, "sha256": fingerprint(safe)})
        return created

    async def health_loop():
        while True:
            start = time.monotonic()
            try:
                await asyncio.wait_for(host.health(), 2)
                await asyncio.wait_for(host.get_snapshot(), 2)
            except Exception:
                report["health_probe"]["failures"] += 1
            elapsed = time.monotonic() - start
            health = report["health_probe"]
            health["samples"] += 1
            health["maximum_elapsed_seconds"] = max(health["maximum_elapsed_seconds"], elapsed)
            await asyncio.sleep(.25)

    async def invoke(kind, index, payload):
        envelope = command(kind, index, payload, timeout=manifest.command_timeout_seconds, revision=manifest.revision, run_id=run_id)
        method = host.prepare_response if kind == "PrepareResponse" else host.commit_interaction
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(method(envelope), manifest.command_timeout_seconds)
            return result, time.monotonic() - start, envelope
        finally:
            phase = "prepare" if kind == "PrepareResponse" else "commit"
            report["attempts"][-1][f"{phase}_elapsed_seconds"] = time.monotonic() - start

    with patch.dict(os.environ, env):
        try:
            host = await make_host()
            probe = asyncio.create_task(health_loop())
            for index, prompt in enumerate(PROMPTS[:rounds], 1):
                if index == 3:
                    before = host.chat.pipeline.session_store.snapshot()
                    session_id = host.chat.active_session_id
                    await host.close()
                    host = await make_host()
                    report["restarts"].append({
                        "after_round": 2, "session_restored_exactly": before == host.chat.pipeline.session_store.snapshot(),
                        "session_id_preserved": session_id == host.chat.active_session_id,
                        "persona_restored_exactly": host.chat.memory.get_persona() == load_builtin("misty").persona,
                    })
                rec = ContextRecorder()
                host.chat.pipeline.observer = rec
                row = {"round": index, "prompt": prompt, "status": "RUNNING", "phase": "prepare", "stages": rec.stages, "contexts": rec.contexts}
                report["attempts"].append(row)
                writer.write(report)
                before = host.chat.pipeline.session_store.snapshot()
                prepared_result, elapsed, envelope = await invoke("PrepareResponse", index, {"text": prompt, "session_id": host.chat.active_session_id})
                row.update(prepare_elapsed_seconds=elapsed, prepare_status=prepared_result.status, prepare_command=envelope.model_dump(mode="json"))
                prepared = prepared_result.result
                if prepared_result.status != "applied" or prepared is None or not isinstance(prepared.decision, ReplyDecision):
                    raise RuntimeError("prepare did not produce an applied ReplyDecision")
                row["prepare_side_effect_free"] = before == host.chat.pipeline.session_store.snapshot()
                row["reply"] = prepared.decision.text
                if not row["reply"].strip() or any(r.get("reply") == row["reply"] for r in report["attempts"][:-1]):
                    raise RuntimeError("empty or repeated reply")
                receipt_path = out_dir / f"delivery-{index:02d}.json"
                EvidenceWriter(receipt_path, secrets).write({"prepare_id": prepared.prepare_id, "segments": prepared.decision.segments})
                delivered = json.loads(receipt_path.read_text(encoding="utf-8"))
                if delivered["segments"] != list(prepared.decision.segments):
                    raise RuntimeError("receipt was redacted; cannot assert verbatim delivery")
                evidence = DeliveryEvidence(
                    prepare_id=prepared.prepare_id, delivered_at=datetime.now(UTC), channel="chat",
                    success=True, delivered_segments=delivered["segments"], external_ref=f"local-receipt:{receipt_path.name}",
                )
                row.update(phase="commit", delivery_evidence=evidence.model_dump(mode="json"))
                writer.write(report)
                committed, elapsed, envelope = await invoke("CommitInteraction", index, {"prepare_id": prepared.prepare_id, "evidence": evidence.model_dump(mode="json")})
                row.update(commit_elapsed_seconds=elapsed, commit_status=committed.status, commit_command=envelope.model_dump(mode="json"), committed_revision=committed.committed_revision)
                if committed.status != "applied" or not row["prepare_side_effect_free"]:
                    raise RuntimeError("commit or prepare isolation failed")
                if host.chat.memory.get_persona() != load_builtin("misty").persona:
                    raise RuntimeError("persisted persona changed")
                row.update(status="PASS", phase="completed")
                writer.write(report)
                print(f"round {index}/{rounds}: committed; prepare={row['prepare_elapsed_seconds']:.2f}s commit={elapsed:.2f}s", flush=True)
            checks = {
                "twenty_rounds_committed": len(report["attempts"]) == 20,
                "restart_exact": bool(report["restarts"]) and all(all(v for k, v in r.items() if k != "after_round") for r in report["restarts"]),
                "contexts_have_fixed_anchors": all(r["contexts"] and all(c["fixed_anchors_present"] for c in r["contexts"]) for r in report["attempts"]),
                "health_responsive": report["health_probe"]["samples"] > 0 and report["health_probe"]["failures"] == 0 and report["health_probe"]["maximum_elapsed_seconds"] <= 2,
                "configuration_unchanged": len({c["sha256"] for c in report["configurations"]}) == 1,
            }
            report["checks"] = checks
            # Fixed-anchor presence in context is not a verdict about model prose.
            if all(checks.values()):
                report["status"] = "AWAITING_REVIEW"
            elif rounds < 20 and all(v for k, v in checks.items() if k not in {"twenty_rounds_committed", "restart_exact"}):
                report["status"] = "INCONCLUSIVE"
            else:
                report["status"] = "FAIL"
        except BaseException as exc:
            report.update(status="INCONCLUSIVE" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "FAIL", error_type=type(exc).__name__, error=str(exc))
            if report["attempts"] and report["attempts"][-1]["status"] == "RUNNING":
                report["attempts"][-1]["status"] = report["status"]
        finally:
            if probe:
                probe.cancel()
                await asyncio.gather(probe, return_exceptions=True)
            writer.write(report)
            try:
                if host:
                    await host.close()  # Wait for an already-started durable commit, including after timeout.
            except Exception as exc:
                report.update(status="FAIL", close_error_type=type(exc).__name__, close_error=str(exc))
            finally:
                report["finished_at"] = datetime.now(UTC).isoformat()
                writer.write(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True, help="Fresh private directory (may contain API keys)")
    parser.add_argument("--out-dir", type=Path, required=True, help="Fresh shareable evidence directory")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--manifest", type=Path, default=Path("presets/runtime/basic-runtime-v1.json"))
    parser.add_argument("--rounds", type=int, default=20, choices=range(1, 21))
    args = parser.parse_args()
    report = asyncio.run(run_probe(state_dir=args.state_dir, out_dir=args.out_dir, config_path=args.config, manifest_path=args.manifest, rounds=args.rounds))
    print(f"status={report['status']}; evidence={args.out_dir / 'summary.json'}")
    return 2 if report["status"] == "FAIL" else 0 if report["status"] == "AWAITING_REVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
