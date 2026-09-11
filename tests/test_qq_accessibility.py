from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from harness.qq.accessibility import (
    AccessibilityUnavailable,
    chat_change_signature,
    chat_observation_items,
    project_qq_accessibility,
)


NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
REF = "evidence://sha256/" + "a" * 64
A11Y = "evidence://sha256/" + "b" * 64
WINDOW = dict(x=0, y=27, width=1280, height=693)


def node(ident, parent, cls="", text="", bounds=(0, 27, 1280, 693), role="section", top="root", window="0x123"):
    return {
        "node_id": ident,
        "parent_id": parent,
        "top_node_id": top,
        "window_id": window,
        "app_id": "org.mistyai.QQ",
        "role": role,
        "name": "",
        "text": text,
        "rect": dict(zip(("x", "y", "width", "height"), bounds)),
        "protected": False,
        "states": ["showing", "visible"] + (["active"] if ident == "root" else []),
        "attributes": {"class": cls},
    }


def make_tree():
    nodes = [
        node("root", None, role="frame"),
        node("title", "root", "chat-header__contact-name", "Test group(7)", (20, 83, 202, 23)),
        node("group", "root", "group-chat"),
        node("area", "group", "chat-msg-area"),
        node("editor", "group", "ProseMirror is-empty", "\n", (4, 549, 1092, 111)),
        node("send", "group", "send-msg", "发送", (1023, 673, 28, 30)),
        node("m1", "area", "message-container", bounds=(4, 149, 1092, 80)),
        node("a1", "m1", "message-container__avatar", bounds=(24, 157, 32, 32)),
        node("n1", "m1", "user-name", "\ufffcAlice", (64, 155, 100, 18)),
        node("badge", "n1", "group-member-label", "owner", (64, 155, 28, 18)),
        node("name", "n1", text="Alice", bounds=(100, 155, 40, 18), role="static"),
        node("b1", "m1", "msg-content-container container--others", bounds=(64, 175, 100, 38)),
        node("t1", "b1", "message-content", "hello", (74, 183, 80, 22)),
        node("duplicate", "t1", text="hello", bounds=(74, 183, 80, 22), role="static"),
    ]
    nodes[4]["states"].append("editable")
    return {
        "schema_version": "a11y-tree-v1",
        "truncated": False,
        "nodes": nodes,
        "scope": {
            "bound": True,
            "active": True,
            "window_id": "0x123",
            "app_id": "org.mistyai.QQ",
            "window_rect": WINDOW,
            "top_node_id": "root",
            "overlays": [],
        },
        "started_at": (NOW + timedelta(milliseconds=10)).isoformat(),
        "finished_at": (NOW + timedelta(milliseconds=500)).isoformat(),
        "frame_captured_at": NOW.isoformat(),
        "semantic_digest": "c" * 64,
    }


def project(tree, **kwargs):
    return project_qq_accessibility(
        tree,
        frame_id="frame",
        evidence_ref=REF,
        a11y_evidence_ref=A11Y,
        frame_captured_at=NOW,
        window_id="0x123",
        app_id="org.mistyai.QQ",
        **kwargs,
    )


def get(tree, ident):
    return next(n for n in tree["nodes"] if n["node_id"] == ident)


def profile_tree(*, uid="QQ 12345678", marker="查看我的QQ空间"):
    tree = make_tree()
    bounds = (760, 362, 322, 348)
    tree["scope"]["overlays"] = [
        {"window_id": "0x456", "window_rect": dict(zip(WINDOW, bounds)), "top_node_id": "popup"}
    ]

    def p(ident, parent, cls, text="", rect=bounds):
        return node(ident, parent, cls, text, rect, top="popup", window="0x456")

    tree["nodes"] += [
        p("popup", None, "RootView"),
        p("card", "popup", "buddy-profile"),
        p("pname", "card", "buddy-profile__header-name", "Alice", (840, 394, 100, 24)),
        p("uid", "card", "buddy-profile__header-uid", uid, (840, 420, 150, 16)),
        p("zone", "card", "qq-zone", rect=(780, 594, 282, 36)),
        p("link", "zone", "buddy-profile__details-item-value", marker, (860, 602, 202, 20)),
    ]
    return tree


