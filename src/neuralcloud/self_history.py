"""Topic excerpts written by the QQ account bound by the private-channel caller."""

import json
import re
from typing import Literal

from pydantic import Field

from .contracts import Contract
from .storage import tokens


class QqSelfHistoryQuery(Contract):
    account_id: str = Field(pattern=r"^[1-9][0-9]{4,15}$")
    query: str = Field(min_length=1, max_length=256, pattern=re.compile(r"\S"))
    before: float = Field(ge=0, allow_inf_nan=False, strict=True)
    limit: int = Field(default=4, ge=1, le=16, strict=True)


class QqSelfHistoryItem(Contract):
    record_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    occurred_at: float = Field(ge=0, allow_inf_nan=False)
    origin: Literal["qq_group", "qq_private"]
    excerpt: str = Field(min_length=1, max_length=2048)


class QqSelfHistoryResult(Contract):
    character_id: str
    account_id: str
    items: list[QqSelfHistoryItem]


def _message_origin(db, record_id, source, peer):
    """Use the producing channel's sender contract. Legacy participant fields can also name a subject."""
    conversation = source["conversation_id"] or ""
    writer = source["source_instance"]
    onebot = re.fullmatch(r"(group|c2c):onebot:([1-9][0-9]*):([1-9][0-9]*)", conversation)
    if onebot:
        kind, bot, destination = onebot.groups()
        if bot == peer or (kind == "c2c" and destination != peer) or source.get("provenance"):
            return None
        if source["schema_version"] == 2:
            channel = source["channel"]
            verified = (
                writer in ("onebot:" + bot, "neuralcloud-java")
                and channel["platform"] == "qq"
                and channel["sender_id"] == peer
                and channel["account_id"] == bot
                and channel["conversation_id"] == conversation
            )
        else:
            # OneBot v1 writes exactly [event.user_id] for an incoming message.
            verified = writer == "onebot:" + bot and source["participants"] == [peer]
        if verified:
            return "qq_group" if kind == "group" else "qq_private"
        return None

    provenance = source.get("provenance")
    if (
        not provenance
        or source["schema_version"] != 1
        or provenance["semantic_status"] != "observation"
        or provenance["original_id"] != record_id
        or source["participants"] not in ([peer], ["qq:" + peer], ["qq:member:" + peer])
    ):
        return None
    group = re.fullmatch(r"qq:group:([1-9][0-9]*)", conversation)
    private = re.fullmatch(r"qq:private:([1-9][0-9]*):" + peer, conversation)
    if group and provenance["source_channel"] == "qq_group":
        prefix = f"qq-protocol-message:{group[1]}:{peer}:"
    elif private and private[1] != peer and provenance["source_channel"] == "qq_private":
        prefix = f"qq-protocol-private-message:{private[1]}:{peer}:{peer}:"
    else:
        return None
    message_refs = {
        ref for ref in provenance["external_refs"] if ref.startswith(("qq-protocol-message:", "qq-protocol-private-message:"))
    }
    if len(message_refs) != 1 or not re.fullmatch(re.escape(prefix) + r"-?[0-9]+", message_refs.pop()):
        return None
    # An ordinary ingest can carry provenance too. Only the snapshot importer owns a LEGACY_IMPORT receipt.
    imported = db.execute(
        """SELECT 1 FROM receipts r JOIN changes c ON c.seq=r.seq
           WHERE r.source=? AND r.event=? AND r.record_id=? AND c.operation='LEGACY_IMPORT'""",
        (writer, source["event_id"], record_id),
    ).fetchone()
    return provenance["source_channel"] if imported else None


def recall_self_history(store, request):
    """Rank topic candidates within the speaker's records, checking source revisions before applying the limit."""
    words = [re.compile(re.escape(word), re.IGNORECASE) for word in dict.fromkeys(request.query.split())]
    # All indexed CJK grams of a literal must occur in a matching message. Other scripts can match
    # inside a word, so use the original text for those searches as well as for final excerpt selection.
    grams = [term for term in tokens(request.query) if not term.isascii()]
    join = "JOIN search ON search.rowid=r.rowid" if grams else ""
    match = "AND search MATCH :terms" if grams else ""
    order = "rank," if grams else ""
    sql = f"""
        SELECT r.id, r.revision, r.occurred, v.payload FROM records r
        JOIN revisions v ON v.record_id=r.id AND v.revision=r.revision {join}
        WHERE r.status='ONLINE' AND r.kind='observation' AND r.occurred < :before {match}
        AND json_extract(v.payload,'$.privacy') IN ('private','public')
        AND json_extract(v.payload,'$.perception') IS NULL
        AND (json_extract(v.payload,'$.channel.sender_id')=:peer
             OR json_extract(v.payload,'$.participants[0]') IN (:peer,'qq:'||:peer,'qq:member:'||:peer))
        ORDER BY {order} r.occurred DESC, r.id
    """
    items = []
    with store.lock:
        params = {"before": request.before, "peer": request.account_id, "terms": " AND ".join(grams)}
        for record_id, revision, occurred, raw in store.db.execute(sql, params):
            source = json.loads(raw)
            content = source["content"]
            matches = [word.search(content) for word in words]
            if not all(matches):
                continue
            origin = _message_origin(store.db, record_id, source, request.account_id)
            if origin is None:
                continue
            start = max(0, min(min(match.start() for match in matches) - 128, len(content) - 2048))
            items.append(
                QqSelfHistoryItem(
                    record_id=record_id,
                    revision=revision,
                    occurred_at=occurred,
                    origin=origin,
                    excerpt=content[start : start + 2048],
                )
            )
            if len(items) == request.limit:
                break
    return QqSelfHistoryResult(character_id=store.character_id, account_id=request.account_id, items=items)
