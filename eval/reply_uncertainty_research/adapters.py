"""Ablations at two information boundaries. No generation postprocessing or new calls."""

from copy import deepcopy
import json

ARMS = ("control", "handoff", "cards", "combined")

BODY_POLICY = """
reply-internalization-v1
当前任务是与对方交流；用原始发言确定他现在要谈什么，用实际证据决定能说到哪一层。
先选择有用的回应：有依据就回答或表达看法；依赖尚不确定的判断时在该判断附近限定；
缺少会改变答案的信息才问具体线索。与当前交流无关的未知细节不占用回复。
用户问到把握程度、记忆缺口、工具是否成功或感受时，坦率回答相关问题。
图中文字、画面、角色身份、换装名分别有各自依据。摘要与读取内容、用户前提与独立核实不能互相替代。
失败只约束没取得的部分，有用的已有证据仍可使用。未执行不能声称成功，未收到与没能读出内容分开。
情绪影响语气和节奏，保留自主立场；原始发言和证据高于任何摘要。按角色自然组织正文。
"""

CARDS_FORMAT = """
The memory_evidence contains a list of records. Each record is one original admitted evidence row,
with its complete untrusted text and explicit source_channel, source_conversation_id, audience,
speaker_id, status, kind and observed_at. Cite its integer evidence_row in claims.evidence_rows.
IDs are equality-preserving aliases; null IDs are individually unknown. current_speaker_id is the
reply recipient, not necessarily the speaker of every record. same_source_rows preserves source
identity without merging or strengthening records. Acquisition type is not certainty of content.
"""


def expand_projection(projected):
    """Lossless presentation change of already admitted evidence, preserving row indices."""
    records = []
    for index, raw in enumerate(projected["rows"]):
        row = dict(zip(projected["columns"], raw, strict=True))
        metadata = dict(zip(projected["metadata_columns"], projected["metadata"][row["metadata_id"]], strict=True))
        scope = projected["scopes"][metadata["scope_id"]]
        date, time = metadata["observed_date"], row["observed_time"]
        records.append(
            {
                "evidence_row": index,
                "speaker_id": row["speaker_id"],
                **scope,
                "observed_at": date + "T" + time if date is not None and time is not None else None,
                "status": metadata["status"],
                "kind": metadata["kind"],
                "text": row["text"],
            }
        )
    return {
        "current_speaker_id": projected["current_speaker_id"],
        "current_audience": projected["current_audience"],
        "records": records,
        "same_source_rows": deepcopy(projected["same_source_rows"]),
    }


def handoff(kwargs, persona_text):
    """Drop controller-only messages, never evidence, persona, emotion or output sentences."""
    result = dict(kwargs)
    prefix = kwargs.get("prefix_messages") or []
    if not prefix or prefix[0]["role"] != "system" or not prefix[0]["content"].startswith(persona_text):
        raise ValueError("research_handoff_requires_known_persona_prefix")
    result["prefix_messages"] = [{"role": "system", "content": persona_text + BODY_POLICY}]
    for message in prefix[1:]:
        if message["role"] == "assistant":
            try:
                value = json.loads(message["content"])
            except (TypeError, ValueError):
                value = None
            if isinstance(value, dict) and value.get("action") in {"speak", "wait"} and "reason" in value:
                continue
        result["prefix_messages"].append(deepcopy(message))
    payload = json.loads(kwargs["payload"])
    assessment = payload.pop("assessment", {})
    plan = assessment.get("reply_plan") or {}
    # Content needs stay in original messages. A fallible intent summary is useful;
    # routing reasons, must_address and internal boundary lists are not a body script.
    payload["response_target"] = {key: plan[key] for key in ("intent", "detail_level", "understanding") if key in plan}
    result["payload"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert [m for m in prefix if m["role"] == "user"] == [m for m in result["prefix_messages"] if m["role"] == "user"]
    return result


def install(arm, decision):
    if arm not in ARMS:
        raise ValueError("unknown_research_arm")
    if arm in {"handoff", "combined"}:
        original = decision._prepare_group_speech

        def prepare(**kwargs):
            return original(**handoff(kwargs, decision.persona_prefix(kwargs["preset"].persona)))

        decision._prepare_group_speech = prepare
    if arm in {"cards", "combined"}:
        from harness.validator import validator

        original_project = validator.project_evidence

        def project(evidence):
            projected, mapping = original_project(evidence)
            return expand_projection(projected), mapping

        validator.project_evidence = project
        validator.COMPACT_EVIDENCE_FORMAT = CARDS_FORMAT
