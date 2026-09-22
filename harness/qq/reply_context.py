"""Bounded style and topic context, built only from verified delivered records.

These are model hints, never a second gate, delivery proof, or memory authority.
Text excerpts are explicitly labelled and cannot authorize historical claims.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re

from harness.qq.conversation import conversation_scope


_HESITATION = re.compile(r"^(?:嗯[，,。…\s]*|唔[，,。…\s]*|让我想(?:一想|一下|想)|我想(?:一想|一下|想)|不知道怎么接)")
_MEMORY_DISCLAIMER = re.compile(r"(?:我(?:真的|确实|现在)?不记得|我没有(?:相关)?记忆|记忆(?:库)?里没有)")
_RELATIONAL = (
    re.compile(r"感动|触动|暖(?:心|暖)|心里一暖|被.{0,6}(?:戳|暖|打动)"),
    re.compile(r"不好意思|害羞|脸红|不知(?:道)?怎么(?:回|接)"),
    re.compile(r"谢谢|感谢|陪着你|陪伴|珍惜|回报|回礼"),
)


def style_diagnostics(text: str, *, detail_level: str = "brief", segments: int = 1) -> dict:
    """Cheap observable proxies only; no rewrite, drop, or emotion inference."""
    sentences = len([part for part in re.split(r"[。！？!?]+", text) if part.strip()])
    return {
        "characters": len(text), "sentences": sentences, "segments": segments,
        "brief_length_exceeded": detail_level == "brief" and len(text) > 120,
        "brief_sentence_count_exceeded": detail_level == "brief" and sentences > 3,
        "brief_segment_count_exceeded": detail_level == "brief" and segments > 2,
        "hesitant_opening": bool(_HESITATION.search(text.lstrip())),
        "memory_disclaimer": bool(_MEMORY_DISCLAIMER.search(text)),
        "relational_cycle_proxy": all(pattern.search(text) for pattern in _RELATIONAL),
    }


def select_partial_receipt_turns(receipts: list, *, max_segments: int = 60) -> list:
    """Bound retained receipts without cutting the first segment off a turn."""
    if max_segments < 3:
        raise ValueError("QQ partial receipt budget must fit one protocol turn")
    groups = {}
    for receipt in receipts:
        groups.setdefault(receipt.decision_id, []).append(receipt)
    newest = sorted(groups.values(), key=lambda group: max(
        (receipt.delivered_at, receipt.receipt_id) for receipt in group), reverse=True)
    selected = []
    for group in newest:
        if len(selected) + len(group) > max_segments:
            break
        selected.extend(group)
    return sorted(selected, key=lambda receipt: (receipt.delivered_at, receipt.receipt_id))


def delivered_style_window(*, receipts: list, partial_receipts: list, group_id: str,
                           self_member_id: str | None, as_of: datetime) -> dict:
    """Last eight sent turns, aggregating only actually delivered segments.

    Inputs are caller-validated receipt DTOs. Failed/unsent drafts never enter.
    Complete aggregate receipts replace their segment copies, not vice versa.
    """
    scope = conversation_scope(group_id)
    if scope["kind"] == "private" and self_member_id != scope["self_member_id"]:
        raise ValueError("QQ style context self account mismatch")
    turns = {}
    for receipt in receipts:
        if (receipt.group_id != group_id or receipt.self_member_id != self_member_id
                or receipt.delivered_at > as_of):
            continue
        identity = receipt.decision_id or receipt.receipt_id
        turns[identity] = {
            "turn_id": identity, "receipt_ids": [receipt.receipt_id],
            "delivered_at": receipt.delivered_at, "text": receipt.delivered_text,
            "delivery": "complete", "segments": len(receipt.delivered_segments) or 1,
        }
    partial = {}
    for receipt in partial_receipts:
        if (receipt.group_id != group_id or receipt.self_member_id != self_member_id
                or receipt.delivered_at > as_of or receipt.decision_id in turns):
            continue
        partial.setdefault(receipt.decision_id, []).append(receipt)
    for identity, records in partial.items():
        records.sort(key=lambda item: item.segment_index)
        indices = [item.segment_index for item in records]
        if indices != list(range(len(records))):
            raise ValueError("QQ style context requires a verified contiguous delivered prefix")
        turns[identity] = {
            "turn_id": identity, "receipt_ids": [item.receipt_id for item in records],
            "delivered_at": max(item.delivered_at for item in records),
            "text": "\n\n".join(item.delivered_text for item in records),
            "delivery": "partial_prefix_not_complete_answer", "segments": len(records),
        }
    selected = sorted(turns.values(), key=lambda item: (item["delivered_at"], item["turn_id"]))[-8:]
    output = []
    for turn in selected:
        text = turn.pop("text")
        # Preserve beginning and ending for structural repetition while keeping
        # eight turns bounded even when an earlier answer was an 8k essay.
        excerpt = text if len(text) <= 240 else text[:160] + "\n[中间省略，仅观察表达结构]\n" + text[-80:]
        output.append({**turn, "delivered_at": turn["delivered_at"].isoformat(),
                       "style_excerpt": excerpt, "excerpt_only": len(text) > 240,
                       "style": style_diagnostics(text, segments=turn["segments"])})
    return {"group_id": group_id, "self_member_id": self_member_id, "max_turns": 8,
            "purpose": "expression_structure_only_not_history_evidence_or_answer_coverage",
            "turns": output}


def refresh_topic_window(payload: dict, *, as_of: datetime) -> None:
    """Reference only final selected whole records; omitted text is never seen."""
    cutoff = as_of - timedelta(minutes=3)
    observations = [item for member in payload["group_context"]["members"] for item in member["observations"]]
    recent_observations = [item["record_id"] for item in observations
                           if cutoff <= datetime.fromisoformat(item["observed_at"]) <= as_of]
    receipts = [item for field in ("delivery_receipts", "segment_delivery_receipts") for item in payload.get(field, [])]
    recent_receipts = [item["receipt_id"] for item in receipts
                       if cutoff <= datetime.fromisoformat(item["delivered_at"]) <= as_of]
    payload["topic_window"] = {
        "seconds": 180, "as_of": as_of.isoformat(),
        "coverage": "selected_verified_records_only_not_complete_chat_history",
        "observation_record_ids": recent_observations, "delivery_receipt_ids": recent_receipts,
        "partial_delivery_is_not_complete_answer": True,
    }