def test_chat_container_dedup_labels_and_empty_editor_have_provenance():
    result = project(make_tree())
    assert result.evidence_refs == (REF, A11Y)
    assert result.screen.visible_draft == ""
    assert len(result.screen.messages) == 1
    message = result.screen.messages[0]
    assert (message.text, message.display_name, message.direction, message.message_id) == (
        "hello",
        "Alice",
        "incoming",
        None,
    )
    assert result.chat_digest == chat_change_signature(make_tree()).chat_digest


def test_digest_ignores_draft_hover_focus_and_roster_but_changes_for_message():
    tree = make_tree()
    digest = chat_change_signature(tree).chat_digest
    get(tree, "editor")["text"] = "unsent draft"
    get(tree, "editor")["attributes"]["class"] = "ProseMirror"
    get(tree, "send")["states"].append("focused")
    tree["nodes"].append(node("roster", "root", "group-user", "unrelated", (1100, 400, 150, 32)))
    assert chat_change_signature(tree).chat_digest == digest
    assert project(tree).screen.visible_draft == "unsent draft"
    get(tree, "t1")["text"] = "new text"
    assert chat_change_signature(tree).chat_digest != digest


def draft_tree(*paragraphs):
    tree = make_tree()
    editor = get(tree, "editor")
    editor["attributes"]["class"] = "ProseMirror ExEditor-qq-msg-editor"
    editor["text"] = "\ufffc" * len(paragraphs)
    for index, text in enumerate(paragraphs):
        paragraph = node(f"p{index}", "editor", text=text,
                         bounds=(24, 554 + index * 20, 1052, 20), role="paragraph")
        paragraph["attributes"] = {"tag": "p"}
        tree["nodes"].append(paragraph)
        tree["nodes"].append(node(f"leaf{index}", paragraph["node_id"], text=text,
                                  bounds=(24, 556 + index * 20, 320, 16), role="static"))
    return tree


def test_unicode_draft_resolves_paragraph_object_without_parent_leaf_duplication():
    text = "中文标点，test 123；🙂 空格 保留"
    tree = draft_tree(text)
    result = project(tree)
    assert result.screen.visible_draft == text
    assert [message.text for message in result.screen.messages] == ["hello"]
    assert result.chat_digest == project(make_tree()).chat_digest


def test_multiple_paragraphs_keep_line_breaks_and_repeated_content():
    result = project(draft_tree("第一段", "第一段", "最后一段。"))
    assert result.screen.visible_draft == "第一段\n第一段\n最后一段。"


def test_text_segments_are_joined_once_and_missing_parent_text_can_use_complete_children():
    tree = draft_tree("中文分段")
    get(tree, "p0").pop("text")
    get(tree, "leaf0")["text"] = "中文"
    tree["nodes"].append(node("tail", "p0", text="分段", bounds=(80, 556, 60, 16), role="static"))
    assert project(tree).screen.visible_draft == "中文分段"


@pytest.mark.parametrize("fault", ["hidden", "protected", "outside", "missing_text", "leaf_object", "image", "mismatch", "missing_child", "mixed_object", "empty_object"])
def test_incomplete_or_unresolved_editor_objects_are_never_read_as_empty(fault):
    tree = draft_tree("完整草稿")
    leaf = get(tree, "leaf0")
    if fault == "hidden":
        leaf["states"] = []
    elif fault == "protected":
        leaf["protected"] = True
    elif fault == "outside":
        leaf["rect"]["y"] = 655
    elif fault == "missing_text":
        leaf.pop("text")
    elif fault == "leaf_object":
        leaf["text"] = "\ufffc"
    elif fault == "image":
        leaf["role"] = "image"
    elif fault == "mismatch":
        leaf["text"] = "不同草稿"
    elif fault == "missing_child":
        tree["nodes"] = [n for n in tree["nodes"] if n["node_id"] not in {"p0", "leaf0"}]
    elif fault == "mixed_object":
        get(tree, "editor")["text"] = "其他\ufffc"
    elif fault == "empty_object":
        get(tree, "p0")["text"] = leaf["text"] = ""
    with pytest.raises(AccessibilityUnavailable):
        project(tree)
    assert chat_change_signature(tree).chat_visible is True


def test_is_empty_class_cannot_hide_nonempty_resolved_draft():
    tree = draft_tree("实际仍有草稿")
    get(tree, "editor")["attributes"]["class"] += " is-empty"
    with pytest.raises(AccessibilityUnavailable, match="a11y_draft_state_conflict"):
        project(tree)


