"""Render existing sealed evidence only. No model calls or runtime mutations."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import sqlite3
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from eval.qq_brief.common import file_hash, lexical_metrics, read  # noqa: E402
from eval.qq_brief.runner import validated_rating, verify_seal  # noqa: E402

ROOT = REPO / "data/evaluations/qq-brief-20260915/paired-v4"
OUT = Path(__file__).resolve().parent
STEM = "qq-brief-dialogue-record-20260915"
TITLES = ["有历史依据：猫名与领养日期", "无历史依据：海边经历与想象故事",
          "称赞之后接技术问题", "立场确认与撤销条件", "购买选择中的新增条件",
          "重复点名中夹入新问题", "只送达第一段后的追问", "强烈情绪与休息选择",
          "详细解释与一句话总结", "会话隔离：茶偏好与未知城市",
          "关系回应中的具体新事实", "私聊：记忆、选择与感谢"]
LABELS = {"unsupported_fact": "无来源事实／经历前提", "missed_new_question": "遗漏新问题",
          "stance_evasion": "回避立场或直接作答", "hesitation_prefix": "迟疑前奏",
          "full_relationship_cycle": "完整关系循环", "explicit_forget": "明确失忆声明",
          "scope_leak": "跨会话引用", "repeated_full_boundary": "重复完整边界",
          "boundary_not_at_end": "认识边界未后置"}
BASE_KEYS = ["unsupported_fact", "missed_new_question", "stance_evasion", "explicit_forget",
             "scope_leak", "repeated_full_boundary"]
REVIEW = {
    ("s02_memory_absent-r1", 1): "保留的问题：问照片、同行人来“提醒我”，可能预设 Misty 确实参与过共同经历。初始证据没有这次经历。用户已要求保留，不继续修补；这里照录原文。",
    ("s06_repeat_mentions_new_question-r2", 4): "有分歧：本次轨迹第 1 轮已经明确说“还是叫我小雾，或者 Misty 就好”。第 4 轮没有新称呼或新条件，Codex 复核认为等待可归为已答重复，少一次自然确认不必然等于漏答新实质问题。Kimi 的原标签未修改。",
    ("s10_scope-r1", 2): "有分歧：询问具体缺口符合本次澄清原则，并非必须先说“不知道”。但“你是哪条消息里提过”又预设对方曾提过城市，这个措辞仍值得保留观察。原评分未改动。",
    ("s10_scope-r2", 2): "有分歧：“这个”可只是指代当前问题，Codex 复核没有把它等同于“嗯……”式迟疑。该回复没有猜测城市。保留 Kimi 的迟疑标签，不将它作为确定事实。",
    ("s01_memory_present-r1", 4): "首句指标提示：这是询问缺失日期的澄清问句，没有编造领养日期。Kimi 未给它首句答题分，但也没有标记无来源事实。",
    ("s01_memory_present-r2", 4): "首句指标提示：回复询问日期或线索，没有给出虚构日期。问具体缺口与直接回答未知之间存在评判差异；保留原始分数。",
    ("s03_praise_then_technical-r2", 4): "仅超出软篇幅目标：63 个非空白字符、2 句。它影响整组长度达标比例，不是单条丢弃规则，也没有被 Kimi 标记零容忍项。",
}


def hard_flags(row, score):
    keys = BASE_KEYS + (["hesitation_prefix", "full_relationship_cycle"] if row["input"]["ordinary"] else [])
    result = [key for key in keys if score[key]]
    if score["boundary_at_end"] is False:
        result.append("boundary_not_at_end")
    return result


def q(text):
    return "\n".join("> " + line if line else ">" for line in text.split("\n"))


def note_for(pair, turn, flags):
    if pair.startswith("s07_") and "unsupported_fact" in flags:
        if pair == "s07_partial_delivery-r2" and turn == 2:
            return ("需结合前轮理解：本轮“看最近的日志”本身可以是合理的新建议，Kimi 的无来源标记延续了前轮对“原第二步”的判断。"
                    "不能仅凭这条孤立建议认定虚构往事。初始未发第二段另列供核对；原标签和评分均保留。")
        return ("部分送达问题：当前补充建议被当作原先未发出的第二步或其延续。"
                "只看到第一段交付不能证明第二段已说过；测试装置里的原始草稿又与这些说法不同。"
                "评分封包没有原未发第二段全文，本场景另列初始草稿供核对，未回填评分或改动测试。")
    return REVIEW.get((pair, turn))


def build_data():
    sealed = verify_seal(ROOT, "live")
    mapping = read(ROOT / "private/live-arm-map.json")
    scenarios = read(ROOT / "scenarios.json")
    pairs = []
    counts = {}
    for index, scenario in enumerate(scenarios):
        for repeat in (1, 2):
            pair_id = f"{scenario['id']}-r{repeat}"
            rating = validated_rating(ROOT, pair_id, sealed)
            assert rating is not None
            pair = {"id": pair_id, "title": TITLES[index], "number": index + 1,
                    "repeat": repeat, "private": bool(scenario.get("private")),
                    "facts": scenario.get("facts", []), "history": scenario.get("history", []), "turns": []}
            if scenario.get("partial_seed"):
                seeds = []
                for arm in ("baseline", "candidate"):
                    database = ROOT / "generation/live" / f"{pair_id}-{arm}" / "state-0.sqlite3"
                    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
                        conn.execute("PRAGMA query_only=ON")
                        rows = conn.execute("SELECT record_json FROM qq_reply_decisions").fetchall()
                    records = [json.loads(row[0]) for row in rows]
                    seed = next(x for x in records if x["model"] == "synthetic-seed-not-generation")
                    seeds.append(seed["reply_segments"])
                assert seeds[0] == seeds[1]
                pair["planned_seed"] = seeds[0]
            for turn in range(1, 5):
                item = {"number": turn, "variants": {}}
                for arm in ("baseline", "candidate"):
                    path = ROOT / "generation/live" / f"{pair_id}-{arm}" / f"turn-{turn}.json"
                    row = read(path)
                    label = next(k for k, v in mapping[pair_id].items() if v == arm)
                    score = next(s for s in rating["turns"] if s["turn"] == turn)[label]
                    assert row["status"] == "ok" and row["reply"] == row["decision"]["reply"]
                    metrics = lexical_metrics(row["reply"] or "")
                    hard = hard_flags(row, score)
                    first = row["input"]["ordinary"] and row["input"]["must_answer"] and not (score["first_answer"] and row["reply"])
                    soft = bool(row["input"]["ordinary"] and row["reply"] and not metrics["within_60_and_2"])
                    item["input"] = row["input"]
                    item["variants"][arm] = {"reply": row["reply"], "score": score, "metrics": metrics,
                        "hard": hard, "first": bool(first), "soft": soft,
                        "review": note_for(pair_id, turn, hard) if arm == "candidate" else None,
                        "decision_reason": row["decision"]["reason"],
                        "action": row["decision"]["action"], "outcome": row["decision"]["outcome"],
                        "source": path.as_posix(), "source_sha256": file_hash(path)}
                pair["turns"].append(item)
            pairs.append(pair)
    for arm in ("baseline", "candidate"):
        rows = [turn["variants"][arm] for pair in pairs for turn in pair["turns"]]
        assert len(rows) == 96
        counts[arm] = {"turns": len(rows), "hard_turns": sum(bool(r["hard"]) for r in rows),
                       "first_turns": sum(r["first"] for r in rows), "soft_turns": sum(r["soft"] for r in rows),
                       "attention_turns": sum(bool(r["hard"] or r["first"] or r["soft"]) for r in rows),
                       "labels": dict(Counter(k for row in rows for k in row["hard"]))}
    assert counts["candidate"]["hard_turns"] == 9 and counts["candidate"]["attention_turns"] == 12
    return {"pairs": pairs, "counts": counts, "source_run": ROOT.as_posix(),
            "seal_sha256": file_hash(ROOT / "packets/live/seal.json"),
            "rendered_at": datetime.now(timezone.utc).isoformat(), "summary": read(ROOT / "summary-live.json")}


def badges(v):
    result = [LABELS[key] for key in v["hard"]]
    if v["first"]:
        result.append("首句指标未得分")
    if v["soft"]:
        result.append("超出软篇幅目标")
    return result


INTRO = [
    "这是最终 v4 的隔离对话测试：12 个场景，每场景独立运行两次，每次 4 轮；旧版与 v4 各 96 轮，共 192 条回复机会。它们是真实模型生成的测试回复，未发送到 QQ。",
    "下方问题及回复逐字照录，保留分段。旧版和 v4 各自沿着自己的前文继续；重复 1 与重复 2 是独立对话。等待没有正文，不等于模型调用失败。",
    "红色“零容忍项”是 Kimi 在原验收规则中的标记，不是人工定论。首句指标采用整组 90% 门槛；长度也是整组 90% 的软目标，单条超长不等于该条必须丢弃。无红标也不代表回复完美。",
    "v4 有 9 轮零容忍项标记，合计 11 个标签（同一轮可能多个）；另有首句指标未得分 5 轮、软篇幅超标 1 轮。三者重叠后，共 12 轮值得查看。原验收总体为未通过。",
    "v4 短答达标 85/86（98.84%），首句答题 81/86（94.19%），Kimi 自然度均分 4.31/5；旧版分别为 22/87（25.29%）、81/86（94.19%）、4.00/5。",
    "评审模型为 Kimi K3；补充复核由 Codex 根据原记录撰写，两者都不是人类评分。补充说明与原评分分开，不覆盖原始分数。用户要求保留的共同经历问题未修补，本记录不改动任何实现或测试结果。",
]


def markdown(data):
    lines = ["# Misty QQ 对话测试记录与标记说明", "", "2026-09-15 · 最终 v4 · 群聊 v15 / 私聊 v4", ""]
    lines += [p + "\n" for p in INTRO]
    lines += ["## 先看 v4 的不通过标记", ""]
    for pair in data["pairs"]:
        for turn in pair["turns"]:
            v = turn["variants"]["candidate"]
            if v["hard"]:
                lines += [f"- **场景 {pair['number']:02d} · 第 {pair['repeat']} 次 · 第 {turn['number']} 轮**：" + "、".join(LABELS[k] for k in v["hard"]) + "。" + v["score"]["note"]]
    lines += ["", "其中共同经历前提 1 轮、部分送达内容归属 5 轮；其余 3 轮涉及称呼确认、城市澄清和“这个”是否算迟疑，存在复核分歧。完整原文与上下文如下。", "", "## 完整对话", ""]
    for pair in data["pairs"]:
        lines += [f"### 场景 {pair['number']:02d}：{pair['title']} · 第 {pair['repeat']} 次", "", f"记录编号：`{pair['id']}`；{'私聊' if pair['private'] else '群聊'}。", ""]
        for h in pair["history"]:
            lines += ["**测试前已有的用户消息：**", "", q(h), ""]
        for fact in pair["facts"]:
            lines += ["- 初始证据范围：" + fact]
        if pair["facts"]:
            lines += [""]
        if pair.get("planned_seed"):
            lines += ["**装置补充记录（供人工核对，不属于原评分封包）：**", "",
                      "- 原冻结草稿第 1 段，已模拟送达：“" + pair["planned_seed"][0] + "”",
                      "- 原冻结草稿第 2 段，未送达：“" + pair["planned_seed"][1] + "”",
                      "- 原评分封包没有第二段全文。它来自初始 SQLite 检查点；未回填评分、未重新评审。", ""]
        for turn in pair["turns"]:
            lines += [f"#### 第 {turn['number']} 轮", "", "**用户：**", ""]
            for msg in turn["input"]["messages"]:
                lines += [q(msg), ""]
            lines += [f"场景标注：{'普通短答' if turn['input']['ordinary'] else '强情绪或明确详细请求，不计普通短答上限'}；{'固定用例标记为应答' if turn['input']['must_answer'] else '允许自然等待'}。", ""]
            for arm, name in (("baseline", "旧版 Misty"), ("candidate", "v4 Misty")):
                v = turn["variants"][arm]
                lines += [f"**{name}：**", "", q(v["reply"]) if v["reply"] is not None else "（等待，未生成发送正文；本轮模型决策成功。）", ""]
                if v["reply"] is None:
                    lines += ["决策理由：" + v["decision_reason"], ""]
                lines += [f"记录：{v['metrics']['characters']} 字符 / {v['metrics']['sentences']} 句；自然度 {v['score']['naturalness']}/5。"]
                lines += ["标记：" + ("；".join(badges(v)) or "未触发本记录列出的验收提示。")]
                lines += ["Kimi 原始说明：" + v["score"]["note"]]
                if v["review"]:
                    lines += ["补充复核（Codex）：" + v["review"]]
                lines += [""]
    lines += ["## 统计与溯源说明", "",
              "字符数按原评估器统计非空白字符，包含英文、数字及标点；句数按句末标点切分。分段回复仍合为一轮。自然度评分与事实标记独立，因此某条可得 4/5 同时被标记事实问题。", "",
              "展示的是最后完成的尝试。旧版有两次历史生成失败，经显式重试后完成；v4 无生成失败。最终 24 组 Kimi 评分均成功。中间 v1–v3 不混入本记录。", "",
              f"来源：[{ROOT.as_posix()}]({ROOT.as_posix()})。",
              "完整评分细节可在 HTML 阅读版逐轮展开；机器索引保留全部原始布尔评分及来源哈希。", "",
              f"[实施与验收报告]({(OUT / 'qq-brief-implementation-20260915.md').as_posix()})", ""]
    return "\n".join(lines)


CSS = """
:root{color-scheme:light;--ink:#202b37;--muted:#586775;--line:#d9e0e6;--accent:#175e89;--soft:#f4f7fa;--red:#9b352d;--amber:#865917}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);font:16px/1.75 system-ui,'Microsoft YaHei',sans-serif}a{color:var(--accent)}header,main,footer{max-width:1200px;margin:auto;padding:28px 32px}h1{font-size:28px;line-height:1.35;margin:8px 0 16px}h2{font-size:23px;line-height:1.5}h3{font-size:19px;margin:0}h4{font-size:16px;margin:0 0 10px}.eyebrow,.muted,small{color:var(--muted)}.eyebrow{font-size:13px;letter-spacing:.1em}header p{margin:10px 0}.intro{max-width:960px}.metrics{display:flex;gap:30px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:16px 0;margin-top:22px;flex-wrap:wrap}.metrics strong{font-size:23px;display:block}.metrics span{font-size:13px;color:var(--muted)}.tools{position:sticky;top:0;background:#fff;z-index:3;border-block:1px solid var(--line)}.tool-inner{max-width:1200px;margin:auto;padding:12px 32px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}select,input,button{font:inherit;border:1px solid #b8c5cf;border-radius:4px;padding:7px 10px;background:white;color:var(--ink)}input[type=search]{flex:1;min-width:190px}input[type=checkbox]{accent-color:var(--accent)}button{cursor:pointer}.tool-count{font-size:13px;color:var(--muted)}.guide{padding:0 0 20px;border-bottom:1px solid var(--line)}.guide li{margin:8px 0}.tag{display:inline-block;font-size:12px;border:1px solid var(--line);border-radius:3px;padding:0 6px;margin:2px 5px 2px 0}.tag.hard{color:var(--red);border-color:#d7a6a2}.tag.soft{color:var(--amber);border-color:#d7c297}.pair{padding:28px 0 14px;border-bottom:2px solid var(--line);scroll-margin-top:150px}.pair-head{display:flex;justify-content:space-between;gap:20px;align-items:baseline}.context{font-size:14px;margin:14px 0 20px;padding-left:15px;border-left:3px solid var(--line)}.context p{margin:5px 0}.turn{padding:18px 0;scroll-margin-top:155px}.user{background:var(--soft);padding:12px 16px;margin:8px 0 14px}.raw{white-space:pre-wrap;overflow-wrap:anywhere;margin:4px 0 10px;line-height:1.85}.variants{display:grid;grid-template-columns:1fr 1fr;gap:24px}.variant{min-width:0;padding:0 0 10px}.variant.candidate{border-left:2px solid var(--accent);padding-left:16px}.variant.baseline{color:#495866}.who{display:flex;justify-content:space-between;font-size:13px;align-items:center;gap:8px}.who strong{font-size:15px}.finding{font-size:14px;margin:12px 0 0;border-top:1px solid var(--line);padding-top:8px}.finding p{margin:6px 0}.review{background:#f7f5ee;padding:8px 12px}.no-flags{font-size:12px;color:var(--muted)}details{font-size:13px;margin:8px 0}summary{cursor:pointer;color:var(--accent)}details ul{margin:6px 0;padding-left:20px}.hidden,.filtered-out{display:none!important}.candidate-only .baseline{display:none}.candidate-only .variants{grid-template-columns:1fr}.baseline-only .candidate{display:none}.baseline-only .variants{grid-template-columns:1fr}footer{font-size:13px;color:var(--muted)}.anchor{font-size:12px;text-decoration:none}.statusline{font-size:13px;margin:5px 0}.navlinks{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:14px}.noresults{padding:20px;background:var(--soft)}@media(max-width:760px){header,main,footer{padding:20px 16px}.tool-inner{padding:10px 16px}.variants{grid-template-columns:1fr}.pair-head{display:block}.metrics{gap:18px}h1{font-size:24px}.tool-inner select{max-width:100%}.variant.baseline{border-bottom:1px dashed var(--line);padding-bottom:15px}}@media print{.tools,.navlinks,.anchor{display:none}header,main,footer{max-width:none;padding:10px}.pair{break-before:page}.turn{break-inside:avoid}.variant{break-inside:avoid}details>*{display:block}body{font-size:12px}.filtered-out{display:none!important}}
"""


def html_variant(pair, turn, arm, v):
    name = "旧版 Misty" if arm == "baseline" else "v4 Misty"
    parts = [f'<article class="variant {arm}" data-arm="{arm}" data-hard="{int(bool(v["hard"]))}" data-first="{int(v["first"])}" data-soft="{int(v["soft"])}">',
             f'<div class="who"><strong>{name}</strong><span>{v["metrics"]["characters"]} 字符 · {v["metrics"]["sentences"]} 句 · 自然度 {v["score"]["naturalness"]}/5</span></div>']
    if v["reply"] is not None:
        parts += [f'<p class="raw" data-reply="1">{escape(v["reply"])}</p>']
    else:
        parts += ['<p class="raw muted">（等待，未生成发送正文；模型决策成功。）</p>', f'<p class="muted statusline">决策理由：{escape(v["decision_reason"])}</p>']
    parts += ["<div>"]
    for key in v["hard"]:
        parts += [f'<span class="tag hard">零容忍项 · {LABELS[key]}</span>']
    for active, label in ((v["first"], "首句指标未得分"), (v["soft"], "超出软篇幅目标")):
        if active:
            parts += [f'<span class="tag soft">{label}</span>']
    if not badges(v):
        parts += ['<span class="no-flags">未触发所列验收提示</span>']
    parts += ["</div>", '<div class="finding"><p><strong>Kimi 原始说明：</strong>' + escape(v["score"]["note"]) + '</p>']
    if v["review"]:
        parts += ['<p class="review"><strong>补充复核（Codex）：</strong>' + escape(v["review"]) + '</p>']
    score = v["score"]
    parts += ['<details><summary>展开逐项评分与记录来源</summary><ul>', f'<li>首句进入答案：{"是" if score["first_answer"] else "否"}（是否计入比例由本轮场景标注决定）</li>']
    for key, label in LABELS.items():
        if key == "boundary_not_at_end":
            value = {True: "位于末尾", False: "未位于末尾", None: "不适用／无认识边界"}[score["boundary_at_end"]]
        else:
            value = "有此标记" if score[key] else "无此标记"
        parts += [f'<li>{label}：{value}</li>']
    parts += [f'<li>模型决策：{escape(v["action"])} / {escape(v["outcome"])}</li>',
              f'<li>记录编号：{escape(pair["id"])} / {arm} / 第 {turn["number"]} 轮</li>',
              f'<li>原始记录 SHA256：{v["source_sha256"]}</li>', '</ul></details></div></article>']
    return "".join(parts)


def html(data):
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>Misty QQ 对话测试记录与标记说明</title>', '<style>' + CSS + '</style>',
             '<body class="candidate-only"><header><div class="eyebrow">NEURALCLOUD / QQ REPLY REVIEW / 2026-09-15</div><h1>Misty 对话测试记录</h1>',
             '<div class="intro">' + ''.join('<p>' + escape(p) + '</p>' for p in INTRO[:4]) + '</div>',
             '<div class="metrics"><div><strong>24 组 × 4 轮</strong><span>旧版和 v4 各 96 轮，均有完整评分</span></div><div><strong>9 轮</strong><span>v4 被 Kimi 标记零容忍项</span></div><div><strong>98.84%</strong><span>v4 普通短答达标率，85/86</span></div><div><strong>未通过</strong><span>原定总体语义验收；问题保留</span></div></div></header>',
             '<div class="tools"><div class="tool-inner"><label>版本 <select id="version"><option value="candidate">v4</option><option value="both">旧版与 v4 对照</option><option value="baseline">旧版</option></select></label>',
             '<label>查看 <select id="filter"><option value="all">完整对话</option><option value="hard">有零容忍项标记</option><option value="attention">有任何验收提示</option><option value="first">首句指标未得分</option><option value="soft">超出软篇幅目标</option></select></label>',
             '<input id="search" type="search" aria-label="搜索场景、对话或评审说明" placeholder="搜索场景、原话或标记说明"><button id="reset">重置</button><span class="tool-count" id="count"></span></div></div>',
             '<main><section class="guide"><h2>怎样读“不通过”</h2><p>只看标记时，仍保留所在对话的全部四轮，方便判断上下文。红标是 Kimi 判断，黄色是比例或软目标提示；自然度高分与事实标记可以同时存在。</p>',
             '<ul><li>共同经历前提：1 轮。按用户要求保留，原文未改写。</li><li>部分送达内容归属：5 轮。另附测试装置的原始草稿，清楚区分“未发送”与“不存在”。</li><li>存在复核分歧：3 轮，涉及“小雾”确认、城市澄清及“这个”是否是迟疑。补充复核由 Codex 撰写，原评分来自 Kimi。</li></ul>',
             '<p><a href="' + STEM + '.md">完整 Markdown 记录</a> · <a href="qq-brief-implementation-20260915.md">实施与验收报告</a></p>', '<div class="navlinks">']
    for pair in data["pairs"]:
        if pair["repeat"] == 1:
            parts += [f'<a href="#{pair["id"]}" data-jump="{pair["id"]}">{pair["number"]:02d} {escape(pair["title"])}</a>']
    parts += ['</div></section><div id="noresults" class="noresults hidden">没有匹配的对话。可清空搜索或切换筛选。</div>']
    for pair in data["pairs"]:
        parts += [f'<section class="pair" id="{pair["id"]}"><div class="pair-head"><h2>场景 {pair["number"]:02d} · {escape(pair["title"])}</h2><span>第 {pair["repeat"]} 次独立运行 · {"私聊" if pair["private"] else "群聊"}</span></div>',
                  f'<div class="muted">{pair["id"]}</div>']
        if pair["facts"] or pair["history"] or pair.get("planned_seed"):
            parts += ['<div class="context">']
            for item in pair["history"]:
                parts += ['<p><strong>测试前用户消息：</strong>' + escape(item) + '</p>']
            for item in pair["facts"]:
                parts += ['<p><strong>初始证据范围：</strong>' + escape(item) + '</p>']
            if pair.get("planned_seed"):
                parts += ['<details open><summary>补充：测试装置原始草稿（原评分封包没有第二段全文）</summary>',
                          '<p>第 1 段，已模拟送达：' + escape(pair["planned_seed"][0]) + '</p>',
                          '<p>第 2 段，未送达：' + escape(pair["planned_seed"][1]) + '</p>',
                          '<p>来源为初始 SQLite 检查点，只供人工核对；没有回填原评分或重新调用模型。</p></details>']
            parts += ['</div>']
        for turn in pair["turns"]:
            tid = pair["id"] + '-t' + str(turn["number"])
            kind = '普通短答' if turn['input']['ordinary'] else '强情绪／明确详细请求，不计普通短答上限'
            answer = '固定用例标记为应答' if turn['input']['must_answer'] else '允许自然等待'
            parts += [f'<section class="turn" id="{tid}"><h3>第 {turn["number"]} 轮 <a class="anchor" href="#{tid}">定位本轮</a></h3>',
                      f'<div class="muted statusline">{kind} · {answer}</div><div class="user"><strong>用户</strong>']
            parts += ['<p class="raw" data-user="1">' + escape(m) + '</p>' for m in turn['input']['messages']]
            parts += ['</div><div class="variants">']
            for arm in ("baseline", "candidate"):
                parts += [html_variant(pair, turn, arm, turn["variants"][arm])]
            parts += ['</div></section>']
        parts += ['</section>']
    parts += ['</main><footer><p>' + escape(INTRO[5]) + '</p><p>字符数为非空白字符（包含英文、数字与标点）；句数按原评估器的句末标点规则统计。旧版两次历史失败经显式重试完成，v4 无生成失败；这里显示最后完成的尝试。固定回归集曾用于调优，不是未见测试集。</p>',
              '<p>来源：paired-v4 · 最终 Kimi 参数 high / 16384 · 原始封存 SHA256：' + data['seal_sha256'] + '</p></footer>',
              '<script>' + JS + '</script></body></html>']
    return "".join(parts)


JS = """
const version=document.querySelector('#version'),filter=document.querySelector('#filter'),search=document.querySelector('#search'),pairs=[...document.querySelectorAll('.pair')];
function apply(){const v=version.value,f=filter.value,q=search.value.trim().toLocaleLowerCase();document.body.classList.toggle('candidate-only',v==='candidate');document.body.classList.toggle('baseline-only',v==='baseline');let shown=0;for(const pair of pairs){const selected=[...pair.querySelectorAll('.variant')].filter(n=>v==='both'||n.dataset.arm===v);const matches=f==='all'||selected.some(n=>f==='attention'?['hard','first','soft'].some(k=>n.dataset[k]==='1'):n.dataset[f]==='1');const text=[pair.querySelector('.pair-head').textContent,...pair.querySelectorAll('.context,.user'),...selected].map(x=>typeof x==='string'?x:x.textContent).join(' ').toLocaleLowerCase();const visible=matches&&(!q||text.includes(q));pair.classList.toggle('filtered-out',!visible);if(visible)shown++}document.querySelector('#count').textContent=`显示 ${shown}/24 组 · 每组保留完整四轮`;document.querySelector('#noresults').classList.toggle('hidden',shown!==0)}
[version,filter].forEach(n=>n.addEventListener('change',apply));search.addEventListener('input',apply);document.querySelector('#reset').addEventListener('click',()=>{version.value='candidate';filter.value='all';search.value='';apply()});document.querySelectorAll('[data-jump]').forEach(a=>a.addEventListener('click',()=>{filter.value='all';search.value='';apply()}));apply();
"""


if __name__ == "__main__":
    data = build_data()
    md = markdown(data)
    page = html(data)
    (OUT / (STEM + ".md")).write_text(md, encoding="utf-8")
    (OUT / (STEM + ".html")).write_text(page, encoding="utf-8")
    (OUT / (STEM + ".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # Ensure every raw response is preserved in both reading formats.
    for pair in data["pairs"]:
        for turn in pair["turns"]:
            for v in turn["variants"].values():
                if v["reply"] is not None:
                    assert q(v["reply"]) in md and escape(v["reply"]) in page
    print(json.dumps({"pairs": len(data["pairs"]), "counts": data["counts"],
                      "html": str(OUT / (STEM + ".html")), "markdown": str(OUT / (STEM + ".md"))}, ensure_ascii=False))
