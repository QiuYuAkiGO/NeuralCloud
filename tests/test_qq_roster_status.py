from datetime import datetime, timezone
import json

from harness.qq.session import _watcher_measurements, read_watcher_status


def test_roster_status_exposes_only_authorized_metadata(tmp_path):
    moment = datetime.now(timezone.utc).isoformat()
    path = tmp_path / "status.json"
    entry = {"group_id": "10001", "label": "Home", "observe": True, "send": True,
             "last_observed_at": moment, "message_count": 4, "read_count": 2, "last_error": None}
    record = {"heartbeat_at": moment, "group_id": "10001", "self_member_id": "20001", "running": True,
              "group_roster": [entry | {"messages": ["PRIVATE"]},
                               {"group_id": "99999", "label": "PRIVATE", "observe": True, "send": False}]}
    path.write_text(json.dumps(record), encoding="utf-8")
    result = read_watcher_status(path, allowed=frozenset({"10001"}), self_member_id="20001")
    assert result["group_roster"] == [entry]
    assert "PRIVATE" not in repr(result)


def test_roster_status_does_not_coerce_or_forward_unbounded_values():
    result = _watcher_measurements({"group_roster": [
        {"group_id": "10001", "label": "PRIVATE\ncontent", "observe": "true", "send": 1,
         "last_observed_at": "PRIVATE", "message_count": True, "read_count": -1, "last_error": "PRIVATE message"},
        {"group_id": "10001", "label": "PRIVATE duplicate"}, {"group_id": 10002}, None,
    ]})
    assert result["group_roster"] == [{"group_id": "10001"}]


def test_roster_status_ignores_excessive_roster_and_preserves_missing_legacy_state():
    assert "group_roster" not in _watcher_measurements({})
    assert "group_roster" not in _watcher_measurements({"group_roster": [{}] * 17})


def test_partial_delivery_counts_are_projected_as_bounded_integers():
    valid = _watcher_measurements({"verified_segments_sent": 3, "partial_deliveries": 1})
    assert valid["verified_segments_sent"] == 3 and valid["partial_deliveries"] == 1
    invalid = _watcher_measurements({"verified_segments_sent": True, "partial_deliveries": -1})
    assert "verified_segments_sent" not in invalid and "partial_deliveries" not in invalid
