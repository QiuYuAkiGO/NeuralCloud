"""Deterministic, fail-closed compilation of structured action requests.

The language model never gets to choose an executable skill by emitting prose.
Only the explicit ``cognitive-action-request.v1`` payload accepted here can be
compiled into a :class:`GoalProposal`.  Intent arguments are validated against
an in-process allowlist and all security-relevant fields are assigned by the
profile for that intent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from harness.contracts.decisions import DeferDecision, GoalProposal
from harness.embodiment.contracts import (
    A11yNodeCriterion,
    ActionCriterion,
    ActionIntent,
    EmbodiedActionSpec,
    FileCriterion,
    IntentionalNoopCriterion,
    PointerCriterion,
    ProcessCriterion,
    WindowCriterion,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


Identifier = Annotated[str, Field(min_length=1, max_length=256)]
ShortText = Annotated[str, Field(min_length=1, max_length=1_000)]


class _EmptyArguments(_StrictModel):
    pass


class _WaitArguments(_StrictModel):
    duration_ms: int = Field(ge=0, le=30_000)


class _AppArguments(_StrictModel):
    app_id: Identifier


class _WindowFocusArguments(_StrictModel):
    window_id: Identifier


class _ElementSelector(_StrictModel):
    observation_id: Identifier
    window_id: Identifier | None = None
    app_id: Identifier | None = None
    role: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    name: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    occurrence: int = Field(default=0, ge=0, le=100)


class _UiActivateArguments(_StrictModel):
    selector: _ElementSelector
    count: int = Field(default=1, ge=1, le=2)


class _UiTypeTextArguments(_StrictModel):
    selector: _ElementSelector
    text: str = Field(max_length=100_000)


_KEY_MODIFIERS = frozenset({"ctrl", "alt", "shift", "super"})
_KEY_VALUES = frozenset(
    {
        "enter",
        "escape",
        "tab",
        "backspace",
        "delete",
        "home",
        "end",
        "page_up",
        "page_down",
        "left",
        "right",
        "up",
        "down",
        "space",
        *(chr(code) for code in range(ord("a"), ord("z") + 1)),
        *(str(number) for number in range(10)),
        *(f"f{number}" for number in range(1, 13)),
    }
)


class _UiKeyChordArguments(_StrictModel):
    keys: list[str] = Field(min_length=1, max_length=8)

    @field_validator("keys")
    @classmethod
    def _normalize_keys(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().lower() for item in value]
        allowed = _KEY_MODIFIERS | _KEY_VALUES
        if any(item not in allowed for item in normalized):
            raise ValueError("unsupported key")
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate key")
        if sum(item in _KEY_VALUES for item in normalized) != 1:
            raise ValueError("exactly one non-modifier key is required")
        return normalized


class _UiScrollArguments(_StrictModel):
    selector: _ElementSelector
    direction: Literal["up", "down", "left", "right"]
    clicks: int = Field(ge=1, le=20)


class _UiMovePointerArguments(_StrictModel):
    selector: _ElementSelector


class _UiDragArguments(_StrictModel):
    source: _ElementSelector
    target: _ElementSelector
    duration_ms: int = Field(default=300, ge=50, le=5_000)


def _validate_relative_path(value: str) -> str:
    posix_candidate = PurePosixPath(value)
    windows_candidate = PureWindowsPath(value)
    if value != "." and (
        "\x00" in value
        or posix_candidate.is_absolute()
        or windows_candidate.is_absolute()
        or bool(windows_candidate.drive)
        or ".." in posix_candidate.parts
        or ".." in windows_candidate.parts
        or not posix_candidate.parts
    ):
        raise ValueError("path must remain inside its authorized root")
    return value


class _FileCreateTextArguments(_StrictModel):
    root_id: Identifier
    relative_path: ShortText
    text: str = Field(max_length=1_000_000)
    overwrite: bool = False

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return _validate_relative_path(value)


class _FileMoveArguments(_StrictModel):
    root_id: Identifier
    relative_path: ShortText
    destination_relative_path: ShortText
    overwrite: bool = False

    @field_validator("relative_path", "destination_relative_path")
    @classmethod
    def _relative_paths(cls, value: str) -> str:
        return _validate_relative_path(value)


class _FileDeleteArguments(_StrictModel):
    root_id: Identifier
    relative_path: ShortText

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return _validate_relative_path(value)


class _ShellRunArguments(_StrictModel):
    command_id: Identifier
    args: list[Annotated[str, Field(max_length=4_096)]] = Field(
        default_factory=list,
        max_length=127,
    )
    cwd_root_id: Identifier | None = None
    cwd_relative_path: ShortText | None = None
    environment: dict[Annotated[str, Field(min_length=1, max_length=128)], str] = Field(
        default_factory=dict,
        max_length=64,
    )

    @model_validator(mode="after")
    def _validate_working_directory(self) -> _ShellRunArguments:
        if self.cwd_root_id is None:
            raise ValueError("an authorized working-directory root is required")
        if self.cwd_relative_path is not None:
            _validate_relative_path(self.cwd_relative_path)
        return self


class _ProcessStopArguments(_StrictModel):
    pid: int = Field(gt=0)


class StructuredActionRequest(_StrictModel):
    """The only payload shape that can request an embodied action."""

    schema_version: Literal["cognitive-action-request.v1"] = "cognitive-action-request.v1"
    policy_id: str = Field(min_length=1, max_length=128)
    intent: ActionIntent
    success_criteria: list[ActionCriterion] = Field(min_length=1, max_length=16)


@dataclass(frozen=True, slots=True)
class _IntentProfile:
    arguments_model: type[BaseModel]
    permission_class: Literal["low", "medium"]
    reversible: bool
    required_approvals: tuple[str, ...]
    criterion_kinds: frozenset[str]


_SecurityProfile = tuple[Literal["low", "medium"], bool, tuple[str, ...]]

_LOW_IMPACT: _SecurityProfile = ("low", True, ())
_RESEARCHER_GATED: _SecurityProfile = ("medium", False, ("researcher",))


def _profile(
    arguments_model: type[BaseModel],
    security: _SecurityProfile,
    *criterion_kinds: str,
) -> _IntentProfile:
    permission_class, reversible, approvals = security
    return _IntentProfile(
        arguments_model=arguments_model,
        permission_class=permission_class,
        reversible=reversible,
        required_approvals=approvals,
        criterion_kinds=frozenset(criterion_kinds),
    )


# Security-sensitive values live only in this table.  The request schema does
# not expose permission_class, reversible, verification_method or approvals.
_INTENT_PROFILES: dict[str, _IntentProfile] = {
    "control.wait.v1": _profile(_WaitArguments, _LOW_IMPACT, "intentional_noop"),
    "control.observe.v1": _profile(_EmptyArguments, _LOW_IMPACT, "intentional_noop"),
    "app.open.v1": _profile(_AppArguments, _LOW_IMPACT, "window", "process"),
    "process.start.v1": _profile(_AppArguments, _LOW_IMPACT, "window", "process"),
    "window.focus.v1": _profile(_WindowFocusArguments, _LOW_IMPACT, "window"),
    "ui.move_pointer.v1": _profile(_UiMovePointerArguments, _LOW_IMPACT, "pointer"),
    "ui.scroll.v1": _profile(
        _UiScrollArguments,
        _LOW_IMPACT,
        "a11y_node",
        "window",
    ),
    "ui.activate.v1": _profile(
        _UiActivateArguments,
        _RESEARCHER_GATED,
        "a11y_node",
        "window",
        "file",
        "process",
    ),
    "ui.type_text.v1": _profile(
        _UiTypeTextArguments,
        _RESEARCHER_GATED,
        "a11y_node",
        "file",
    ),
    "ui.key_chord.v1": _profile(
        _UiKeyChordArguments,
        _RESEARCHER_GATED,
        "window",
        "file",
        "process",
    ),
    "ui.drag.v1": _profile(
        _UiDragArguments,
        _RESEARCHER_GATED,
        "a11y_node",
        "pointer",
    ),
    "file.create_text.v1": _profile(_FileCreateTextArguments, _RESEARCHER_GATED, "file"),
    "file.move.v1": _profile(_FileMoveArguments, _RESEARCHER_GATED, "file"),
    "file.delete.v1": _profile(_FileDeleteArguments, _RESEARCHER_GATED, "file"),
    "shell.run.v1": _profile(
        _ShellRunArguments,
        _RESEARCHER_GATED,
        "file",
        "process",
        "window",
        "a11y_node",
    ),
    "process.stop.v1": _profile(_ProcessStopArguments, _RESEARCHER_GATED, "process"),
}


@dataclass(frozen=True, slots=True)
class _Policy:
    policy_id: str
    label: str
    rank: int
    action_request: Any = None


class _PlanningRejected(ValueError):
    def __init__(self, reason: str, required_evidence: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.required_evidence = required_evidence


class ActionDecisionPlanner:
    """Compile an explicit request or the top autonomous policy, never prose."""

    def decide(
        self,
        payload: dict[str, Any],
        *,
        mode: str,
    ) -> GoalProposal | DeferDecision | None:
        explicit = "action_request" in payload
        raw_request = payload.get("action_request") if explicit else None
        policy_hint: str | None = None

        try:
            if explicit:
                policies = self._read_policies(payload)
            elif mode == "autonomous":
                raw_policies = payload.get("policies")
                if not isinstance(raw_policies, list) or not raw_policies:
                    return None
                policies = self._read_policies(payload)
            else:
                policies = []
            if not explicit and mode == "autonomous":
                selected = min(policies, key=lambda item: item.rank)
                if selected.action_request is None:
                    return None
                raw_request = selected.action_request
                policy_hint = selected.policy_id
            if raw_request is None:
                if explicit:
                    raise _PlanningRejected(
                        "structured action request is missing",
                        "valid cognitive-action-request.v1 payload",
                    )
                return None

            policy_hint = self._safe_policy_id(raw_request) or policy_hint
            try:
                request = StructuredActionRequest.model_validate(raw_request)
            except ValidationError as exc:
                raise _PlanningRejected(
                    "structured action request failed strict schema validation",
                    "valid cognitive-action-request.v1 payload",
                ) from exc

            policy = self._matching_policy(policies, request.policy_id)
            if mode == "autonomous" and policy.rank != min(item.rank for item in policies):
                raise _PlanningRejected(
                    "autonomous action does not reference the highest-ranked policy",
                    "highest-ranked autonomous policy",
                )
            profile = _INTENT_PROFILES.get(request.intent.intent_id)
            if profile is None:
                raise _PlanningRejected(
                    "action intent is not in the cognitive allowlist",
                    "provider-supported typed action intent",
                )
            try:
                normalized_arguments = profile.arguments_model.model_validate(request.intent.arguments).model_dump(
                    mode="json"
                )
            except ValidationError as exc:
                raise _PlanningRejected(
                    "action intent arguments failed strict validation",
                    "valid typed arguments for the selected intent",
                ) from exc

            self._validate_criteria(request, profile, normalized_arguments)
            criteria = list(request.success_criteria)
            return GoalProposal(
                policy_id=policy.policy_id,
                goal=policy.label,
                success_criteria=[self._criterion_summary(item) for item in criteria],
                permission_class=profile.permission_class,
                reversible=profile.reversible,
                verification_method="provider_typed_criteria.v1",
                required_approvals=list(profile.required_approvals),
                embodied_action=EmbodiedActionSpec(
                    intent=ActionIntent(
                        intent_id=request.intent.intent_id,
                        arguments=normalized_arguments,
                    ),
                    success_criteria=criteria,
                ),
            )
        except _PlanningRejected as exc:
            return DeferDecision(
                policy_id=policy_hint,
                reason=f"structured action deferred: {exc.reason}",
                required_evidence=[exc.required_evidence],
            )

    @staticmethod
    def _safe_policy_id(raw_request: Any) -> str | None:
        if not isinstance(raw_request, dict):
            return None
        value = raw_request.get("policy_id")
        if isinstance(value, str) and 0 < len(value) <= 128:
            return value
        return None

    @staticmethod
    def _read_policies(payload: dict[str, Any]) -> list[_Policy]:
        raw_policies = payload.get("policies")
        if not isinstance(raw_policies, list) or not raw_policies:
            raise _PlanningRejected(
                "matching policy context is unavailable",
                "matching homeostasis policy candidate",
            )
        if len(raw_policies) > 64:
            raise _PlanningRejected(
                "policy context exceeds its bounded size",
                "bounded homeostasis policy candidates",
            )
        policies: list[_Policy] = []
        seen: set[str] = set()
        for raw in raw_policies:
            if not isinstance(raw, dict):
                raise _PlanningRejected(
                    "policy context is malformed",
                    "valid homeostasis policy candidates",
                )
            policy_id = raw.get("policy_id")
            label = raw.get("label")
            rank = raw.get("rank")
            if (
                not isinstance(policy_id, str)
                or not 0 < len(policy_id) <= 128
                or not isinstance(label, str)
                or not 0 < len(label) <= 4_096
                or isinstance(rank, bool)
                or not isinstance(rank, int)
                or rank < 1
            ):
                raise _PlanningRejected(
                    "policy context is malformed",
                    "valid homeostasis policy candidates",
                )
            if policy_id in seen:
                raise _PlanningRejected(
                    "policy context contains duplicate identifiers",
                    "unambiguous homeostasis policy candidates",
                )
            seen.add(policy_id)
            policies.append(
                _Policy(
                    policy_id=policy_id,
                    label=label,
                    rank=rank,
                    action_request=raw.get("action_request"),
                )
            )
        return policies

    @staticmethod
    def _matching_policy(policies: list[_Policy], policy_id: str) -> _Policy:
        for policy in policies:
            if policy.policy_id == policy_id:
                return policy
        raise _PlanningRejected(
            "structured action references an unavailable policy",
            "matching homeostasis policy candidate",
        )

    @staticmethod
    def _validate_criteria(
        request: StructuredActionRequest,
        profile: _IntentProfile,
        arguments: dict[str, Any],
    ) -> None:
        for criterion in request.success_criteria:
            if criterion.kind not in profile.criterion_kinds:
                raise _PlanningRejected(
                    "success criterion is incompatible with the selected intent",
                    "compatible typed success criterion",
                )
            if isinstance(criterion, WindowCriterion) and not (
                criterion.app_id or criterion.window_id or criterion.title
            ):
                raise _PlanningRejected(
                    "window criterion has no selector",
                    "window criterion with an explicit selector",
                )
            if isinstance(criterion, A11yNodeCriterion) and not (criterion.app_id or criterion.role or criterion.name):
                raise _PlanningRejected(
                    "accessibility criterion has no selector",
                    "accessibility criterion with an explicit selector",
                )
            if isinstance(criterion, FileCriterion):
                try:
                    _validate_relative_path(criterion.relative_path)
                except ValueError as exc:
                    raise _PlanningRejected(
                        "file criterion escapes its authorized root",
                        "file criterion within an authorized root",
                    ) from exc

        ActionDecisionPlanner._validate_intent_binding(request, arguments)

    @staticmethod
    def _validate_intent_binding(
        request: StructuredActionRequest,
        arguments: dict[str, Any],
    ) -> None:
        intent_id = request.intent.intent_id
        criteria = request.success_criteria
        if intent_id == "control.wait.v1":
            bound = any(isinstance(item, IntentionalNoopCriterion) and item.condition == "waited" for item in criteria)
        elif intent_id == "control.observe.v1":
            bound = any(
                isinstance(item, IntentionalNoopCriterion) and item.condition == "observed" for item in criteria
            )
        elif intent_id in {"app.open.v1", "process.start.v1"}:
            bound = any(
                (
                    isinstance(item, WindowCriterion)
                    and item.app_id == arguments["app_id"]
                    and item.condition in {"present", "present_and_focused", "title_equals"}
                )
                or (
                    isinstance(item, ProcessCriterion)
                    and item.app_id == arguments["app_id"]
                    and item.condition == "running"
                )
                for item in criteria
            )
        elif intent_id == "window.focus.v1":
            bound = any(
                isinstance(item, WindowCriterion)
                and item.window_id == arguments["window_id"]
                and item.condition == "present_and_focused"
                for item in criteria
            )
        elif intent_id == "file.create_text.v1":
            bound = any(
                isinstance(item, FileCriterion)
                and item.root_id == arguments["root_id"]
                and item.relative_path == arguments["relative_path"]
                and item.condition in {"exists", "sha256_equals", "size_equals"}
                for item in criteria
            )
        elif intent_id == "file.move.v1":
            destination_verified = any(
                isinstance(item, FileCriterion)
                and item.root_id == arguments["root_id"]
                and item.relative_path == arguments["destination_relative_path"]
                and item.condition in {"exists", "sha256_equals", "size_equals"}
                for item in criteria
            )
            source_absent = any(
                isinstance(item, FileCriterion)
                and item.root_id == arguments["root_id"]
                and item.relative_path == arguments["relative_path"]
                and item.condition == "absent"
                for item in criteria
            )
            bound = destination_verified and source_absent
        elif intent_id == "file.delete.v1":
            bound = any(
                isinstance(item, FileCriterion)
                and item.root_id == arguments["root_id"]
                and item.relative_path == arguments["relative_path"]
                and item.condition == "absent"
                for item in criteria
            )
        elif intent_id == "process.stop.v1":
            bound = any(
                isinstance(item, ProcessCriterion) and item.pid == arguments["pid"] and item.condition == "not_running"
                for item in criteria
            )
        elif intent_id == "ui.move_pointer.v1":
            bound = any(
                isinstance(item, PointerCriterion)
                and ActionDecisionPlanner._selector_matches(arguments["selector"], item)
                for item in criteria
            )
        elif intent_id == "ui.drag.v1":
            bound = any(ActionDecisionPlanner._selector_matches(arguments["target"], item) for item in criteria)
        elif intent_id in {"ui.activate.v1", "ui.type_text.v1", "ui.scroll.v1"}:
            bound = any(ActionDecisionPlanner._selector_matches(arguments["selector"], item) for item in criteria)
        elif intent_id == "ui.key_chord.v1":
            bound = True
        else:
            bound = True
        if not bound:
            raise _PlanningRejected(
                "success criteria do not identify the requested action target",
                "target-bound typed success criterion",
            )

    @staticmethod
    def _selector_matches(selector: dict[str, Any], criterion: ActionCriterion) -> bool:
        fields: tuple[str, ...]
        if isinstance(criterion, WindowCriterion):
            fields = ("app_id", "window_id")
        elif isinstance(criterion, (A11yNodeCriterion, PointerCriterion)):
            fields = ("app_id", "role", "name")
        else:
            return False
        selected = {field: selector.get(field) for field in fields if selector.get(field) is not None}
        return bool(selected) and all(getattr(criterion, field) == value for field, value in selected.items())

    @staticmethod
    def _criterion_summary(criterion: ActionCriterion) -> str:
        payload = criterion.model_dump(mode="json")
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["ActionDecisionPlanner", "StructuredActionRequest"]
