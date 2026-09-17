"""Anonymous review packets, validated human ratings, and reproducible reveal reports."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DIMENSIONS = [
    {"id": "naturalness", "label": "中文自然度", "description": "措辞、语序和节奏是否像自然中文聊天"},
    {"id": "relevance", "label": "贴题接话", "description": "是否直接回应这句话，并接住用户真正关心的事"},
    {"id": "persona", "label": "角色一致性", "description": "是否符合小雾的人格、表达习惯和知识边界"},
    {"id": "emotion", "label": "情绪分寸", "description": "是否体贴克制，避免说教、夸张或突然过度亲密"},
    {"id": "preference", "label": "总体喜欢程度", "description": "作为聊天对象，你有多喜欢这一条回复"},
]
ISSUES = ["客服腔", "说教", "回避立场", "虚构经历", "过度亲密", "表达生硬", "重复啰嗦", "其他"]
RATINGS_SCHEMA = "chinese-reply-ratings-v1"
PACKET_SCHEMA = "chinese-reply-review-v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _publish(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n", exclusive=exclusive)


def _publish(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    """A failed write never leaves a truncated authoritative file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if exclusive:
            # A hard link atomically publishes the completed inode and refuses replacement.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest(run_dir: Path) -> dict:
    manifest = _read(run_dir / "manifest.json")
    # Also understand the common {data: ..., manifest_sha256: ...} envelope.
    if "data" in manifest and "models" not in manifest:
        return {**manifest["data"], "manifest_sha256": manifest.get("manifest_sha256")}
    return manifest


def _results(run_dir: Path) -> list[dict]:
    path = run_dir / "private" / "results.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pairs = [(row["model_id"], row["scenario_id"]) for row in rows]
    if len(pairs) != len(set(pairs)):
        raise ValueError("Duplicate model/scenario results; review requires one immutable result per slot")
    return rows


def _eligible(row: dict) -> bool:
    return (row.get("status") == "complete" and isinstance(row.get("response_text"), str)
            and bool(row["response_text"].strip())
            and row.get("finish_reason") not in {"length", "max_tokens", "max_output_tokens"})


def _validate_binding(packet: dict, mapping: dict, manifest: dict, eligible: list[dict]) -> None:
    """Bind anonymous identities to the frozen source, including identical replies."""
    commitment = packet.get("instructions", {}).get("blind_map_commitment")
    if commitment != _hash({key: value for key, value in mapping.items() if key != "packet_sha256"}):
        raise ValueError("Private blind mapping commitment does not match the frozen packet")
    source = {(row["model_id"], row["scenario_id"]): row for row in eligible}
    scenarios = {item["id"]: item for item in manifest["scenarios"]}
    sample_ids = [sample["sample_id"] for sample in packet["samples"]]
    if len(set(sample_ids)) != len(sample_ids) or set(sample_ids) != set(mapping["samples"]):
        raise ValueError("Blind mapping does not cover every anonymous sample exactly once")
    seen = set()
    for sample in packet["samples"]:
        target = mapping["samples"][sample["sample_id"]]
        pair = (target["model_id"], target["scenario_id"])
        if pair in seen or pair not in source:
            raise ValueError("Blind mapping duplicates or invents a frozen result")
        seen.add(pair)
        row, scenario = source[pair], scenarios[pair[1]]
        if (sample["scenario_id"] != pair[1] or sample["response_text"] != row["response_text"]
                or sample["response_sha256"] != _text_hash(row["response_text"])
                or sample["user_text"] != scenario["user_text"] or sample["scenario_label"] != scenario["label"]):
            raise ValueError("Anonymous sample does not match its mapped frozen result")
    if seen != set(source) or packet["persona"] != manifest["persona"] or packet["run_id"] != manifest["run_id"]:
        raise ValueError("Anonymous packet does not match all eligible frozen results")


