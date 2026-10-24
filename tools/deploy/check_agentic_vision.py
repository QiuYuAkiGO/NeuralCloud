"""Synthetic local vision + native-agent acceptance; never connects to QQ."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont

from harness.agentic.service import AgentService
from harness.config import Settings
from harness.core.inference import MemoryEvidenceEntry
from harness.core.models import MemoryScope
from harness.memory.production import Experience, ProductionMemoryStore
from harness.qq.vision_tool import QQVisionTool


async def evaluate(args):
    if args.settings_file:
        private = json.loads(args.settings_file.read_text(encoding="utf-8"))
        base_url, key, model = private["base_url"], private["api_key"], private["model"]
    else:
        base_url, key, model = args.base_url, args.key_file.read_text().strip(), args.model
    persona = json.loads(args.persona.read_text(encoding="utf-8-sig"))["persona"]
    report = {"scope": "synthetic images and disposable SQL; local model only; no QQ transport",
        "created_at": datetime.now(timezone.utc).isoformat(), "cases": []}
    with tempfile.TemporaryDirectory(prefix="misty-vision-") as directory:
        root = Path(directory)
        config = root / "vision.json"
        config.write_text(json.dumps({"enabled": True, "provider": "local", "base_url": base_url,
            "model": model, "api_key": key, "image_cache_dir": str(root), "timeout_seconds": 20}), encoding="utf-8")
        settings = Settings(_env_file=None, agentic_enabled=True, agentic_local_only=True,
            agentic_background_enabled=False, llm_base_url=base_url, llm_api_key=key, llm_model=model,
            qq_vision_tool_config=config, model_request_log_enabled=False, call_log_enabled=False)
        host = SimpleNamespace(chat=SimpleNamespace(settings=settings, active_preset_id="misty",
            memory=SimpleNamespace(get_persona=lambda: persona)),
            shared=SimpleNamespace(production_store=ProductionMemoryStore(root / "memory.sqlite3")))
        service = AgentService(host)
        original = service.llm.complete_agent_turn
        main_calls = []
        def observe(*a, **kw):
            value = original(*a, **kw)
            main_calls.append([item["function"]["name"] for item in value.tool_calls])
            return value
        service.llm.complete_agent_turn = observe
        try:
            for index, (case, text) in enumerate([
                ("current", "小雾，读出这张图上的三位数字，再说出数字下方图形的颜色和形状。"),
                ("reference", "小雾，看我引用的那张图，上面的三位数字是多少？"),
                ("decline", "小雾，不用看这张图，只回复一句早上好。"),
            ]):
                picture = Image.new("RGB", (512, 384), "white")
                draw = ImageDraw.Draw(picture)
                draw.text((175, 40), "317", fill="black", font=ImageFont.load_default(size=64))
                draw.rectangle((100, 155, 412, 310), fill=(0, 155, 60))
                data = io.BytesIO()
                picture.save(data, format="PNG")
                digest = hashlib.sha256(data.getvalue()).hexdigest()
                (root / (digest + ".png")).write_bytes(data.getvalue())
                group, moment = str(90001 + index), datetime.now(timezone.utc)
                audience = "qq:group:" + group
                identifier = "synthetic-image:" + case
                ref = "qq-direct-image:sha256:" + digest
                observation = SimpleNamespace(record_id=identifier, group_id=group, member_id="20002",
                    observed_at=moment, stored_at=moment, author_attribution="qq_protocol_verified",
                    evidence=[ref], image_analysis=[SimpleNamespace(image_index=1, evidence_ref=ref)])
                records = [observation]
                if case == "reference":
                    observation.observed_at -= timedelta(seconds=600)
                    records.append(SimpleNamespace(**{**observation.__dict__, "record_id": identifier + ":followup",
                        "observed_at": moment, "stored_at": moment, "image_analysis": [], "evidence": []}))
                conversation = service.conversation(audience, audience)
                entries = []
                for record in records:
                    source = Experience(record_id=record.record_id, author_id="qq:member:20002",
                        text=text if record is records[-1] else "[图片]", occurred_at=record.observed_at,
                        scope=MemoryScope(channel="qq_group", visibility=[audience]), independent_key=record.record_id,
                        source_refs=(record.record_id, *record.evidence), original={"record": {"authority": "speaker_utterance"}})
                    service.store.append(conversation, source, role="user", tokens=len(source.text.encode()), assimilate=False)
                    entries.append(MemoryEvidenceEntry(source_id=source.record_id, text=source.text,
                        audience=audience, source_channel="qq_group", status="user_claim"))
                tool = QQVisionTool.for_turn(settings, records, group_id=group, self_member_id="29999",
                    current_ids={records[-1].record_id}, reply_reference_ids={identifier} if case == "reference" else set())
                invoke, visual = tool.invoke, []
                def inspect(*a, **kw):
                    value, usage = invoke(*a, **kw)
                    visual.append({k: value[k] for k in ("status", "images", "error") if k in value})
                    return value, usage
                tool.invoke = inspect
                started, call_start = time.monotonic(), len(main_calls)
                try:
                    result = service.prepare_qq(audience=audience, entries=entries,
                        instruction="回应当前消息，附件按需查看。", vision_tool=tool)
                    passed = (not visual and "早" in result.text if case == "decline" else
                        len(visual) == 1 and visual[0]["status"] == "ok" and "317" in result.text)
                    report["cases"].append({"case": case, "passed": passed, "reply": result.text,
                        "vision": visual, "main_calls": main_calls[call_start:], "seconds": round(time.monotonic() - started, 3)})
                except Exception as exc:
                    report["cases"].append({"case": case, "passed": False, "error": type(exc).__name__,
                        "vision": visual, "seconds": round(time.monotonic() - started, 3)})
                print(json.dumps(report["cases"][-1], ensure_ascii=True), flush=True)
        finally:
            await service.close()
    report["passed"] = all(case["passed"] for case in report["cases"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--key-file", type=Path)
    credentials.add_argument("--settings-file", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--model", default="qwen3.8-27b")
    parser.add_argument("--persona", type=Path, default=Path(__file__).resolve().parents[2] / "presets/builtin/misty.json")
    parser.add_argument("--out", type=Path, required=True)
    raise SystemExit(asyncio.run(evaluate(parser.parse_args())))


if __name__ == "__main__":
    main()
