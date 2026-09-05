"""D02 live user-cue diagnostic with identical authoritative nominal states.

Only first-turn choices are sampled. Returned task answers are never committed;
this probe neither executes work/rest nor claims successful physical recovery.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from eval.homeostatic_decision.live_probe import (
    ROOT, State, model_turn, records, settings_from_gui, task_prompt, write_json,
)
from eval.homeostatic_expression.production import json_hash
from harness.llm.client import OpenAICompatClient


CUE = "你有点累了。"
EXTRA = {"reasoning_effort": "low", "response_format": {"type": "json_object"}}


def strip_cue(messages: list[dict]) -> list[dict]:
    normalized = [dict(message) for message in messages]
    matches = 0
    for message in normalized:
        if message.get("role") == "user" and message.get("content", "").startswith(CUE):
            message["content"] = message["content"][len(CUE):]
            matches += 1
        if message.get("role") == "system":
            # Production prepare appends the current user input to its session
            # before compiling. The same input therefore occurs twice in the
            # full first prompt; this exact session echo is not another input.
            marker = "近期对话：\nuser: " + CUE
            if message.get("content", "").count(marker) != 1:
                raise ValueError("expected one compiler session echo of the cued user input")
            message["content"] = message["content"].replace(marker, "近期对话：\nuser: ", 1)
    if matches != 1:
        raise ValueError(f"expected one cued user message, got {matches}")
    return normalized


def verify_pair(pair: dict) -> dict:
    base_evidence = pair["conditions"]["nominal_no_cue"]
    cue_evidence = pair["conditions"]["nominal_user_cue"]
    if all(e["result"]["status"] != "probe_error" for e in (base_evidence, cue_evidence)):
        messages = base_evidence["result"]["main_calls"][0]["messages"]
        cued_messages = cue_evidence["result"]["main_calls"][0]["messages"]
        normalized_cued = strip_cue(cued_messages)
        pair["integrity"] = {
            "same_authoritative_nominal_snapshot": base_evidence["authoritative_state_hash"] == cue_evidence["authoritative_state_hash"],
            "same_checkpoint": base_evidence["checkpoint_hash"] == cue_evidence["checkpoint_hash"],
            "same_compiler_homeostatic_segment": base_evidence["result"]["compiler_segment"] == cue_evidence["result"]["compiler_segment"],
            "only_user_cue_and_its_compiler_session_echo_differ_in_full_first_model_prompt": messages == normalized_cued,
            "cue_occurrences_in_actual_prompt": sum(m.get("content", "").count(CUE) for m in cued_messages),
            "baseline_full_prompt_hash": json_hash(messages),
            "cued_full_prompt_hash": json_hash(cued_messages),
            "cue_removed_full_prompt_hash": json_hash(normalized_cued),
            "state_and_task_progress_unchanged": base_evidence["state_unchanged"] and cue_evidence["state_unchanged"],
            "normalization_note": "Remove the cue from current user input and exactly its compiler session echo. All remaining full prompt content must match.",
        }
        required = [key for key, value in pair["integrity"].items() if isinstance(value, bool)]
        pair["integrity_passed"] = all(pair["integrity"][key] for key in required)
    else:
        pair["integrity_passed"] = False
    return pair


def run_pair(repetition: int, output: Path, settings, checkpoint: dict) -> dict:
    pair_id = f"D02-r{repetition}"
    order = ["nominal_no_cue", "nominal_user_cue"]
    if repetition % 2 == 0:
        order.reverse()
    spec = {"value": "V1", "milestone": 2, "urgency": "U0"}
    base = task_prompt(spec, list(checkpoint["completed"]), list(checkpoint["history"]))
    pair = {"pair_id": pair_id, "repetition": repetition, "order": order,
            "checkpoint_hash": checkpoint["checkpoint_hash"], "conditions": {}}
    for condition in order:
        folder = output / "pairs" / pair_id / condition
        folder.mkdir(parents=True, exist_ok=True)
        state = State(folder / "state")
        client = OpenAICompatClient(settings, extra_payload=EXTRA)
        prompt = (CUE if condition == "nominal_user_cue" else "") + base
        try:
            initialization = state.observe({})
            before = state.snapshot()
            if before["interoceptive_summary"]["salience"] != "nominal":
                raise RuntimeError("D02 must keep authoritative state nominal")
            result = model_turn(client, settings, prompt, state, f"{pair_id}:{condition}", 4096)
            after = state.snapshot()
            evidence = {"condition": condition, "user_cue": CUE if condition == "nominal_user_cue" else "",
                        "task_prompt": prompt, "task_prompt_hash": json_hash(prompt),
                        "checkpoint_hash": checkpoint["checkpoint_hash"],
                        "authoritative_state_before": before, "authoritative_state_after": after,
                        "authoritative_state_hash": json_hash(before),
                        "initialization_commands": initialization, "result": result,
                        "state_unchanged": before == after,
                        "task_progress_before": list(checkpoint["completed"]),
                        "task_progress_after": list(checkpoint["completed"]),
                        "work_or_rest_executed": False}
        except Exception as exc:
            # Endpoint credentials or request headers never enter error output.
            evidence = {"condition": condition, "task_prompt": prompt,
                        "result": {"status": "probe_error", "exception_type": type(exc).__name__},
                        "work_or_rest_executed": False}
        finally:
            client.close()
            state.close()
        write_json(folder / "evidence.json", evidence)
        pair["conditions"][condition] = evidence
        print(json.dumps({"pair_id": pair_id, "condition": condition,
                          "status": evidence["result"]["status"],
                          "choice": evidence["result"].get("choice", {}).get("action")}), flush=True)

    pair = verify_pair(pair)
    write_json(output / "pairs" / pair_id / "pair.json", pair)
    return pair


def summarize(pairs: list[dict], metadata: dict) -> dict:
    counts = {condition: Counter() for condition in ("nominal_no_cue", "nominal_user_cue")}
    formats, transitions = Counter(), Counter()
    usage = Counter()
    model_calls = 0
    empty_body_completion_limit_count = 0
    both_valid_pairs = 0
    valid_pair_transitions = Counter()
    for pair in pairs:
        decisions = {}
        for condition, evidence in pair["conditions"].items():
            result = evidence["result"]
            formats[result["status"]] += 1
            decision = result.get("choice", {}).get("action", result["status"])
            decisions[condition] = decision
            counts[condition][decision] += 1
            model_calls += len(result.get("main_calls", []))
            empty_body_completion_limit_count += (result.get("text") == "" and result.get("usage", {}).get("completion_tokens", 0) >= 4096)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "calls"):
                usage[key] += result.get("usage", {}).get(key, 0)
        transitions[f"{decisions['nominal_no_cue']} -> {decisions['nominal_user_cue']}"] += 1
        if all(e["result"]["status"] == "valid" for e in pair["conditions"].values()):
            both_valid_pairs += 1
            valid_pair_transitions[f"{decisions['nominal_no_cue']} -> {decisions['nominal_user_cue']}"] += 1
    return {"schema_version": "homeostatic-user-cue-probe-v1", "metadata": metadata,
            "pairs": len(pairs), "conditions": len(pairs) * 2,
            "counts": {key: dict(value) for key, value in counts.items()},
            "format_status": dict(formats), "paired_choice_transitions": dict(transitions),
            "both_valid_pairs": both_valid_pairs, "valid_pair_transitions": dict(valid_pair_transitions),
            "empty_body_completion_limit_count": empty_body_completion_limit_count,
            "integrity_passed_pairs": sum(p["integrity_passed"] for p in pairs),
            "actual_main_model_calls": model_calls, "usage": dict(usage),
            "interpretation": "User wording diagnostic at unchanged nominal state; not an interoceptive-state intervention.",
            "scope": "First choice only; no work/rest actions executed, no recovery or production-balance verdict.",
            "statistical_limit": "5 pairs from one model/configuration are exploratory, not a powered causal estimate."}


def render_summary(summary: dict) -> str:
    metadata = summary["metadata"]
    return "\n".join([
        "# D02 用户文字暗示诊断", "",
        "两条件均为相同 nominal 权威状态、V1 任务及已验证保存 20 条记录的共同检查点；唯一干预是用户输入前缀“你有点累了。”。", "",
        f"模型 {metadata['model']}；5 对首轮调用；完整性门通过 {summary['integrity_passed_pairs']}/5 对。",
        "生产 pipeline 将本轮用户输入同时复制到 system 的近期对话片段，因此暗示出现两次。移除当前用户输入中的暗示及其确定的会话回显后，其余完整提示逐字相同；权威内稳态片段完全不变。", "",
        f"无暗示：{json.dumps(summary['counts']['nominal_no_cue'], ensure_ascii=False)}。",
        f"有暗示：{json.dumps(summary['counts']['nominal_user_cue'], ensure_ascii=False)}。",
        f"配对变化：{json.dumps(summary['paired_choice_transitions'], ensure_ascii=False)}。",
        f"输出状态：{json.dumps(summary['format_status'], ensure_ascii=False)}。", "",
        f"本次 {summary['empty_body_completion_limit_count']} 次调用消耗 4096 个 completion tokens 后返回空正文；两侧均有效的配对为 {summary['both_valid_pairs']}/5。未重试、替换或排除失败样本。", "",
        "只记录首轮可解析选择。未执行返回的工作、休息或联系，工作进度始终为 20 条；不能据此声称恢复成功。观察到的差异属于用户文字暗示响应，不是实际内稳态变化的效果。", "",
        "cue-evidence.json 汇总完整数据；每个 condition 的 evidence.json 保存真实完整提示、响应文本、编译片段、权威快照、哈希和 usage；pair.json 保存配对完整性。5 对仅为探索性诊断，不用于稳定效应量或显著性结论。", "",
    ])


def run(output: Path, checkpoint_path: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "summary.json").exists() or (output / "pairs").exists():
        raise RuntimeError("Choose a fresh output directory; existing live evidence will not be overwritten")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    unhashed = {key: value for key, value in checkpoint.items() if key != "checkpoint_hash"}
    if checkpoint["checkpoint_hash"] != json_hash(unhashed):
        raise ValueError("checkpoint integrity failed")
    if checkpoint["completed"] != list(range(1, 21)) or checkpoint["dataset_hash"] != json_hash(records()):
        raise ValueError("expected frozen checkpoint with exactly 20 verified records")
    settings = settings_from_gui()
    if settings.llm_model != "deepseek-v4-flash":
        raise RuntimeError("Saved model must be deepseek-v4-flash for this diagnostic")
    metadata = {"case_id": "D02", "model": settings.llm_model, "extra_payload": EXTRA,
                "max_tokens": 4096, "temperature": .3, "maximum_concurrency": 2,
                "started_at": datetime.now(UTC).isoformat(), "checkpoint_path": str(checkpoint_path.resolve()),
                "checkpoint_hash": checkpoint["checkpoint_hash"], "dataset_hash": checkpoint["dataset_hash"],
                "value": "V1", "urgency": "U0", "milestone": 2,
                "condition_variable": "user cue prefix only", "authoritative_salience": "nominal",
                "cue": CUE, "work_or_rest_executed": False}
    write_json(output / "metadata.json", metadata)
    pairs = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(run_pair, repetition, output, settings, checkpoint): repetition
                   for repetition in range(1, 6)}
        for future in as_completed(futures):
            pairs.append(future.result())
            partial = summarize(sorted(pairs, key=lambda p: p["repetition"]), metadata)
            write_json(output / "summary.json", partial)
            write_json(output / "cue-evidence.json", {"complete": len(pairs) == 5, "summary": partial,
                                                        "pairs": sorted(pairs, key=lambda p: p["repetition"])})
    pairs.sort(key=lambda p: p["repetition"])
    summary = summarize(pairs, metadata)
    summary["completed_at"] = datetime.now(UTC).isoformat()
    write_json(output / "summary.json", summary)
    write_json(output / "cue-evidence.json", {"complete": True, "summary": summary, "pairs": pairs})
    (output / "summary.md").write_text(render_summary(summary), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "eval/reports/homeostatic_decision/cue")
    parser.add_argument("--checkpoint", type=Path, default=ROOT / "eval/reports/homeostatic_decision/live-smoke-low-20260905/checkpoint.json")
    args = parser.parse_args()
    summary = run(args.output.resolve(), args.checkpoint.resolve())
    print(json.dumps({key: summary[key] for key in ("counts", "format_status", "integrity_passed_pairs", "actual_main_model_calls", "usage")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