def export_review(run_dir: Path) -> dict:
    """Create once; re-export preserves every anonymous ID and shuffle position."""
    run_dir = Path(run_dir)
    manifest, results = _manifest(run_dir), _results(run_dir)
    known_models = {model["id"] for model in manifest["models"]}
    known_scenarios = {scenario["id"] for scenario in manifest["scenarios"]}
    if any(row["model_id"] not in known_models or row["scenario_id"] not in known_scenarios for row in results):
        raise ValueError("Result refers to a model or scenario outside the frozen manifest")
    planned = manifest.get("limits", {}).get("planned", len(known_models) * len(known_scenarios))
    if len(results) != planned:
        raise ValueError("Sampling is incomplete; finish all planned slots before freezing the anonymous packet")
    eligible = [row for row in results if _eligible(row)]
    corpus_hash = _hash({"manifest": manifest, "results": results})
    packet_path = run_dir / "review" / "samples.json"
    mapping_path = run_dir / "private" / "blind-map.json"
    envelope_path = run_dir / "private" / "export-envelope.json"
    if envelope_path.exists() or packet_path.exists() or mapping_path.exists():
        if envelope_path.exists():
            envelope = _read(envelope_path)
            packet, mapping = envelope["packet"], envelope["mapping"]
        elif packet_path.exists() and mapping_path.exists():
            packet, mapping = _read(packet_path), _read(mapping_path)
        else:
            raise ValueError("Incomplete legacy review export without a recovery envelope")
        if mapping.get("corpus_sha256") != corpus_hash:
            raise ValueError("Results changed after anonymous packet freeze; use a separate run directory")
        expected = packet.get("packet_sha256")
        if _hash({key: value for key, value in packet.items() if key != "packet_sha256"}) != expected:
            raise ValueError("Anonymous packet hash does not match its content")
        if expected != mapping.get("packet_sha256"):
            raise ValueError("Anonymous packet and private mapping do not match")
        _validate_binding(packet, mapping, manifest, eligible)
        for path, value in [(mapping_path, mapping), (packet_path, packet)]:
            if path.exists():
                if _read(path) != value:
                    raise ValueError("Published packet or private mapping differs from its immutable export commitment")
            else:
                _write(path, value, exclusive=True)
        if not envelope_path.exists():
            _write(envelope_path, {"packet": packet, "mapping": mapping}, exclusive=True)
        _publish(run_dir / "review" / "index.html", Path(__file__).with_name("review.html").read_bytes())
        return packet
    packet = {
        "schema_version": PACKET_SCHEMA, "run_id": manifest["run_id"],
        "persona": manifest["persona"], "dimensions": DIMENSIONS, "samples": [],
        "instructions": {
            "issues": ISSUES,
            "scale": {"1": "明显不合适", "2": "有较大问题", "3": "基本可接受", "4": "自然且贴切", "5": "很喜欢，表现突出"},
            "evidence_required": "任一维度 ≤ 2 分或勾选问题时，请引用原文并说明原因。",
            "scope": "本轮只比较这两个场景的单次回复；不推导模型总体中文能力。",
        },
    }
    mapping = {"run_id": manifest["run_id"], "corpus_sha256": corpus_hash,
               "binding_nonce": secrets.token_hex(32), "samples": {}}
    rng = secrets.SystemRandom()
    for scenario in manifest["scenarios"]:
        scene_rows = [row for row in eligible if row["scenario_id"] == scenario["id"]]
        rng.shuffle(scene_rows)
        for row in scene_rows:
            sample_id = secrets.token_hex(16)
            packet["samples"].append({
                "sample_id": sample_id, "scenario_id": scenario["id"],
                "scenario_label": scenario["label"], "user_text": scenario["user_text"],
                "response_text": row["response_text"], "response_sha256": _text_hash(row["response_text"]),
            })
            mapping["samples"][sample_id] = {"model_id": row["model_id"], "scenario_id": row["scenario_id"]}
    # Salted commitment reveals no model IDs; it also catches swaps of identical replies.
    packet["instructions"]["blind_map_commitment"] = _hash(mapping)
    packet["packet_sha256"] = _hash(packet)
    mapping["packet_sha256"] = packet["packet_sha256"]
    _validate_binding(packet, mapping, manifest, eligible)
    try:
        # One authoritative envelope preserves the original random IDs through crashes.
        _write(envelope_path, {"packet": packet, "mapping": mapping}, exclusive=True)
    except FileExistsError:
        pass
    return export_review(run_dir)


