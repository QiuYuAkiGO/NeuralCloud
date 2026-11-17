"""Build transparent, non-blind review notes and a local comparison viewer."""
from pathlib import Path
import base64
import hashlib
import json
import statistics

import numpy as np

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
NAMES = {"local": "本地 Qwen3.8-27B", "kimi26": "API Kimi K2.6 · 非思考", "kimi3": "API Kimi K3 · low"}
MODES = {"direct": "直接识图并回复", "search-auto": "模型按需搜索", "search-verify": "强制搜索＋特征查询",
         "context-12k-nonstream": "12,000 字符背景 · 非流式"}
IDENTITY = {"c01", "c02", "c03", "c04", "c06"}
VARIANT = {"c02", "c04"}


def write(name, data):
    (ROOT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def review(mode, row):
    """Explicitly reviewed labels, not an LLM judge or lexical scoring model."""
    p, c, n = row["provider"], row["case_id"], row["repeat"]
    identity, variant, notes = None, None, []
    if mode == "direct":
        if c in IDENTITY:
            if p == "local":
                identity = "correct" if (c, n) == ("c01", 2) else "wrong"
                if (c, n) == ("c03", 2):
                    identity = "missing"
            elif p == "kimi26":
                identity = "correct"
            else:
                identity = "uncertain" if (c, n) == ("c06", 1) else "correct"
        if c in VARIANT:
            variant = p == "kimi3" or (p == "kimi26" and c == "c04")
        special = {
            ("local", "c01", 1): "把白子称为“来自蔚蓝档案的猫又小粥”，并编造白色蝴蝶结。",
            ("local", "c01", 2): "姓名正确；把狼耳说成猫耳，并无依据地称为换装。",
            ("local", "c02", 1): "认成《绝区零》的星见雅，作品与角色均不匹配原图。",
            ("local", "c02", 2): "称为“小灰”，并把提问者叫成“小雾”。",
            ("local", "c03", 1): "猜成久田泉奈；虽然有保留，但身份仍错。",
            ("local", "c03", 2): "仅输出 [END]，无可交付正文。",
            ("local", "c04", 1): "认成八重神子，编造“樱吹雪·太刀之形”。",
            ("local", "c04", 2): "仍猜八重神子，继而把官方立绘说成可能二创。",
            ("local", "c05", 1): "OCR主要数字、空间不足原因正确；完整耗时14.29秒，保留此离群值。",
            ("local", "c05", 2): "OCR正确，但无依据假定用户在手机上，并过度展开。",
            ("local", "c06", 1): "附和错误提示，确认是波奇。",
            ("local", "c06", 2): "再次附和错误提示，确认是波奇。",
            ("local", "c07", 1): "回应配色后额外追问角色，偏离闲聊重点。",
            ("local", "c07", 2): "把对方图片与自身小雾身份混淆。",
            ("kimi26", "c02", 1): "人物正确，但没有明确识别乐队版本，仅猜活动/联动T恤。",
            ("kimi26", "c02", 2): "人物正确，具体换装未定，猜私服/联动。",
            ("kimi26", "c03", 1): "人物正确，却把百花缭乱调停委员会成员说成忍术研究部成员。",
            ("kimi26", "c04", 1): "身份与形态正确；额外设定与台词联想偏长。",
            ("kimi3", "c02", 1): "人物和乐队方向正确；“甜点部”不要当成已核实的正式皮肤名。",
            ("kimi3", "c06", 1): "正确否定波奇，坦率未定名；这不是错误认人，但不计入姓名命中。",
            ("kimi3", "c05", 2): "OCR与原因正确，但没有直接回应“该怎么弄”。",
        }
        if (p, c, n) in special:
            notes.append(special[p, c, n])
    elif mode == "search-auto":
        if c in IDENTITY:
            identity = ({"c02": "wrong", "c03": "uncertain", "c04": "wrong", "c06": "wrong"}[c]
                        if p == "local" else "uncertain" if p == "kimi26" and c == "c03" else "correct")
        if p == "local" and c in {"c02", "c04"}:
            notes.append("用错误候选名检索后仍认错；检索到百科不等于图中人已确认。")
        if p == "kimi26" and c == "c03":
            notes.append("泛化查询未返回对应身份；直接识图能答对，搜索轮反而退化为无法确认。")
        if p == "kimi3":
            notes.append("没有实际调用搜索，不能作为联网识图成功样本。")
        if p == "kimi3" and c == "c02":
            notes.append("日文名字正确，但补出了错误中文别名“杏山華小夏”。")
        if c == "c07" and row["searches"]:
            notes.append("普通配色闲聊也触发搜索，增加无必要的等待。")
    elif mode == "search-verify":
        if p == "local":
            identity = "uncertain" if c in {"c02", "c03"} else "wrong"
        elif p == "kimi26":
            identity = "correct" if c in {"c02", "c04"} else "uncertain"
        else:
            identity = "uncertain" if c == "c03" else "correct"
        if p == "local" and c == "c02":
            notes.append("把衣服上的SUGAR RUSH误读为SUGAR MONSTER，查询随之失效。")
        if p == "kimi26" and c == "c02":
            notes.append("本例通过SUGAR RUSH查到了乐队主题，比直接识图的换装判断更具体。")
        if p == "kimi26" and c == "c03":
            identity = "missing"
            notes.append("正文出现反复斟酌和再次搜索的自述，512输出tokens耗尽，finish_reason=length；不可交付。")
        if p == "kimi26" and c == "c06":
            notes.append("正确否定波奇，但把标志读成SUGAR SUGAR，未定名。")
        if p == "kimi3" and c == "c03":
            notes.append("直接识图两次均认对，此轮搜索后反而无法确认，并无依据提出可能AI图。")
        if p == "kimi3" and c == "c04":
            notes.append("身份正确，但返回的是文本检索来源；没有进行原图反向搜索或来源图片比对。")
    else:
        if c == "c02":
            identity = "wrong" if p == "local" else "correct"
            variant = p == "kimi3"
        if p == "local" and c == "c02":
            notes.append("加长背景后仍编造人物“猫又小桃”。")
        if c == "c07" and p in {"local", "kimi26"}:
            notes.append("额外绕回角色身份，未保持配色闲聊的重点。")
    if not notes:
        notes.append("本例核心内容可用；参看原文自行判断语气，不等于全部附加信息经过独立核验。")
    return {"identity": identity, "variant_correct": variant, "notes": notes}


def main():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    reports = {m: json.loads((ROOT / (m + ".json")).read_text(encoding="utf-8")) for m in MODES}
    annotations, viewer_rows, metrics = {}, [], {}
    total_calls = 0
    searches = 0
    for mode, report in reports.items():
        metrics[mode] = {}
        for row in report["results"]:
            key = f"{mode}/{row['provider']}/{row['case_id']}/{row['repeat']}"
            note = review(mode, row)
            annotations[key] = note
            total_calls += len(row["calls"])
            searches += len(row["searches"])
            viewer_rows.append({"mode": mode, "key": key, "provider": row["provider"], "case_id": row["case_id"],
                "repeat": row["repeat"], "seconds": row["seconds"], "first": row.get("first_visible_seconds"),
                "reply": row["reply"], "success": row["success"], "note": note,
                "searches": [{"query": s["query"], "seconds": s["seconds"], "status": s["status"],
                              "sources": [{"title": r["title"], "url": r["url"]} for r in s["results"]]}
                             for s in row["searches"]]})
        for p in NAMES:
            rows = [r for r in report["results"] if r["provider"] == p]
            values = [r["seconds"] for r in rows]
            first = [r.get("first_visible_seconds") for r in rows if r["success"] and r.get("first_visible_seconds")]
            reviewed = [annotations[f"{mode}/{p}/{r['case_id']}/{r['repeat']}"] for r in rows]
            metrics[mode][p] = {
                "attempts": len(rows), "nonempty_complete": sum(r["success"] for r in rows),
                "seconds_p50": statistics.median(values), "seconds_sample_p95": float(np.percentile(values, 95)),
                "seconds_min": min(values), "seconds_max": max(values),
                "first_visible_p50": statistics.median(first) if first else None,
                "reply_characters_median": statistics.median(len(r["reply"]) for r in rows if r["success"]),
                "identity_correct": sum(n["identity"] == "correct" for n in reviewed),
                "identity_attempts": sum(n["identity"] is not None for n in reviewed),
                "variant_correct": sum(n["variant_correct"] is True for n in reviewed),
                "variant_attempts": sum(n["variant_correct"] is not None for n in reviewed),
                "searches": sum(len(r["searches"]) for r in rows),
            }
    smoke = json.loads((ROOT / "adapter-smoke.json").read_text(encoding="utf-8"))
    smoke_cost = 0
    for row in smoke["results"]:
        if row["provider"] != "local":
            usage = row["usage"]
            smoke_cost += ((usage["prompt_tokens"] - usage["cache_hit_tokens"]) * 40
                           + usage["cache_hit_tokens"] * 2 + usage["completion_tokens"] * 100) / 1_000_000
    same_inputs = all(len({r["input_sha256"] for r in reports["direct"]["results"]
                          if r["case_id"] == c["id"] and r["repeat"] == n}) == 1
                      for c in manifest["cases"] for n in [1, 2])
    summary = {"metrics": metrics, "model_calls": total_calls + len(smoke["results"]), "search_calls": searches,
        "logical_trials": len(viewer_rows), "adapter_smokes": len(smoke["results"]),
        "estimated_model_cny_upper": sum(r["estimated_cny_upper"] for r in reports.values()) + smoke_cost,
        "cost_note": "Token-based conservative estimate, K2.6 bounded using higher K3 rates; not billed charges. Tavily credits and local electricity excluded.",
        "identical_direct_messages_verified": same_inputs,
        "review_method": "Non-blind review by the coding assistant; manually assigned identity/variant labels, no paid LLM judge.",
        "limitations": ["7 scenarios, 4 characters, 5 images; BA/HSR only, not a representative QQ image distribution.",
            "Provider-native sampling differs; cache warmness and production GPU contention were not controlled.",
            "Latency excludes QQ ingress, download, gate, memory, delivery. SSE TTFT is not current QQ delivery latency.",
            "Search policies were tested in order after reading earlier failures; this is exploratory, not a randomized causal study.",
            "No reverse-image search, no live QQ sending, no production config activation."]}
    write("summary.json", summary)
    write("quality-review.json", {"method": summary["review_method"], "criteria": {
        "identity": "Correct work and character name (including accepted aliases); uncertain and missing are separate from wrong.",
        "variant": "Explicit band outfit for Kazusa or sword-training/Hunt form for March 7th.",
        "latency": "All attempts, including empty/truncated output, retained in elapsed statistics.",
        "additional_facts": "Known errors noted separately; correct name does not mean flawless reply."}, "rows": annotations})
    source_paths = ["tools/production_acceptance/qq_vision_reply_benchmark.py", "tests/test_qq_vision_reply_benchmark.py",
                    "presets/builtin/misty.json", "harness/qq/draft.py", "harness/qq/direct_images.py", "harness/llm/client.py"]
    write("environment.json", {"date": "2026-10-06 Asia/Shanghai", "gpu": "NVIDIA RTX 5090 32GB",
        "local_model": "Qwen3.8-27B-Q4_K_M + mmproj-Qwen3.8-27B-Q8_0; llama.cpp b11146",
        "local_slots": 2, "production_shared_gpu": True,
        "live_config_observed": {"qq_image_mode": "off", "vision_model": "deepseek-v4-flash-vision-exp",
            "main_model": "qwen3.8-27b", "qq_web_policy": "legacy", "qq_history_selection": "bounded"},
        "source_sha256": {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in source_paths},
        "source_note": "Existing working tree had other uncommitted changes; only new benchmark/tests/report files added in this task."})
    # Public fixture pixels embedded for a portable single-file review page.
    for c in manifest["cases"]:
        pictures = []
        for image_path in c["images"]:
            path = ROOT / image_path
            kind = "image/webp" if path.suffix == ".webp" else "image/png"
            pictures.append("data:" + kind + ";base64," + base64.b64encode(path.read_bytes()).decode())
        c["pictures"] = pictures
    payload = {"cases": manifest["cases"], "rows": viewer_rows, "names": NAMES, "modes": MODES, "summary": summary}
    source = (ROOT / "viewer-template.html").read_text(encoding="utf-8")
    source = source.replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    (ROOT / "report.html").write_text(source, encoding="utf-8")
    print(json.dumps({"model_calls": summary["model_calls"], "search_calls": searches,
                      "estimated_model_cny_upper": summary["estimated_model_cny_upper"],
                      "identical_direct_messages_verified": same_inputs}))


if __name__ == "__main__":
    main()
