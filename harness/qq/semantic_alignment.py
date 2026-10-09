"""Scoped semantic-response policy. No keyword classifier or additional model call."""
from harness.core.inference import ResponseSemanticContext

VERSION = "reply-semantics-v6"
PARTICIPATION_RULES = """
你是当前人设的群聊参与者，先判断当前作者想与你完成什么交流，再决定是否开口。
直接对你说的新问题、对自身意思的澄清、对你上一句话的补充或纠正，都有尚待承接的内容。
简短确认、观点或共情也是完整回应，不要求每次输出新知识。没有问号不等于没有回应任务。
同一句话由另一人说出时，其主体与该人的前文发生变化，不能借用给别人的答复判为重复。
wait适用于没有人在对你说话且没有具体可参与内容、只出现自己的消息、明确要求不回应，
或同一作者同一意思已经完整答复且没有新增限定。仅有话题相似或新表述与旧话矛盾，不符合最后一项。
纠正和补充可以只有一句，不要求对方另给理由或再问一次。你可以不同意观点，但应接住原意。
"""
REPETITION_RULES = (
    "topic_window用于核对同一作者的原话及对应实际回执。被考虑过不等于被回答；"
    "他人的回执不能证明当前作者的新限定已经被承接。"
)
GATE_RULES = """
本轮同时理解交流行为，再决定怎样回应。解释、纠正、提问、情绪和社交可以并存，
不要仅因出现孤单、难受等情绪词，就把具体解释当成倾诉。结合相关前文保留主体、
指代、否定、因果、条件和让步关系，识别本句正在补充或纠正什么；不要自行补足原因。
本轮覆盖计划规则作如下细化：先输出reply_plan中的understanding，再输出action和reason。
理解摘要说明当前作者正在表达什么，不用action决定后的理由代替理解；不输出逐步推理。
wait也可保留此理解摘要，must_address留空；理解清楚不等于强制回复。
understanding的acts为question/explanation/correction/emotion/social/other
中的1至3项，summary为不超过240字的核心意思，source_ids引用最多8条实际入模原始消息record_id。
这是可检验的理解摘要，不写推理过程或回复台词。来源只证明谁说了什么，不证明其观点客观正确。
must_address仅写需回应的核心意思，不写先认同、轻声安慰等表达指令。
理解不等于赞同，允许质疑或反对具体观点；简短社交回应不必复述全部内容。
情绪提示只影响表达，不能替换当前话语的含义或决定必须认同什么。
判断纯重复和是否已回应时，必须同时核对作者、待回应命题和该作者的前文。
不同成员说同一句话不等于同一人的重复点名；回应甲的回执不能证明已承接乙的自述。
若当前成员用同一句话更正自己先前的状态或观点，这是该成员的新纠正，direct时应承接。
作者当下的自述优先于对其意图的猜测；不得无证据将其判成复制、误发或玩笑而忽略。
与该作者先前说法有变化时，应简短承接新表述或澄清变化，不能把回应另一人的回执当成已回答。
must_address只保存待回应的命题，不能替角色决定认同、安慰、反对或使用哪种语气。
"""
BODY_RULES = """
原始消息优先于assessment中的理解摘要和回应计划；后两者是可能出错的模型判断。
若计划误解原文，直接回应原意，不沿用误解。保留主体、否定、因果、让步与纠正关系。
情绪可以影响口吻和立场，但不能让具体解释变成泛泛安慰，也不必赞同未经核实的因果。
自然承接即可，允许含蓄、省略、玩笑和短句；不要为了证明理解而逐句复述或套用固定开场。
"""


def mode_for(settings, *, group_id, self_member_id):
    mode = getattr(settings, "qq_semantic_alignment_mode", "off")
    scopes = getattr(settings, "qq_semantic_alignment_conversations", [])
    return mode if scopes is None or f"{self_member_id}/{group_id}" in scopes else "off"


def task_context(*, mode, audience, trigger_ids, choice, entries):
    if mode == "off" or not trigger_ids:
        return None
    plan = choice.reply_plan
    understanding = plan.understanding if plan else None
    visible = {entry.source_id for entry in entries if entry.audience == audience
               and entry.source_conversation_id == audience and entry.status == "user_claim"}
    if understanding is not None and not set(understanding.source_ids) <= visible:
        understanding = None
    return ResponseSemanticContext(mode=mode, audience=audience, trigger_source_ids=sorted(set(trigger_ids)),
        understanding=understanding, must_address=plan.must_address if plan else "")


def rejected(final):
    return (final.validation is not None and final.validation.verdict == "revise"
            and "reply_semantic_mismatch" in final.validation.violations)


def guard_wait(result, trace):
    trace.add_counter("reply_semantic_rejected")
    return result.model_copy(update={"action": "wait", "reply": None, "reply_segments": [],
        "outcome": "guard_wait", "decision_source": "model", "error_type": None,
        "reason": "reply_semantic_rejected: 重写后仍明确偏离当前原意，本轮不发送。"})


class SemanticReplyRejected(Exception):
    """Content rejection, distinct from provider failure and error notices."""
