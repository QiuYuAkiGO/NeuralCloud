"""Run fixed QQ contract scenarios offline, with no external model or transport.

These synthetic inputs/outputs exercise production validation and durable
lifecycle boundaries. They are not evidence that a model preserves persona.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace

from harness.cognitive.qq_memory import wait_foreground_idle
from harness.cognitive.web import WebStepChoice
from harness.emotion.models import EmotionFrame
from harness.emotion.semantics import validate_guidance_text
from harness.embodiment.web_contracts import WebError, validate_public_url
from harness.qq.decision import _validate_reply_segments
from harness.qq.memory import QQImageAnalysis, protocol_message_evidence_ref
from harness.qq.shared import QQSharedPrepareRequest
from harness.runtime.adapters import PipelineCognitiveAdapter
from harness.runtime.cognitive_store import CognitiveStateStore, DELETED

DEFAULT_FIXTURE = Path(__file__).parent / "fixtures" / "qq_latency_contract_cases.json"


async def _cancellation(mode):
    owner = SimpleNamespace(_closing=False, command_tasks=set())
    host = SimpleNamespace(adapter=owner)
    started, release, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    committed = []
    async def operation():
        started.set()
        await release.wait()
        committed.append("once")
        return "durable-result"
    caller = asyncio.create_task(PipelineCognitiveAdapter._run_command(owner, operation))
    await started.wait()
    command = next(iter(owner.command_tasks))
    try:
        if mode in {"rpc_cancel", "cancel_then_commit", "cancel_then_wait"}:
            caller.cancel()
            try:
                await caller
            except asyncio.CancelledError:
                pass
            assert not command.done() and command in owner.command_tasks
        waiter = None
        if mode in {"maintenance_cancel", "shutdown_wait", "cancel_then_wait"}:
            waiter = asyncio.create_task(wait_foreground_idle(host, stopping))
            await asyncio.sleep(0)
            assert not waiter.done()
            if mode == "maintenance_cancel":
                waiter.cancel()
                try:
                    await waiter
                except asyncio.CancelledError:
                    pass
                assert not command.cancelled()
            elif mode == "shutdown_wait":
                stopping.set()
                assert await waiter is False and not command.cancelled()
        release.set()
        assert await command == "durable-result" and committed == ["once"]
        if mode == "cancel_then_wait":
            assert await waiter is True
    finally:
        release.set()
        await asyncio.gather(caller, command, return_exceptions=True)
    return True


def _restart(mode, directory):
    path = directory / "owner.json"
    store = CognitiveStateStore(path)
    store.activate(enabled=True)
    try:
        store.commit({"owner_meta": {"state": {"revision": 7}}, "ledger": {"Prepare\0one": {"reply": "已准备"}}})
        if mode == "committed":
            reopened = CognitiveStateStore(path)
            try:
                assert reopened.activate(enabled=True)["ledger"]["Prepare\0one"] == {"reply": "已准备"}
            finally:
                reopened.close()
        elif mode == "flag_off":
            assert store.activate(enabled=False)["revision"] == 7
            assert json.loads(path.read_text(encoding="utf-8"))["revision"] == 7
        elif mode == "checkpoint":
            assert store.checkpoint(force=True)
            assert json.loads(path.read_text(encoding="utf-8"))["ledger"]["Prepare\0one"]["reply"] == "已准备"
        elif mode == "delete":
            store.commit({"ledger": {"Prepare\0one": DELETED}})
            assert store.load()["ledger"] == {}
        elif mode == "uncommitted":
            connection = sqlite3.connect(store.path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM records")
            finally:
                connection.close()
            assert store.load()["revision"] == 7
        else:
            raise AssertionError("unknown restart scenario")
    finally:
        store.close()
    return True


def run_case(case, directory):
    kind, values = case["contract"], case["input"]
    accepted = False
    output = None
    try:
        if kind == "segments":
            output = _validate_reply_segments(values["segments"], values["reply"])
        elif kind == "identity":
            output = protocol_message_evidence_ref(values["conversation"], values["member"], values["message"])
        elif kind == "image":
            output = QQImageAnalysis.model_validate(values).model_dump(mode="json")
        elif kind == "public_url":
            output = validate_public_url(values["url"])
        elif kind == "web_choice":
            output = WebStepChoice.model_validate(values).model_dump(mode="json")
        elif kind == "shared_request":
            output = QQSharedPrepareRequest.model_validate(values).model_dump(mode="json")
        elif kind == "emotion":
            labels = values.get("labels", {})
            intensity = max(labels.values(), default=0.0)
            frame = EmotionFrame(context_id=case["case_id"], scope="qq:group:10001", revision=1,
                as_of=datetime(2026, 9, 16, tzinfo=UTC), labels=labels,
                dialogue_trends=values.get("trends", {}), intensity=intensity,
                dominant_source="dialogue" if labels else "personality",
                weights={"dialogue": 1.0 if labels else 0.0, "personality": 0.0 if labels else 1.0, "homeostasis": 0.0},
                strengths={"dialogue": intensity, "personality": .25, "homeostasis": 0.0})
            accepted = validate_guidance_text(values["text"], frame=frame)
        elif kind == "cancellation":
            output = asyncio.run(_cancellation(values["mode"]))
        elif kind == "restart":
            output = _restart(values["mode"], directory)
        else:
            raise AssertionError("unrecognized fixture contract")
        if kind != "emotion":
            accepted = True
    except (ValueError, TypeError, WebError):
        accepted = False
    assert accepted == case["expected"]["accepted"], f"{case['case_id']}: acceptance differs"
    if accepted and "output" in case["expected"]:
        assert output == case["expected"]["output"], f"{case['case_id']}: evidence/output differs"
    if accepted and kind == "image":
        assert output["authority"] == "model_inference"
        if output["status"] == "unknown":
            assert output["description"] is None and output["ocr_text"] is None
    return {"case_id": case["case_id"], "category": case["category"], "status": "passed"}


def run_fixture(path=DEFAULT_FIXTURE):
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = fixture["cases"]
    assert len(cases) >= 120 and len({case["case_id"] for case in cases}) == len(cases)
    assert {"text", "multi_identity", "emotion", "images", "web", "revision", "cancellation", "restart"} <= {
        case["category"] for case in cases}
    results = []
    with tempfile.TemporaryDirectory(prefix="qq-offline-acceptance-") as temporary:
        for case in cases:
            directory = Path(temporary) / case["case_id"]
            directory.mkdir()
            try:
                result = run_case(case, directory)
            except (AssertionError, KeyError) as exc:
                result = {"case_id": case["case_id"], "category": case["category"], "status": "failed",
                          "error_type": type(exc).__name__}
            results.append(result)
    return {"schema_version": "qq-offline-acceptance-v1", "provenance": fixture["provenance"],
            "cases": len(results), "passed": sum(row["status"] == "passed" for row in results),
            "categories": dict(Counter(case["category"] for case in cases)), "results": results,
            "persona_quality": "pending_natural_canary_review",
            "latency_evidence": "pending_24_hours_and_100_qualified_samples_per_cohort",
            "limitations": ["Synthetic contract fixtures are not live model outputs or persona quality judgments.",
                            "Passing these cases does not establish production latency improvement."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_fixture(args.fixture)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    raise SystemExit(int(report["passed"] != report["cases"]))


if __name__ == "__main__":
    main()
