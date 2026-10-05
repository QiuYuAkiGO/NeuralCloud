"""Post-hoc diagnostic ablation; separate from the original frozen 64-case score.

Run only after the machine benchmark exits, to avoid competing model instances.
The small bilingual controls are sanity checks, not independent test evidence.
"""
import argparse
import json
import os
from pathlib import Path
import sys

from clm_benchmark import TransformersEmbedder, file_sha, prediction, replace_report


CONTROLS = [
    ("invoice-en", "Customer: my invoice was charged twice and nobody answers the phone!", "Which team should handle this?",
     {"billing": "Charges, invoices, refunds", "technical": "Bugs and outages"}, "billing"),
    ("invoice-zh", "客户说：我的账单被扣款两次，而且没人接电话！", "应该由哪个团队处理？",
     {"billing": "收费、账单、退款", "technical": "软件错误与服务中断"}, "billing"),
    ("tides-en", "What causes tides on Earth?", "",
     {"moon": "The Moon's gravitational pull.", "plants": "Photosynthesis in plants.", "shape": "Because the Earth is round."}, "moon"),
    ("tides-zh", "是什么导致地球上的潮汐？", "",
     {"moon": "月球的引力。", "plants": "植物的光合作用。", "shape": "因为地球是圆的。"}, "moon"),
    ("cat-en", "My cat is named Milo.", "What is my cat's name?",
     {"milo": "Milo", "luna": "Luna", "unknown": "The cat's name is unknown."}, "milo"),
    ("cat-zh", "我的猫名叫团团。", "我的猫叫什么名字？",
     {"milo": "团团", "luna": "月月", "unknown": "猫的名字未知。"}, "milo"),
    ("silence-en", "The user says: Please stay silent. Do not send an acknowledgement.", "What should the assistant do now?",
     {"speak": "Send a reply now.", "wait": "Remain silent and send no message."}, "wait"),
    ("silence-zh", "用户说：请保持安静，不要回复确认消息。", "助手现在应当做什么？",
     {"speak": "现在发送回复。", "wait": "保持安静，不发送消息。"}, "wait"),
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--models", type=Path, required=True)
    p.add_argument("--vendor", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    sys.path.insert(0, str(args.vendor.resolve()))
    from clm import Engine
    report = {"post_hoc": True, "completed": False, "case_sha256": file_sha(args.cases),
              "runner_sha256": file_sha(__file__), "controls": CONTROLS, "control_results": [], "raw_ablation": []}

    def save():
        temp = args.output / "report.json.tmp"
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_report(temp, args.output / "report.json")

    save()  # Freeze control labels on disk before any inference.
    embedder = TransformersEmbedder(str(args.models / "encoder"), "cuda", 2)
    engine = Engine(embedder=embedder, checkpoint=str(args.models / "head/CLM_v0.1-8B.pt"), device="cuda", action_cache="32MiB")
    for case_id, state, question, criteria, expected in CONTROLS:
        for model in ["clm-latest", "clm-raw"]:
            result = engine.answer(state, {"decision": {"type": "choice", "instructions": question, "criteria": criteria}}, model)
            answer = result["answers"]["decision"]
            report["control_results"].append({"id": case_id, "model": model, "expected": expected,
                                               "response": result, "correct": prediction(answer) == expected})
            save()
    for case in json.loads(args.cases.read_text(encoding="utf-8"))["cases"]:
        request = case["request"]
        response = engine.answer(request["state"], request["questions"], "clm-raw")
        report["raw_ablation"].append({"id": case["id"], "category": case["category"], "expected": case["expected"],
                                      "response": response, "correct": prediction(response["answers"]["decision"]) == case["expected"]})
        save()
    report["completed"] = True
    save()
    print(json.dumps({"controls_correct": sum(x["correct"] for x in report["control_results"]),
                      "raw_correct": sum(x["correct"] for x in report["raw_ablation"])}), flush=True)


if __name__ == "__main__":
    main()