def empty_editor_with_virtual_break():
    tree = make_tree()
    tree["nodes"].append(node(
        "trailing-break", "editor", "ProseMirror-trailingBreak", "\n",
        (28, 1078, 0, 16), role="static",
    ))
    return tree


def test_cleared_editor_explicit_newline_and_zero_width_virtual_break_prove_empty():
    tree = empty_editor_with_virtual_break()
    result = project(tree)
    assert result.screen.visible_draft == ""
    assert [message.text for message in result.screen.messages] == ["hello"]
    assert result.chat_digest == project(make_tree()).chat_digest


def test_explicit_empty_paragraph_text_is_readable_without_placeholder_inference():
    tree = make_tree()
    get(tree, "editor")["text"] = ""
    paragraph = node("empty-paragraph", "editor", text="", bounds=(24, 554, 1052, 20), role="paragraph")
    paragraph["character_count"] = 0
    paragraph["attributes"] = {"tag": "p"}
    tree["nodes"].append(paragraph)
    assert project(tree).screen.visible_draft == ""
    # A count alone must not substitute for a successfully read Text value.
    paragraph.pop("text")
    with pytest.raises(AccessibilityUnavailable):
        project(tree)


@pytest.mark.parametrize("fault", [
    "object_parent", "missing_parent", "not_empty", "object_break", "missing_break",
    "ordinary_text", "protected", "hidden", "child", "nonzero_width", "parent_conflict",
])
def test_virtual_break_exception_cannot_hide_unreadable_or_nonempty_draft(fault):
    tree = empty_editor_with_virtual_break()
    editor, leaf = get(tree, "editor"), get(tree, "trailing-break")
    if fault == "object_parent":
        editor["text"] = "\ufffc"
    elif fault == "missing_parent":
        editor.pop("text")
    elif fault == "not_empty":
        editor["attributes"]["class"] = "ProseMirror"
    elif fault == "object_break":
        leaf["text"] = "\ufffc"
    elif fault == "missing_break":
        leaf.pop("text")
    elif fault == "ordinary_text":
        leaf["attributes"]["class"] = ""
    elif fault == "protected":
        leaf["protected"] = True
    elif fault == "hidden":
        leaf["states"] = []
    elif fault == "child":
        tree["nodes"].append(node("unexpected", "trailing-break", text="not empty"))
    elif fault == "nonzero_width":
        leaf["rect"]["width"] = 100
    elif fault == "parent_conflict":
        editor["text"] = "not empty"
    with pytest.raises(AccessibilityUnavailable):
        project(tree)


def test_same_text_in_separate_containers_is_not_deduplicated():
    tree = make_tree()
    for original in list(tree["nodes"]):
        if original["node_id"] in {"m1", "a1", "n1", "badge", "name", "b1", "t1", "duplicate"}:
            copy = deepcopy(original)
            copy["node_id"] += "-2"
            if copy["parent_id"] != "area":
                copy["parent_id"] += "-2"
            copy["rect"]["y"] += 100
            tree["nodes"].append(copy)
    assert len(project(tree).screen.messages) == 2


@pytest.mark.parametrize("change", ["clipped_avatar", "clipped_bubble", "missing_avatar"])
def test_partial_history_is_omitted_without_fabricating_anchors(change):
    tree = make_tree()
    if change == "missing_avatar":
        tree["nodes"] = [n for n in tree["nodes"] if n["node_id"] != "a1"]
    else:
        get(tree, "a1" if change == "clipped_avatar" else "b1")["rect"]["y"] = 110
    result = project(tree)
    assert result.screen.messages == []
    assert result.coverage["omitted_partial_messages"] == 1