def import_ratings(run_dir: Path, ratings: dict) -> dict:
    """Validate and preserve the first submission; changes are separate revisions."""
    run_dir = Path(run_dir)
    packet = export_review(run_dir)
    allowed = {"schema_version", "run_id", "packet_sha256", "rater_id", "calibrated", "ratings", "submitted_at"}
    if not isinstance(ratings, dict) or set(ratings) != allowed:
        raise ValueError("评分文件字段不完整或包含未知字段")
    if ratings["schema_version"] != RATINGS_SCHEMA:
        raise ValueError("评分格式版本不匹配")
    if ratings["run_id"] != packet["run_id"] or ratings["packet_sha256"] != packet["packet_sha256"]:
        raise ValueError("评分与本轮匿名样本不匹配")
    if ratings["calibrated"] is not True or ratings["rater_id"] != "human-01":
        raise ValueError("需要单人人工审核并完成量表校准")
    try:
        submitted_at = datetime.fromisoformat(ratings["submitted_at"].replace("Z", "+00:00"))
        if submitted_at.tzinfo is None:
            raise ValueError("Timezone required")
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("提交时间必须是包含时区的 ISO 日期") from exc
    if not isinstance(ratings["ratings"], list) or not packet["samples"]:
        raise ValueError("没有可以封存的有效人工评分")
    expected = {sample["sample_id"]: sample for sample in packet["samples"]}
    seen = set()
    dimension_ids = {dimension["id"] for dimension in DIMENSIONS}
    for rating in ratings["ratings"]:
        if not isinstance(rating, dict) or set(rating) != {"sample_id", "response_sha256", "scores", "issues", "evidence"}:
            raise ValueError("样本评分字段不完整或包含未知字段")
        sample_id = rating["sample_id"]
        if not isinstance(sample_id, str) or sample_id not in expected or sample_id in seen:
            raise ValueError("样本编号无效或重复")
        seen.add(sample_id)
        if rating["response_sha256"] != expected[sample_id]["response_sha256"]:
            raise ValueError("回复正文校验失败")
        scores = rating["scores"]
        if not isinstance(scores, dict) or set(scores) != dimension_ids:
            raise ValueError("请为每个样本填写全部五个维度")
        if any(type(value) is not int or not 1 <= value <= 5 for value in scores.values()):
            raise ValueError("每个维度必须是 1–5 的整数分数")
        issues = rating["issues"]
        if (not isinstance(issues, list) or any(not isinstance(issue, str) or issue not in ISSUES for issue in issues)
                or len(issues) != len(set(issues))):
            raise ValueError("问题标记无效或重复")
        evidence = rating["evidence"]
        if not isinstance(evidence, str) or len(evidence) > 10000:
            raise ValueError("原文依据必须是最多 10000 字的文本")
        if (min(scores.values()) <= 2 or issues) and not evidence.strip():
            raise ValueError("低分或问题标记需要原文依据")
    if seen != set(expected):
        raise ValueError("只有全部有效样本完成评分后才可封存揭盲")
    ratings_hash = _hash(ratings)
    seal = {
        "schema_version": "chinese-reply-seal-v1", "sealed_at": datetime.now(timezone.utc).isoformat(),
        "ratings_sha256": ratings_hash, "packet_sha256": packet["packet_sha256"],
        "manifest_sha256": _manifest(run_dir).get("manifest_sha256"),
        "blind_map_sha256": _hash(_read(run_dir / "private" / "blind-map.json")),
        "ratings": ratings,
    }
    seal_path = run_dir / "private" / "ratings" / f"{ratings_hash}.json"
    try:
        _write(seal_path, seal, exclusive=True)
    except FileExistsError:
        if _read(seal_path)["ratings"] != ratings:
            raise ValueError("Existing rating seal content does not match its hash") from None
    first_path = run_dir / "private" / "ratings" / "first-seal.json"
    try:
        _write(first_path, {"ratings_sha256": ratings_hash}, exclusive=True)
    except FileExistsError:
        pass
    first_hash = _read(first_path)["ratings_sha256"]
    return {
        "status": "sealed" if first_hash == ratings_hash else "revision_preserved",
        "ratings_sha256": ratings_hash, "first_ratings_sha256": first_hash,
        "path": str(seal_path), "sample_count": len(seen),
        "message": "首轮评分已封存。" if first_hash == ratings_hash else "修订已另存；默认报告保留首轮评分。",
    }


