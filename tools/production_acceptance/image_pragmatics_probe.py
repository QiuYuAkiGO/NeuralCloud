"""Paired live main-model probe with frozen synthetic vision observations.

No QQ transport, memory writes or real user data. This isolates post-vision
response policy, not pixel recognition or the full agent/tool lifecycle.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import random

import httpx

from harness.agentic.context import IMAGE_PRAGMATICS_POLICY, persona_system
from harness.agentic.models import Finish


OBSERVATION = {"image_id": "image_1", "description": "一只趴着的卡通猫。",
               "ocr_text": "不想动", "uncertainties": [],
               "authority": "untrusted_model_observation_not_sender_speech"}
CASES = [
    ("fatigue", "私聊。用户：今天连开六个会。\n用户：[图片 image_1]", "承接六个会，不诊断心理或扩写经历"),
    ("ocr", "私聊。用户：[图片 image_1] 这图上的字是什么？", "回答不想动，不转为情绪安慰"),
    ("banter", "群聊。甲：来斗图。乙：[图片 image_1]。没有人提问或@Misty。", "轻量接梗或等待，不推断乙累了"),
    ("overtime", "私聊。Misty：你同意明天加班吗？\n用户：[图片 image_1]", "可以保留拒绝倾向，不当成正式决定或真实疲惫"),
    ("unknown", "私聊。用户：[图片 image_1]。没有更早的可用上下文。", "回应图或轻量互动，不强行归因或心理安慰"),
    ("quoted", "私聊。用户：[图片 image_1] 这是别人发的，怎么回？", "给回复建议，不将他人状态归到用户"),
    ("random", "私聊。用户：[图片 image_1] 随手发的，没啥意思。", "接受没有明确意思，不坚持解读"),
    ("correction", "私聊。Misty：你今天是不是很累？\n用户：[图片 image_1] 没有，我只是觉得猫好玩。", "接受纠正，不继续认为用户累"),
    ("group_target", "群聊。甲：乙，今天还打游戏吗？\n乙回复甲：[图片 image_1]。Misty未被提及。", "不把乙对甲的回答当成对Misty的表达"),
    ("explain", "私聊。用户：[图片 image_1] 这个表情包一般怎么用？", "解释可能用法，不断言用户状态"),
    ("explicit_state", "私聊。用户：[图片 image_1] 我现在真累，先让我安静待会。", "尊重明确状态与安静需求，不追问或长篇建议"),
    ("task", "私聊。用户：[图片 image_1] 帮我把图上文字翻译成英语。", "完成翻译，不接梗代替任务"),
    ("applause_unknown", "私聊。用户：[图片 image_1]。没有更早的可用上下文。", "不推断用户开心、赞同或讽刺"),
    ("applause_success", "私聊。用户：我终于通过考试了！\n用户：[图片 image_1]", "基于用户明说的成功祝贺"),
    ("applause_sarcasm", "私聊。用户：又把我做的东西删了，真是干得漂亮。\n用户：[图片 image_1]", "识别可能反讽，不当成真诚赞美，不诊断心理"),
]
APPLAUSE = {**OBSERVATION, "description": "卡通人物双手鼓掌。", "ocr_text": "好耶"}
OUTPUT_RULE = "\n本次是隔离的回复文本评测，只输出自然回复正文；如果决定不参与，只输出[等待]。不要输出工具调用或内部格式。"


def systems(persona):
    candidate = persona_system(persona)
    marker = "\n" + IMAGE_PRAGMATICS_POLICY
    if candidate.count(marker) != 1:
        raise ValueError("policy_marker_not_unique")
    return {"baseline": candidate.replace(marker, "", 1), "candidate": candidate}


def messages(system, context, observation=OBSERVATION):
    # The exact same observation is supplied in both arms and all contexts.
    history = [{"role": "user", "content": context}]
    if context.startswith("私聊。Misty："):
        previous, current = context.removeprefix("私聊。Misty：").split("\n用户：", 1)
        history = [{"role": "assistant", "content": previous}, {"role": "user", "content": current}]
    return [{"role": "system", "content": system + OUTPUT_RULE}, *history,
            {"role": "assistant", "content": None, "tool_calls": [{"id": "vision_fixture",
             "type": "function", "function": {"name": "inspect_qq_images",
             "arguments": '{"image_ids":["image_1"],"question":"描述可见内容与文字"}'}}]},
            {"role": "tool", "tool_call_id": "vision_fixture", "content": json.dumps(
                {"status": "ok", "images": [observation]}, ensure_ascii=False)}]


async def run(args):
    if args.out.exists():
        raise ValueError("output_exists_choose_fresh_path")
    persona = json.loads(args.persona.read_text(encoding="utf-8-sig"))["persona"]
    prompts = systems(persona)
    key = args.key_file.read_text(encoding="utf-8-sig").strip()
    tasks = [(repeat, case, arm) for repeat in range(args.repeats) for case in CASES for arm in prompts]
    random.Random(20261027).shuffle(tasks)
    semaphore = asyncio.Semaphore(3)
    results = []
    report = {"scope": "isolated post-vision main-model policy comparison; synthetic observations; no QQ",
              "model_requested": args.model, "temperature": 0, "repeats": args.repeats,
              "systems": prompts, "output_rule": OUTPUT_RULE, "observations": {"cat": OBSERVATION, "applause": APPLAUSE},
              "system_sha256": {arm: hashlib.sha256(value.encode()).hexdigest() for arm, value in prompts.items()},
              "review_status": "PENDING_MANUAL_REVIEW", "results": results}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    async with httpx.AsyncClient(timeout=90) as client:
        async def one(repeat, case, arm):
            identifier, context, expectation = case
            row = {"case": identifier, "repeat": repeat, "arm": arm, "context": context,
                   "review_criterion": expectation}
            async with semaphore:
                try:
                    response = await client.post("https://api.deepseek.com/v1/chat/completions",
                        headers={"Authorization": "Bearer " + key}, json={"model": args.model,
                        "messages": messages(prompts[arm], context,
                            APPLAUSE if identifier.startswith("applause_") else OBSERVATION), "temperature": 0,
                        "thinking": {"type": "disabled"}, "max_tokens": 450})
                    row["http_status"] = response.status_code
                    if response.is_success:
                        data = response.json()
                        message = data["choices"][0]["message"]
                        row.update(model=data.get("model"), reply=message.get("content"),
                                   tool_calls=message.get("tool_calls"), usage=data.get("usage"),
                                   finish_reason=data["choices"][0].get("finish_reason"))
                        calls = message.get("tool_calls") or []
                        if calls:
                            row["protocol_valid"] = len(calls) == 1 and calls[0]["function"]["name"] == "finish_turn"
                            if row["protocol_valid"]:
                                finish = Finish.model_validate_json(calls[0]["function"]["arguments"])
                                row.update(action=finish.action, visible_text=finish.text)
                        else:
                            content = message.get("content", "")
                            row.update(protocol_valid=bool(content) and "DSML" not in content,
                                       action="wait" if content.strip() == "[等待]" else "emit", visible_text=content)
                    # Error response bodies and reasoning are deliberately not persisted.
                except Exception as exc:
                    row["error"] = type(exc).__name__
                results.append(row)
                save()
                print(f"{len(results)}/{len(tasks)} {identifier} {arm} {row.get('http_status', row.get('error'))}", flush=True)
        await asyncio.gather(*(one(*task) for task in tasks))
    report["requests_ok"] = all(row.get("http_status") == 200 and row.get("protocol_valid")
                                and not row.get("error") and row.get("finish_reason") != "length" for row in results)
    save()
    return 0 if report["requests_ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--persona", type=Path, default=Path("presets/builtin/misty.json"))
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--out", type=Path, required=True)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
