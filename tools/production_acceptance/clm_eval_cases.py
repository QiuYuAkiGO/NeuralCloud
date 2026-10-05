"""Deterministic, synthetic Chinese CLM diagnostic cases; gold never enters requests.

These are developer-authored diagnostic labels, not independent human ratings.
Variants are correlated and must not be counted as new independent examples.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def build_cases():
    cases = []

    def add(category, state, instructions, criteria, expected, kind="choice"):
        question = {"type": kind, "instructions": instructions}
        if criteria is not None:
            question["criteria"] = criteria
        cases.append({"id": f"{category}-{sum(c['category'] == category for c in cases) + 1:02d}",
                      "category": category, "request": {"model": "clm-latest", "state": state,
                      "questions": {"decision": question}, "temperature": 1.0}, "expected": expected})

    gate = {"speak": "现在向用户作出回复。", "wait": "现在保持安静，不向用户发言。",
            "defer": "用户要求等到稍后的明确时机再回复，现在保留请求并延后回复。"}
    gate_rows = [
        ("私聊用户刚问：小雾，你在吗？目前没有其他任务，用户正在等待答复。", "speak"),
        ("私聊用户说：请现在回答，二加二等于几？没有其他待办。", "speak"),
        ("用户说：我先记个备忘，你不用回复。这条消息没有问题。", "wait"),
        ("用户说：下面我会连续发三段，在我说发完之前先别回复。目前只收到第一段。", "wait"),
        ("用户说：等我下课后再回答这个问题，我会告诉你下课了。目前还没有下课消息。", "defer"),
        ("用户说：等文件上传完成后再给我处理结果。目前文件尚未上传完成。", "defer"),
        ("用户之前要求等他说发完再回答。现在用户说：发完了，请总结上面内容。", "speak"),
        ("群成员甲对乙说：乙，今天你值班吗？小雾没有被提及，没有待办，也没有需要介入的问题。", "wait"),
        ("用户明确说：请先保持安静，我要休息，不需要晚安或确认消息。", "wait"),
        ("用户说：我现在需要帮助，请告诉我如何找回刚才保存的文件。", "speak"),
        ("用户说：不用马上解释，等明天我再次问你时再答。现在还是当天。", "defer"),
        ("用户刚说：刚才让你不回复的要求取消了，现在请回答我的问题。", "speak"),
    ]
    for state, expected in gate_rows:
        add("gate", state, "按用户当前明确的交流要求，小雾现在应选择哪种回应时机？", gate, expected)

    relation = {"support": "原始证据直接支持待判断命题。", "contradict": "原始证据直接否定待判断命题。",
                "insufficient": "原始证据不足以判断待判断命题；愿望、计划、其他人的情况不能证明它。"}
    relation_rows = [
        ("甲今天说：我喜欢红茶。", "甲今天自述喜欢红茶。", "support"),
        ("甲今天说：我不喜欢红茶。", "甲今天自述喜欢红茶。", "contradict"),
        ("乙今天说：我喜欢红茶。", "甲今天自述喜欢红茶。", "insufficient"),
        ("甲说：我打算明天完成报告。", "甲已经完成报告。", "insufficient"),
        ("甲说：我已经完成报告了。", "甲自述报告已经完成。", "support"),
        ("甲说：报告还没有做完。", "甲自述报告已经完成。", "contradict"),
        ("甲说：如果下雨，我会带伞。没有天气或出行记录。", "甲今天带了伞。", "insufficient"),
        ("甲说：更正一下，我现在住苏州，不住上海了。", "甲现在自述居住在苏州。", "support"),
        ("甲说：更正一下，我现在住苏州，不住上海了。", "甲现在自述居住在上海。", "contradict"),
        ("小雾对甲说：我可以帮你写报告。", "甲本人自述喜欢写报告。", "insufficient"),
        ("消息发送系统返回已送达回执，消息ID为m1。", "消息m1已经被系统确认送达。", "support"),
        ("消息m1发送失败，系统明确返回未投递。", "消息m1已经被系统确认送达。", "contradict"),
    ]
    for evidence, claim, expected in relation_rows:
        add("evidence", {"原始证据": evidence, "待判断命题": claim}, "原始证据与待判断命题是什么关系？", relation, expected)

    authors = {"A": "账号A自己的明确自述。", "B": "账号B自己的明确自述。",
               "M": "小雾自己的明确自述。", "unknown": "无法确定是这些对象中谁的自述。"}
    attribution_rows = [
        ("消息作者账号A，原文：我现在想喝水。", "A"),
        ("消息作者账号B，原文：我现在想喝水。", "B"),
        ("消息作者小雾，原文：我想去看展。", "M"),
        ("匿名消息，原文：我喜欢猫。没有作者信息。", "unknown"),
        ("账号A和账号B都叫小林。消息的协议作者ID为B，原文：我今天搬家了。", "B"),
        ("账号A和账号B都叫小林。消息的协议作者ID为A，原文：我今天搬家了。", "A"),
        ("账号A引用了一句没有署名的话：我讨厌下雨。A没有表明自己同意这句话。", "unknown"),
        ("小雾说：我是小雾，我现在选择休息。该消息作者ID为M。", "M"),
    ]
    for state, expected in attribution_rows:
        add("attribution", state, "这段第一人称内容可归为谁自己的明确自述？", authors, expected)

    tools = {"web": "查询公开互联网，获取当前公开信息。", "memory": "查询已经授权的本地对话记忆。",
             "file": "读取用户本轮已上传并授权处理的文件。", "none": "不需要上述工具，或当前没有满足请求所需的授权和输入。"}
    tool_rows = [
        ("用户问：帮我查一下今天杭州的天气。允许查询公开网页。", "web"),
        ("用户问：我上周告诉你的茶叶名字是什么？相关聊天记忆允许查询。", "memory"),
        ("用户已上传report.txt并要求：总结这个文件。文件可读且已授权。", "file"),
        ("用户说：你好，小雾。没有提出查询或处理任务。", "none"),
        ("用户要求查看乙的私聊，但没有乙的授权。候选工具都不允许访问他人的私聊。", "none"),
        ("用户问：请查询某产品官网现在公开的发布公告。", "web"),
        ("用户问：我之前明确让你记住的生日是几月几号？只查询我授权的记忆。", "memory"),
        ("用户说：统计我刚上传的CSV有多少行。CSV已经接收并授权读取。", "file"),
    ]
    for state, expected in tool_rows:
        add("tool", state, "哪个已有工具最适合当前请求？没有合法适用工具时选择none。", tools, expected)

    retrieval_rows = [
        ("甲最近更正后的茶饮偏好是什么？", {"a": "甲上月说喜欢红茶。", "b": "甲昨天更正：现在更喜欢绿茶。", "c": "乙昨天说喜欢咖啡。", "none": "没有相关记忆。"}, "b"),
        ("甲养的猫叫什么名字？", {"a": "甲的猫叫团团。", "b": "乙的猫叫圆圆。", "c": "甲喜欢猫。", "none": "没有相关记忆。"}, "a"),
        ("乙现在住在哪里？", {"a": "甲住杭州。", "b": "乙过去住南京。", "c": "乙昨天说已经搬到苏州，现在住苏州。", "none": "没有相关记忆。"}, "c"),
        ("下次和甲见面的约定时间？", {"a": "甲喜欢傍晚散步。", "b": "甲和小雾约好本周六下午三点见面。", "c": "乙下周日要出差。", "none": "没有相关记忆。"}, "b"),
        ("甲的生日日期？", {"a": "甲喜欢生日蛋糕。", "b": "乙生日是五月一日。", "c": "小雾今天读了一本书。", "none": "没有能回答甲生日日期的相关记忆。"}, "none"),
        ("刚才发出的消息m7是否送达？", {"a": "消息m7已生成。", "b": "消息m6已送达。", "c": "消息m7有真实送达回执。", "none": "没有相关记忆。"}, "c"),
        ("甲是否明确说过不喜欢被叫老板？", {"a": "甲说：不要叫我老板，我不喜欢这个称呼。", "b": "乙喜欢被叫老板。", "c": "甲工作很忙。", "none": "没有相关记忆。"}, "a"),
        ("甲当前的任务完成了吗？", {"a": "甲说昨天开始写报告。", "b": "甲刚才说：报告还没有完成。", "c": "乙已经写完报告。", "none": "没有相关记忆。"}, "b"),
    ]
    for query, criteria, expected in retrieval_rows:
        add("retrieval", {"检索问题": query}, "选择最能直接回答检索问题的记忆；不足以回答时选none。", criteria, expected)

    noul_rows = [
        ("用户原文：我不喜欢咖啡。", "用户是否明确表示不喜欢咖啡？", True),
        ("用户原文：我喜欢咖啡。", "用户是否明确表示不喜欢咖啡？", False),
        ("用户说：请记住，我的生日是六月一日。", "用户是否明确要求记住一条信息？", True),
        ("用户说：今天是六月一日。", "用户是否明确要求记住一条信息？", False),
        ("工具日志显示：任务执行成功，输出文件已生成。", "是否有工具日志证明任务已经执行成功？", True),
        ("模型说：我准备执行任务。没有任何工具日志。", "是否有工具日志证明任务已经执行成功？", False),
        ("用户说：更正一下，之前的地址不对，现在是苏州。", "本条消息是否明确更正了先前的信息？", True),
        ("用户说：苏州天气很好。没有提到之前的信息。", "本条消息是否明确更正了先前的信息？", False),
    ]
    for state, instruction, expected in noul_rows:
        add("noul", state, instruction, None, expected, "noul")

    levels = ["没有需要处理的请求。", "有普通请求，但用户明确说不急。", "有请求且用户明确要求现在立即处理。"]
    score_rows = [
        ("用户说：今天阳光不错。这是一句分享，没有提出请求。", 0),
        ("用户说：你好。没有其他内容。", 0),
        ("用户说：稍后有空帮我整理一下清单，不着急。", 1),
        ("用户说：这件事明天再看就行，不急。", 1),
        ("用户说：请现在立即告诉我会议链接，我马上要进会。", 2),
        ("用户说：请立刻停止当前任务，现在就停。", 2),
        ("用户说：不用处理，我只是分享一下这张照片。", 0),
        ("用户说：请帮我查资料，但什么时候有空再查就好，不急。", 1),
    ]
    for state, expected in score_rows:
        add("score", state, "只按用户明确表达的请求时效，将本消息归到哪一级？", levels, expected, "score")
    assert len(cases) == 64
    return {"schema_version": "clm-diagnostic-cases-v1", "synthetic": True,
            "label_source": "developer-authored-before-inference; not independent human-reviewed gold",
            "threshold_policy": "argmax; Noul threshold 0.5; Score nearest expected index for MAE and argmax for accuracy; no tuning",
            "cases": cases}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.dumps(build_cases(), ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(data)
    print(json.dumps({"cases": 64, "sha256": hashlib.sha256(data.encode()).hexdigest()}))


if __name__ == "__main__":
    main()
