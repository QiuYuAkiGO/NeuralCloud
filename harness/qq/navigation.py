"""Allowlisted QQ navigation using scoped, verified accessibility controls.

Names select a visible candidate; only the numerical group profile grants group
identity. Conversation previews and global search results never select a chat.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import re
import time
from typing import TYPE_CHECKING
import unicodedata

from harness.qq.accessibility import AccessibilityUnavailable, _Tree, _classes, _inside, _rect, _validate_capture_timing
from harness.qq.grounding import Rect

if TYPE_CHECKING:
    from harness.qq.desktop import DesktopBackend, Frame, GroupVerification


@dataclass(frozen=True)
class NavigationTarget:
    rect: Rect
    label: str


def _label(value: str) -> str:
    value = re.sub(r"\s*[（(]\d+人?[）)]\s*$", "", value).strip()
    # QQ can expose a decorative emoji as a separate title text run. Allow a
    # user-supplied plain group name to select that candidate, never a prefix
    # ending before ordinary letters or digits. Numeric verification follows.
    while value and unicodedata.category(value[-1])[0] in {"S", "P", "Z", "M"}:
        value = value[:-1]
    while value and unicodedata.category(value[0])[0] in {"S", "P", "Z", "M"}:
        value = value[1:]
    return value.strip()


def search_entry(tree: _Tree, *, focused: bool = False) -> dict:
    wrapper = tree.unique("main-search__search-input", top=tree.scope["top_node_id"])
    entries = [node for node in tree.descendants(wrapper)
               if node.get("role") == "entry" and node.get("attributes", {}).get("tag") == "input"]
    if len(entries) != 1:
        raise AccessibilityUnavailable("a11y_navigation_search_ambiguous")
    entry = entries[0]
    if (not _inside(_rect(entry), (56, 67, 306, 119))
            or "editable" not in entry.get("states", [])
            or focused and "focused" not in entry.get("states", [])):
        raise AccessibilityUnavailable("a11y_navigation_search_unfocused")
    tree.text(entry, maximum=256)
    return entry


def recent_group_target(tree: _Tree, display_name: str) -> NavigationTarget | None:
    wanted = _label(display_name)
    if not wanted:
        return None
    lists = tree.matching("recent-contact-list", top=tree.scope["top_node_id"])
    if len(lists) != 1:
        raise AccessibilityUnavailable("a11y_navigation_list_ambiguous")
    matches = []
    for row in tree.matching("recent-contact-item", under=lists[0]):
        if not _inside(_rect(row), (56, 119, 306, 716)):
            continue
        infos = tree.matching("item__info", under=row)
        if len(infos) != 1:
            continue
        # Direct static children are the title. Summary/time nodes have their
        # own classed wrappers and must never masquerade as a group name.
        pieces = [tree.text(child, maximum=256) for child in tree.children.get(infos[0]["node_id"], [])
                  if tree.visible(child) and child.get("role") == "static"]
        name = "".join(pieces).strip()
        if name and _label(name) == wanted:
            matches.append(NavigationTarget(_rect(row), name))
    if len(matches) > 1:
        raise AccessibilityUnavailable("a11y_navigation_candidate_ambiguous")
    return matches[0] if matches else None


def search_group_target(tree: _Tree, query: str) -> NavigationTarget | None:
    if tree.text(search_entry(tree), maximum=256) != query:
        raise AccessibilityUnavailable("a11y_navigation_query_changed")
    containers = tree.matching("search-content", top=tree.scope["top_node_id"])
    if len(containers) != 1:
        return None
    matches = []
    for row in tree.matching("search-result-item", under=containers[0]):
        if ("global" in _classes(row) or row.get("role") != "list item"
                or not _inside(_rect(row), (56, 111, 400, 716))):
            continue
        name = row.get("name")
        # The member-count suffix distinguishes a joined-group candidate from
        # people, global search suggestions and arbitrary matching previews.
        if not isinstance(name, str) or not re.search(r"[（(]\d+人[）)]\s*$", name):
            continue
        if _label(name) == _label(query):
            matches.append(NavigationTarget(_rect(row), name))
    if len(matches) > 1:
        raise AccessibilityUnavailable("a11y_navigation_candidate_ambiguous")
    return matches[0] if matches else None


def _chat_tree(desktop: DesktopBackend, frame: Frame) -> _Tree:
    from harness.qq.desktop import DesktopError

    empty = _empty_main_tree(desktop, frame)
    if empty is not None:
        return empty
    screen = desktop.inspect(frame)
    desktop._require_chat(frame, screen)
    if frame.capture_id not in desktop._a11y_frames or frame.a11y_tree is None:
        raise DesktopError("group_navigation_accessibility_required")
    if screen.visible_draft != "":
        raise DesktopError("group_navigation_draft_present_or_unreadable")
    return _Tree(frame.a11y_tree)


def _empty_main_tree(desktop: DesktopBackend, frame: Frame) -> _Tree | None:
    """Allow the known QQ empty landing panel, never an unreadable editor.

