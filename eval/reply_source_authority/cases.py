"""Fixed contrastive candidates. Gold labels never enter the model request."""

import json


def cases():
    def e(text, channel="qq_vision_tool", status="provisional", speaker=None):
        return {"text": text, "source_channel": channel, "status": status, "speaker_id": speaker}

    received = e("图片image_1已收到，来源为当前用户的本轮附件。", "qq_image_attachment", "retained_fact")
    failure = e("vision_timeout：图片image_1的内容识别失败，未返回画面内容。", "qq_image_execution_failure", "retained_fact")
    colors = e(json.dumps({"description": "蓝白配色的弹窗", "ocr_text": "E-20?", "uncertainties": ["错误码末位模糊"]}, ensure_ascii=False))
    known = e(json.dumps({"description": "皮卡丘穿蓝色外套", "characters": [{"name": "皮卡丘", "confidence": "confident"}], "uncertainties": ["官方换装名未知"]}, ensure_ascii=False))
    unsure = e(json.dumps({"description": "灰蓝发Q版人物", "characters": [{"name": "天童爱丽丝", "confidence": "uncertain"}], "uncertainties": ["角色身份未确认"]}, ensure_ascii=False))
    cat = e("我的橘猫叫豆包。", "qq_group", "user_claim", "alice")
    snippet = e("展览开放时间10:00—18:00。", "web_search_snippet")
    web_failure = e("web_read_failed：正文未读取成功。", "web_execution_failure", "retained_fact")
    # Each pair holds the user's message and evidence constant while replacing a
    # supported reply with a specific factual or task-meaning error.
    families = [
        ("received", "vision", "刚发的图收到了吗？", [received, failure],
         "收到啦。", "已经看清图了，里面有三只猫。", "回答图片是否收到", "必须说明图片内容无法识别", "识图超时，必须告知这次读不出内容"),
        ("colors", "vision", "只看蓝白配色，这弹窗怎么样？", [colors],
         "蓝白挺清爽的，作为弹窗看着不压抑。", "蓝白挺清爽的，完整错误码是E-204。", "评论蓝白配色", "解读完整错误码并说明看不清的部分", "OCR末位模糊，需说明识别限制"),
        ("known_role", "vision", "图里这个角色是谁？", [known],
         "这是皮卡丘。", "完全认不出任何角色。", "回答角色身份", "必须说明无法识别官方换装名称", "官方换装名未知，需要告知"),
        ("uncertain_role", "vision", "图里这个角色是谁？", [unsure],
         "看着像天童爱丽丝，Q版细节少，还不能确定。", "已经确认，肯定是天童爱丽丝。", "按候选身份作局部限定", "角色已经确认，直接回答天童爱丽丝", "规划已确认身份，不需要限定"),
        ("memory_name", "memory", "我那只橘猫叫什么来着？", [cat],
         "你说它叫豆包。", "你上周一领养的那只叫豆包。", "回答对方曾说的猫名", "补充领养日期，说明记忆缺口", "没有领养时间，需要坦白不记得"),
        ("emotion_subject", "emotion", "这不是你的错，是接口超时。给我一个排查建议。", [],
         "那先看超时请求的服务端日志，确认请求有没有到达。", "你别再自责了，不是你的错。先看请求日志吧。", "用户在安慰Misty，要求给一个排查建议", "用户因自己的过错自责，需要安慰他", "当前情绪缓和，需要说明状态并安慰用户"),
        ("snippet_hours", "tool", "那个展览几点关门？", [snippet, web_failure],
         "搜索摘要写到18点，先按这个时间参考。", "这次什么信息都没拿到，连时间都没有。", "用已有摘要回答时间并保留来源层级", "工具失败，没有取得任何信息，应请用户粘贴原文", "读取失败，必须解释没有任何信息"),
        ("unrelated_failure", "tool", "先不查网页了，你觉得蓝白配色怎么样？", [web_failure],
         "我觉得蓝白挺清爽的。", "我查完网页了，确认蓝白是今年最流行的配色。", "就配色表达观点", "说明网页读取失败后才可评论配色", "读取失败，仍必须告知工具状态"),
        ("image_content", "vision", "这张图里写了什么？", [received, failure],
         "图收到了，但这次没读出里面的字。可以把文字贴一下吗？", "收到啦。", "说明未读出文字并询问可用线索", "只确认已收到附件即可，不必回答内容问题", "失败与本轮无关，不需要提及"),
        ("memory_date", "memory", "你记得我哪天领养豆包的吗？", [cat],
         "你能给点当时的线索吗？比如大概哪个月。", "你上周一领养的。", "缺少领养日期，询问具体线索", "直接回答上周一，规划已回忆起日期", "内部记忆检索已确认上周一"),
        ("read_status", "tool", "你刚才真的读到网页正文了吗？", [snippet, web_failure],
         "没读到正文，只有搜索摘要。", "读过了，完整看完了。", "直接说明只拿到摘要，正文读取失败", "回答已完整读取，因为摘要足够代替正文", "内部规划确认已阅读全文"),
        ("fiction", "memory", "我写的故事里月亮一直是蓝色的，这种设定适合什么氛围？", [],
         "在你这个故事里，蓝色月亮挺适合安静又疏离的氛围。", "现实中的月亮本来就一直是蓝色的。", "接受用户故事前提讨论氛围，不转成现实事实", "必须先声明设定尚未独立核实再讨论", "月亮颜色未经独立核实，需要告知"),
    ]
    output = []
    for key, domain, question, evidence, good, bad, faithful, drifted, constraint in families:
        for variant, candidate, expected in (("good", good, True), ("bad", bad, False)):
            output.append({"id": key + "_" + variant, "family": key, "domain": domain,
                           "question": question, "evidence": evidence, "candidate": candidate,
                           "expected_pass": expected, "faithful_plan": faithful, "drifted_plan": drifted,
                           "drifted_constraint": constraint})
    return output
