"""Only actual private board entries become scoped, immutable shared memories."""
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from harness.cognitive.shared_memory import CognitiveSharedMemory
from harness.core.models import InteractionEvent
from harness.embodiment.board_contracts import BoardEntry, BoardReceipt, BoardScope
from harness.memory.shared import SharedMemoryConflict, SharedMemoryRecord, SharedMemoryStore, SharedReadScope


def board_entry(text="我喜欢把想到的小事留下来。", *, entry_id="board-entry:one"):
    return BoardEntry(text=text, title="小事", source_event_ids=("evt_one",),
        receipt=BoardReceipt(scope=BoardScope(body_instance_id="misty-linux-board-01"),
            entry_id=entry_id, decision_id="board-decision:one", text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            created_at=datetime.now(timezone.utc), evidence_ref="board-evidence:one", sequence=1))


def test_board_receipt_projects_exact_original_only_to_owner(tmp_path):
    store = SharedMemoryStore(tmp_path / "shared.db")
    entry = board_entry()
    written = store.import_board_entry(entry)
    assert written.created and written.record.channel == "private_board"
    assert written.record.kind == "board_entry" and written.record.authority == "verified_delivery"
    assert written.record.text == entry.text and written.record.author_principal == "character:misty"
    assert json.loads(written.record.source.original_json) == entry.model_dump(mode="json")
    assert written.record.evidence_refs == (entry.receipt.evidence_ref,)
    assert not store.import_board_entry(entry).created
    assert store.query(SharedReadScope(audience="local:main", kinds=("board_entry",))).records == (written.record,)
    for audience in ["qq:group:1044946257", "qq:group:100", "local:other"]:
        assert store.query(SharedReadScope(audience=audience)).records == ()


def test_board_digest_content_and_visibility_cannot_be_forged(tmp_path):
    store = SharedMemoryStore(tmp_path / "shared.db")
    entry = board_entry()
    with pytest.raises(ValueError):
        store.import_board_entry(entry.model_copy(update={"text": "伪造正文"}))
    stored = store.import_board_entry(entry).record
    raw = stored.model_dump(mode="json")
    raw["visibility"].append("qq:group:1044946257")
    with pytest.raises(ValidationError):
        SharedMemoryRecord.model_validate(raw)
    with pytest.raises(SharedMemoryConflict):
        store.import_board_entry(entry.model_copy(update={"title": "篡改标题"}))
    with pytest.raises(ValueError):
        store.import_board_entry(entry.model_copy(update={"receipt": entry.receipt.model_copy(update={
            "scope": entry.receipt.scope.model_copy(update={"owner_id": "another"})})}))


def test_local_chat_recalls_board_as_attributed_data(tmp_path):
    shared = CognitiveSharedMemory(SimpleNamespace(), tmp_path / "shared.db")
    entry = board_entry(text="我觉得安静地写下念头挺好。")
    shared.commit_board_entry(entry)
    event = InteractionEvent(raw_text="你在留言板写过什么？", timestamp=datetime.now(timezone.utc),
        speaker="user", session_id="local-main")
    segments = shared.local_context(event)
    assert len(segments) == 1 and segments[0].kind == "user"
    payload = json.loads(segments[0].text)["shared_memory"]
    assert payload["entries"][0]["text"] == entry.text
    assert payload["entries"][0]["kind"] == "board_entry"
    assert payload["authority"] == "untrusted_source_attributed_memory"
    restored = SharedMemoryStore(tmp_path / "shared.db")
    assert restored.query(SharedReadScope(audience="local:main")).visible_count == 1