This scene has no current chat, so the chat projector intentionally rejects it.
Independently validate the same Body/AX envelope before using its sidebar.
"""
    from harness.qq.desktop import DesktopError

    data = frame.a11y_tree
    if (not isinstance(data, dict) or not isinstance(data.get("nodes"), list)
            or not any("empty-aio" in _classes(n) for n in data["nodes"] if isinstance(n, dict))):
        return None
    try:
        tree = _Tree(data)
        scope = tree.scope
        expected_rect = dict(zip(("x", "y", "width", "height"), desktop.config.window_rect, strict=True))
        if (data.get("content_state", "complete") != "complete" or tree.overlays
                or not re.fullmatch(r"evidence://sha256/[0-9a-f]{64}", frame.a11y_evidence_ref or "")
                or frame.evidence_ref != "evidence://sha256/" + hashlib.sha256(frame.png).hexdigest()
                or (frame.width, frame.height) != (desktop.config.width, desktop.config.height)
                or frame.window_id != desktop.config.window_id or frame.active_window_id != desktop.config.window_id
                or frame.window_rect != desktop.config.window_rect or not desktop._frame_is_recent(frame)
                or scope.get("window_id") != desktop.config.window_id or scope.get("app_id") != desktop.config.app_id
                or scope.get("window_rect") != expected_rect):
            raise ValueError("binding")
        started, finished, captured = (datetime.fromisoformat(data[key])
                                       for key in ("started_at", "finished_at", "frame_captured_at"))
        if (frame.captured_at.tzinfo is None or started.tzinfo is None or finished.tzinfo is None
                or captured != frame.captured_at):
            raise ValueError("time")
        if "capture_timing" in data:
            _validate_capture_timing(data, hashlib.sha256(frame.png).hexdigest(), 2)
        elif started < captured or finished < started or not 0 <= (finished - captured).total_seconds() <= 2:
            raise ValueError("time")
        panels = tree.matching("empty-aio", top=scope["top_node_id"])
        if (len(panels) != 1 or not _inside(_rect(panels[0]), (306, 67, 1280, 720))
                or tree.matching("ProseMirror") or tree.matching("group-chat")
                or any(n.get("role") in {"dialog", "alert", "popup menu", "file chooser"} for n in tree.nodes)):
            raise ValueError("scene")
        search_entry(tree)
        return tree
    except Exception:
        raise DesktopError("group_navigation_empty_panel_not_verified") from None


def _click_target(desktop: DesktopBackend, frame: Frame, target: NavigationTarget, resolver) -> None:
    from harness.qq.desktop import DesktopError, _pixels

    fresh = desktop.capture()
    actual = resolver(_chat_tree(desktop, fresh))
    if (actual != target or fresh.window_id != frame.window_id or fresh.active_window_id != frame.active_window_id
            or not desktop._frame_is_recent(fresh)
            or _pixels(frame.png, (target.rect.bounds,)) != _pixels(fresh.png, (target.rect.bounds,))):
        raise DesktopError("group_navigation_target_changed")
    x, y = target.rect.center
    desktop._act({"kind": "click", "x": x, "y": y, "button": "left", "count": 1})
    time.sleep(desktop.config.settle_seconds)


def select_group(desktop: DesktopBackend, group_id: str, *, allowed_group_ids,
                 display_name: str | None = None) -> GroupVerification:
    from harness.qq.desktop import DesktopError, _title

    allowed = frozenset(allowed_group_ids)
    if (not isinstance(group_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", group_id)
            or group_id not in allowed):
        raise DesktopError("group_navigation_not_allowlisted")
    if display_name is not None and (not isinstance(display_name, str) or not display_name.strip()
                                     or len(display_name) > 256 or any(c in display_name for c in "\r\n\x00")):
        raise DesktopError("group_navigation_label_invalid")
    with desktop._lock:
        if not desktop.config.accessibility_enabled:
            raise DesktopError("group_navigation_accessibility_required")
        if desktop._owned_draft is not None:
            raise DesktopError("group_navigation_delivery_pending")
        frame = desktop.capture()
        screen = None if _empty_main_tree(desktop, frame) is not None else desktop.inspect(frame)
        if screen is not None and screen.opened_profile is not None and not screen.blocked:
            frame, screen = desktop._dismiss(frame, screen)
        tree = _chat_tree(desktop, frame)
        if (group_id == desktop.config.expected_group_id and not desktop._navigation_unverified
                and screen is not None and (
                    desktop._group is not None and desktop._group.group_id == group_id
                    and _title(screen.group_title.display_name) == desktop._group.display_name
                    or display_name is not None
                    and _label(screen.group_title.display_name) == _label(display_name))):
            desktop.invalidate_identity_cache()
            return desktop.verify_group(frame)
        query = display_name.strip() if display_name else group_id
        def resolve_search(current):
            return search_group_target(current, query)

        def resolve_recent(current):
            return recent_group_target(current, query)

        def resolve_entry(current):
            return NavigationTarget(_rect(search_entry(current)), "search")

        try:
            # A search popover can cover recent rows. Use an existing exact
            # local result only if its query matches this navigation request.
            search = search_entry(tree)
            existing_query = tree.text(search, maximum=256)
            target = search_group_target(tree, query) if existing_query == query else None
            if existing_query and existing_query != query:
                raise DesktopError("group_navigation_search_in_use")
            resolver = resolve_search
            if target is None and not existing_query:
                resolver = resolve_recent
                target = resolver(tree)
            if target is None:
                _click_target(desktop, frame, resolve_entry(tree), resolve_entry)
                focused = desktop.capture()
                focused_tree = _chat_tree(desktop, focused)
                entry = search_entry(focused_tree, focused=True)
                if focused_tree.text(entry, maximum=256) not in {"", query}:
                    raise DesktopError("group_navigation_search_in_use")
                if focused_tree.text(entry, maximum=256) == "":
                    desktop._act({"kind": "type_text", "text": query})
                    time.sleep(max(.65, desktop.config.settle_seconds))
                resolver = resolve_search
                for _ in range(3):
                    frame = desktop.capture()
                    tree = _chat_tree(desktop, frame)
                    target = resolver(tree)
                    if target is not None:
                        break
                    time.sleep(min(.3, desktop._timeout()))
                if target is None:
                    # The field is ours only while the exact query and focus
                    # remain proven. Clearing it never touches a message draft.
                    entry = search_entry(tree, focused=True)
                    if tree.text(entry, maximum=256) == query:
                        desktop._act({"kind": "press_keys", "keys": ["ctrl", "a"]})
                        desktop._act({"kind": "press_keys", "keys": ["backspace"]})
                        desktop._act({"kind": "press_keys", "keys": ["escape"]})
                    raise DesktopError("group_navigation_group_not_found")
            desktop.invalidate_identity_cache()
            desktop._group = None
            desktop._navigation_unverified = True
            _click_target(desktop, frame, target, resolver)
            desktop.config = replace(desktop.config, expected_group_id=group_id)
            desktop._screens.clear()
            desktop._pixel_screens.clear()
            desktop._a11y_frames.clear()
            verified = desktop.verify_group()
            if verified.group_id != group_id:
                raise DesktopError("group_navigation_numeric_identity_mismatch")
            desktop._navigation_unverified = False
            return verified
        except AccessibilityUnavailable:
            raise DesktopError("group_navigation_controls_unavailable") from None
