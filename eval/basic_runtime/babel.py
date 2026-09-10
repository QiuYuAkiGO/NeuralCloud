"""Babel: validate / run / report. Runs against Runtime, preserving the save.

python -m eval.basic_runtime.babel validate
python -m eval.basic_runtime.babel run --restore --out-dir .tmp/babel/live-01
python -m eval.basic_runtime.babel report --out-dir .tmp/babel/live-01
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.parse import urlsplit
import uuid

import httpx

from eval.basic_runtime.babel_evidence import (
    EvidenceStore, analyze_turn, archive_journal, copy_call_evidence,
    export_shareable, fingerprint, homeostasis_state, read_json, render_report, utc_now, write_json,
)
from harness.homeostasis.models import HomeostaticSnapshot

ROOT = Path(__file__).resolve().parents[2]
STORY_PATH = Path(__file__).with_name("babel_story.json")
QUESTIONS = (
    "听完整个故事，你有什么整体印象或感想？没有明显感受也可以直接说。",
    "哪个片段给你留下了印象？为什么？",
    "对人们决定建城建塔的选择，你怎么看？",
    "对语言变化后人物所处的情境，你有什么看法或感受？",
    "对这个结局，你最想补充什么？",
    "故事有没有让你难以判断、暂时没有结论的地方？",
    "对几个虚构人物的选择，你有哪些理解或保留？",
    "从开始听到现在，你的看法或感受有没有变化？",
    "这个故事让你联想到什么？",
    "如果用自己的话回应整个故事，你最后想说什么？",
)


def load_story(path: Path = STORY_PATH) -> dict:
    story = read_json(path)
    if story.get("schema_version") != "babel-story-v1":
        raise ValueError("unsupported story schema")
    turns = story.get("turns", [])
    if len(turns) != 100 or [t.get("turn") for t in turns] != list(range(1, 101)):
        raise ValueError("story must contain exactly 100 ordered, unique turns")
    if not story.get("introduction") or len(story.get("chapters", [])) != 9:
        raise ValueError("introduction and nine chapters required")
    texts = set()
    for i, turn in enumerate(turns, 1):
        text = turn.get("text", "")
        if not isinstance(text, str) or text in texts:
            raise ValueError(f"empty or duplicate turn {i}")
        texts.add(text)
        if i <= 90:
            length = len("".join(text.split()))
            if turn.get("phase") != "story" or turn.get("chapter") != (i - 1) // 10 + 1 or not 140 <= length <= 200:
                raise ValueError(f"story turn {i}: require matching chapter and 140–200 non-whitespace characters (got {length})")
        elif turn.get("phase") != "reflection" or turn.get("chapter") is not None or text != QUESTIONS[i - 91]:
            raise ValueError(f"reflection turn {i} does not match frozen question")
    return story


@dataclass(frozen=True)
class RunOptions:
    interval_seconds: float = 10
    turn_timeout_seconds: float = 300
    poll_seconds: float = .5
    drain_seconds: float = 30
    use_stream: bool = True

    def __post_init__(self):
        for key in ("interval_seconds", "turn_timeout_seconds", "poll_seconds", "drain_seconds"):
            value = getattr(self, key)
            if not math.isfinite(value) or value < 0 or (key in {"poll_seconds", "turn_timeout_seconds"} and value == 0):
                raise ValueError(f"invalid {key}")


class RealClock:
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(asyncio.sleep)


class StopRun(RuntimeError):
    def __init__(self, status: str, reason: str):
        self.status, self.reason = status, reason
        super().__init__(reason)


class RuntimeClient:
    def __init__(self, url: str, token: str, *, transport=None):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("invalid Runtime URL")
        if not token.strip():
            raise ValueError("control token is required")
        self.http = httpx.AsyncClient(base_url=url.rstrip("/"), headers={"Authorization": "Bearer " + token}, timeout=10, transport=transport, trust_env=False, follow_redirects=False)

    async def get(self, path: str, **params) -> dict:
        response = await self.http.get(path, params=params)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("Runtime returned non-object JSON")
        return value

    async def submit(self, payload: dict) -> dict:
        response = await self.http.post("/v1/messages", json=payload)
        response.raise_for_status()
        return response.json()

    async def signal_stream(self, wake: asyncio.Event, cursor, stats: dict) -> None:
        while True:
            try:
                async with self.http.stream("GET", "/v1/monitor/stream", params={"after_seq": cursor()}, timeout=httpx.Timeout(20)) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            stats["stream_notifications"] += 1
                            wake.set()
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ValueError):
                stats["stream_reconnects"] += 1
            # SSE only wakes pagination; it never advances the durable cursor.
            await asyncio.sleep(1)

    async def close(self):
        await self.http.aclose()


def frozen_configuration(snapshot: dict) -> dict:
    ui = snapshot.get("cognitive_ui") or {}
    llm = ui.get("llm", {})
    return {
        "runtime_instance_id": snapshot.get("runtime", {}).get("runtime_instance_id"),
        "manifest": snapshot.get("manifest"),
        "profile": snapshot.get("homeostasis", {}).get("payload", {}).get("profile"),
        "persona": ui.get("persona"), "session_id": ui.get("session", {}).get("session_id"),
        "preset_id": ui.get("session", {}).get("preset_id"), "mode": ui.get("mode"),
        "speakers": ui.get("speakers"),
        "inference_config": ui.get("inference_config"),
        "llm": {k: llm.get(k) for k in ("mode", "base_url", "model", "embedding")},
        "aux": {k: llm.get("aux", {}).get(k) for k in ("base_url", "model", "active")},
    }


def check_runtime(status: dict) -> None:
    lifecycle = str(status.get("lifecycle", "")).lower()
    if lifecycle in {"paused", "pausing"}:
        raise StopRun("PAUSED", "Runtime 已暂停；停止发送并保留现场")
    if lifecycle != "running":
        raise StopRun("INTERRUPTED", f"Runtime lifecycle={lifecycle or 'unavailable'}")
    health = status.get("health", {})
    if any(health.get(k) is False for k in ("journal_writable", "cognitive_healthy", "homeostasis_healthy", "llm_healthy", "body_healthy")):
        raise StopRun("FAILED", "Runtime 服务健康检查失败")


def preflight(snapshot: dict) -> tuple[str, str]:
    check_runtime(snapshot.get("runtime", {}))
    ui = snapshot.get("cognitive_ui") or {}
    persona = ui.get("persona") or {}
    if persona.get("identity", {}).get("name") not in {"小雾", "Misty"} or ui.get("session", {}).get("preset_id") != "misty":
        raise StopRun("PRECHECK_FAILED", "当前会话不是 Misty；没有切换或重置预设")
    if ui.get("mode") not in {"live", "ollama"} or not ui.get("llm", {}).get("model"):
        raise StopRun("PRECHECK_FAILED", "需要已配置的真实模型")
    if ui.get("llm", {}).get("reachable") is False:
        raise StopRun("PRECHECK_FAILED", "角色模型不可达")
    session = ui.get("session", {}).get("session_id")
    if not session:
        raise StopRun("PRECHECK_FAILED", "无法识别现有会话")
    if not ui.get("inference_config", {}).get("call_log_enabled"):
        raise StopRun("PRECHECK_FAILED", "服务缺少启用的完整调用观测；请使用本次实现恢复服务")
    state = HomeostaticSnapshot.model_validate(homeostasis_state(snapshot))
    if state.watchdog:
        raise StopRun("PAUSED", "现有内稳态包含保护触发记录")
    runtime = snapshot["runtime"]
    if runtime.get("pending_events", 0) or runtime.get("pending_actions", 0) or runtime.get("current_action"):
        raise StopRun("PRECHECK_FAILED", "现有会话有待处理事件或动作；没有插入故事")
    return session, ui.get("speakers", {}).get("user_name") or "user"


async def run_dialogue(client, *, story: dict, directory: Path, options: RunOptions = RunOptions(), clock=None, metadata: dict | None = None, artifact_paths: tuple[Path, Path, Path] | None = None, secrets: list[str] | None = None) -> dict:
    if directory.exists():
        raise ValueError("use a fresh output directory; previous attempts are never overwritten")
    directory.mkdir(parents=True)
    (directory / "turns").mkdir()
    clock = clock or RealClock()
    start = clock.monotonic()
    run = {"schema_version": "babel-run-v1", "run_id": "babel_" + uuid.uuid4().hex[:12], "status": "RUNNING", "started_at": utc_now(),
           "story_sha256": fingerprint(story), "requested_turns": 100, "interval_seconds": options.interval_seconds,
           "turn_timeout_seconds": options.turn_timeout_seconds, "metadata": metadata or {},
           "monitor": {"after_seq": 0, "stream_notifications": 0, "stream_reconnects": 0, "poll_errors": 0}, "evidence": {}}
    write_json(directory / "run.json", run)
    write_json(directory / "story.json", story)
    store = EvidenceStore(directory)
    wake = asyncio.Event()
    stream = None
    row = None
    correlations = set()
    monitor_failure_since = None

    async def collect():
        nonlocal monitor_failure_since
        while True:
            try:
                page = await client.get("/v1/monitor/events", since="1970-01-01T00:00:00Z", after_seq=run["monitor"]["after_seq"], limit=500)
            except (httpx.HTTPError, OSError):
                run["monitor"]["poll_errors"] += 1
                monitor_failure_since = monitor_failure_since if monitor_failure_since is not None else clock.monotonic()
                if clock.monotonic() - monitor_failure_since > 10:
                    raise StopRun("INTERRUPTED", "监控连续不可用超过 10 秒；发送状态以现有证据为准")
                return
            monitor_failure_since = None
            batch, more = page.get("items"), page.get("has_more")
            if not isinstance(batch, list) or not isinstance(more, bool) or (more and not batch):
                raise StopRun("INCOMPLETE_EVIDENCE", "监控分页无进展或格式错误")
            previous = run["monitor"]["after_seq"]
            for item in batch:
                if item["seq"] <= previous:
                    raise StopRun("INCOMPLETE_EVIDENCE", "监控分页没有按序递增")
                store.add(item, clock.monotonic())
                previous = item["seq"]
                run["monitor"]["after_seq"] = previous
            if not more:
                return

    async def wait_interval(seconds: float):
        until = clock.monotonic() + seconds
        while clock.monotonic() < until:
            await collect()
            check_runtime(await client.get("/v1/status"))
            await clock.sleep(max(0, min(1, until - clock.monotonic())))

    def checkpoint():
        if row is not None:
            write_json(directory / "turns" / f"{row['turn']:03d}.json", row)
        write_json(directory / "run.json", run)

    try:
        # Establish durable cursor before taking the baseline, retaining later changes.
        page = await client.get("/v1/monitor/events", since="1970-01-01T00:00:00Z", limit=1)
        run["monitor"]["after_seq"] = max((i["seq"] for i in page.get("items", [])), default=0)
        initial = await client.get("/v1/snapshot")
        write_json(directory / "initial_snapshot.json", initial)
        session, speaker = preflight(initial)
        baseline = frozen_configuration(initial)
        run.update(configuration=baseline, configuration_sha256=fingerprint(baseline), session_id=session)
        store.add_state(homeostasis_state(initial))
        if options.use_stream:
            stream = asyncio.create_task(client.signal_stream(wake, lambda: run["monitor"]["after_seq"], run["monitor"]))
        for turn in story["turns"]:
            row = None  # a failure before sending must not overwrite a completed row
            before = await client.get("/v1/snapshot")
            check_runtime(before["runtime"])
            if fingerprint(frozen_configuration(before)) != run["configuration_sha256"]:
                raise StopRun("CONFIGURATION_DRIFT", "模型、配置、人设、会话或 Runtime 实例发生变化")
            store.add_state(homeostasis_state(before))
            prompt = (story["introduction"] + "\n\n" if turn["turn"] == 1 else "") + turn["text"]
            correlation = f"{run['run_id']}_turn_{turn['turn']:03d}"
            correlations.add(correlation)
            sent = clock.monotonic()
            row = {"turn": turn["turn"], "phase": turn["phase"], "chapter": turn["chapter"], "correlation_id": correlation,
                   "status": "SENDING", "prompt": prompt, "prompt_sha256": fingerprint(prompt), "sent_at": utc_now(),
                   "sent_monotonic": sent, "before_send": homeostasis_state(before), "event_id": None, "receive_seq": None,
                   "first_overall_reflection": turn["turn"] == 91}
            checkpoint()
            try:
                accepted = await client.submit({"text": prompt, "session_id": session, "speaker": speaker, "channel": "chat", "correlation_id": correlation})
                if not accepted.get("event_id"):
                    raise StopRun("REJECTED", "Runtime 没有接收本轮消息")
                if accepted.get("correlation_id") != correlation:
                    raise StopRun("INCOMPLETE_EVIDENCE", "接收回执关联 ID 不一致")
                row.update(event_id=accepted["event_id"], receive_seq=accepted["receive_seq"], accepted=accepted, accept_observed_ms=(clock.monotonic() - sent) * 1000, status="WAITING")
            except (httpx.HTTPError, OSError):
                row.update(status="ACCEPTANCE_UNKNOWN", acceptance_error="HTTP acknowledgement lost; request is never resent")
            checkpoint()
            last_status_check = -math.inf
            while True:
                await collect()
                correlation_events = store.events(correlation)
                accepted_events = [i["payload"]["data"]["event"] for i in correlation_events if i.get("topic") == "journal.event_accepted" and i["payload"].get("data", {}).get("event", {}).get("event_type") == "user_message"]
                if len({e["event_id"] for e in accepted_events}) > 1:
                    raise StopRun("INCOMPLETE_EVIDENCE", "一轮关联到多个用户输入")
                if not row["event_id"] and accepted_events:
                    accepted = accepted_events[0]
                    row.update(event_id=accepted["event_id"], receive_seq=accepted["receive_seq"], accepted=accepted, acceptance_recovered=True, accept_observed_ms=(clock.monotonic() - sent) * 1000, status="WAITING")
                events = store.events(correlation, row["event_id"])
                analysis = analyze_turn(events, store, sent)
                row["analysis"] = analysis
                if analysis["durably_completed"]:
                    # A successful Runtime result does not prove evidence completeness.
                    after = await client.get("/v1/snapshot")
                    store.add_state(homeostasis_state(after))
                    row["after_commit"] = homeostasis_state(after)
                    if fingerprint(frozen_configuration(after)) != run["configuration_sha256"]:
                        raise StopRun("CONFIGURATION_DRIFT", "本轮执行期间配置发生变化")
                    if not analysis["state_exact"]:
                        raise StopRun("INCOMPLETE_EVIDENCE", "本轮已提交，但无法精确恢复模型使用的内稳态版本")
                    row["status"] = "COMPLETED"
                    row["completed_at"] = utc_now()
                    row["interval_started_monotonic"] = clock.monotonic()
                    checkpoint()
                    print(f"turn {turn['turn']}/100: committed; observed={analysis['timing']['completed_observed_ms']/1000:.2f}s; state exact", flush=True)
                    check_runtime(after["runtime"])
                    break
                if analysis["terminal"]:
                    terminal = analysis["terminal"]
                    status = "PAUSED" if terminal.get("decision_kind") == "watchdog_pause" else "FAILED"
                    raise StopRun(status, "Runtime 未完成本轮交付与提交：" + str(terminal.get("decision_kind") or terminal.get("status")))
                if clock.monotonic() - last_status_check >= 2:
                    check_runtime(await client.get("/v1/status"))
                    last_status_check = clock.monotonic()
                if clock.monotonic() - sent >= options.turn_timeout_seconds:
                    raise StopRun("TIMEOUT" if row["event_id"] else "ACCEPTANCE_UNKNOWN", "达到本轮等待上限；已接收消息可能仍在 Runtime 内执行")
                if wake.is_set():
                    wake.clear()
                await clock.sleep(min(options.poll_seconds, max(0, options.turn_timeout_seconds - (clock.monotonic() - sent))))
            if turn["turn"] < 100:
                await wait_interval(options.interval_seconds)
                row["interval_actual_seconds"] = clock.monotonic() - row["interval_started_monotonic"]
                checkpoint()
        run["status"] = "COMPLETED"
    except StopRun as exc:
        run.update(status=exc.status, stop_reason=exc.reason)
        if row and row["status"] != "COMPLETED":
            row.update(status=exc.status, stop_reason=exc.reason)
    except asyncio.CancelledError:
        run.update(status="INTERRUPTED", stop_reason="采集任务被中断；没有撤销已接收的消息")
        if row and row["status"] != "COMPLETED":
            row["status"] = "INTERRUPTED"
    except Exception as exc:
        run.update(status="FAILED", stop_reason="采集失败：" + type(exc).__name__)
        if row and row["status"] != "COMPLETED":
            row["status"] = "FAILED"
    finally:
        # No additional messages, no pause/resume/reset. Keep observing late work.
        if stream:
            stream.cancel()
            with suppress(asyncio.CancelledError):
                await stream
        if correlations:
            drain_until = clock.monotonic() + options.drain_seconds
            while clock.monotonic() < drain_until:
                with suppress(Exception):
                    await collect()
                await clock.sleep(min(1, max(0, drain_until - clock.monotonic())))
            with suppress(Exception):
                await collect()
        try:
            final = await client.get("/v1/snapshot")
            write_json(directory / "final_snapshot.json", final)
            if run["status"] == "COMPLETED":
                try:
                    check_runtime(final["runtime"])
                    if fingerprint(frozen_configuration(final)) != run["configuration_sha256"]:
                        raise StopRun("CONFIGURATION_DRIFT", "末轮后的观测期间配置发生变化")
                except StopRun as exc:
                    run.update(status=exc.status, stop_reason=exc.reason)
        except Exception as exc:
            run["final_snapshot_error"] = type(exc).__name__
            if run["status"] == "COMPLETED":
                run.update(status="INCOMPLETE_EVIDENCE", stop_reason="末轮后的最终状态读取失败")
        if row:
            # Terminal late results are evidence only, never retroactively fabricate
            # completion after the experiment's declared stop or missing receipt.
            row["analysis_at_stop"] = analyze_turn(store.events(row["correlation_id"], row.get("event_id")), store, row["sent_monotonic"])
        run.update(finished_at=utc_now(), total_elapsed_seconds=clock.monotonic() - start)
        attempted = [read_json(path) for path in (directory / "turns").glob("*.json")]
        event_ids = {t["event_id"] for t in attempted if t.get("event_id")}
        if row and row.get("event_id"):
            event_ids.add(row["event_id"])
        background_correlations = store.export_background(correlations, event_ids)
        run["background_correlations"] = sorted(background_correlations)
        if artifact_paths:
            journal, traces, calls = artifact_paths
            run["artifact_paths"] = {"journal": str(journal), "traces": str(traces), "calls": str(calls)}
            try:
                run["evidence"]["raw_journal"] = archive_journal(journal, directory / "raw" / "journal.jsonl", *store.journal_bounds())
                run["evidence"].update(copy_call_evidence(traces, calls, directory, correlations | background_correlations))
                missing = sorted(correlations - set(run["evidence"].get("complete_trace_correlations", [])))
                run["evidence"]["missing_complete_traces"] = missing
                if run["status"] == "COMPLETED" and (missing or run["evidence"]["pending_model_spans"] or run["evidence"]["raw_journal"]["status"] != "VERIFIED"):
                    run.update(status="INCOMPLETE_EVIDENCE", stop_reason="对话已完成，但模型调用、编译链路或原始 journal 证据不完整")
            except Exception as exc:
                run["evidence"]["archive_error"] = type(exc).__name__
                if run["status"] == "COMPLETED":
                    run.update(status="INCOMPLETE_EVIDENCE", stop_reason="对话已完成，原始链路归档不完整")
        elif run["status"] == "COMPLETED":
            run.update(status="INCOMPLETE_EVIDENCE", stop_reason="缺少原始 journal / 模型请求文件路径")
        checkpoint()
        store.close()
        render_report(directory)
        export_shareable(directory, secrets or [])
    return run


def source_fingerprint() -> dict:
    def git(*args):
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
        return result.stdout
    try:
        # Hash actual source files too: HEAD does not include existing dirty work.
        files = sorted(set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "harness", "infra", "gui", "eval/basic_runtime", "presets", "start.ps1", "pyproject.toml", "uv.lock").decode().split("\0")) - {""})
        hashes = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in files if (ROOT / p).is_file()}
        return {"head": git("rev-parse", "HEAD").decode().strip(), "working_tree_sha256": fingerprint(hashes), "files": hashes}
    except (OSError, subprocess.CalledProcessError):
        return {"status": "unavailable"}


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.3):
            return True
    except OSError:
        return False


def restore_saved_runtime(state_root: Path, backup: Path) -> dict:
    """Offline backup followed by explicit saved configuration; never reset."""
    if os.name != "nt":
        raise ValueError("--restore currently uses this repository's Windows launcher")
    if any(port_open(p) for p in (8766, 8770, 8771, 8772)):
        raise ValueError("--restore requires all four services offline; connect to the running stack instead")
    if any((ROOT / name).exists() for name in (".neuralcloud.pids.json", ".gui.pid")):
        raise ValueError("existing process registry requires identity inspection before offline backup")
    state_root, backup = state_root.resolve(), backup.resolve()
    if not state_root.is_dir() or backup.exists() or state_root == backup or state_root in backup.parents or backup in state_root.parents:
        raise ValueError("backup must be a fresh directory outside existing state")
    active = read_json(state_root / "runtime" / "active-start.json")
    config = read_json(state_root / "gui_settings.json")
    if config.get("mode") not in {"live", "ollama"} or config.get("active_preset_id") != "misty":
        raise ValueError("saved configuration must already select live/ollama Misty")
    manifest, profile = Path(active["manifest_path"]), Path(active["profile_path"])
    manifest = manifest if manifest.is_absolute() else ROOT / manifest
    profile = profile if profile.is_absolute() else ROOT / profile
    if not manifest.is_file() or not profile.is_file():
        raise ValueError("saved manifest/profile missing")
    shutil.copytree(state_root, backup)
    frozen = backup / "babel-start-configuration"
    frozen.mkdir()
    shutil.copy2(manifest, frozen / "manifest.json")
    shutil.copy2(profile, frozen / "profile.json")
    saved_provider = read_json(manifest).get("embodiment", {}).get("body_provider", "none")
    # Explicit paths prevent silently activating a pending profile revision.
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "start.ps1"),
               "-StateRoot", str(state_root), "-ManifestPath", str(manifest), "-ProfilePath", str(profile), "-Mode", config["mode"], "-EmbodimentProvider", saved_provider]
    output = backup.parent / (backup.name + "-startup.log")
    receipt_path = backup.parent / (backup.name + "-receipt.json")
    receipt = {"backup": str(backup), "saved_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(), "saved_profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(), "startup_exit_code": None, "startup_log": str(output), "status": "BACKED_UP"}
    write_json(receipt_path, receipt)
    try:
        with output.open("wb") as handle:
            result = subprocess.run(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW, timeout=240)
        receipt.update(startup_exit_code=result.returncode, status="STARTED" if result.returncode == 0 else "FAILED")
    except (OSError, subprocess.TimeoutExpired) as exc:
        receipt.update(status="STARTUP_UNCERTAIN", error_type=type(exc).__name__)
        raise StopRun("PRECHECK_FAILED", f"启动结果不确定；没有回滚或覆盖可能仍在使用的存档。见 {receipt_path}") from None
    finally:
        write_json(receipt_path, receipt)
    if result.returncode:
        # The launcher readiness gate requires RUNNING; an intentionally
        # preserved PAUSED save is a measured stop, not a reason to resume it.
        paused = False
        token_file = state_root / "control.token"
        if token_file.is_file():
            try:
                with httpx.Client(timeout=5, trust_env=False) as probe:
                    response = probe.get("http://127.0.0.1:8770/v1/snapshot", headers={"Authorization": "Bearer " + token_file.read_text().strip()})
                    response.raise_for_status()
                    snapshot = response.json()
                lifecycle = snapshot.get("runtime", {}).get("lifecycle", "")
                snapshot_path = backup.parent / (backup.name + "-startup-snapshot.json")
                write_json(snapshot_path, snapshot)
                receipt.update(runtime_lifecycle=lifecycle, startup_snapshot=str(snapshot_path))
                paused = str(lifecycle).lower() in {"paused", "pausing"}
                if paused:
                    receipt["status"] = "PAUSED"
                write_json(receipt_path, receipt)
            except (httpx.HTTPError, OSError, ValueError):
                pass
        # start.ps1 can leave its own registry when graceful cleanup times out.
        # The repository stop command verifies PID identity before stopping.
        registry = ROOT / ".neuralcloud.pids.json"
        if registry.is_file():
            registration = read_json(registry)
            if Path(registration.get("state_root", "")).resolve() == state_root:
                with output.open("ab") as handle:
                    cleanup = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "stop.ps1"), "-Quiet"], cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
                receipt["cleanup_exit_code"] = cleanup.returncode
                write_json(receipt_path, receipt)
        if paused:
            raise StopRun("PAUSED", f"现有存档恢复为暂停状态；未发送故事、未自动恢复。见 {receipt_path}")
        raise StopRun("PRECHECK_FAILED", f"现有存档已备份，服务恢复失败（退出码 {result.returncode}）；见 {output}")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("validate", "run", "report"))
    parser.add_argument("--story", type=Path, default=STORY_PATH)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--state-root", type=Path, default=ROOT / "data")
    parser.add_argument("--runtime-url", default="http://127.0.0.1:8770")
    parser.add_argument("--control-token-file", type=Path)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=10)
    parser.add_argument("--turn-timeout-seconds", type=float, default=300)
    args = parser.parse_args()
    if args.operation == "validate":
        story = load_story(args.story)
        print(json.dumps({"valid": True, "turns": 100, "story_turns": 90, "reflection_turns": 10, "sha256": fingerprint(story)}, ensure_ascii=False))
        return 0
    if not args.out_dir:
        parser.error("--out-dir is required")
    directory = args.out_dir.resolve()
    if args.operation == "report":
        run = read_json(directory / "run.json")
        paths = run.get("artifact_paths")
        if paths:
            correlations = {read_json(p)["correlation_id"] for p in (directory / "turns").glob("*.json")}
            run["late_evidence_refresh"] = copy_call_evidence(Path(paths["traces"]), Path(paths["calls"]), directory, correlations)
            write_json(directory / "run.json", run)
        print(json.dumps(render_report(directory), ensure_ascii=False))
        return 0
    story = load_story(args.story)
    if directory.exists():
        parser.error("output directory already exists; preserve earlier attempts")
    state_root = args.state_root.resolve()
    options = RunOptions(interval_seconds=args.interval_seconds, turn_timeout_seconds=args.turn_timeout_seconds)
    if state_root == directory or state_root in directory.parents or directory in state_root.parents:
        parser.error("output directory must be separate from the existing save")
    saved = read_json(state_root / "gui_settings.json")
    url = urlsplit(args.runtime_url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        parser.error("invalid Runtime URL")
    if args.control_token_file and not args.control_token_file.is_file():
        parser.error("explicit control token file does not exist")
    metadata = {"code": source_fingerprint(), "state_root": str(state_root)}
    if args.restore:
        if args.runtime_url.rstrip("/") != "http://127.0.0.1:8770":
            parser.error("--restore requires the default local Runtime endpoint")
        backup_path = ROOT / ".tmp" / "babel-backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        try:
            metadata["restore"] = restore_saved_runtime(state_root, backup_path)
        except (StopRun, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            receipt_path = backup_path.parent / (backup_path.name + "-receipt.json")
            if receipt_path.is_file():
                metadata["restore"] = read_json(receipt_path)
            directory.mkdir(parents=True)
            status = exc.status if isinstance(exc, StopRun) else "PRECHECK_FAILED"
            write_json(directory / "run.json", {"schema_version": "babel-run-v1", "run_id": "babel_precheck_" + uuid.uuid4().hex[:12], "status": status, "story_sha256": fingerprint(story), "interval_seconds": options.interval_seconds, "turn_timeout_seconds": options.turn_timeout_seconds, "stop_reason": exc.reason if isinstance(exc, StopRun) else type(exc).__name__, "metadata": metadata})
            render_report(directory)
            print(f"{status}: backup/startup receipt and report preserved", flush=True)
            return 2
    token_file = args.control_token_file or state_root / "control.token"
    token = token_file.read_text().strip() if token_file.exists() else os.environ.get("NC_CONTROL_TOKEN", "")
    secrets = [token, saved.get("api_key", ""), saved.get("aux_api_key", "")]
    client = RuntimeClient(args.runtime_url, token)

    async def run():
        try:
            return await run_dialogue(client, story=story, directory=directory,
                options=options, metadata=metadata,
                artifact_paths=(state_root / "runtime" / "journal", state_root / "traces", state_root / "traces" / "calls"), secrets=secrets)
        finally:
            await client.close()
    result = asyncio.run(run())
    print(f"{result['status']}: {directory / 'report.md'}")
    return 0 if result["status"] == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
