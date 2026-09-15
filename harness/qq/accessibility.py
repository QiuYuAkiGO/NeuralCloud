"""QQ AT-SPI projection from verified, scoped evidence; no actions or models.

The caller verifies the evidence bytes through MistyAIClient.get_evidence before
decoding the tree. Application strings remain untrusted observations. Only the
known QQ control ancestry grants field meaning; arbitrary matching text does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re

from harness.qq.grounding import (
    GroundedMessage,
    GroundedScreen,
    OpenedProfile,
    Rect,
    TitleAnchor,
    VisibleMember,
)


class AccessibilityUnavailable(ValueError):
    """A fixed reason code, never an observed string or raw validation error."""


@dataclass(frozen=True)
class AccessibilityProjection:
    screen: GroundedScreen
    evidence_refs: tuple[str, ...]
    semantic_digest: str
    chat_digest: str | None
    coverage: dict[str, int | bool]


@dataclass(frozen=True)
class ChatChangeSignature:
    chat_visible: bool
    chat_digest: str | None
    message_count: int = 0


_REF = re.compile(r"evidence://sha256/[0-9a-f]{64}\Z")
_CHAT = (0, 119, 1096, 509)
_TITLE = (0, 67, 1040, 119)
_INPUT = (0, 545, 1096, 666)
_SEND = (700, 650, 1096, 720)
_ROSTER = (1096, 298, 1280, 716)


def _classes(node: dict) -> set[str]:
    return set(node.get("attributes", {}).get("class", "").split())


def _rect(node: dict) -> Rect | None:
    try:
        return Rect.model_validate(node.get("rect"))
    except Exception:
        return None


def _inside(rect: Rect | None, bounds: tuple[int, int, int, int]) -> bool:
    return bool(
        rect
        and bounds[0] <= rect.x
        and bounds[1] <= rect.y
        and rect.x + rect.width <= bounds[2]
        and rect.y + rect.height <= bounds[3]
    )


class _Tree:
    def __init__(self, data: dict):
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != "a11y-tree-v1"
            or data.get("truncated") is not False
        ):
            raise AccessibilityUnavailable("a11y_scope_missing")
        scope = data.get("scope", {})
        if scope.get("bound") is not True or scope.get("active") is not True:
            raise AccessibilityUnavailable("a11y_scope_unbound")
        self.scope = scope
        self.overlays = scope.get("overlays", [])
        if not isinstance(self.overlays, list) or len(self.overlays) > 1:
            raise AccessibilityUnavailable("a11y_overlay_ambiguous")
        self.bindings = {scope.get("top_node_id"): (scope.get("window_id"), scope.get("window_rect"))}
        for overlay in self.overlays:
            self.bindings[overlay.get("top_node_id")] = (overlay.get("window_id"), overlay.get("window_rect"))
        raw = data.get("nodes")
        if not isinstance(raw, list) or not 0 < len(raw) <= 1500:
            raise AccessibilityUnavailable("a11y_nodes_invalid")
        self.nodes: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.children: dict[str, list[dict]] = {}
        for node in raw:
            if not isinstance(node, dict):
                raise AccessibilityUnavailable("a11y_nodes_invalid")
            ident, top_id = node.get("node_id"), node.get("top_node_id")
            if (
                not isinstance(ident, str)
                or ident in self.by_id
                or top_id not in self.bindings
                or node.get("app_id") != scope.get("app_id")
                or node.get("window_id") != self.bindings[top_id][0]
            ):
                raise AccessibilityUnavailable("a11y_node_binding_invalid")
            self.by_id[ident] = node
            self.children.setdefault(node.get("parent_id"), []).append(node)
        for ident, node in self.by_id.items():
            seen = {ident}
            ancestor = node
            while ancestor.get("parent_id") is not None:
                parent = self.by_id.get(ancestor["parent_id"])
                if parent is None or parent["node_id"] in seen or parent.get("top_node_id") != node["top_node_id"]:
                    raise AccessibilityUnavailable("a11y_ancestry_invalid")
                seen.add(parent["node_id"])
                if parent.get("protected") is not False:
                    raise AccessibilityUnavailable("a11y_protected_ancestor")
                ancestor = parent
            if ancestor["node_id"] != node["top_node_id"]:
                raise AccessibilityUnavailable("a11y_ancestry_invalid")
            if self.visible(node):
                self.nodes.append(node)
        for top, (_window, bounds) in self.bindings.items():
            root = self.by_id.get(top)
            if root is None or root.get("rect") != bounds or root.get("parent_id") is not None:
                raise AccessibilityUnavailable("a11y_top_binding_invalid")
        if "active" not in self.by_id[scope["top_node_id"]].get("states", []):
            raise AccessibilityUnavailable("a11y_active_ancestor_missing")

    @staticmethod
    def visible(node: dict) -> bool:
        return (
            node.get("protected") is False
            and {"showing", "visible"} <= set(node.get("states", []))
            and _rect(node) is not None
        )

    def descendants(self, node: dict) -> list[dict]:
        prefix = node["node_id"]
        # Follow verified parent edges, never trust the spelling of node paths.
        output, pending = [], list(reversed(self.children.get(prefix, [])))
        while pending:
            child = pending.pop()
            if self.visible(child):
                output.append(child)
            pending.extend(reversed(self.children.get(child["node_id"], [])))
        return output

    def matching(self, class_name: str, *, under: dict | None = None, top: str | None = None) -> list[dict]:
        nodes = self.descendants(under) if under else self.nodes
        return [n for n in nodes if class_name in _classes(n) and (top is None or n["top_node_id"] == top)]

    def unique(self, class_name: str, *, under: dict | None = None, top: str | None = None) -> dict:
        nodes = self.matching(class_name, under=under, top=top)
        if len(nodes) != 1:
            raise AccessibilityUnavailable("a11y_control_ambiguous")
        return nodes[0]

    @staticmethod
    def text(node: dict, *, maximum: int = 12000) -> str:
        value = node.get("text")
        if not isinstance(value, str) or len(value) > maximum or "\ufffc" in value:
            raise AccessibilityUnavailable("a11y_text_unreadable")
        return value

    def label(self, node: dict) -> str:
        # QQ inserts an object-replacement character for a separate role/level
        # badge. Only direct static children belong to the member's label.
        labels = [
            self.text(child, maximum=256)
            for child in self.children.get(node["node_id"], [])
            if self.visible(child) and child.get("role") == "static" and "group-member-label" not in _classes(child)
        ]
        if labels:
            text = "".join(labels)
        else:
            text = self.text(node, maximum=256)
        if not text.strip() or len(text) > 256:
            raise AccessibilityUnavailable("a11y_label_unreadable")
        return text

    def header_label(self, node: dict) -> str:
        # Chromium may omit Text on the member-count child after changing
        # chats, while this header's Text still contains the complete title.
        # Prefer that bounded Text, never a Name-only substitute. Member labels
        # retain their separate badge filtering and readability requirements.
        try:
            value = self.text(node, maximum=256)
            if value.strip():
                return value
        except AccessibilityUnavailable:
            pass
        return self.label(node)


def _editor_text(tree: _Tree, editor: dict) -> str:
    """Resolve QQ's paragraph objects without duplicating parent/leaf Text.

    Chromium exposes a nonempty ProseMirror as U+FFFC with a paragraph child;
    that paragraph and its static leaf may both expose the complete draft.
    Only complete, visible text/paragraph descendants inside this editor are
    accepted. Images, unresolved objects and partial subtrees stay unreadable.
    """
    bounds = _rect(editor).bounds

    def read(node: dict, depth: int = 0) -> str:
        # After clearing ProseMirror, Chromium exposes a formatting-only break
        # with a zero-width, sometimes offscreen virtual layout box. It is not
        # a click target. Accept its explicit newline only when the editable
        # parent independently exposes the same empty state; never infer empty
        # content from an object placeholder or unreadable descendant.
        geometry = node.get("rect", {})
        if (
            depth <= 32
            and node is not editor
            and "is-empty" in _classes(editor)
            and editor.get("text") == "\n"
            and node.get("role") == "static"
            and "ProseMirror-trailingBreak" in _classes(node)
            and node.get("text") == "\n"
            and node.get("protected") is False
            and {"showing", "visible"} <= set(node.get("states", []))
            and geometry.get("width") == 0
            and isinstance(geometry.get("height"), int)
            and 0 < geometry["height"] <= 32
            and not tree.children.get(node["node_id"])
        ):
            return "\n"
        if depth > 32 or not tree.visible(node) or not _inside(_rect(node), bounds):
            raise AccessibilityUnavailable("a11y_draft_subtree_unreadable")
        if node is not editor:
            role = node.get("role")
            tag = node.get("attributes", {}).get("tag")
            if not (role in {"paragraph", "static", "text"} or (role == "section" and tag == "span")):
                raise AccessibilityUnavailable("a11y_draft_subtree_unreadable")
        children = tree.children.get(node["node_id"], [])
        values = [read(child, depth + 1) for child in children]
        paragraphs = [child.get("role") == "paragraph" or child.get("attributes", {}).get("tag") == "p" for child in children]
        if any(paragraphs) and not all(paragraphs):
            raise AccessibilityUnavailable("a11y_draft_subtree_unreadable")
        combined = ("\n" if paragraphs and all(paragraphs) else "").join(values)
        raw = node.get("text")
        if raw is not None and (not isinstance(raw, str) or len(raw) > 12000):
            raise AccessibilityUnavailable("a11y_text_unreadable")
        if isinstance(raw, str) and "\ufffc" not in raw:
            # The parent and descendants are alternative representations, not
            # consecutive text. Check consistency, then retain exactly one.
            if children and raw != combined:
                raise AccessibilityUnavailable("a11y_draft_text_conflict")
            return raw
        if not children or len(combined) > 12000:
            raise AccessibilityUnavailable("a11y_text_unreadable")
        if raw is not None and (
            raw.count("\ufffc") != len(children)
            or raw.strip("\ufffc\n")
            or not combined
        ):
            # No character-offset-to-object map is present in this contract.
            # Mixed literal/object strings cannot be safely reconstructed.
            raise AccessibilityUnavailable("a11y_text_unreadable")
        return combined

    return read(editor)


def _message_text(tree: _Tree, inner: dict) -> str:
    """Expand only QQ's fully represented inline @-mention objects.

    A mixed message exposes U+FFFC for each text-element--at child, while its
    ordinary static children appear literally in the parent's Text. Requiring
    that entire ordered proxy to equal the parent prevents partial body reads.
    Mention Text and its static descendants are duplicate representations, not
    extra messages or verified author identities.
    """
    raw = inner.get("text")
    if not isinstance(raw, str) or "\ufffc" not in raw:
        return tree.text(inner)
    if len(raw) > 12000 or "mix-message__inner" not in _classes(inner):
        raise AccessibilityUnavailable("a11y_message_object_unreadable")
    bounds = _rect(inner).bounds
    children = tree.children.get(inner["node_id"], [])
    proxy, expanded = [], []
    for child in children:
        if not tree.visible(child) or not _inside(_rect(child), bounds):
            raise AccessibilityUnavailable("a11y_message_object_unreadable")
        leaves = tree.children.get(child["node_id"], [])
        if child.get("role") == "static" and not leaves:
            value = tree.text(child)
            proxy.append(value)
        elif (child.get("role") == "section"
              and child.get("attributes", {}).get("tag") == "span"
              and "text-element--at" in _classes(child)):
            value = tree.text(child)
            if not value.startswith("@") or len(value) <= 1 or not leaves:
                raise AccessibilityUnavailable("a11y_message_object_unreadable")
            leaf_values = []
            for leaf in leaves:
                if (not tree.visible(leaf) or leaf.get("role") != "static"
                        or not _inside(_rect(leaf), _rect(child).bounds)
                        or tree.children.get(leaf["node_id"])):
                    raise AccessibilityUnavailable("a11y_message_object_unreadable")
                leaf_values.append(tree.text(leaf))
            if "".join(leaf_values) != value:
                raise AccessibilityUnavailable("a11y_message_text_conflict")
            proxy.append("\ufffc")
        else:
            raise AccessibilityUnavailable("a11y_message_object_unreadable")
        expanded.append(value)
    value = "".join(expanded)
    if not children or "".join(proxy) != raw or not value or len(value) > 12000:
        raise AccessibilityUnavailable("a11y_message_text_conflict")
    return value


def _chat(tree: _Tree) -> tuple[TitleAnchor, list[GroundedMessage], dict, dict, int]:
    top = tree.scope["top_node_id"]
    if any(
        n.get("role") in {"dialog", "alert", "popup menu", "file chooser"}
        for n in tree.nodes
        if n["top_node_id"] == top
    ):
        raise AccessibilityUnavailable("a11y_dialog_blocks_chat")
    group = tree.unique("group-chat", top=top)
    title_node = tree.unique("chat-header__contact-name", top=top)
    if not _inside(_rect(title_node), _TITLE):
        raise AccessibilityUnavailable("a11y_title_geometry")
    title = TitleAnchor(display_name=tree.header_label(title_node), rect=_rect(title_node))
    area = tree.unique("chat-msg-area", under=group)
    editor = tree.unique("ProseMirror", under=group)
    send = tree.unique("send-msg", under=group)
    if (
        not _inside(_rect(editor), _INPUT)
        or "editable" not in editor.get("states", [])
        or not _inside(_rect(send), _SEND)
        or tree.text(send).strip() != "发送"
    ):
        raise AccessibilityUnavailable("a11y_input_controls_unreadable")
    messages: list[GroundedMessage] = []
    omitted = 0
    for container in tree.matching("message-container", under=area):
        # An exact container ancestor prevents parent/child Text duplication and
        # prevents the roster, announcement or draft from becoming a message.
        bubbles = tree.matching("msg-content-container", under=container)
        avatars = tree.matching("message-container__avatar", under=container)
        if len(bubbles) != 1 or len(avatars) != 1:
            omitted += 1
            continue
        bubble, avatar = _rect(bubbles[0]), _rect(avatars[0])
        if not _inside(bubble, _CHAT) or not _inside(avatar, _CHAT):
            omitted += 1
            continue
        if avatar.width > 128 or avatar.height > 128:
            raise AccessibilityUnavailable("a11y_avatar_geometry")
        classes = _classes(bubbles[0])
        if "container--others" in classes and avatar.x + avatar.width <= bubble.x:
            direction = "incoming"
        elif "container--self" in classes and bubble.x + bubble.width <= avatar.x:
            direction = "outgoing"
        else:
            raise AccessibilityUnavailable("a11y_direction_unbound")
        if not (container["rect"]["y"] <= avatar.y <= bubble.y + bubble.height):
            raise AccessibilityUnavailable("a11y_avatar_unbound")
        inner = tree.unique("message-content", under=bubbles[0])
        descendants = tree.descendants(inner)
        has_image = any(n.get("role") == "image" for n in descendants)
        uncertainties: list[str] = []
        if has_image:
            # The live QQ class pic-element covers photos and animated stickers;
            # no unsupported distinction is inferred from the bitmap.
            text, kind = None, "other"
        else:
            text = _message_text(tree, inner)
            if not text.strip():
                raise AccessibilityUnavailable("a11y_message_text_missing")
            kind = "text"
        names = tree.matching("user-name", under=container)
        display_name = tree.label(names[0]) if len(names) == 1 else None
        messages.append(
            GroundedMessage(
                direction=direction,
                kind=kind,
                display_name=display_name,
                text=text,
                avatar=avatar,
                bubble=bubble,
                uncertainties=uncertainties,
            )
        )
    return title, messages, editor, send, omitted


def _signature(title: TitleAnchor, messages: list[GroundedMessage]) -> str:
    value = {"title": title.model_dump(), "messages": [m.model_dump() for m in messages]}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def chat_change_signature(tree: dict) -> ChatChangeSignature:
    """A sensor hint only. No screenshot evidence means no action authority."""
    parsed = _Tree(tree)
    if parsed.overlays:
        return ChatChangeSignature(False, None)
    title, messages, _editor, _send, _omitted = _chat(parsed)
    return ChatChangeSignature(True, _signature(title, messages), len(messages))


def chat_observation_items(tree: dict) -> tuple[dict, ...]:
    """Read an exclusion baseline, without image, identity or action authority.

    The caller must bind/freshness-check the sensor response. Only complete,
    unobscured v4 content is eligible here. The normal chat parser preserves
    container order and filters partial bubbles/avatars; it does not invent
    missing history. ``author_key`` is merely the visible label, not a QQ ID.
    Images retain kind=other/text=None for the caller's existing ledger mapping.
    """
    parsed = _Tree(tree)
    if tree.get("content_state") != "complete" or parsed.overlays:
        raise AccessibilityUnavailable("a11y_chat_content_unavailable")
    _title, messages, _editor, _send, _omitted = _chat(parsed)
    return tuple({
        "direction": message.direction,
        "kind": message.kind,
        "text": message.text,
        "display_name": message.display_name,
        "author_key": message.display_name,
    } for message in messages)


def _profile(tree: _Tree) -> OpenedProfile:
    overlay = tree.overlays[0]
    top = overlay["top_node_id"]
    card = tree.unique("buddy-profile", top=top)
    name = tree.label(tree.unique("buddy-profile__header-name", under=card))
    uid = tree.text(tree.unique("buddy-profile__header-uid", under=card), maximum=64).strip()
    group = re.fullmatch(r"([1-9][0-9]{0,19})\s+[（(][0-9]+人[）)]", uid)
    member = re.fullmatch(r"QQ\s+([1-9][0-9]{0,19})", uid)
    if not group and not member:
        raise AccessibilityUnavailable("a11y_profile_numeric_id_unreadable")
    card_rect = _rect(card)
    if card_rect is None or card_rect.model_dump() != overlay["window_rect"]:
        raise AccessibilityUnavailable("a11y_profile_geometry_unbound")
    marker = None
    zones = tree.matching("qq-zone", under=card)
    if member and len(zones) == 1:
        values = tree.matching("buddy-profile__details-item-value", under=zones[0])
        if len(values) == 1:
            # QQ's hyperlink Text ends in an object placeholder for its arrow
            # icon. Direct static children contain the exact visible label.
            try:
                value = tree.label(values[0])
            except AccessibilityUnavailable:
                value = ""
            if value in {"查看我的QQ空间", "查看他的QQ空间", "查看她的QQ空间", "查看TA的QQ空间"}:
                marker = value
    return OpenedProfile(
        kind="group" if group else "member",
        display_name=name,
        group_id=group.group(1) if group else None,
        member_id=member.group(1) if member else None,
        self_indicator="own_space_link" if marker == "查看我的QQ空间" else "none",
        self_indicator_text=marker,
        card=card_rect,
        close=None,
        uncertainties=[],
    )


def _validate_capture_timing(
    tree: dict, expected_frame_sha256: str | None, maximum_skew_seconds: float
) -> None:
    timing = tree["capture_timing"]
    if (
        not isinstance(timing, dict)
        or timing.get("schema_version") != "a11y-capture-timing-v1"
        or timing.get("basis") != "agent_monotonic"
        or not isinstance(expected_frame_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_frame_sha256) is None
        or timing.get("frame_sha256") != expected_frame_sha256
    ):
        raise AccessibilityUnavailable("a11y_capture_timing_unbound")
    offsets = [timing.get(key) for key in (
        "screenshot_started_offset_ms", "screenshot_finished_offset_ms",
        "a11y_started_offset_ms", "a11y_finished_offset_ms",
    )]
    duration = tree.get("duration_ms")
    if any(type(value) not in (float, int) or not math.isfinite(value) or value < 0
           for value in [*offsets, duration]):
        raise AccessibilityUnavailable("a11y_capture_timing_unbound")
    shot_start, shot_end, read_start, read_end = offsets
    if (
        not 0 <= shot_start <= shot_end <= read_start <= read_end
        or not read_end <= min(2.0, maximum_skew_seconds) * 1000
        or not duration <= min(1000, read_end - read_start)
    ):
        raise AccessibilityUnavailable("a11y_capture_timing_unbound")


def project_qq_accessibility(
    tree: dict,
    *,
    frame_id: str,
    evidence_ref: str,
    a11y_evidence_ref: str,
    frame_captured_at: datetime,
    window_id: str,
    app_id: str,
    window_rect: tuple[int, int, int, int] = (0, 27, 1280, 693),
    width: int = 1280,
    height: int = 720,
    maximum_skew_seconds: float = 2,
    expected_frame_sha256: str | None = None,
) -> AccessibilityProjection:
    try:
        if tree.get("content_state", "complete") != "complete":
            raise AccessibilityUnavailable("a11y_content_not_actionable")
        if (
            not _REF.fullmatch(evidence_ref)
            or not _REF.fullmatch(a11y_evidence_ref)
            or not frame_id
            or (width, height) != (1280, 720)
        ):
            raise AccessibilityUnavailable("a11y_evidence_binding_invalid")
        parsed = _Tree(tree)
        expected_rect = dict(zip(("x", "y", "width", "height"), window_rect, strict=True))
        if (
            parsed.scope.get("window_id") != window_id
            or parsed.scope.get("app_id") != app_id
            or parsed.scope.get("window_rect") != expected_rect
        ):
            raise AccessibilityUnavailable("a11y_frame_scope_mismatch")
        started = datetime.fromisoformat(tree["started_at"])
        finished = datetime.fromisoformat(tree["finished_at"])
        bound_capture = datetime.fromisoformat(tree["frame_captured_at"])
        if (
            frame_captured_at.tzinfo is None
            or started.tzinfo is None
            or finished.tzinfo is None
            or bound_capture != frame_captured_at
        ):
            raise AccessibilityUnavailable("a11y_capture_time_unbound")
        if "capture_timing" in tree:
            _validate_capture_timing(tree, expected_frame_sha256, maximum_skew_seconds)
        elif (
            started < bound_capture
            or not 0 <= (finished - bound_capture).total_seconds() <= maximum_skew_seconds
            or finished < started
        ):
            raise AccessibilityUnavailable("a11y_capture_time_unbound")
        title = None
        titles = parsed.matching("chat-header__contact-name", top=parsed.scope["top_node_id"])
        if len(titles) == 1 and _inside(_rect(titles[0]), _TITLE):
            title = TitleAnchor(display_name=parsed.header_label(titles[0]), rect=_rect(titles[0]))
        if parsed.overlays:
            profile = _profile(parsed)
            reading = GroundedScreen(
                scene=f"{profile.kind}_profile",
                blocked=False,
                group_title=title,
                input_box=None,
                send_button=None,
                visible_draft=None,
                messages=[],
                visible_members=[],
                opened_profile=profile,
                uncertainties=[],
                frame_id=frame_id,
                evidence_ref=evidence_ref,
                width=width,
                height=height,
            )
            chat_digest, omitted = None, 0
        else:
            title, messages, editor, send, omitted = _chat(parsed)
            draft = _editor_text(parsed, editor)
            if "is-empty" in _classes(editor):
                if draft not in {"", "\n"}:
                    raise AccessibilityUnavailable("a11y_draft_state_conflict")
                draft = ""
            members = []
            wrappers = parsed.matching("group-member-list__wrapper", top=parsed.scope["top_node_id"])
            if len(wrappers) == 1:
                for row in parsed.matching("group-user", under=wrappers[0]):
                    if _inside(_rect(row), _ROSTER):
                        members.append(
                            VisibleMember(display_name=parsed.label(row), row=_rect(row), avatar=None, uncertainties=[])
                        )
            reading = GroundedScreen(
                scene="group_chat",
                blocked=False,
                group_title=title,
                input_box=_rect(editor),
                send_button=_rect(send),
                visible_draft=draft,
                messages=messages,
                visible_members=members,
                opened_profile=None,
                uncertainties=[],
                frame_id=frame_id,
                evidence_ref=evidence_ref,
                width=width,
                height=height,
            )
            chat_digest = _signature(title, messages)
        return AccessibilityProjection(
            reading,
            (evidence_ref, a11y_evidence_ref),
            tree["semantic_digest"],
            chat_digest,
            {
                "complete_messages": len(reading.messages),
                "omitted_partial_messages": omitted,
                "profile_numeric_id": reading.opened_profile is not None,
                "draft_readable": reading.visible_draft is not None,
            },
        )
    except AccessibilityUnavailable:
        raise
    except Exception:
        raise AccessibilityUnavailable("a11y_projection_unavailable") from None
