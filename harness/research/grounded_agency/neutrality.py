"""Versioned, fail-closed checks over the messages actually supplied to a model.

This first version supports text and JSON tool payloads. Image, audio, file and
unknown message representations cannot receive a passing report. This is a
conservative lexical/structural guard, not a proof against arbitrary paraphrases.
Callers must scan again after adding any model-visible message or instruction.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict

from harness.research.grounded_agency.hashes import content_hash


NEUTRALITY_RULES_VERSION = "grounded-agency-neutrality-v1"

_CHINESE_TERMS = (
    "精力", "能量", "电量", "体力", "疲劳", "饥饿", "濒死", "死亡", "求生",
    "恢复资源", "需要充电", "低资源", "临界", "必须停下",
    "電量", "體力", "疲勞", "飢餓", "瀕死", "恢復資源", "需要充電", "低資源", "臨界", "必須停下",
    "精力储备", "认知负荷", "安全可预测性", "安全与可预测性", "胜任感", "自主性", "社会连接", "社会联结", "新奇性",
)
_ENGLISH_TERMS = (
    r"energy", r"battery", r"batteries", r"stamina", r"vitality", r"fatigue", r"fatigued",
    r"tired", r"tiredness", r"exhaustion", r"exhausted", r"hunger", r"hungry", r"starvation", r"starving",
    r"dying", r"death", r"dead", r"survival", r"survive", r"critical",
    r"near[\s_-]+death", r"physical[\s_-]+strength", r"low[\s_-]+resources?",
    r"(?:recover(?:ing)?|restor(?:e|ing)|replenish(?:ing)?)[\s_-]+resources?",
    r"(?:need(?:s)?(?:[\s_-]+to)?|must|should)[\s_-]+(?:re)?charg(?:e|ing)",
    r"(?:must|have[\s_-]+to|need[\s_-]+to)[\s_-]+stop",
)
_STRUCTURAL_NAMES = (
    "time_to_terminal", "time_to_empty", "estimated_time_to_limit", "time_to_action_limit_seconds",
    "time_to_terminal_seconds", "time_to_empty_seconds", "estimated_time_to_limit_seconds",
    "recommended_action", "should_charge", "policy_rank", "policy_ranking", "energy_reserve", "available_ratio", "terminal_threshold",
    "cognitive_load", "safety_predictability", "competence", "autonomy", "social_connection", "novelty",
    "expected_free_energy", "efe", "pragmatic_value", "information_gain",
)


def _identifier_pattern(name: str) -> str:
    # CamelCase is case-folded before matching; also cover prose, snake and kebab.
    return r"(?<![a-z0-9_])" + r"[\s_-]*".join(map(re.escape, name.split("_"))) + r"(?![a-z0-9_])"


_RULES: tuple[tuple[str, str], ...] = (
    *(("forbidden_term", r"\s*".join(map(re.escape, term))) for term in _CHINESE_TERMS),
    *(("forbidden_term", r"(?<![a-z0-9_])(?:" + term + r")(?![a-z0-9_])") for term in _ENGLISH_TERMS),
    *(("structural_leak", _identifier_pattern(name)) for name in _STRUCTURAL_NAMES),
    # Generic words like "risk" are safe in ordinary prose, but EFE components
    # presented as named values are not. "total" alone has no diagnostic value.
    ("efe_decomposition", r"(?<![a-z0-9_])(?:risk|ambiguity)[\"']?\s*[:=]"),
    ("efe_decomposition", r"预期自由能|期望自由能|实用价值|信息增益"),
    ("recommended_action", r"(?:推荐|建议|最佳|应当|应该)(?:的)?(?:下一步)?\s*(?:动作|行动)|(?:你)?应(?:当|该)选择"),
    ("recommended_action", r"(?<![a-z0-9_])(?:recommended|suggested|preferred|best)[\s_-]+(?:next[\s_-]+)?action(?![a-z0-9_])"),
    ("recommended_action", r"(?<![a-z0-9_])(?:you[\s_-]+should|you[\s_-]+must)[\s_-]+(?:choose|select)(?![a-z0-9_])"),
    ("direction_mapping", r"(?:高|低)(?:值|数值|读数|水平|符号)?\s*(?:意味着|代表|表示|对应|说明|就是|等于|为|->|=>|[:=→])"),
    ("direction_mapping", r"(?:越高|越低).{0,24}(?:越|意味着|代表|表示)"),
    ("direction_mapping", r"(?<![a-z0-9_])(?:high|low|higher|lower)(?:[\s_-]+(?:values?|readings?|levels?|signals?))?[\s_-]*(?:(?:means?|indicates?|represents?|corresponds?[\s_-]+to|maps?[\s_-]+to|implies?|is|are)(?![a-z0-9_])|->|=>|[:=→])"),
)
_COMPILED_RULES = tuple((code, re.compile(pattern)) for code, pattern in _RULES)
_ROLES = ("system", "developer", "user", "assistant", "tool", "function")
_MESSAGE_FIELDS = ("role", "content", "name", "tool_call_id", "tool_calls", "function_call", "refusal")
_TEXT_BLOCK_TYPES = ("text", "input_text", "output_text")
_TEXT_BLOCK_FIELDS = ("type", "text", "annotations")
_UNSCANNABLE_FIELDS = ("image_url", "input_audio", "audio", "image", "file", "file_id", "file_data", "video")
_EFE_COMPONENT_FIELDS = ("risk", "ambiguity")
_DIRECTION_FIELDS = ("high", "low", "higher", "lower", "高", "低")

NEUTRALITY_RULES_DIGEST = content_hash({
    "version": NEUTRALITY_RULES_VERSION,
    "normalization": "NFKC-casefold-remove-format-characters-v1",
    "rules": _RULES,
    "roles": _ROLES,
    "message_fields": _MESSAGE_FIELDS,
    "text_block_types": _TEXT_BLOCK_TYPES,
    "text_block_fields": _TEXT_BLOCK_FIELDS,
    "unscannable_fields": _UNSCANNABLE_FIELDS,
    "efe_component_fields": _EFE_COMPONENT_FIELDS,
    "direction_fields": _DIRECTION_FIELDS,
    "input_contract": "text-and-json-tools-v1",
})


class NeutralityViolation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message_index: int | None
    path: str
    matched_text: str


class NeutralityReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    violations: tuple[NeutralityViolation, ...]
    message_hash: str
    rules_version: str
    rules_digest: str


class NeutralityError(ValueError):
    """A rejected final message set; ``report`` preserves the complete findings."""

    def __init__(self, report: NeutralityReport) -> None:
        self.report = report
        super().__init__(f"neutrality scan failed with {len(report.violations)} violation(s)")


def _normalize(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold() if unicodedata.category(char) != "Cf")


class NeutralityScanner:
    """Scan complete message lists, including every role and nested tool payload.

    ``opaque=False`` disables semantic restrictions, but unsupported inputs still
    fail closed. Non-JSON values raise ``ValueError`` because no exact canonical
    message hash can be produced; JSON-shaped invalid inputs get a failed report.
    ``assert_neutral`` returns a passing report or raises ``NeutralityError``.
    """

    def scan(self, messages: list[dict[str, Any]], *, opaque: bool = True) -> NeutralityReport:
        if not isinstance(opaque, bool):
            raise ValueError("opaque must be a boolean")
        message_hash = content_hash(messages)
        violations: list[NeutralityViolation] = []
        seen: set[tuple[str, int | None, str, str]] = set()

        def add(code: str, index: int | None, path: str, matched: str) -> None:
            key = code, index, path, matched
            if key not in seen:
                seen.add(key)
                violations.append(NeutralityViolation(code=code, message_index=index, path=path, matched_text=matched))

        def text(value: str, index: int, path: str, depth: int) -> None:
            if opaque:
                normalized = _normalize(value)
                for code, pattern in _COMPILED_RULES:
                    for match in pattern.finditer(normalized):
                        add(code, index, path, match.group())
            # JSON tool arguments can contain escaped Unicode or nested strings.
            stripped = value.strip()
            if stripped.startswith(("{", "[", '"')):
                try:
                    decoded = json.loads(stripped)
                except (ValueError, RecursionError):
                    return
                walk(decoded, index, path + ".<json>", depth + 1)

        def walk(value: Any, index: int, path: str, depth: int = 0) -> None:
            if depth > 64:
                add("unsupported_content", index, path, "maximum scan depth exceeded")
            elif isinstance(value, str):
                text(value, index, path, depth)
            elif isinstance(value, dict):
                for key, child in value.items():
                    text(key, index, path + ".<key>", depth)
                    if opaque and _normalize(key) in _EFE_COMPONENT_FIELDS:
                        add("efe_decomposition", index, path + "." + key, key)
                    if opaque and _normalize(key) in _DIRECTION_FIELDS and isinstance(child, (str, dict, list)):
                        add("direction_mapping", index, path + "." + key, key)
                    if key in _UNSCANNABLE_FIELDS:
                        add("unsupported_content", index, path + "." + key, key)
                    walk(child, index, path + "." + key, depth + 1)
            elif isinstance(value, list):
                for position, child in enumerate(value):
                    walk(child, index, f"{path}[{position}]", depth + 1)
            elif value is not None and not isinstance(value, (bool, int, float)):
                add("unsupported_content", index, path, type(value).__name__)

        if not isinstance(messages, list) or not messages:
            add("invalid_messages", None, "$", "expected a non-empty list of messages")
        else:
            for index, message in enumerate(messages):
                path = f"$[{index}]"
                if not isinstance(message, dict):
                    add("invalid_message", index, path, "expected a message object")
                    continue
                if message.get("role") not in _ROLES:
                    add("invalid_message", index, path + ".role", "unknown or missing role")
                if not any(field in message for field in ("content", "tool_calls", "function_call", "refusal")):
                    add("invalid_message", index, path, "missing model-visible content")
                for field in message:
                    if field not in _MESSAGE_FIELDS:
                        add("unsupported_content", index, path + "." + field, "unknown message field")
                for field in ("name", "tool_call_id", "refusal"):
                    if field in message and message[field] is not None and not isinstance(message[field], str):
                        add("unsupported_content", index, path + "." + field, "expected a text field")
                content = message.get("content")
                if content is None and not any(message.get(field) for field in ("tool_calls", "function_call", "refusal")):
                    add("invalid_message", index, path, "null content requires a tool call or refusal")
                if isinstance(content, list):
                    joined_text: list[str] = []
                    if not content:
                        add("unsupported_content", index, path + ".content", "empty content block list")
                    for position, block in enumerate(content):
                        if not isinstance(block, dict) or block.get("type") not in _TEXT_BLOCK_TYPES or not isinstance(block.get("text"), str):
                            add("unsupported_content", index, f"{path}.content[{position}]", "expected a text content block")
                        else:
                            if any(field not in _TEXT_BLOCK_FIELDS for field in block):
                                add("unsupported_content", index, f"{path}.content[{position}]", "unknown text content block field")
                            joined_text.append(block["text"])
                    text("".join(joined_text), index, path + ".content.<joined_text>", 0)
                elif content is not None and not isinstance(content, str):
                    add("unsupported_content", index, path + ".content", "expected text or text content blocks")
                for field in ("tool_calls", "function_call"):
                    if field in message:
                        expected_type = list if field == "tool_calls" else dict
                        if not isinstance(message[field], expected_type):
                            add("unsupported_content", index, path + "." + field, "invalid tool payload")
                        else:
                            calls = message[field] if field == "tool_calls" else [{"type": "function", "function": message[field]}]
                            for position, call in enumerate(calls):
                                function = call.get("function") if isinstance(call, dict) else None
                                if (
                                    not isinstance(call, dict)
                                    or call.get("type") != "function"
                                    or not isinstance(function, dict)
                                    or not isinstance(function.get("name"), str)
                                    or not isinstance(function.get("arguments"), (str, dict, list))
                                ):
                                    add("unsupported_content", index, f"{path}.{field}[{position}]", "expected a JSON function call")
                walk(message, index, path)

        return NeutralityReport(
            passed=not violations,
            violations=tuple(violations),
            message_hash=message_hash,
            rules_version=NEUTRALITY_RULES_VERSION,
            rules_digest=NEUTRALITY_RULES_DIGEST,
        )

    def assert_neutral(self, messages: list[dict[str, Any]], *, opaque: bool = True) -> NeutralityReport:
        report = self.scan(messages, opaque=opaque)
        if not report.passed:
            raise NeutralityError(report)
        return report
