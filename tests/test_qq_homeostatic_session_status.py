from harness.qq.session import _watcher_measurements


def test_status_projects_only_bounded_homeostasis_and_negotiated_capabilities():
    value = _watcher_measurements({
        "homeostasis": {"enabled": True, "revision": 7, "salience": "marked",
            "profile_ref": "qq-social-drive-v1@1", "feedback_pending": 0,
            "feedback_applied": 3, "feedback_retired": 1, "tick_seconds": 5,
            "private_text": "never expose"},
        "qq_body": {"embodiment": {"state": "available",
            "effective_capabilities": ["communication.qq.send.v1", "private arbitrary data"],
            "negotiated": {"missing_required": [], "incompatible_required": []}, "health": {"detail": "private"}}}})
    assert value["homeostasis"]["revision"] == 7
    assert value["homeostasis"]["profile_ref"] == "qq-social-drive-v1@1"
    assert value["qq_body"] == {"state": "available", "required_satisfied": True,
                                "effective_capabilities": ["communication.qq.send.v1"]}
    assert "private" not in repr(value)


def test_status_does_not_coerce_untrusted_nested_values():
    value = _watcher_measurements({"homeostasis": {"enabled": "true", "revision": True,
        "salience": "payload", "profile_ref": "arbitrary text", "feedback_pending": -1},
        "qq_body": {"embodiment": {"state": "payload", "effective_capabilities": [None, {}],
            "negotiated": {"required_satisfied": "true"}}}})
    assert value["homeostasis"] == {}
    assert value["qq_body"] == {"effective_capabilities": []}


def test_required_capability_failures_override_any_supplied_success_label():
    value = _watcher_measurements({"qq_body": {"embodiment": {"negotiated": {
        "required_satisfied": True, "missing_required": ["communication.qq.send.v1"],
        "incompatible_required": []}}}})
    assert value["qq_body"]["required_satisfied"] is False
