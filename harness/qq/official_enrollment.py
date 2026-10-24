"""Capture a one-time, operator-issued C2C challenge without granting access."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat

from harness.qq.official_events import normalize_official
from harness.qq.official_identity import identity


def capture_enrollment(settings, payload):
    if payload.get("event_type") != "C2C_MESSAGE_CREATE":
        return False
    request = settings.qqbot_credentials_file.with_name("qqbot-private-enrollment.json")
    try:
        info = request.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            return False
        pending = json.loads(request.read_text())
        now = datetime.now(timezone.utc)
        if pending["app_id"] != settings.qqbot_app_id or now.timestamp() > pending["expires_at"]:
            return False
        text = payload.get("data", {}).get("content", "")
        if not isinstance(text, str) or not hmac.compare_digest(
                hashlib.sha256(text.strip().encode()).hexdigest(), pending["challenge_sha256"]):
            return False
        peer = identity(settings.qqbot_app_id, "user", payload["data"]["author"]["user_openid"])
        event = normalize_official(payload, self_id=settings.self_member_id,
            groups=(), private_peers=(peer,), received_at=now)
        if event is None or datetime.fromisoformat(event["message_time"]).timestamp() < pending["created_at"] - 5:
            return False
        candidate = {"app_id": settings.qqbot_app_id, "peer_id": peer,
            "requested_qq_label": pending["requested_qq_label"],
            "challenge_sha256": pending["challenge_sha256"], "message_id": event["message_id"],
            "received_at": now.isoformat(), "message_time": event["message_time"],
            "verification": "operator_challenge_in_authenticated_c2c"}
        # Only an operator updates allowlists. First matching callback wins;
        # duplicate deliveries cannot overwrite a captured binding.
        target = settings.evidence_dir / "private-enrollment-candidate.json"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(candidate, output)
            output.flush()
            os.fsync(output.fileno())
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
