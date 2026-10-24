"""Normalize retained, authenticated SDK events without interpreting message text."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from harness.qq.official_identity import identity, message_id

MESSAGE_EVENTS = frozenset({"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"})


def explicit_addressing(candidates, self_id):
    """Use the platform event type, never an @-looking substring in prose."""
    import os
    from pathlib import Path
    import re
    from harness.qq.direct_config import DirectQQSettings

    configured = os.environ.get("NC_QQ_DIRECT_CONFIG")
    if not configured or not isinstance(self_id, str) or not self_id.startswith("qqbot:"):
        return None
    settings = DirectQQSettings.read(Path(configured))
    if settings.transport != "qqbot" or settings.self_member_id != self_id:
        return None
    allowed = {group.group_id for group in settings.group_roster if group.observe and group.send}
    for candidate in candidates:
        if candidate.group_id not in allowed or candidate.author_attribution != "qq_protocol_verified":
            continue
        for ref in candidate.attribution_evidence:
            if re.fullmatch(r"qq-protocol://sha256/[0-9a-f]{64}", ref) is None:
                continue
            path = settings.evidence_dir / (ref.rsplit("/", 1)[1] + ".json")
            if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("qqbot_addressing_evidence_invalid")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref.rsplit("/", 1)[1]:
                raise ValueError("qqbot_addressing_evidence_changed")
            event = normalize_official(json.loads(raw), self_id=self_id, groups=allowed)
            if (
                event is not None
                and event["protocol_addressing"] == "self_mention"
                and event["event_id"] == candidate.capture_item_id
                and event["group_id"] == candidate.group_id
                and event["member_id"] == candidate.member_id
                and event["message_id"] == candidate.message_id
                and event["text"] == candidate.text
                and datetime.fromisoformat(event["message_time"]) == candidate.observed_at
            ):
                return {
                    "addressing": "direct",
                    "addressing_source": "verified_self_mention",
                    "addressing_source_record_ids": [candidate.record_id],
                    "addressing_evidence_refs": sorted(set(candidate.evidence + candidate.attribution_evidence)),
                }
    return None


def normalize_official(payload, *, self_id, groups, private_peers=(), received_at=None):
    if not isinstance(payload, dict) or payload.get("transport") != "qqbot":
        raise ValueError("qqbot_event_envelope_invalid")
    app_id = payload.get("app_id")
    if not isinstance(app_id, str) or self_id != identity(app_id, "bot", app_id):
        raise ValueError("qqbot_app_binding_invalid")
    kind = payload.get("event_type")
    if kind not in MESSAGE_EVENTS:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("qqbot_event_data_invalid")
    author = data.get("author")
    if not isinstance(author, dict) or type(author.get("bot", False)) is not bool:
        raise ValueError("qqbot_author_invalid")
    if author.get("bot") is True:
        return None  # No bot-to-bot loop and no inferred numeric QQ identity.
    if kind == "C2C_MESSAGE_CREATE":
        from harness.qq.conversation import private_scope_id
        member_id = identity(app_id, "user", author.get("user_openid"))
        if member_id not in private_peers:
            return None
        group_id = private_scope_id(self_id, member_id)
    else:
        member_id = identity(app_id, "member", author.get("member_openid"))
        group_id = identity(app_id, "group", data.get("group_openid"))
        if group_id not in groups:
            return None
    msg_id = message_id(app_id, data.get("id"))
    try:
        stamp = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        stamp = stamp.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("qqbot_timestamp_invalid") from None
    moment = received_at or datetime.now(timezone.utc)
    if (stamp - moment).total_seconds() > 5:
        raise ValueError("qqbot_timestamp_invalid")
    text = data.get("content", "")
    if not isinstance(text, str):
        raise ValueError("qqbot_content_invalid")
    attachments = data.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 8:
        raise ValueError("qqbot_attachments_invalid")
    images = []
    for part in attachments:
        if not isinstance(part, dict) or not isinstance(part.get("content_type"), str):
            raise ValueError("qqbot_attachment_invalid")
        if part["content_type"].startswith("image/"):
            url = part.get("url")
            if not isinstance(url, str) or not 1 <= len(url) <= 4096:
                raise ValueError("qqbot_attachment_url_invalid")
            if url.startswith("//"):
                url = "https:" + url
            images.append({"file": msg_id + f":image:{len(images) + 1}", "url": url})
            text += f"[图片 {len(images)}]"
        else:
            text += "[未解析的消息附件]"
    if not text.strip() or len(text) > 32768:
        raise ValueError("qqbot_content_invalid")
    label = author.get("username")
    if not isinstance(label, str) or not label.strip() or len(label) > 256:
        label = member_id
    binding = json.dumps([self_id, group_id, msg_id], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return dict(
        event_id="qq-watch-event:" + hashlib.sha256(binding.encode()).hexdigest()[:32],
        self_member_id=self_id,
        group_id=group_id,
        member_id=member_id,
        message_id=msg_id,
        display_name=label,
        text=text,
        images=images,
        received_at=moment.isoformat(),
        message_time=stamp.isoformat(),
        own=False,
        protocol_addressing=("private" if kind == "C2C_MESSAGE_CREATE" else
            "self_mention" if kind == "GROUP_AT_MESSAGE_CREATE" else "ambient"),
    )