def analyze(run_dir: Path) -> dict:
    """Analyze only sealed human scores; never synthesize missing ratings."""
    run_dir = Path(run_dir)
    packet = export_review(run_dir)
    manifest, results = _manifest(run_dir), _results(run_dir)
    first_path = run_dir / "private" / "ratings" / "first-seal.json"
    report: dict = {
        "schema_version": "chinese-reply-report-v1", "run_id": manifest["run_id"],
        "status": "waiting_for_human" if packet["samples"] else "no_valid_samples",
        "eligible_sample_count": len(packet["samples"]),
        "limitations": "本轮为单人对两个固定场景、每模型每场景一次回复的偏好记录，不能推导模型总体中文能力或统计显著性。",
        "models": [], "technical_missing": [],
    }
    indexed = {(row["model_id"], row["scenario_id"]): row for row in results}
    for model in manifest["models"]:
        for scenario in manifest["scenarios"]:
            row = indexed.get((model["id"], scenario["id"]))
            if row is None or not _eligible(row):
                report["technical_missing"].append({
                    "model_id": model["id"], "scenario_id": scenario["id"],
                    "status": row.get("status", "unknown") if row else "not_attempted",
                    "finish_reason": row.get("finish_reason") if row else None,
                    "response_text": row.get("response_text", "") if row else "",
                })
    lines = ["# 中文角色回复人工评审", "", report["limitations"], ""]
    if first_path.exists():
        first_hash = _read(first_path)["ratings_sha256"]
        seal = _read(first_path.parent / f"{first_hash}.json")
        mapping = _read(run_dir / "private" / "blind-map.json")
        if (_hash(seal["ratings"]) != first_hash or seal["packet_sha256"] != packet["packet_sha256"]
                or seal["blind_map_sha256"] != _hash(mapping)):
            raise ValueError("评分封存或匿名映射校验失败")
        scores_by_pair = {}
        for rating in seal["ratings"]["ratings"]:
            item = mapping["samples"][rating["sample_id"]]
            scores_by_pair[(item["model_id"], item["scenario_id"])] = rating
        report.update(status="sealed", ratings_sha256=first_hash, sealed_at=seal["sealed_at"])
        report["revision_count"] = max(0, len(list(first_path.parent.glob("*.json"))) - 2)
        lines += [f"封存时间：{seal['sealed_at']}", f"首轮评分 SHA-256：{first_hash}", ""]
        for model in manifest["models"]:
            model_report = {"model_id": model["id"], "provider": model.get("provider"),
                            "reasoning_policy": model.get("reasoning_policy"),
                            "limitations": model.get("limitations", []), "scenarios": []}
            lines += [f"## {model['id']}", ""]
            lines += ["思考设置：" + json.dumps(model_report["reasoning_policy"], ensure_ascii=False), "",
                      "适配限制：" + json.dumps(model_report["limitations"], ensure_ascii=False), ""]
            for scenario in manifest["scenarios"]:
                pair = (model["id"], scenario["id"])
                if pair not in scores_by_pair:
                    continue
                rating, result = scores_by_pair[pair], indexed[pair]
                item = {"scenario_id": scenario["id"], "scenario_label": scenario["label"],
                        "user_text": scenario["user_text"], "rating": rating,
                        "result": result}
                model_report["scenarios"].append(item)
                score_text = "；".join(f"{dimension['label']} {rating['scores'][dimension['id']]}/5" for dimension in DIMENSIONS)
                lines += [f"### {scenario['label']}", "", score_text, "", f"用户：{scenario['user_text']}", "", "回复原文：", ""]
                lines += ["> " + line for line in result["response_text"].splitlines()]
                lines += ["", "问题标记：" + ("、".join(rating["issues"]) or "无"), "", "人工依据：" + (rating["evidence"] or "未填写"), ""]
                lines += [f"请求型号：{model['id']}；返回型号：{result.get('response_model') or '未提供'}。代理返回型号只作身份线索。",
                          "用量：" + json.dumps(result.get("usage"), ensure_ascii=False)
                          + f"；耗时：{result.get('elapsed_ms', '未提供')} ms。", ""]
            report["models"].append(model_report)
    else:
        lines += ["状态：等待人工审核。尚未生成任何质量分数。" if packet["samples"] else "状态：没有有效样本，无法进行人工评分。",
                  f"可评分样本：{len(packet['samples'])} 条。", ""]
    lines += ["## 技术缺失（不记作质量低分）", ""]
    for item in report["technical_missing"]:
        lines += [f"- {item['model_id']} / {item['scenario_id']}：{item['status']}；结束原因 {item['finish_reason'] or '无'}"]
    if not report["technical_missing"]:
        lines += ["无。"]
    _write(run_dir / "private" / "report.json", report)
    _publish(run_dir / "private" / "report.md", ("\n".join(lines) + "\n").encode("utf-8"))
    return report
