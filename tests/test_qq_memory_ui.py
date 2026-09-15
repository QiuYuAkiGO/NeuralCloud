from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / "gui" / "static"


def render(payload, *, shared=False):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    script = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const panel = {innerHTML: ''};
const $ = () => panel;
const names = ['esc', 'renderQQMemorySummary', 'qqMemoryStatusLabel', 'renderQQMemoryEvidence', 'renderQQMemoryTab', 'renderSharedMemory'];
eval(names.map(name => {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`Missing ${name}`);
  const end = source.indexOf('\nfunction ', start + 1);
  return source.slice(start, end);
}).join('\n'));
if (process.argv[2] === 'shared') {
  renderSharedMemory(payload);
  console.log(JSON.stringify(panel.innerHTML));
} else {
  console.log(JSON.stringify(renderQQMemoryTab(payload)));
}
"""
    result = subprocess.run([node, "-e", script, str(STATIC / "app.js"), "shared" if shared else "tab"],
                            input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
                            timeout=10, check=True)
    return json.loads(result.stdout)


def memory():
    return {
        "enabled": True, "recall_enabled": False, "processing": True,
        "queue": {"pending": 3, "preparing": 1, "retry": 2},
        "tiers": {"short": 8, "mid": 4, "long": 2, "forgotten": 1},
        "episodes": 15, "impressions": 1, "stable_judgments": 0, "committed_windows": 7,
        "impression_states": {"provisional": 0, "stable": 0, "disputed": 1, "dormant": 0},
        "last_completed_at": "2026-09-14T16:10:00+08:00", "last_error_type": "timeout",
        "last_duration_ms": 125.5, "last_recall_ms": 2.2,
        "impression_records": [{
            "impression_id": "impression-1", "scope": {"conversation_id": "100"},
            "kind": "person", "subject_refs": ["qq:member:200"], "topic": "互助", "context": "遇到问题时",
            "first_impression": "起初感觉他话不多", "current_impression": "后来也有主动帮忙，继续观察",
            "confidence": 0.45, "strength": 1.4, "status": "disputed",
            "evidence": [
                {"episode_id": "episode-1", "source_ids": ["message-1"], "time": "2026-09-13T10:00:00+08:00",
                 "stance": "support", "rationale": "最初只作简短回应", "statement": "第一次观察"},
                {"episode_id": "episode-2", "source_ids": ["message-2", "message-3"], "time": "2026-09-14T10:00:00+08:00",
                 "stance": "contradict", "rationale": "与初次表现有反差", "statement": "主动给出了完整解释"},
            ],
            "history": [
                {"revision": 1, "statement": "初版判断", "confidence": 0.3, "status": "provisional",
                 "evidence_episode_id": "episode-1", "evidence_stance": "support", "revised_at": "2026-09-13T10:00:00+08:00"},
                {"revision": 2, "statement": "反证后的保留判断", "confidence": 0.45, "status": "disputed",
                 "evidence_episode_id": "episode-2", "evidence_stance": "contradict", "revised_at": "2026-09-14T10:00:00+08:00"},
            ],
        }],
    }


def test_qqmemory_tab_is_registered_as_read_only_and_loads_details():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'data-tab="qqmemory">QQ记忆</button>' in html
    assert 'case "qqmemory":' in source
    assert "root.innerHTML = renderQQMemoryTab(data)" in source
    click_handler = source[source.index('$("debug-tabs").addEventListener'):source.index("function setHomeostasisStatus")]
    assert 'if (state.debug.tab === "qqmemory")' in click_handler
    assert "await loadDebugState()" in click_handler
    assert "debugPost" not in click_handler


def test_impression_tab_shows_first_current_support_contrast_and_revision_history():
    html = render({"qq_memory": memory()})
    for phrase in ("只读", "群 100", "qq:member:200", "起初感觉他话不多", "后来也有主动帮忙，继续观察",
                   "支持证据 · 1 条", "反证 · 1 条", "与初次表现有反差", "message-2 · message-3",
                   "修订历史 · 2 次", "初版判断", "反证后的保留判断", "置信度 0.45", "记忆强度 1.4",
                   "排队 3", "处理中 1", "待重试 2", "短期 8", "中期 4", "长期 2", "已淡忘 1",
                   "有争议 1", "125.5 ms", "2.2 ms", "timeout"):
        assert phrase in html
    assert html.index("初版判断") < html.index("反证后的保留判断")
    assert re.search(r"<details open>\s*<summary>反证", html)
    assert not re.search(r"<(?:input|textarea|select|button|form)\b", html)
    assert "original_json" not in html


def test_every_text_projection_escapes_hostile_content():
    value = memory()
    attack = '<img src=x onerror="bad()"><script>bad()</script>'
    record = value["impression_records"][0]
    for key in ("impression_id", "kind", "topic", "context", "first_impression", "current_impression", "status", "confidence", "strength"):
        record[key] = attack
    record["scope"]["conversation_id"] = attack
    record["subject_refs"] = [attack]
    for evidence in record["evidence"]:
        for key in ("episode_id", "time", "rationale", "statement"):
            evidence[key] = attack
        evidence["source_ids"] = [attack]
    for revision in record["history"]:
        for key in ("revision", "statement", "confidence", "status", "evidence_episode_id", "revised_at"):
            revision[key] = attack
    for key in ("last_error_type", "last_completed_at", "last_duration_ms", "last_recall_ms"):
        value[key] = attack
    value["queue"]["pending"] = attack
    value["tiers"]["long"] = attack
    html = render({"qq_memory": value})
    assert "&lt;img" in html and "&quot;bad()&quot;" in html
    assert "<img" not in html and "<script" not in html
    assert 'data-impression-id="&lt;img' in html


def test_shared_memory_card_adds_compact_queue_and_tier_summary_only():
    html = render({"shared_mind": {"owner_id": "misty", "mode": "shared", "memory_records": 8},
                   "shared_memory": {"entries": []}, "qq_memory": memory()}, shared=True)
    assert "共享记忆 · misty" in html
    assert "排队 3" in html and "中期 4" in html and "稳定判断 0" in html
    assert "调试 → QQ记忆" in html
    assert "起初感觉他话不多" not in html
    assert "修订历史" not in html


def test_continuation_is_visible_without_inflating_support_count():
    value = memory()
    value["impression_records"][0]["continuation_evidence"] = [{
        "episode_id": "same-round-2", "source_refs": ["continued-source"],
        "stance": "support", "statement": "继续同一轮对话", "independent": False,
        "observed_text": "该成员的连续发言", "time": "2026-09-14T10:01:00+08:00"}]
    html = render({"qq_memory": value})
    assert "支持证据 · 1 条" in html
    assert "同轮延续（不计独立证据） · 1 条" in html
    assert "continued-source" in html and "该成员的连续发言" in html


@pytest.mark.parametrize("payload,message", [({}, "尚未提供QQ记忆状态"),
    ({"qq_memory": {}}, "尚未形成QQ印象"),
    ({"qq_memory": {"impressions": 4}}, "印象详情尚未返回")])
def test_empty_and_legacy_gui_states_remain_readable(payload, message):
    assert message in render(payload)


def test_private_scope_counts_and_owner_grant_are_visible():
    value = memory()
    value["input_budget"] = {"estimated_tokens": 96000, "characters": 192000,
                             "recall_items": 8, "recall_characters": 16000}
    value.update(private_enabled=True, private_recall_enabled=True, owner_private_full_recall_enabled=True,
                 channels={"qq_group": {"episodes": 15, "impressions": 1},
                           "qq_private": {"episodes": 3, "impressions": 1}})
    value["impression_records"][0]["scope"] = {"channel": "qq_private", "conversation_id": "qq:private:999:200"}
    html = render({"qq_memory": value})
    for text in ("私聊 qq:private:999:200", "私聊清洗已启用", "私聊召回已启用", "所有者私聊完整召回已启用",
                 "群聊情景 15", "私聊情景 3", "私聊印象 1", "QQ资料输入预算约 96000 tokens", "8 条 / 16000 字符"):
        assert text in html
    assert "群 qq:private" not in html