@pytest.mark.parametrize(
    "change",
    ["scope", "node_window", "parent", "protected_parent", "duplicate", "not_active", "old_time", "frame_time"],
)
def test_unbound_or_stale_data_is_refused(change):
    tree = make_tree()
    if change == "scope":
        tree["scope"]["bound"] = False
    elif change == "node_window":
        get(tree, "t1")["window_id"] = "0x999"
    elif change == "parent":
        get(tree, "m1")["parent_id"] = "not-present"
    elif change == "protected_parent":
        get(tree, "group")["protected"] = True
    elif change == "duplicate":
        tree["nodes"].append(deepcopy(tree["nodes"][0]))
    elif change == "not_active":
        get(tree, "root")["states"].remove("active")
    elif change == "old_time":
        tree["finished_at"] = (NOW + timedelta(seconds=3)).isoformat()
    elif change == "frame_time":
        tree["frame_captured_at"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(AccessibilityUnavailable):
        project(tree)


def test_profile_uses_mapped_ancestor_numeric_control_and_excludes_chat():
    tree = profile_tree()
    result = project(tree)
    assert result.screen.scene == "member_profile"
    assert result.screen.opened_profile.member_id == "12345678"
    assert result.screen.opened_profile.verified_self_indicator == "own_space_link"
    assert result.screen.messages == [] and result.screen.visible_draft is None
    assert chat_change_signature(tree).chat_visible is False
    assert chat_change_signature(tree).chat_digest is None


@pytest.mark.parametrize("marker", ["查看他的QQ空间", "查看她的QQ空间", "QQ空间", "编辑备注", ""])
def test_matching_number_and_name_never_replace_exact_self_control(marker):
    assert project(profile_tree(marker=marker)).screen.opened_profile.verified_self_indicator == "none"


def test_marker_in_unrelated_profile_text_does_not_establish_self():
    tree = profile_tree(marker="查看他的QQ空间")
    tree["nodes"].append(
        node("fake-marker", "card", text="查看我的QQ空间", bounds=(800, 480, 180, 20), top="popup", window="0x456")
    )
    assert project(tree).screen.opened_profile.verified_self_indicator == "none"


def test_actual_space_button_hypertext_arrow_is_not_part_of_label():
    tree = profile_tree(marker="查看我的QQ空间\ufffc")
    tree["nodes"] += [
        node(
            "link-text",
            "link",
            text="查看我的QQ空间",
            bounds=(860, 602, 100, 20),
            role="static",
            top="popup",
            window="0x456",
        ),
        node("link-arrow", "link", bounds=(970, 602, 10, 20), role="image", top="popup", window="0x456"),
    ]
    assert project(tree).screen.opened_profile.verified_self_indicator == "own_space_link"
    get(tree, "link-text")["text"] = "查看他的QQ空间"
    assert project(tree).screen.opened_profile.verified_self_indicator == "none"


def test_same_window_dialog_blocks_background_chat():
    tree = make_tree()
    tree["nodes"].append(node("dialog", "root", bounds=(200, 200, 300, 200), role="dialog"))
    with pytest.raises(AccessibilityUnavailable, match="dialog_blocks"):
        project(tree)


def test_group_member_patterns_are_distinct_and_never_search_chat_for_number():
    result = project(profile_tree(uid="1040000000 (17人)"))
    assert result.screen.scene == "group_profile"
    assert result.screen.opened_profile.group_id == "1040000000"
    assert result.screen.opened_profile.member_id is None
    tree = profile_tree(uid="number unavailable")
    get(tree, "t1")["text"] = "QQ 12345678"
    with pytest.raises(AccessibilityUnavailable, match="numeric_id"):
        project(tree)


def test_image_animation_is_not_guessed_as_text_or_sticker():
    tree = make_tree()
    get(tree, "t1")["text"] = "\ufffc"
    tree["nodes"].append(node("image", "t1", "pic-element", bounds=(64, 175, 100, 38), role="image"))
    result = project(tree)
    assert result.screen.messages[0].kind == "other"
    assert result.screen.messages[0].text is None
    signature = chat_change_signature(tree)
    get(tree, "image")["name"] = "animation frame changed"
    assert chat_change_signature(tree) == signature


def test_background_showing_card_cannot_be_admitted_as_current_popup():
    tree = profile_tree()
    tree["scope"]["overlays"] = []
    with pytest.raises(AccessibilityUnavailable, match="node_binding"):
        project(tree)


def test_obscured_sensor_roots_preserve_ancestry_for_gap_but_never_grant_grounding():
    tree = profile_tree()
    tree["scope"]["skip_obscured_content"] = True
    tree["content_state"] = "occluded"
    tree["content_coverage"] = "top_level_bindings_only"
    tree["nodes"] = [n for n in tree["nodes"] if n["parent_id"] is None]
    for n in tree["nodes"]:
        n.pop("text")
        n["name"] = ""
        n["attributes"] = {}
    signal = chat_change_signature(tree)
    assert not signal.chat_visible and signal.chat_digest is None
    assert signal.message_count == 0
    with pytest.raises(AccessibilityUnavailable, match="a11y_content_not_actionable"):
        project(tree)
    # The signal still requires actual complete root bindings, not just a flag.
    tree["nodes"].pop()
    with pytest.raises(AccessibilityUnavailable, match="a11y_top_binding_invalid"):
        chat_change_signature(tree)


def test_opt_in_unobscured_complete_tree_keeps_action_evidence():
    tree = make_tree()
    tree["scope"]["skip_obscured_content"] = True
    tree["content_state"] = "complete"
    tree["content_coverage"] = "scoped_tree"
    assert project(tree).screen.scene == "group_chat"
    assert project(tree).screen.messages[0].text == "hello"


def test_exclusion_items_preserve_container_order_direction_and_visual_author_only():
    tree = make_tree()
    tree["content_state"] = "complete"
    get(tree, "editor")["text"] = "unsent Unicode 草稿"
    get(tree, "editor")["attributes"]["class"] = "ProseMirror"
    for original in list(tree["nodes"]):
        if original["node_id"] in {"m1", "a1", "n1", "badge", "name", "b1", "t1", "duplicate"}:
            copy = deepcopy(original)
            copy["node_id"] += "-2"
            if copy["parent_id"] != "area":
                copy["parent_id"] += "-2"
            copy["rect"]["y"] += 100
            tree["nodes"].append(copy)
    get(tree, "b1-2")["attributes"]["class"] = "msg-content-container container--self"
    get(tree, "a1-2")["rect"]["x"] = 180
    get(tree, "name-2")["text"] = "Visible self label"
    items = chat_observation_items(tree)
    assert isinstance(items, tuple) and len(items) == 2
    assert [(item["direction"], item["text"], item["author_key"]) for item in items] == [
        ("incoming", "hello", "Alice"), ("outgoing", "hello", "Visible self label"),
    ]
    assert all(set(item) == {"direction", "kind", "text", "display_name", "author_key"} for item in items)
    assert all(item["display_name"] == item["author_key"] for item in items)
    assert not any(item["text"] == get(tree, "editor")["text"] for item in items)
    # A duplicate static Text child is read once per message container, while
    # equal text in two distinct containers retains both ordered occurrences.
    assert [item["kind"] for item in items] == ["text", "text"]


def test_exclusion_items_preserve_observed_nontext_without_inventing_content():
    tree = make_tree()
    tree["content_state"] = "complete"
    get(tree, "t1")["text"] = "\ufffc"
    tree["nodes"].append(node("image", "t1", "pic-element", bounds=(64, 175, 100, 38), role="image"))
    item, = chat_observation_items(tree)
    assert item["kind"] == "other" and item["text"] is None


@pytest.mark.parametrize("fault", [
    "missing_state", "occluded", "unknown_state", "truncated", "overlay", "dialog",
    "unbound", "missing_text", "object_text",
])
def test_exclusion_items_reject_unknown_unbound_or_obscured_content(fault):
    tree = make_tree()
    tree["content_state"] = "complete"
    if fault == "missing_state":
        tree.pop("content_state")
    elif fault in {"occluded", "unknown_state"}:
        tree["content_state"] = fault
    elif fault == "truncated":
        tree["truncated"] = True
    elif fault == "overlay":
        tree = profile_tree()
        tree["content_state"] = "complete"
    elif fault == "dialog":
        tree["nodes"].append(node("dialog", "root", bounds=(200, 200, 300, 200), role="dialog"))
    elif fault == "unbound":
        tree["scope"]["bound"] = False
    elif fault == "missing_text":
        get(tree, "t1").pop("text")
    elif fault == "object_text":
        get(tree, "t1")["text"] = "\ufffc"
    with pytest.raises(AccessibilityUnavailable):
        chat_observation_items(tree)


def test_exclusion_items_use_same_complete_container_filter_without_fabricated_anchors():
    tree = make_tree()
    tree["content_state"] = "complete"
    get(tree, "a1")["rect"]["y"] = 110
    assert chat_observation_items(tree) == ()
    assert project(tree).screen.messages == []


def mention_tree(*, mention="@Misty", tail=" 你好，test 123？"):
    tree = make_tree()
    tree["content_state"] = "complete"
    tree["nodes"] = [n for n in tree["nodes"] if n["node_id"] != "duplicate"]
    inner = get(tree, "t1")
    inner["attributes"] = {"class": "message-content mix-message__inner", "tag": "div"}
    inner["rect"].update(width=180)
    get(tree, "b1")["rect"].update(width=200)
    inner["text"] = "\ufffc" + tail
    span = node("mention", "t1", "text-element--at", mention, (74, 186, 80, 16))
    span["attributes"]["tag"] = "span"
    tree["nodes"] += [
        span,
        node("mention-leaf", "mention", text=mention, bounds=(74, 186, 80, 16), role="static"),
        node("tail", "t1", text=tail, bounds=(154, 186, 100, 16), role="static"),
    ]
    return tree


def test_complete_inline_mention_proxy_matches_sensor_projection_and_exclusion_text():
    tree = mention_tree()
    expanded = "@Misty 你好，test 123？"
    message, = project(tree).screen.messages
    assert message.text == expanded
    assert message.display_name == "Alice" and message.message_id is None
    item, = chat_observation_items(tree)
    assert item["text"] == expanded and item["author_key"] == "Alice"
    signature = chat_change_signature(tree)
    assert signature.message_count == 1 and signature.chat_digest == project(tree).chat_digest
    # An equivalent literal Text representation has exactly the same semantic
    # signature. The duplicate mention/static labels are never appended twice.
    literal = deepcopy(tree)
    get(literal, "t1")["text"] = expanded
    literal["nodes"] = [n for n in literal["nodes"] if n["node_id"] not in {"mention", "mention-leaf", "tail"}]
    assert chat_change_signature(literal) == signature
    get(tree, "tail")["text"] += "!"
    get(tree, "t1")["text"] += "!"
    assert chat_change_signature(tree).chat_digest != signature.chat_digest


def test_multiple_mentions_preserve_original_order_without_duplicate_parent_leaf_text():
    tree = mention_tree(mention="@Misty", tail=" ")
    get(tree, "t1")["rect"]["width"] = 280
    get(tree, "b1")["rect"]["width"] = 300
    get(tree, "tail")["rect"].update(width=20)
    span = node("mention2", "t1", "text-element--at", "@另一个人", (174, 186, 100, 16))
    span["attributes"]["tag"] = "span"
    tree["nodes"] += [
        span,
        node("mention2-leaf", "mention2", text="@另一个人", bounds=(174, 186, 100, 16), role="static"),
    ]
    get(tree, "t1")["text"] += "\ufffc"
    assert project(tree).screen.messages[0].text == "@Misty @另一个人"


@pytest.mark.parametrize("fault", [
    "unknown_class", "unknown_role", "missing_tail", "missing_mention_leaf", "leaf_conflict",
    "missing_leaf_text", "unresolved_leaf", "hidden", "protected", "outside", "parent_conflict",
    "not_at_label", "oversize",
])
def test_mention_proxy_never_accepts_partial_unknown_or_unbound_message_body(fault):
    tree = mention_tree()
    if fault == "unknown_class":
        get(tree, "mention")["attributes"]["class"] = "unsupported-inline"
    elif fault == "unknown_role":
        get(tree, "mention")["role"] = "link"
    elif fault in {"missing_tail", "missing_mention_leaf"}:
        missing = "tail" if fault == "missing_tail" else "mention-leaf"
        tree["nodes"] = [n for n in tree["nodes"] if n["node_id"] != missing]
    elif fault == "leaf_conflict":
        get(tree, "mention-leaf")["text"] = "@不同文本"
    elif fault == "missing_leaf_text":
        get(tree, "mention-leaf").pop("text")
    elif fault == "unresolved_leaf":
        get(tree, "mention-leaf")["text"] = "\ufffc"
    elif fault == "hidden":
        get(tree, "mention-leaf")["states"] = []
    elif fault == "protected":
        get(tree, "mention-leaf")["protected"] = True
    elif fault == "outside":
        get(tree, "mention")["rect"]["y"] = 500
    elif fault == "parent_conflict":
        get(tree, "t1")["text"] = "\ufffc未在子节点出现的正文"
    elif fault == "not_at_label":
        get(tree, "mention")["text"] = get(tree, "mention-leaf")["text"] = "缺少提及标记"
    elif fault == "oversize":
        get(tree, "tail")["text"] = "x" * 12000
        get(tree, "t1")["text"] = "\ufffc" + "x" * 12000
    for reader in (chat_change_signature, project, chat_observation_items):
        with pytest.raises(AccessibilityUnavailable):
            reader(tree)



def timing_tree():
    tree = make_tree()
    tree["duration_ms"] = 490
    tree["capture_timing"] = {
        "schema_version": "a11y-capture-timing-v1",
        "basis": "agent_monotonic",
        "frame_sha256": "a" * 64,
        "screenshot_started_offset_ms": 5,
        "screenshot_finished_offset_ms": 30,
        "a11y_started_offset_ms": 40,
        "a11y_finished_offset_ms": 540,
    }
    return tree


@pytest.mark.parametrize("wall_step", [-3, 3])
def test_monotonic_pairing_accepts_wall_step_only_with_exact_png(wall_step):
    tree = timing_tree()
    tree["finished_at"] = (NOW + timedelta(seconds=wall_step)).isoformat()
    before = deepcopy(tree)
    result = project(tree, expected_frame_sha256="a" * 64)
    assert result.screen.scene == "group_chat"
    assert tree == before  # Original wall times remain unchanged evidence.
    with pytest.raises(AccessibilityUnavailable, match="a11y_capture_timing_unbound"):
        project(tree, expected_frame_sha256="b" * 64)


@pytest.mark.parametrize("mutation", [
    "missing_expected", "wrong_digest", "version", "basis", "missing_offset",
    "negative", "nan", "infinity", "bool", "string", "wrong_order", "overlap",
    "late_tree", "late_capture_start", "duration_missing", "duration_negative",
    "duration_longer_than_read", "duration_over_one_second", "wrong_frame_wall",
    "malformed_timing", "narrower_window",
])
def test_new_capture_timing_rejects_unbound_or_late_tree(mutation):
    tree = timing_tree()
    timing = tree["capture_timing"]
    kwargs = {"expected_frame_sha256": "a" * 64}
    if mutation == "missing_expected":
        kwargs = {}
    elif mutation == "wrong_digest":
        timing["frame_sha256"] = "b" * 64
    elif mutation == "version":
        timing["schema_version"] = "future-v2"
    elif mutation == "basis":
        timing["basis"] = "wall_clock"
    elif mutation == "missing_offset":
        del timing["screenshot_finished_offset_ms"]
    elif mutation in {"negative", "nan", "infinity", "bool", "string"}:
        timing["screenshot_started_offset_ms"] = {
            "negative": -1, "nan": float("nan"), "infinity": float("inf"),
            "bool": True, "string": "5",
        }[mutation]
    elif mutation == "wrong_order":
        timing["screenshot_finished_offset_ms"] = 3
    elif mutation == "overlap":
        timing["a11y_started_offset_ms"] = 29
    elif mutation == "late_tree":
        timing["a11y_finished_offset_ms"] = 2000.001
    elif mutation == "late_capture_start":
        for key in tuple(timing):
            if key.endswith("offset_ms"):
                timing[key] += 1500
    elif mutation == "duration_missing":
        del tree["duration_ms"]
    elif mutation == "duration_negative":
        tree["duration_ms"] = -1
    elif mutation == "duration_longer_than_read":
        tree["duration_ms"] = 501
    elif mutation == "duration_over_one_second":
        timing["a11y_finished_offset_ms"] = 1400
        tree["duration_ms"] = 1000.001
    elif mutation == "wrong_frame_wall":
        tree["frame_captured_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif mutation == "malformed_timing":
        tree["capture_timing"] = None
    elif mutation == "narrower_window":
        kwargs["maximum_skew_seconds"] = 0.5
    with pytest.raises(AccessibilityUnavailable):
        project(tree, **kwargs)


def test_legacy_wall_pairing_stays_strict_even_with_new_caller_digest():
    tree = make_tree()
    assert project(tree, expected_frame_sha256="a" * 64).screen.scene == "group_chat"
    tree["finished_at"] = (NOW + timedelta(seconds=3)).isoformat()
    with pytest.raises(AccessibilityUnavailable, match="a11y_capture_time_unbound"):
        project(tree, expected_frame_sha256="a" * 64)
