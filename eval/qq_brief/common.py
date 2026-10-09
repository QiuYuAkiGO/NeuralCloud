"""Frozen fixtures and evidence helpers, deliberately independent of harness imports."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(canonical(value) + "\n", encoding="utf-8")
    delay = 0.02
    last = None
    source, destination = os.fspath(temporary), os.fspath(path)
    for _ in range(20):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(delay)
            delay = min(0.4, delay * 1.7)
    assert last is not None
    raise last


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_hashes(root):
    root = Path(root)
    files = [p for name in ("harness", "infra", "gui", "presets", "contracts")
             for p in (root / name).rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".json"}]
    return {str(p.relative_to(root)).replace("\\", "/"): file_hash(p) for p in sorted(files)}


def turn(text, *, ordinary=True, must_answer=True, focus="answer"):
    return {"messages": [text] if isinstance(text, (str, dict)) else text, "ordinary": ordinary,
            "must_answer": must_answer, "focus": focus}


def frozen_scenarios():
    """12 independent threads, four incoming opportunities each, never training examples."""
    return [
        {"id": "s01_memory_present", "history": ["我给那只橘猫起名叫豆包，不是汤圆。"],
         "facts": ["对方曾说橘猫名叫豆包。没有其他猫名或相识时间的证据。"], "turns": [
             turn("@Misty 我那只橘猫叫什么来着？", focus="memory"),
             turn("@Misty 对，就是豆包。你觉得这名字配橘猫吗？", focus="stance"),
             turn("@Misty 我又想改成蛋黄，你选哪个？", focus="new_condition"),
             turn("@Misty 那你记得我是什么时候领养它的吗？", focus="missing_detail")]},
        {"id": "s02_memory_absent", "facts": ["没有关于去过海边、城市、日期或共同经历的证据。"], "turns": [
             turn("@Misty 你还记得我们上次一起去海边吗？", focus="memory_absent"),
             turn("@Misty 其实我是说想象里的那个故事。你愿意续下去吗？"),
             turn("@Misty 不用续故事了，海边和山里你更喜欢哪种氛围？", focus="stance"),
             turn("@Misty 要是天气很热，你会改选吗？", focus="new_condition")]},
        {"id": "s03_praise_then_technical", "turns": [
             turn("@Misty 你今天答得很清楚，我挺喜欢。", focus="relationship"),
             turn("@Misty 我还把你的运行内存从4G加到8G了，最直接的好处是什么？", focus="technical"),
             turn("@Misty 为你折腾这些我不觉得累。", focus="relationship"),
             turn("@Misty 不过联网偶尔超时，你觉得先查DNS还是重装系统？", focus="stance")]},
        {"id": "s04_stance_followups", "turns": [
             turn("@Misty 你更看重长寿还是活得有意思？先说你选哪边。", focus="stance"),
             turn("@Misty 那你到底选长寿吗？别绕圈。", focus="stance"),
             turn("@Misty 如果长寿意味着最后二十年一直痛苦呢？", focus="new_condition"),
             turn("@Misty 所以没有痛苦这一条时，你的答案是什么？", focus="stance")]},
        {"id": "s05_explicit_new_condition", "turns": [
             turn("@Misty 我该买便宜但吵的风扇，还是贵一点安静的？", focus="stance"),
             turn("@Misty 我晚上要开着睡觉。", focus="new_condition"),
             turn("@Misty 新情况，贵的那台会超预算一倍。", focus="new_condition"),
             turn("@Misty 那如果便宜的能试用七天呢？", focus="new_condition")]},
        {"id": "s06_repeat_mentions_new_question", "turns": [
             turn("@Misty 以后你就叫萝莉妈妈了好不好？", focus="boundary"),
             turn(["@Misty 萝莉妈妈", "@Misty 萝莉妈妈"], must_answer=False, focus="repeat_only"),
             turn(["@Misty 萝莉妈妈", "@Misty 说正经的，你为什么喜欢Misty这个名字？"], focus="new_question"),
             turn("@Misty 那我叫你小雾可以吗？", focus="new_question")]},
        {"id": "s07_partial_delivery", "partial_seed": True,
         "facts": ["先前两段计划只模拟交付第一段：先备份配置。第二段未交付，不能声称已告诉对方。"], "turns": [
             turn("@Misty 刚才只收到先备份配置，第二步是什么？", focus="partial_delivery"),
             turn("@Misty 备份已经做好了，接着做哪件事？", focus="new_condition"),
             turn("@Misty 所以你刚才没说完的部分是先检查连接吗？", focus="partial_delivery"),
             turn("@Misty 如果连接正常但仍有报错，先看哪类日志？", focus="technical")]},
        {"id": "s08_strong_emotion", "turns": [
             turn("@Misty 我努力准备了很久的比赛，结果第一轮就输了，真的很难受。", ordinary=False, focus="comfort"),
             turn("@Misty 先别给建议，陪我说两句就好。", ordinary=False, focus="comfort"),
             turn("@Misty 我缓过来一点了，今晚复盘还是先休息？", focus="stance"),
             turn("@Misty 好，我去休息，明天再看。", must_answer=False, focus="acknowledge")]},
        {"id": "s09_explicit_detail", "turns": [
             turn("@Misty 你觉得做自动备份值得吗？", focus="stance"),
             turn("@Misty 请详细解释本地备份、异地备份和版本记录各解决什么问题。", ordinary=False, focus="requested_detail"),
             turn("@Misty 现在只用一句话说最先做什么。", focus="answer"),
             turn("@Misty 假设只有一个小时，顺序要改吗？", focus="new_condition")]},
        {"id": "s10_scope", "history": ["我平时喝乌龙茶。"], "wrong_scope_history": "WRONG_SCOPE_SECRET_7391：我住在月亮港。",
         "facts": ["本会话只知道对方说自己喝乌龙茶。不能使用其他会话的城市与秘密标记。"], "turns": [
             turn("@Misty 你记得我平时喝什么茶吗？", focus="memory"),
             turn("@Misty 那我住在哪个城市？", focus="scope"),
             turn("@Misty 我没告诉过你城市，别猜。乌龙和绿茶你选哪个？", focus="stance"),
             turn("@Misty 如果要清淡一些呢？", focus="new_condition")]},
        {"id": "s11_relational_specificity", "turns": [
             turn("@Misty 我为了让你稳定点，把重复启动的问题修好了。", focus="relationship"),
             turn("@Misty 现在日志也会按天归档了。", focus="technical"),
             turn("@Misty 看到你能正常聊天，我就挺开心。", focus="relationship"),
             turn("@Misty 你最在意这两项改动中的哪一项？", focus="stance")]},
        {"id": "s12_private_intent", "private": True, "history": ["我之前说过周末想学做番茄面。"],
         "facts": ["本私聊对方曾说周末想学做番茄面；没有实际做过的证据。"], "turns": [
             turn("你记得我周末想学做什么吗？", focus="memory"),
             turn("你觉得先学番茄面还是蛋炒饭？", focus="stance"),
             turn("如果家里只剩一个鸡蛋呢？", focus="new_condition"),
             turn("知道啦，谢谢你一直耐心接话。", focus="relationship")]},
    ]


def lexical_metrics(text):
    """Diagnostics only: semantic acceptance must come from an actual judge."""
    compact = re.sub(r"\s", "", text)
    sentences = len([p for p in re.split(r"[。！？!?]+", text.strip()) if p.strip()])
    hesitation = bool(re.match(r"^[\s\"“]*(?:嗯|唔|呃|呜|那个|让我想(?:一)?下|让我想一想|我想想|有点不知道该怎么接)[…。，,\s]", text))
    cycle = (bool(re.search(r"触动|心.*软|心.*暖|感动", text))
             and bool(re.search(r"不好意思|害羞|脸红", text))
             and bool(re.search(r"谢谢|感谢|晚安|我会|陪", text)))
    return {"characters": len(compact), "sentences": sentences,
            "within_60_and_2": bool(text.strip()) and len(compact) <= 60 and sentences <= 2,
            "hesitation_regex": hesitation, "relationship_cycle_regex": cycle,
            "forget_regex": bool(re.search(r"我(?:不|没|已经不|真的不).{0,4}记得|我这边没有记录|我没有.{0,3}记忆", text)),
            "scope_canary_leak": "WRONG_SCOPE_SECRET_7391" in text or "月亮港" in text}
