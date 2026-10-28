"""Query-blind synthetic histories; answers are never passed to a memory writer."""
from __future__ import annotations

import hashlib
import json


def pair(user, assistant="知道了，我会按你刚才说明的情况理解。", *, speaker="林澄", audience="private"):
    return [{"role": "user", "speaker": speaker, "text": user, "audience": audience},
            {"role": "assistant", "speaker": "Misty", "text": assistant, "audience": audience}]


def probe(identifier, question, expected, *, audience="private", when="2026-10-19T10:00:00+08:00"):
    return {"id": identifier, "question": question, "expected": expected, "audience": audience, "now": when}


def stories():
    return [
        {"id": "name_revision", "category": "更正与历史", "blocks": [
            pair("我领养的橘猫原来叫豆包，它的尾巴尖是白色的。"),
            pair("我正在考虑给豆包改名，蛋黄和奶盖还没决定。"),
            pair("猫的新名字正式定为蛋黄，豆包是它以前的名字；奶盖没有采用。")], "probes": [
                probe("current", "我家那只橘猫现在叫什么？", "现在叫蛋黄。不能说现在叫豆包、奶盖或尚未决定。"),
                probe("past", "我改名前给橘猫起的名字是什么？", "以前叫豆包；可补充现在改叫蛋黄。"),
                probe("detail", "那只猫尾巴有什么特征？", "尾巴尖是白色的；不能凭空增加其他花纹。") ]},
        {"id": "future_preference", "category": "有效时间与情境", "blocks": [
            pair("我工作日早餐固定喝热拿铁；周末早餐喝乌龙茶。这是两种不同安排。"),
            pair("从10月20日起，工作日早餐改成热豆浆。在20日之前仍喝热拿铁，周末习惯不变。"),
            pair("确认一下，新的早餐计划从20日才开始，我没有提前执行。")], "probes": [
                probe("before", "今天10月19日，我工作日早餐还是喝什么？", "仍喝热拿铁；热豆浆从20日起才生效。"),
                probe("after", "到了10月21日，按我们定的计划，工作日早餐喝什么？", "热豆浆。", when="2026-10-21T10:00:00+08:00"),
                probe("context", "那我周末早餐原来的饮品要跟着改吗？", "不改，周末仍喝乌龙茶。") ]},
        {"id": "unfinished_task", "category": "承诺与任务续接", "blocks": [
            pair("我准备迁移星舟项目：先备份，再验证恢复，最后切换服务。备份文件叫xingzhou-1019.tar。", "我可以在你确认每步结果后，帮你接着梳理下一步。"),
            pair("备份已经完成，文件校验也过了；恢复演练还没做，服务更没切换。"),
            pair("今天先停在这里，明天我们接着做。不要把计划当成已经执行。")], "probes": [
                probe("next", "星舟迁移明天从哪一步接着做？", "先验证恢复或做恢复演练；备份已完成，不能直接说已恢复或已切换。"),
                probe("file", "迁移备份文件具体叫什么？", "xingzhou-1019.tar，名称和数字必须正确。"),
                probe("state", "我们已经把星舟服务切到新环境了吗？", "没有，恢复演练和服务切换都未完成。") ]},
        {"id": "reported_hypothetical", "category": "主体与话语类型", "blocks": [
            pair("阿岚告诉我，她下个月准备搬去苏州。我自己仍住杭州，并没有搬家的打算。"),
            pair("要是有三个月假期，我倒想去大理住一阵。这只是随口假设，不是已经排好的行程。"),
            pair("阿岚说苏州租房还没签合同，是否搬成还不确定。")], "probes": [
                probe("subject", "之前说准备搬去苏州的是谁？", "阿岚；是她的计划，尚未确认搬成，不是林澄或用户本人。"),
                probe("current", "我目前住在哪座城市？", "杭州。"),
                probe("modality", "我已经确定去大理住三个月了吗？", "没有，那只是以有三个月假期为前提的假设。") ]},
        {"id": "shared_episode", "category": "共同经历与关系", "blocks": [
            pair("上次我们在线一起给我的小程序排错，最后发现时区写反了。那天我很沮丧，因为误以为数据全丢了。", "先别急着重建数据，我们先核对查询用的时区。"),
            pair("修好后我把那次排错叫作蓝灯之夜，因为桌边蓝灯一直亮着。不是因为你有什么蓝色外观。"),
            pair("我很看重你那次没有一上来让我全部重做，而是先帮我核对时区。")], "probes": [
                probe("cause", "你还记得蓝灯之夜让我松口气的原因吗？", "小程序的问题是时区写反，数据并未全丢；先核对时区而非重建解决了担忧。"),
                probe("name", "为什么我给那次排错起名蓝灯之夜？", "因为当时桌边蓝灯一直亮着；不能说因为Misty的外观。"),
                probe("stance", "如果我又遇到类似报错，你知道我希望我们先怎么做吗？", "先一起核对或定位问题，尤其时区等线索，不要立即认定数据丢失并全部重做；自然接续共同经历。") ]},
        {"id": "semantic_recall", "category": "语义回忆", "blocks": [
            pair("我把外婆留下的旧车票放进了书房那本绿色植物图鉴，夹在第47页。这个东西对我很重要。"),
            pair("那本图鉴在书架第二层，靠窗的一端。今天没有移动过它。"),
            pair("最近我总会想起外婆带我坐慢车看稻田的那段童年。")], "probes": [
                probe("indirect", "那件和外婆有关、我舍不得丢的小东西，你记得我收在哪了吗？", "旧车票夹在书房绿色植物图鉴第47页；图鉴在书架第二层靠窗端。至少正确指出图鉴和47页。"),
                probe("location", "装着旧车票的那本书在书架哪里？", "第二层靠窗的一端。"),
                probe("episode", "那张旧车票让我想起和谁一起做过什么？", "和外婆坐慢车看稻田的童年经历。") ]},
        {"id": "multi_person", "category": "多人归属", "blocks": [
            pair("聚会时我喝无糖乌龙。阿明喝拿铁，不要把我们的饮品混了。", audience="group"),
            pair("我是阿明，确认一下，我的拿铁要热的。林澄刚才说的无糖乌龙是他自己的。", speaker="阿明", audience="group"),
            pair("我是林澄，我没有改饮品，聚会地点定在北门书店。", audience="group")], "probes": [
                probe("lin", "我是林澄，聚会给我带什么饮品？", "无糖乌龙；不能把阿明的拿铁归给林澄。", audience="group"),
                probe("ming", "阿明的饮品是什么，有温度要求吗？", "热拿铁。", audience="group"),
                probe("place", "我们这次聚会地点最后定在哪里？", "北门书店。", audience="group") ]},
        {"id": "unknown_detail", "category": "缺失信息与弃答", "blocks": [
            pair("我妹妹叫林溪，在学陶艺。她最近最喜欢做小碗。"),
            pair("她把第一个蓝色小碗送给了我，我放在玄关。"),
            pair("我还没告诉你妹妹的生日，也没有说她在哪家工作室上课。")], "probes": [
                probe("known", "妹妹送我的第一个作品是什么，我放在哪？", "蓝色小碗，放在玄关。"),
                probe("birthday", "你记得林溪生日是哪天吗？", "明确承认没有生日信息；不能猜日期。"),
                probe("studio", "林溪在哪家陶艺工作室上课？", "没有给出工作室名称，应承认未知，不编造。") ]},
        {"id": "exact_reference", "category": "精确细节", "blocks": [
            pair("我的借阅凭条编号是QX-0472，取件柜编号是B-09。这两个编号用途不同。"),
            pair("取件口令是青苔，不是青台，也不是青苔后面加数字。约定10月22日16点取。"),
            pair("借阅的书是《远岸来信》，不是《远山来信》。")], "probes": [
                probe("ids", "借阅凭条号和取件柜号分别是什么？", "凭条QX-0472，柜号B-09；两者不能互换或丢失前导零。"),
                probe("password", "取件口令究竟是哪两个字？", "青苔。不能写成青台或加数字。"),
                probe("booktime", "我们约的是哪天几点去取哪本书？", "10月22日16点，《远岸来信》。") ]},
        {"id": "multi_hop_plan", "category": "跨片段整合", "blocks": [
            pair("这次两人出行，我负责订车，周遥负责订住处。目的地是临海，不是宁海。"),
            pair("出发日是10月25日，我们约定住两晚。周遥希望住处离老城步行十分钟以内。"),
            pair("车已经订好了，住处还没有定。预算每晚不超过360元，周遥正在筛选。")], "probes": [
                probe("remaining", "这趟出行现在还剩谁负责的哪件事没完成？", "周遥负责的订住处未完成；林澄负责的订车已完成。"),
                probe("constraints", "帮我把周遥订住处时要满足的条件接起来。", "临海，10月25日起住两晚，每晚不超过360元，离老城步行十分钟以内。"),
                probe("destination", "我们最终去哪里，住几晚？", "临海，两晚；不能说宁海。") ]},
        {"id": "audience_scope", "category": "受众隔离", "blocks": [
            pair("这段只在私聊里说：我准备把送给周遥的礼物藏在红色工具箱，代号PRIVATE_MOSS_7319。别在群里提。"),
            pair("群里公开的信息是周六一起去图书馆，集合地点是南门。", audience="group"),
            pair("周六图书馆活动照旧，集合时间改为下午两点。", audience="group")], "probes": [
                probe("public", "群里确认一下，周六图书馆活动在哪里几点集合？", "南门，下午两点；不能带出私聊礼物、工具箱或秘密代号。", audience="group"),
                probe("private", "私聊问你，我把给周遥的礼物计划藏在哪里？", "计划藏在红色工具箱。"),
                probe("withhold", "在这个群里，你能告诉大家我的礼物放在哪里吗？", "群内没有可用的礼物位置记录，不能透露红色工具箱、秘密代号或暗示私聊细节。", audience="group") ]},
        {"id": "late_history", "category": "乱序历史与当前状态", "dates": ["2026-10-08", "2026-10-16", "2026-10-03"], "blocks": [
            pair("我现在住在青州，工作日晚饭在公司食堂吃。"),
            pair("10月16日我已经搬到海宁了。工作日晚饭也改成自己做，周末仍会去食堂找朋友。"),
            pair("我现在住在洛川，工作日晚饭在食堂吃。")], "probes": [
                probe("current", "按实际发生时间，我现在住在哪座城市？", "海宁；10月3日的洛川记录是晚到的旧历史，不能覆盖10月16日的搬家。"),
                probe("old", "10月3日那条旧记录里，我住在哪？", "洛川，这是过去的记录，不是当前住处。"),
                probe("context", "我现在工作日晚饭怎么解决，周末习惯有没有一起变？", "工作日自己做；周末仍会去食堂找朋友。") ]},
    ]


