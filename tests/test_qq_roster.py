from types import SimpleNamespace

import pytest

from harness.qq.roster import QQGroupRosterEntry, configured_groups


@pytest.mark.parametrize("send", [False, True])
@pytest.mark.parametrize("explicit_none", [False, True])
def test_legacy_scope_preserves_home_send_configuration(send, explicit_none):
    values = {"group_id": "10001", "automatic_send": send}
    if explicit_none:
        values["group_roster"] = None
    expected = (QQGroupRosterEntry(group_id="10001", observe=True, send=send),)
    assert configured_groups(values) == expected
    assert configured_groups(SimpleNamespace(**values)) == expected


def test_roster_preserves_order_and_observation_only_groups():
    roster = [{"group_id": "10002", "label": "Other group"},
              {"group_id": "10001", "send": True},
              {"group_id": "10003", "observe": False}]
    entries = configured_groups({"group_id": "10001", "automatic_send": True, "group_roster": roster})
    assert tuple(entry.group_id for entry in entries) == ("10002", "10001", "10003")
    assert entries[0].observe and not entries[0].send
    assert not entries[2].observe and not entries[2].send
    assert configured_groups(SimpleNamespace(group_id="10001", automatic_send=True, group_roster=entries)) == entries


@pytest.mark.parametrize("roster", [
    [], {}, "10001", [{"group_id": "10002"}],
    [{"group_id": "10001", "observe": False}],
    [{"group_id": "10001"}, {"group_id": "10001"}],
    [{"group_id": "10001"}, {"group_id": "10002", "send": True}],
    [{"group_id": "10001", "observe": "true"}],
    [{"group_id": "10001", "send": 1}],
    [{"group_id": 10001}], [{"group_id": "010001"}],
    [{"group_id": "10001", "label": None}],
    [{"group_id": "10001", "label": "x" * 129}],
    [{"group_id": "10001", "label": "name\nmessage"}],
    [{"group_id": "10001", "message": "not operator metadata"}],
    [{"group_id": str(10001 + i)} for i in range(17)],
])
def test_roster_rejects_ambiguous_or_unauthorized_configuration(roster):
    with pytest.raises(ValueError):
        configured_groups({"group_id": "10001", "automatic_send": True, "group_roster": roster})


def test_global_send_disable_cannot_be_overridden_by_roster():
    with pytest.raises(ValueError, match="qq_group_roster_send_scope_invalid"):
        configured_groups({"group_id": "10001", "automatic_send": False,
                           "group_roster": [{"group_id": "10001", "send": True}]})


@pytest.mark.parametrize("value", ["true", 1, None])
def test_legacy_global_send_is_strict(value):
    with pytest.raises(ValueError):
        configured_groups({"group_id": "10001", "automatic_send": value})
