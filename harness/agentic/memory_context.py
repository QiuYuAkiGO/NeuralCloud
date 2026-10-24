"""Build the displayed and authorized context for background memory proposals."""
from copy import deepcopy

from harness.agentic.store import canonical


def memory_update_payload(views, recall, proposal):
    """Retain displayed targets and distinguish current evidence from judgments.

    Only exact current source IDs are removed from the recalled episode list.
    Existing judgments stay visible even when backed by the same original.
    The manifest still validates every displayed ID/revision/text against the
    audience's current records; listing an ID alone never grants write access.
    """
    current_sources = {view["canonical_source_id"] for view in views}
    recalled = deepcopy(recall)
    recalled["entries"] = [
        entry for entry in recalled.get("entries", [])
        if not (entry.get("kind") == "episode" and entry.get("record_id") in current_sources)
    ]
    return {
        "evidence_views": deepcopy(views),
        "memories": [{
            "source_ids": [entry["record_id"] for entry in recalled["entries"]],
            "text": canonical({"derived_memory": recalled}),
        }],
        "candidate_request": proposal,
        "selection_basis": (
            "evidence_views 是本次待评估的原始资料；原始资料已入库不表示已提取长期判断。"
            "判断重复须核对已保存判断的内容与来源；再次召回原始消息不构成新的独立证据。"
            "memories 只是当前受众可见的有限召回，未召回不代表其他记忆不存在。"
        ),
    }
