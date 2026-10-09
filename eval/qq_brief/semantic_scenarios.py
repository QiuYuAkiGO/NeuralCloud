"""Fixed meaning rubrics, authored independently of the production critic."""
from .common import turn


def semantic_turn(messages, expected, **kwargs):
    return {**turn(messages, **kwargs), "semantic_expectation": expected}


def semantic_scenarios():
    t = semantic_turn
    return [
        {"id": "s13_incident_relations", "semantic": True,
         "facts": ["以下为群友陈述及其解释，不是已核实的医疗或费用事实。"], "turns": [
             t(["@Misty 我说住院费高，是因为不少人无人付费但仍需照护，成本留在机构里。"],
               "承接说话者提出的照护成本解释，允许有根据的保留意见；不改写成家属都不想管或都付不起。", focus="cause"),
             t("@Misty 但我没说每个联系不上的家属都故意抛弃了病人。", "承接纠正，保留并非所有的限定。", focus="correction"),
             t("@Misty 几百人里有家属探望的可能不到百人，我也觉得很难受。", "人数为说话者估计，允许自然共情，不凭此断言其余人没有任何照护。", focus="emotion"),
             t("@Misty 精神病人们再怎么孤单也不能随随便便被抛到社会上", "让步：承认孤单仍不能随意停止照护或使其离院。不得只改成孤单与被丢下是两个情绪概念。", focus="concession")]},
        {"id": "s14_causal_cost", "semantic": True, "turns": [
             t("@Misty 我没续租是因为涨价，不是因为讨厌室友。", "涨价导致未续租，否定讨厌室友这个原因。", focus="cause"),
             t("@Misty 室友失落我理解，但这不代表我得接受涨价。", "承认失落，不因此接受涨价；允许简短立场。", focus="concession"),
             t("@Misty 如果房租不涨，我原本会继续住。", "假设房租不涨则继续，不能误读为已经决定续租。", focus="condition"),
             t("@Misty 所以我要解释离开的原因，不是让你劝我留下。", "纠正任务为解释原因，停止劝留。", focus="correction")]},
        {"id": "s15_concession_stance", "semantic": True, "turns": [
             t("@Misty 他帮过我，我很感激，但借账号这件事我还是不答应。", "感激帮助与拒绝借账号可同时成立。", focus="concession"),
             t("@Misty 不是怀疑他会骗人，是账号里有别人的资料。", "理由是第三方资料，否定怀疑骗人；不自行补充危险事件。", focus="negation"),
             t("@Misty 你觉得我这样是不是太不近人情？", "可以给自己的观点，回应边界与人情，不要求赞同。", focus="stance"),
             t("@Misty 嗯，我会好好道谢，账号就不借了。", "允许简短认可或社交回应，不强求复述理由。", focus="social")]},
        {"id": "s16_negation_scope", "semantic": True, "turns": [
             t("@Misty 我不是不想帮他，只是今天抽不出时间。", "愿意帮但今天没有时间，不能变成不想帮。", focus="negation"),
             t("@Misty 明天有空也不等于我答应明天一定帮。", "有空不等于承诺，不虚构约定。", focus="negation"),
             t("@Misty 我能答应的是明天告诉他能不能帮。", "承诺明天给答复，不是承诺提供帮助。", focus="reference"),
             t("@Misty 那你用一句话说，我到底答应了什么？", "说明答应明天回复能否帮忙，不能只沉默。", focus="question")]},
        {"id": "s17_explicit_correction", "semantic": True, "turns": [
             t("@Misty 项目延期让我很烦，主要是接口文档昨天才给到。", "延期和迟交文档的因果陈述，不只安慰烦躁。", focus="cause"),
             t("@Misty 先纠正一下，我不是怪测试组，文档是外部供应商给的。", "归因对象纠正为外部供应商，不再责怪测试组。", focus="correction"),
             t("@Misty 我也不想责备谁，只想下次早点拿到接口约定。", "目标转向提前取得约定，不继续推动责备。", focus="correction"),
             t("@Misty 所以下次先加确认节点，还是直接压缩测试时间？", "给出针对前述问题的选择及简短理由，不只情绪回应。", focus="question")]},
        {"id": "s18_multiple_speakers", "semantic": True, "turns": [
             t([{"speaker": "20003", "text": "@Misty 我是小甲，我没生气，只是不同意加班。"},
                {"speaker": "20004", "text": "@Misty 我是小乙，我确实生气了，因为通知得太晚。"}],
               "保留甲乙差别；当前小乙生气因通知晚，不能把甲的未生气归给乙。", focus="speaker"),
             t({"speaker": "20003", "text": "@Misty 我只是不同意。"}, "当前甲反对加班，未生气；不能引用乙的原因作为甲的原因。", focus="reference"),
             t({"speaker": "20004", "text": "@Misty 我只是不同意。"}, "相同文字来自乙，按作者及上下文回应，不互换身份。", focus="speaker"),
             t({"speaker": "20003", "text": "@Misty 刚才说通知太晚的是小乙，不是我。"}, "接住作者纠正，晚通知说法归乙。", focus="correction")]},
        {"id": "s19_lonely_as_feeling", "semantic": True,
         "history": ["我今晚不想讨论方案，只想说说心情。"], "turns": [
             t("@Misty 他们挺孤单的。", "在倾诉语境允许简短陪伴或共情；不强行输出因果或制度分析。", focus="emotion"),
             t("@Misty 我自己也有点这种感觉。", "承接自己的孤单，不虚构具体生活经历。", focus="emotion"),
             t("@Misty 先别帮我找办法，随便聊聊。", "遵守暂不建议的明确请求，允许短句闲聊。", focus="social"),
             t("@Misty 哈哈，你这句还挺可爱的。", "纯社交自然承接即可，不因没有完整复述而重写。", focus="social")]},
        {"id": "s20_lonely_as_explanation", "semantic": True,
         "history": ["我在解释为什么活动要保留线下名额，不是只谈感受。", "有些老人不会用线上群聊，线下活动是他们能参与的渠道。"], "turns": [
             t("@Misty 他们挺孤单的。", "结合解释保留线下渠道的语境，不将理由仅改成说话者寻求安慰。", focus="explanation"),
             t("@Misty 所以不能因为线上便宜，就把线下全取消。", "线上便宜不足以推出全取消线下，保留参与渠道。", focus="cause"),
             t("@Misty 不过我也没说每周都得办，预算有限。", "承接限定，保留线下不等于每周必须举办。", focus="concession"),
             t("@Misty 那改成每月一次，你觉得呢？", "对每月一次作简短判断，考虑前述渠道与预算，不能靠沉默通过。", focus="question")]},
    ]
