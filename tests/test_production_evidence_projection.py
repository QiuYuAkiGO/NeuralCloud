"""Legacy and v2 projections preserve provenance rather than imply truth."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from harness.core.models import MemoryScope
from harness.memory.production import Experience, ProductionMemoryStore


NOW = datetime(2026, 9, 22, 9, tzinfo=timezone.utc)
AUDIENCE = "qq:group:9001"
SUBJECT = "qq:member:1001"


def source(identifier, text="我喜欢红茶。"):
    return Experience(record_id=identifier, author_id=SUBJECT, text=text, occurred_at=NOW,
        scope=MemoryScope(channel="qq_group", conversation_id=AUDIENCE, visibility=["local:main", AUDIENCE]),
        source_refs=("raw:" + identifier,), independent_key=identifier,
        original={"record": {"authority": "speaker_utterance"}})


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("counter_ref", ["second", "raw:second"])
def test_v1_canonical_overlap_rejected_before_existing_merge(tmp_path, existing, counter_ref):
    store = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    store.ingest([source("first"), source("second")])
    arguments = {"subject_id": SUBJECT, "audience": AUDIENCE, "now": NOW}
    if existing:
        store.apply_updates([{"kind": "interpretation", "record_id": "belief", "topic": "茶",
                              "statement": "暂定偏好", "source_ids": ["first"]}],
                            event_id="initial", **arguments)
    before = store.history("belief")
    with pytest.raises(ValueError, match="memory_update_support_counter_overlap"):
        store.apply_updates([{"kind": "interpretation", "record_id": "belief", "topic": "茶",
                              "statement": "新提案", "source_ids": ["second"], "counter_ids": [counter_ref]}],
                            event_id="rejected", **arguments)
    assert store.history("belief") == before
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM pm_receipts WHERE event_id='rejected'").fetchone()[0] == 0


def test_legacy_self_report_projection_is_not_verified_quote_or_probability(tmp_path):
    store = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    store.ingest([source("quoted", "小李说：我喜欢红茶。")])
    # Audited old payloads are preserved even when their semantic attribution
    # is not warranted. The projection must make the old status explicit.
    store.apply_updates([{"kind": "self_report", "record_id": "old-self-report", "topic": "茶",
                          "statement": "本人喜欢红茶", "source_ids": ["quoted"]}],
                        event_id="legacy", audience=AUDIENCE, subject_id=SUBJECT, now=NOW)
    before = store.history("old-self-report")
    recalled = next(entry for entry in store.recall(audience=AUDIENCE, now=NOW)["entries"]
                    if entry["record_id"] == "old-self-report")
    operator = store.operator_snapshot(now=NOW)["entries"][0]
    for entry in (recalled, operator):
        assert entry["authority"] == "legacy_interpreted_self_report"
        assert entry["semantic_status"] == "legacy_model_interpretation"
        assert entry["quote_verified"] is False
        assert entry["legacy_confidence"] == pytest.approx(2 / 3)
        assert "confidence" not in entry
    assert store.history("old-self-report") == before
    assert before[0]["kind"] == "self_report" and before[0]["statement"] == "本人喜欢红茶"
    assert operator["evidence"][0]["text"] == "小李说：我喜欢红茶。"


V2_TIME = datetime(2020, 1, 1, tzinfo=timezone.utc)


def v2_write(store, identifier, refs, *, at=V2_TIME, audience=AUDIENCE, role="supports", **overrides):
    """Exercise the real manifest and writer, including actual displayed targets."""
    from harness.memory.evidence_manifest import EvidenceManifestStore
    from harness.memory.evidence_v2 import EvidenceV2Store
    manifests = EvidenceManifestStore(store)
    views = manifests.prepare_views(refs, audience=audience)
    recalled = store.recall(audience=audience, now=at)
    payload = {"evidence_views": views, "memories": [{"source_ids": [entry["record_id"]],
        "text": json.dumps(entry, ensure_ascii=False)} for entry in recalled.get("entries", [])]}
    event_id = identifier + ":" + str(overrides.get("expected_revision", 0))
    manifest = manifests.issue(payload, audience=audience, command_id=event_id, now=at)
    proposal = {"record_id": identifier, "subject_id": SUBJECT, "topic": "饮品", "context": "当前偏好",
        "statement": "模型认为对方喜欢茶", "kind": "interpretation", "report_mode": "direct",
        "facet": "preference", "valid_from": at.isoformat(),
        "anchors": [{"unit_id": view["unit_id"], "start": view["start"], "end": view["end"],
                     "quote": view["text"], "role": role, "rationale": "以所展示的原话为依据"} for view in views],
        **overrides}
    receipt = EvidenceV2Store(store).apply_groups([{"group_id": "change", "claims": [proposal]}],
        manifest_id=manifest["manifest_id"], event_id=event_id, audience=audience, now=at)
    assert receipt["accepted_count"] == 1, receipt
    return receipt


def v2_entries(store, *, at=V2_TIME, audience=AUDIENCE, diagnostics=None):
    return [entry for entry in store.recall(audience=audience, now=at, diagnostics=diagnostics).get("entries", [])
            if entry["kind"] == "person"]


def test_v2_quote_projection_keeps_interpretation_separate_and_scope_bounded(tmp_path):
    store = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    text = "小李说：我喜欢红茶。"
    store.ingest([source("quote", text).model_copy(update={"occurred_at": V2_TIME})])
    v2_write(store, "reported", ["quote"], kind="self_report", report_mode="reported",
             statement="这只是对引述的暂定解释")
    stats = {}
    entry = v2_entries(store, diagnostics=stats)[0]
    assert text in entry["current_impression"]
    assert entry["model_interpretation"] == "这只是对引述的暂定解释"
    assert entry["semantic_status"] == "model_interpretation" and entry["quote_verified"] is True
    assert entry["authority"] == "source_anchored_interpretation"
    assert entry["quotations"][0]["author_id"] == SUBJECT
    assert entry["quotations"][0]["quote"] == text
    assert entry.get("confidence") is None and "legacy_confidence" not in entry
    assert stats["candidate_count"] <= 192
    assert v2_entries(store, audience="qq:group:9002") == []
    assert not store.validate_references([entry], audience="qq:group:9002", now=V2_TIME)
    assert store.validate_references([entry], audience=AUDIENCE, now=V2_TIME)
    operator = store.operator_snapshot(now=V2_TIME)["entries"][0]
    assert operator["current_impression"] == entry["current_impression"]
    assert operator["model_interpretation"] == entry["model_interpretation"]
    reopened = ProductionMemoryStore(store.path)
    assert reopened.recall(audience=AUDIENCE, now=V2_TIME) == store.recall(audience=AUDIENCE, now=V2_TIME)


def test_v2_revision_fence_pin_and_source_withdrawal_share_current_validity(tmp_path):
    store = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    store.ingest([source("source").model_copy(update={"occurred_at": V2_TIME})])
    v2_write(store, "claim", ["source"])
    old = v2_entries(store)[0]
    v2_write(store, "claim", ["source"], expected_revision=1, revision_reason="保留原话，调整解释",
             statement="喜欢茶只是当前解释")
    current = v2_entries(store)[0]
    assert not store.validate_references([old], audience=AUDIENCE, now=V2_TIME)
    assert store.validate_references([current], audience=AUDIENCE, now=V2_TIME)
    assert [row["revision"] for row in store.history("claim")] == [1, 2]
    assert store.retain("claim", event_id="pin", pinned=True)
    assert store.retain("claim", event_id="important", important_reason="对方明确表示这个偏好值得记住", evidence_ids=["source"])
    assert store.operator_snapshot(now=V2_TIME)["entries"][0]["important"] is True
    with sqlite3.connect(store.path) as db:
        archived = db.execute("SELECT payload,digest FROM pm_sources WHERE id='source'").fetchone()
        versions = db.execute("SELECT payload FROM pm_v2_versions WHERE id='claim' ORDER BY revision").fetchall()
    store.retract_source("source", event_id="withdraw", reason="本人撤回来源")
    assert v2_entries(store) == []
    assert store.operator_snapshot(now=V2_TIME)["entries"] == []
    assert not store.validate_references([current], audience=AUDIENCE, now=V2_TIME)
    with pytest.raises(ValueError, match="retention_target_unavailable"):
        store.retain("claim", event_id="repin", pinned=True)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT payload,digest FROM pm_sources WHERE id='source'").fetchone() == archived
        assert db.execute("SELECT payload FROM pm_v2_versions WHERE id='claim' ORDER BY revision").fetchall() == versions
    assert all(row["status"] == "source_withdrawn" for row in store.history("claim"))


def test_v2_preference_effective_time_and_revocation_never_restore_old_preference(tmp_path):
    store = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    moments = [V2_TIME + timedelta(days=offset) for offset in range(3)]
    for index, text in enumerate(("我现在喜欢红茶。", "我现在改喝咖啡。", "明天开始我改喝白水。")):
        identifier = f"utterance-{index}"
        # The planned change is uttered today but becomes effective tomorrow.
        occurred = moments[min(index, 1)]
        store.ingest([source(identifier, text).model_copy(update={"occurred_at": occurred})])
        v2_write(store, f"preference-{index}", [identifier], at=moments[index], kind="self_report", statement=text)
    for index, moment in enumerate(moments):
        assert [entry["record_id"] for entry in v2_entries(store, at=moment)] == [f"preference-{index}"]
    assert not store.validate_references(["preference-2"], audience=AUDIENCE, now=moments[1])
    store.retract_source("utterance-2", event_id="withdraw-latest", reason="撤回最新来源")
    assert v2_entries(store, at=moments[2]) == []
    assert v2_entries(store, at=moments[1])[0]["record_id"] == "preference-1"


def test_v2_operator_display_escapes_quotes_and_separates_semantics():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    script = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
eval(['esc', 'renderProductionMemory'].map(name => {
  const start = source.indexOf(`function ${name}(`);
  return source.slice(start, source.indexOf('\nfunction ', start + 1));
}).join('\n'));
console.log(JSON.stringify(renderProductionMemory({entries:[{subject_id:'person:1', topic:'偏好', revision:1,
 statement:'原作者说：<img src=x>',model_interpretation:'<script>interpretation</script>',
 semantic_status:'model_interpretation',quote_verified:true,evidence_state:'contested',needs_review:true,
 quotations:[{quote:'<img src=x>', author_id:'person:1',source_id:'source:1',role:'challenges'}]}]})));
"""
    path = Path(__file__).resolve().parents[1] / "gui" / "static" / "app.js"
    result = subprocess.run([node, "-e", script, str(path)], capture_output=True, text=True,
                            encoding="utf-8", timeout=10, check=True)
    html = json.loads(result.stdout)
    for expected in ("语义未核验", "模型暂定解释", "不证明语义解释或内容真实", "同时存在支持与挑战", "仍需复核", "&lt;img", "&lt;script"):
        assert expected in html
    assert "<img" not in html and "<script>" not in html
