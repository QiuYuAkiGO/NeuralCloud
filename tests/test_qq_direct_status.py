from datetime import datetime, timedelta, timezone
import json

import pytest

from harness.qq.session import read_direct_status


def test_protocol_status_is_scoped_fresh_and_content_free(tmp_path):
    path = tmp_path / "direct.status.json"
    payload = dict(service="qq-direct-onebot-v1", self_member_id="12345", running=True,
        connected=True, automatic_send=True, paused=False, desktop_owner_active=False, images_enabled=True,
        heartbeat_at=datetime.now(timezone.utc).isoformat(), state="watching",
        token="private", message="private", group_roster=[dict(group_id="23456", observe=True, send=False)],
        counts={"sends": {"sent": 2, "private message": 3, "unknown": True}})
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = read_direct_status(path, allowed=frozenset({"23456"}), self_member_id="12345")
    assert result["connected"] and result["images_enabled"]
    assert result["counts"] == {"sends": {"sent": 2}}
    assert "private" not in json.dumps(result)
    assert read_direct_status(path, allowed=frozenset({"23456"}), self_member_id="99999")["running"] is False


@pytest.mark.parametrize("change", [dict(running=False), dict(self_member_id="99999"), dict(service="other"),
    dict(heartbeat_at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())])
def test_protocol_status_rejects_stale_or_unbound_heartbeat(tmp_path, change):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(dict(service="qq-direct-onebot-v1", self_member_id="12345", running=True,
        heartbeat_at=datetime.now(timezone.utc).isoformat()) | change), encoding="utf-8")
    assert not read_direct_status(path, allowed=frozenset({"23456"}), self_member_id="12345")["running"]
