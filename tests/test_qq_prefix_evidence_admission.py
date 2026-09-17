"""Escaped history in split prefix JSON remains exact, scoped evidence."""
import json

import pytest

from harness.core.inference import ContextSegment, MemoryEvidenceContext, MemoryEvidenceEntry


@pytest.mark.parametrize("text", ['完整原文\n包含"引号"和\\斜线', '多字节🙂\t制表'])
def test_multiple_prefix_json_values_admit_exact_escaped_text(text):
    serialized = "\n".join(json.dumps(value, ensure_ascii=False) for value in (
        {"group_id": "10001"}, {"records": [{"record_id": "history:1", "text": text}]}, {"turn": "当前问题"}))
    segment = ContextSegment(kind="prefix_user", why="qq_stable_prefix", text=serialized, source_ids=["history:1"])
    entry = MemoryEvidenceEntry(source_id="history:1", text=text, audience="qq:group:10001",
        source_channel="qq_group", source_refs=["trusted:1"], status="user_claim")
    evidence = MemoryEvidenceContext.bind(audience="qq:group:10001", entries=[entry], segment=segment)
    assert evidence.admitted([segment], trigger_ref="current:1").entries == evidence.entries
    assert not evidence.admitted([segment.model_copy(update={"source_ids": []})], trigger_ref="current:1").entries
    changed = segment.model_copy(update={"text": serialized.replace("完整原文", "篡改原文") + " "})
    assert not evidence.admitted([changed], trigger_ref="current:1").entries
    assert not evidence.admitted([segment], trigger_ref="trusted:1").entries
