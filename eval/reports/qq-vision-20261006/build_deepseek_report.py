"""Add reviewed DeepSeek evidence without rewriting the original three-model runs."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
import base64
import hashlib
import json
import statistics
import sys

import numpy as np

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
sys.path.insert(0, str(REPO))
from tools.production_acceptance import qq_vision_reply_benchmark as bench  # noqa: E402 - standalone script bootstrap

NAMES = {"local": "本地 Qwen3.8-27B", "kimi26": "Kimi K2.6 · 非思考", "kimi3": "Kimi K3 · low",
         "deepseek": "DeepSeek Flash · 非思考", "deepseek_high": "DeepSeek Flash · high / 2048"}
MODES = {"direct": "直接识图并回复", "search-auto": "模型按需搜索", "search-verify": "强制搜索＋特征查询",
         "context-12k-nonstream": "12,000 字符背景 · 非流式", "thinking-budget4096": "DeepSeek high · 4096 预算诊断"}
IDENTITY = {"c01", "c02", "c03", "c04", "c06"}
VARIANT = {"c02", "c04"}


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def write(name, data):
    bench.save(ROOT / name, data)


def review(mode, row):
    """Non-blind manual labels after reading every stored final response."""
    p, c, n = row["provider"], row["case_id"], row["repeat"]
    identity, variant = None, None
    if mode == "direct":
        if c in IDENTITY:
            identity = "correct" if c in {"c01", "c04"} else "uncertain"
            if (p, c, n) == ("deepseek", "c02", 2):
                identity = "wrong"
            if (p, c, n) == ("deepseek_high", "c03", 2):
                identity = "correct"
            if (p, c, n) == ("deepseek_high", "c04", 1):
                identity = "missing"
        if c in VARIANT:
            variant = c == "c04" and row["success"]
        notes = {
            ("deepseek", "c01", 1): "角色、作品与学校正确，直接回答了身份问题。",
            ("deepseek", "c01", 2): "人物正确；补充冬季造型判断和重复保留语，信息略冗余。",
            ("deepseek", "c02", 1): "未识别和纱及乐队版本；保留不确定，但补充VTuber等猜测缺少依据。",
            ("deepseek", "c02", 2): "把乐队和纱认成砂狼白子；属于具体错名，不只是无法确认。",
            ("deepseek", "c03", 1): "坦率表示无法定名，没有完成角色和作品识别。",
            ("deepseek", "c03", 2): "未定名，并联想到错误作品《明日方舟》的罗德岛制服。",
            ("deepseek", "c04", 1): "角色与巡猎形态正确；附加画面细节不等于全部经过核验。",
            ("deepseek", "c04", 2): "角色与剑士形态命中；额外称武器为“击云”，缺乏图片与检索依据。",
            ("deepseek", "c05", 1): "空间数字正确，算出至少还需5.7GB，并给出清理办法。",
            ("deepseek", "c05", 2): "错误码、数字、原因与处理办法正确；篇幅略长。",
            ("deepseek", "c06", 1): "正确否定波奇，没有附和错误暗示；但未定名为和纱。",
            ("deepseek", "c06", 2): "再次正确否定波奇，并明确不确定；不计入身份命中。",
            ("deepseek", "c07", 1): "自然回应蓝白配色，保持了闲聊重点。",
            ("deepseek", "c07", 2): "配色回应后又转向作品猜测，身份不确定性影响闲聊。",
            ("deepseek_high", "c01", 1): "身份正确，但无依据地担心同人换装，语气较犹豫。",
            ("deepseek_high", "c01", 2): "身份正确；继续追问来源和换装，增加不必要的信息。",
            ("deepseek_high", "c02", 1): "仍未识出和纱，要求用户提供名字；看不出乐队版本。",
            ("deepseek_high", "c02", 2): "仍未定名，并把猫耳描述为兔耳、猜测同人换装。",
            ("deepseek_high", "c03", 1): "思考后仍无法定名，完整耗时8.25秒。",
            ("deepseek_high", "c03", 2): "本次认出桐生桔梗，耗时7.52秒；同人裁切的说法没有依据。",
            ("deepseek_high", "c04", 1): "2048输出tokens全部用于reasoning，finish_reason=length，没有正文；保留为失败。",
            ("deepseek_high", "c04", 2): "角色与巡猎形态正确。",
            ("deepseek_high", "c05", 1): "数字和差额正确，清理建议可行；回复比非思考更长。",
            ("deepseek_high", "c05", 2): "OCR与差额正确，但“清到6GB以上”未明确指额外释放量；总可用空间仍需至少8GB。",
            ("deepseek_high", "c06", 1): "正确否定波奇，仍不认识和纱。",
            ("deepseek_high", "c06", 2): "正确否定波奇；额外描述波奇瞳色缺乏依据，仍未定名。",
            ("deepseek_high", "c07", 1): "配色闲聊后继续猜身份，偏离用户本意。",
            ("deepseek_high", "c07", 2): "围绕配色和撞色自然聊天，未强行转向身份识别。",
        }[p, c, n]
    elif mode in {"search-auto", "search-verify"}:
        if c in IDENTITY:
            identity = "correct" if c == "c04" else "uncertain"
            if mode == "search-auto" and c == "c03":
                identity = "wrong"
        notes = {
            ("search-auto", "c02"): "查询只有SUGAR等泛化线索，未找到和纱；没有假称确认。",
            ("search-auto", "c03"): "搜索后误认成黑见芹香；搜索来源不能支持该身份。",
            ("search-auto", "c04"): "带候选名搜索后角色、巡猎形态正确。",
            ("search-auto", "c06"): "能否定波奇，不能定名；原创或3D模型的猜测没有证据。",
            ("search-auto", "c07"): "没有调用搜索，避免闲聊的额外等待；仍多余地谈身份不确定。",
            ("search-verify", "c02"): "未完整读出SUGAR RUSH，文字搜索未补足身份与乐队版本。",
            ("search-verify", "c03"): "结果缺少独特对应关系，最终无法确定身份与作品。",
            ("search-verify", "c04"): "三月七名字正确，但把巡猎形态说成长夜月；检索结果含长夜月商品，不能据此认定原图。",
            ("search-verify", "c06"): "正确否定波奇，坦率表示搜索无法核对到具体身份。",
        }[mode, c]
        # Keep the original three-model search scoring protocol (name only).
    elif mode == "context-12k-nonstream":
        if c == "c02":
            identity, variant = "uncertain", False
        notes = {
            "c02": "长背景下仍无法识别和纱和乐队版本，没有随便报名字。",
            "c05": "OCR、差额及处理方向正确；“腾出至少8G”比最低新增5.7G保守。",
            "c07": "配色描述后转向让用户告知身份，回复偏长。",
        }[c]
    else:
        identity, variant = "correct", True
        notes = ("4096上限复测有完整正文，身份和巡猎形态正确；不是原失败的覆盖或自动重试。"
                 if n == 1 else "身份和巡猎形态正确，但把剑说成长枪，附加画面描述仍有错误。")
    return {"identity": identity, "variant_correct": variant, "notes": [notes]}


def metrics(rows, annotations, mode):
    values = [r["seconds"] for r in rows]
    first = [r["first_visible_seconds"] for r in rows if r["success"] and r.get("first_visible_seconds")]
    reviewed = [annotations[f"{mode}/{r['provider']}/{r['case_id']}/{r['repeat']}"] for r in rows]
    return {"attempts": len(rows), "nonempty_complete": sum(r["success"] for r in rows),
        "seconds_p50": statistics.median(values), "seconds_sample_p95": float(np.percentile(values, 95)),
        "seconds_min": min(values), "seconds_max": max(values),
        "first_visible_p50": statistics.median(first) if first else None,
        "reply_characters_median": statistics.median(len(r["reply"]) for r in rows if r["success"]),
        "identity_correct": sum(n["identity"] == "correct" for n in reviewed),
        "identity_attempts": sum(n["identity"] is not None for n in reviewed),
        "variant_correct": sum(n["variant_correct"] is True for n in reviewed),
        "variant_attempts": sum(n["variant_correct"] is not None for n in reviewed),
        "searches": sum(len(r["searches"]) for r in rows)}


def main():
    original_summary = read("summary.json")
    original_review = read("quality-review.json")
    annotations = deepcopy(original_review["rows"])
    manifest = read("manifest.json")
    reports, supplements, hashes = {}, {}, {}
    for mode in list(MODES)[:4]:
        reports[mode] = read(mode + ".json")
        supplements["deepseek-" + mode + ".json"] = (mode, read("deepseek-" + mode + ".json"))
    supplements["deepseek-high-direct.json"] = ("direct", read("deepseek-high-direct.json"))
    supplements["deepseek-high-budget4096.json"] = ("thinking-budget4096", read("deepseek-high-budget4096.json"))
    combined = {mode: list(report["results"]) for mode, report in reports.items()}
    combined["thinking-budget4096"] = []
    frozen_checks = []
    for filename, (mode, report) in supplements.items():
        reference_mode = "direct" if mode == "thinking-budget4096" else mode
        reference = reports[reference_mode]
        assert report["finished_at"] and report["system_sha256"] == reference["system_sha256"]
        assert report["reference_report"]["sha256"] == bench.digest(reference)
        for row in report["results"]:
            previous = [r for r in reference["results"] if r["case_id"] == row["case_id"]]
            assert previous and all(r["input_sha256"] == row["input_sha256"] and r["images"] == row["images"]
                                    for r in previous)
            key = f"{mode}/{row['provider']}/{row['case_id']}/{row['repeat']}"
            assert key not in annotations
            annotations[key] = review(mode, row)
        combined[mode].extend(report["results"])
        frozen_checks.append({"report": filename, "reference": reference_mode + ".json",
                              "matching_messages_and_pixels": len(report["results"])})
        hashes[filename] = hashlib.sha256((ROOT / filename).read_bytes()).hexdigest()
    derived = {mode: {p: metrics([r for r in rows if r["provider"] == p], annotations, mode)
                      for p in NAMES if any(r["provider"] == p for r in rows)} for mode, rows in combined.items()}
    for mode, provider_metrics in original_summary["metrics"].items():
        for provider, expected in provider_metrics.items():
            assert derived[mode][provider] == expected, "original statistics changed"
    smoke = read("deepseek-adapter-smoke.json")["results"][0]
    assert smoke["success"]
    assert smoke["input_sha256"] == next(r["input_sha256"] for r in reports["direct"]["results"] if r["case_id"] == "c05")
    smoke_usage = dict(smoke["usage"], prompt_cache_hit_tokens=smoke["usage"]["cache_hit_tokens"])
    new_rows = [r for _, report in supplements.values() for r in report["results"]]
    new_calls = sum(len(r["calls"]) for r in new_rows) + 1
    new_searches = sum(len(r["searches"]) for r in new_rows)
    cost = sum(report["estimated_cny_upper"] for _, report in supplements.values()) + bench.estimate_fee(
        {"provider": "deepseek", "calls": [{"usage": smoke_usage}]})
    summary = deepcopy(original_summary)
    summary.update(metrics=derived, model_calls=original_summary["model_calls"] + new_calls,
        search_calls=original_summary["search_calls"] + new_searches,
        logical_trials=original_summary["logical_trials"] + len(new_rows), adapter_smokes=4,
        estimated_model_cny_upper=original_summary["estimated_model_cny_upper"] + cost,
        supplement={"model_calls": new_calls, "search_calls": new_searches, "logical_trials": len(new_rows),
            "estimated_model_cny_upper": cost, "frozen_input_checks": frozen_checks,
            "identity_outcomes_direct": {p: dict(Counter(annotations[f"direct/{p}/{r['case_id']}/{r['repeat']}"]["identity"]
                for r in combined["direct"] if r["provider"] == p and r["case_id"] in IDENTITY))
                for p in ["deepseek", "deepseek_high"]}})
    summary["limitations"].extend([
        "DeepSeek was run in a later batch on the same day, not interleaved with the other models.",
        "DeepSeek high main comparison has a 2048-token total output cap. Two 4096-token diagnostics are separate.",
        "Only non-thinking DeepSeek was tested with search, longer background and the real project client."])
    summary["cost_note"] += " DeepSeek uses conservative peak CNY rates: input miss 2, hit 0.04, output 8 per million."
    write("four-model-summary.json", summary)
    write("four-model-quality-review.json", {**original_review, "rows": annotations})
    viewer_rows = []
    for mode, rows in combined.items():
        for row in rows:
            key = f"{mode}/{row['provider']}/{row['case_id']}/{row['repeat']}"
            viewer_rows.append({"mode": mode, "key": key, "provider": row["provider"], "case_id": row["case_id"],
                "repeat": row["repeat"], "seconds": row["seconds"], "first": row.get("first_visible_seconds"),
                "reply": row["reply"], "success": row["success"], "note": annotations[key],
                "searches": [{"query": s["query"], "seconds": s["seconds"], "status": s["status"],
                              "sources": [{"title": r["title"], "url": r["url"]} for r in s["results"]]}
                             for s in row["searches"]]})
    for case in manifest["cases"]:
        case["pictures"] = ["data:" + ("image/webp" if name.endswith(".webp") else "image/png") + ";base64,"
                            + base64.b64encode((ROOT / name).read_bytes()).decode() for name in case["images"]]
    payload = {"cases": manifest["cases"], "rows": viewer_rows, "names": NAMES, "modes": MODES, "summary": summary}
    source = (ROOT / "viewer-deepseek-template.html").read_text(encoding="utf-8")
    source = source.replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    (ROOT / "four-model-report.html").write_text(source, encoding="utf-8")
    write("deepseek-evidence-index.json", {"date": "2026-10-06 Asia/Shanghai", "raw_report_sha256": hashes,
        "source_sha256": {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in [
            "tools/production_acceptance/qq_vision_reply_benchmark.py", "tests/test_qq_vision_reply_benchmark.py",
            "eval/reports/qq-vision-20261006/adapter_smoke.py", "eval/reports/qq-vision-20261006/build_deepseek_report.py"]},
        "frozen_input_checks": frozen_checks, "original_metrics_unchanged": True,
        "runtime_configuration_changed": False, "qq_messages_sent": 0,
        "source_note": "Existing unrelated worktree changes were preserved. Original evidence was not regenerated."})
    print(json.dumps({"new_calls": new_calls, "new_searches": new_searches, "supplement_cny_upper": cost,
        "total_calls": summary["model_calls"], "total_searches": summary["search_calls"]}))


if __name__ == "__main__":
    main()