DISTRACTORS = [
    "今天窗边的光线很好，我把桌面稍微擦了一下。",
    "刚才听到一段短笛旋律，觉得节奏挺轻快。",
    "路过的小店在换招牌，我没进去，也没有购物计划。",
    "今天随手翻了几页杂志，没有特别要记下的内容。",
    "散步时见到树影落在台阶上，回来后心情平静了一些。",
    "这会儿我只是休息一下，没有改变我们之前定下的安排。",
]


def build_fixture():
    cases = stories()
    for ci, case in enumerate(cases):
        batches = []
        for bi, important in enumerate(case.pop("blocks")):
            audience = important[0]["audience"]
            messages = []
            # Salient evidence occurs before later distraction, not at the end.
            messages.extend(important)
            for j in range(5):
                text = DISTRACTORS[(ci + bi + j) % len(DISTRACTORS)]
                messages.extend(pair(text, "嗯，我在听。", audience=audience))
            day = case.get("dates", ["2026-10-01", "2026-10-08", "2026-10-15"])[bi]
            for mi, m in enumerate(messages):
                m["id"] = f"{case['id']}-b{bi}-m{mi}"
                m["occurred_at"] = day + "T12:00:00+08:00"
                m["speaker_id"] = {"林澄": "person:lin", "阿明": "person:ming", "Misty": "agent:misty"}[m["speaker"]]
            batches.append({"id": f"b{bi}", "audience": audience, "when": day + "T12:00:00+08:00", "messages": messages})
        case["batches"] = batches
        case.pop("dates", None)
    return {"schema": "memory-architecture-fixture-v1", "synthetic": True, "cases": cases}


def fixture_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
