"""Synthetic desktop navigation only; no real UI, private config or sends."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from harness.qq.accessibility import AccessibilityUnavailable, _Tree
from harness.qq.desktop import DesktopError, GroupVerification
from harness.qq.navigation import recent_group_target, search_entry, search_group_target
from tests import test_qq_accessibility as ax
from tests.test_qq_desktop import backend as backend, frame, screen


NAME = "超级豪人大宅门"
ALLOWED = ["10001", "1060979317"]


def tree_with_navigation(*, query="", focused=False, name=NAME + "🚪", search_result=False):
    tree = ax.make_tree()
    tree["nodes"] += [
        ax.node("search", "root", "q-search main-search__search-input", bounds=(68, 79, 192, 28)),
        ax.node("search-entry", "search", text=query, role="entry", bounds=(94, 83, 128, 20)),
        ax.node("recent", "root", "recent-contact-list", bounds=(56, 119, 250, 597)),
        ax.node("row", "recent", "recent-contact-item", bounds=(56, 119, 250, 64)),
        ax.node("info", "row", "item__info", bounds=(120, 131, 174, 40)),
        ax.node("row-name", "info", text=name, role="static", bounds=(120, 133, 120, 16)),
        ax.node("summary", "info", "summary-main", NAME, bounds=(120, 155, 158, 16)),
    ]
    entry = ax.get(tree, "search-entry")
    entry["attributes"] = {"tag": "input"}
    entry["states"] += ["editable"] + (["focused"] if focused else [])
    if search_result:
        tree["nodes"] += [
            ax.node("results", "root", "search-content", bounds=(68, 111, 296, 160)),
            ax.node("result", "results", "list-item search-result-item empty-summary", role="list item",
                    bounds=(69, 156, 294, 56)),
        ]
        ax.get(tree, "result")["name"] = name + " (31人)"
    return tree


def test_recent_title_accepts_decorative_emoji_but_never_matches_preview_or_partial_name():
    tree = tree_with_navigation()
    assert recent_group_target(_Tree(tree), NAME).label == NAME + "🚪"
    ax.get(tree, "row-name")["text"] = "Another group"
    assert recent_group_target(_Tree(tree), NAME) is None
    ax.get(tree, "row-name")["text"] = "⫰⫠PHANTOM-C"
    assert recent_group_target(_Tree(tree), "PHANTOM-C").label == "⫰⫠PHANTOM-C"
    ax.get(tree, "row-name")["text"] = NAME + "二群"
    assert recent_group_target(_Tree(tree), NAME) is None


@pytest.mark.parametrize("fault", ["hidden", "protected", "outside", "duplicate"])
def test_recent_group_rejects_unavailable_or_ambiguous_candidate(fault):
    tree = tree_with_navigation()
    if fault == "hidden":
        ax.get(tree, "row")["states"] = []
        ax.get(tree, "info")["states"] = []
        ax.get(tree, "row-name")["states"] = []
    elif fault == "protected":
        ax.get(tree, "row")["protected"] = True
    elif fault == "outside":
        ax.get(tree, "row")["rect"]["y"] = 700
    else:
        duplicate = [deepcopy(ax.get(tree, ident)) for ident in ("row", "info", "row-name")]
        for node in duplicate:
            node["node_id"] += "-2"
            if node["parent_id"] != "recent":
                node["parent_id"] += "-2"
        tree["nodes"].extend(duplicate)
    if fault in {"protected", "duplicate"}:
        with pytest.raises(AccessibilityUnavailable):
            recent_group_target(_Tree(tree), NAME)
    else:
        assert recent_group_target(_Tree(tree), NAME) is None


def test_search_requires_exact_query_and_local_group_count_not_global_or_person():
    tree = tree_with_navigation(query=NAME, focused=True, search_result=True)
    assert search_group_target(_Tree(tree), NAME).label == NAME + "🚪 (31人)"
    assert search_entry(_Tree(tree), focused=True)["node_id"] == "search-entry"
    with pytest.raises(AccessibilityUnavailable, match="query_changed"):
        search_group_target(_Tree(tree), "another query")
    result = ax.get(tree, "result")
    result["name"] = NAME
    assert search_group_target(_Tree(tree), NAME) is None
    result["name"] = NAME + " (31人)"
    result["attributes"]["class"] += " global"
    assert search_group_target(_Tree(tree), NAME) is None


@pytest.mark.parametrize("fault", ["focus", "geometry", "not_entry", "other_ancestor"])
def test_search_focus_cannot_be_confused_with_chat_editor(fault):
    tree = tree_with_navigation(focused=True)
    entry = ax.get(tree, "search-entry")
    if fault == "focus":
        entry["states"].remove("focused")
    elif fault == "geometry":
        entry["rect"]["y"] = 549
    elif fault == "not_entry":
        entry["role"] = "section"
    else:
        entry["parent_id"] = "group"
    with pytest.raises(AccessibilityUnavailable):
        search_entry(_Tree(tree), focused=True)


def prepare(desktop, monkeypatch, *, draft="", tree=None):
    desktop.config = replace(desktop.config, accessibility_enabled=True)
    tree = tree or tree_with_navigation()
    index = 0

    def capture():
        nonlocal index
        index += 1
        return replace(frame(desktop, f"nav-{index}"), a11y_tree=tree)

    def inspect(item):
        desktop._a11y_frames.add(item.capture_id)
        return screen(item, draft=draft)

    def verify(item=None):
        item = item or capture()
        verified = GroupVerification(desktop.config.expected_group_id, NAME, [item.evidence_ref], item, inspect(item))
        desktop._group = verified
        return verified

    monkeypatch.setattr(desktop, "capture", capture)
    monkeypatch.setattr(desktop, "inspect", inspect)
    monkeypatch.setattr(desktop, "verify_group", verify)
    monkeypatch.setattr("harness.qq.navigation.time.sleep", lambda _: None)
    return tree


def test_selected_recent_candidate_must_reverify_numeric_group_and_reset_identity(backend, monkeypatch):
    desktop, calls = backend
    prepare(desktop, monkeypatch)
    old_epoch = desktop._identity_epoch
    verified = desktop.select_group("1060979317", allowed_group_ids=ALLOWED, display_name=NAME)
    assert verified.group_id == desktop.config.expected_group_id == "1060979317"
    assert not desktop._navigation_unverified and desktop._identity_epoch != old_epoch
    assert [call["kind"] for call in calls] == ["click"]
    assert calls[0]["y"] == 151


@pytest.mark.parametrize("draft", ["preserve this", None])
def test_nonempty_or_unreadable_draft_blocks_all_navigation_actions(backend, monkeypatch, draft):
    desktop, calls = backend
    prepare(desktop, monkeypatch, draft=draft)
    with pytest.raises(DesktopError, match="draft_present_or_unreadable"):
        desktop.select_group("1060979317", allowed_group_ids=ALLOWED, display_name=NAME)
    assert calls == [] and desktop.config.expected_group_id == "10001"


def test_owned_or_uncertain_delivery_blocks_switch_even_with_empty_visible_editor(backend, monkeypatch):
    desktop, calls = backend
    prepare(desktop, monkeypatch)
    desktop._owned_draft = ("pending", "10001", desktop.config.window_id)
    with pytest.raises(DesktopError, match="delivery_pending"):
        desktop.select_group("1060979317", allowed_group_ids=ALLOWED, display_name=NAME)
    assert calls == []


def test_allowlist_rejection_happens_before_any_capture(backend, monkeypatch):
    desktop, calls = backend
    monkeypatch.setattr(desktop, "capture", lambda: pytest.fail("must not capture"))
    with pytest.raises(DesktopError, match="not_allowlisted"):
        desktop.select_group("99999", allowed_group_ids=ALLOWED, display_name=NAME)
    assert calls == []


def test_wrong_numeric_profile_after_switch_keeps_send_blocked(backend, monkeypatch):
    desktop, calls = backend
    prepare(desktop, monkeypatch)

    def reject(*args):
        raise DesktopError("group_profile_not_verified")

    monkeypatch.setattr(desktop, "verify_group", reject)
    with pytest.raises(DesktopError, match="group_profile_not_verified"):
        desktop.select_group("1060979317", allowed_group_ids=ALLOWED, display_name=NAME)
    assert desktop._navigation_unverified and desktop._group is None
    result = desktop.deliver("never send", reservation_id="test", expected_content_digest="a" * 64,
                             before_click=lambda: pytest.fail("must not reserve"))
    assert result.status == "not_sent" and result.reason_code == "group_navigation_not_verified"
    assert len(calls) == 1


def test_changed_recent_row_does_not_click_or_fall_back_to_other_row(backend, monkeypatch):
    desktop, calls = backend
    tree = prepare(desktop, monkeypatch)
    original = desktop.capture
    count = 0

    def capture():
        nonlocal count
        count += 1
        if count > 1:
            ax.get(tree, "row-name")["text"] = "some other group"
        return original()

    monkeypatch.setattr(desktop, "capture", capture)
    with pytest.raises(DesktopError, match="target_changed"):
        desktop.select_group("1060979317", allowed_group_ids=ALLOWED, display_name=NAME)
    assert calls == [] and desktop._navigation_unverified


def test_user_search_text_is_preserved(backend, monkeypatch):
    desktop, calls = backend
    prepare(desktop, monkeypatch, tree=tree_with_navigation(query="personal search", focused=True))
    with pytest.raises(DesktopError, match="search_in_use"):
        desktop.select_group("1060979317", allowed_group_ids=ALLOWED, display_name=NAME)
    assert calls == []


def test_startup_config_group_is_not_treated_as_actual_open_group(backend, monkeypatch):
    desktop, calls = backend
    prepare(desktop, monkeypatch)
    # At startup the configuration says home, while the visible UI could be
    # anywhere. With no prior numerical proof it must locate the home row.
    verified = desktop.select_group("10001", allowed_group_ids=ALLOWED, display_name=NAME)
    assert verified.group_id == "10001" and len(calls) == 1


def test_same_group_shortcut_requires_the_current_title_to_match_previous_proof(backend, monkeypatch):
    desktop, calls = backend
    prepare(desktop, monkeypatch)
    old = frame(desktop, "old")
    desktop._group = GroupVerification("10001", "Old group", [old.evidence_ref], old, screen(old))
    verified = desktop.select_group("10001", allowed_group_ids=ALLOWED, display_name=NAME)
    assert verified.group_id == "10001" and len(calls) == 1


def test_name_search_fallback_selects_only_focused_local_group_result(backend, monkeypatch):
    desktop, calls = backend
    tree = tree_with_navigation(name="unrelated")
    prepare(desktop, monkeypatch, tree=tree)
    act = desktop._act

    def update(primitive, **kwargs):
        result = act(primitive, **kwargs)
        entry = ax.get(tree, "search-entry")
        if primitive["kind"] == "click" and primitive["y"] == 93:
            entry["states"].append("focused")
        if primitive["kind"] == "type_text":
            entry["text"] = primitive["text"]
            complete = tree_with_navigation(query=NAME, focused=True, search_result=True)
            tree["nodes"].extend([node for node in complete["nodes"] if node["node_id"] in {"results", "result"}])
        return result

    monkeypatch.setattr(desktop, "_act", update)
    verified = desktop.select_group("1060979317", allowed_group_ids=ALLOWED, display_name=NAME)
    assert verified.group_id == "1060979317"
    assert [call["kind"] for call in calls] == ["click", "type_text", "click"]
    assert calls[1]["text"] == NAME
    assert all(call.get("y", 0) < 545 for call in calls)


def test_fresh_backend_already_showing_named_home_does_not_toggle_selected_recent_row(backend, monkeypatch):
    desktop, calls = backend
    prepare(desktop, monkeypatch)
    inspect = desktop.inspect

    def home_screen(item):
        result = inspect(item)
        return result.model_copy(update={"group_title": result.group_title.model_copy(update={"display_name": "⫰⫠PHANTOM-C(17)"})})

    monkeypatch.setattr(desktop, "inspect", home_screen)
    verified = desktop.select_group("10001", allowed_group_ids=ALLOWED, display_name="PHANTOM-C")
    assert verified.group_id == "10001" and calls == []


@pytest.mark.parametrize("fault", [None, "draft", "overlay", "scope", "stale", "missing_panel"])
def test_empty_qq_landing_panel_can_select_home_only_with_complete_verified_ax(backend, monkeypatch, fault):
    desktop, calls = backend
    tree = prepare(desktop, monkeypatch)
    retained = {"root", "search", "search-entry", "recent", "row", "info", "row-name", "summary"}
    tree["nodes"] = [node for node in tree["nodes"] if node["node_id"] in retained]
    tree["nodes"].append(ax.node("empty", "root", "empty-panel empty-aio", bounds=(306, 67, 970, 649)))
    if fault == "draft":
        tree["nodes"].append(ax.node("editor", "root", "ProseMirror", "preserve", bounds=(306, 549, 790, 111)))
    if fault == "overlay":
        tree["nodes"].append(ax.node("dialog", "root", role="dialog"))
    if fault == "missing_panel":
        ax.get(tree, "empty")["attributes"]["class"] = "unknown-panel"
    captured = desktop.capture

    def capture():
        item = captured()
        tree["scope"]["window_id"] = desktop.config.window_id
        for node in tree["nodes"]:
            node["window_id"] = desktop.config.window_id
        tree["frame_captured_at"] = item.captured_at.isoformat()
        tree["started_at"] = item.captured_at.isoformat()
        tree["finished_at"] = (item.captured_at + timedelta(milliseconds=10)).isoformat()
        if fault == "scope":
            tree["scope"]["app_id"] = "wrong-app"
        if fault == "stale":
            tree["frame_captured_at"] = (item.captured_at - timedelta(minutes=1)).isoformat()
        return replace(item, a11y_evidence_ref="evidence://sha256/" + "b" * 64)

    monkeypatch.setattr(desktop, "capture", capture)
    if fault == "missing_panel":
        monkeypatch.setattr(desktop, "inspect", lambda _: (_ for _ in ()).throw(DesktopError("unambiguous_group_chat_required")))
    if fault:
        with pytest.raises(DesktopError):
            desktop.select_group("10001", allowed_group_ids=ALLOWED, display_name=NAME)
        assert calls == []
    else:
        verified = desktop.select_group("10001", allowed_group_ids=ALLOWED, display_name=NAME)
        assert verified.group_id == "10001" and [call["kind"] for call in calls] == ["click"]
