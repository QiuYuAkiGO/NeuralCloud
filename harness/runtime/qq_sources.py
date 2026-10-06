"""Read one exact QQ trigger after validating its current transport authority."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


def _reply_references(request, sources, config):
    """Resolve actual protocol reply segments within this account/conversation."""
    from harness.qq.direct_config import DirectQQSettings
    from harness.qq.direct_store import numeric
    from harness.qq.feedback import _observation, _readonly

    if request.transport != "onebot":
        return []
    direct = DirectQQSettings.read(Path(os.environ["NC_QQ_DIRECT_CONFIG"]))
    message_ids = []
    for source in sources:
        refs = [ref for ref in source.get("attribution_evidence", [])
                if re.fullmatch(r"qq-protocol://sha256/[0-9a-f]{64}", ref)]
        for ref in refs:
            path = direct.evidence_dir / (ref.rsplit("/", 1)[1] + ".json")
            if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("qq_reply_reference_evidence_invalid")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref.rsplit("/", 1)[1]:
                raise ValueError("qq_reply_reference_evidence_changed")
            event = json.loads(raw)
            for item in event.get("message", []):
                if isinstance(item, dict) and item.get("type") == "reply":
                    message_ids.append(numeric(item["data"]["id"], signed=True))
    record_ids = []
    with closing(sqlite3.connect(direct.state_db.as_uri() + "?mode=ro", uri=True)) as db, closing(_readonly(config.memory_db)) as memory:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        for message_id in dict.fromkeys(message_ids):
            row = db.execute("SELECT source_record_id,member_id FROM qq_direct_events "
                "WHERE self_id=? AND group_id=? AND message_id=?",
                (request.self_member_id, request.group_id, message_id)).fetchone()
            if row is None or row["source_record_id"] is None:
                continue  # Unknown quoted history grants no invented source.
            item = _observation(memory, request.group_id, row["source_record_id"])
            if item is None or item.message_id != message_id or item.member_id != row["member_id"]:
                raise ValueError("qq_reply_reference_source_invalid")
            record_ids.append(item.record_id)
    return list(dict.fromkeys(record_ids))


def verified_qq_source(request, config=None):
    from harness.qq.attention import require_attention
    attention_revision = require_attention(request.group_id)
    from harness.qq.feedback import _observation, _readonly
    from harness.qq.session import QQSessionSettings
    from harness.runtime.qq_turns import _source

    if config is None:
        path = os.environ.get("NC_QQ_SESSION_CONFIG")
        if not path:
            raise ValueError("qq_source_configuration_missing")
        config = QQSessionSettings.model_validate_json(Path(path).read_text(encoding="utf-8"))
    status_file = getattr(config, "watcher_status_file", None)
    watcher = Path(os.environ["NC_QQ_WATCHER_CONFIG"]) if os.environ.get("NC_QQ_WATCHER_CONFIG") else (
        status_file.parent / "watcher.json" if status_file else None)
    batch = None
    if getattr(request, "request_id", None) is not None:
        from harness.qq.direct_config import DirectQQSettings
        from harness.qq.reply_requests import validate_reply_request
        direct = DirectQQSettings.read(Path(os.environ["NC_QQ_DIRECT_CONFIG"]))
        batch = validate_reply_request(direct.state_db, request, memory_config=config)
    _source(config, watcher, request)
    if request.idle_intent is not None:
        value = request.idle_intent.model_dump(mode="json")
        text = ""
    else:
        with closing(_readonly(config.memory_db)) as db:
            rows = db.execute("SELECT record_id FROM qq_visual_observations "
                "WHERE group_id=? AND capture_item_id=? AND member_id=?",
                (request.group_id, request.trace.event_id, request.trigger_member_id)).fetchall()
            if len(rows) != 1:
                raise ValueError("qq_source_not_unique")
            observation = _observation(db, request.group_id, rows[0]["record_id"])
            if observation is None or observation.capture_item_id != request.trace.event_id:
                raise ValueError("qq_source_observation_invalid")
            value = observation.model_dump(mode="json")
            text = observation.text or ""
    frozen = {"anchor": value, "supplements": batch["sources"]} if batch is not None else value
    digest = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    if batch is not None:
        text = "\n".join(item.get("text", "") for item in batch["sources"])
    references = _reply_references(request, batch["sources"] if batch is not None else [value], config)
    require_attention(request.group_id, revision=attention_revision)
    return {"digest": digest, "text": text, "source": value, "reply_reference_record_ids": references}
