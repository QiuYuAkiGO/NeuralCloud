"""Bounded, deterministic repair of model output (never persisted/input JSON)."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import math
import re
import time
from uuid import uuid4

from jsonschema import Draft202012Validator
from json_repair import repair_json

from harness.llm.deadline import remaining_model_seconds

MAX_BYTES = 1024 * 1024
MAX_DEPTH = 64
MAX_CANDIDATES = 32
logger = logging.getLogger(__name__)


class JSONOutputError(ValueError):
    def __init__(self, code, diagnostics=()):
        super().__init__(code)
        self.error_code = code
        self.diagnostics = list(diagnostics)


@dataclass
class JSONOutput:
    value: object
    text: str
    actions: dict[str, int]
    elapsed_ms: float
    validated: bool


def _reject_constant(value):
    raise JSONOutputError("json_output_nonfinite")


def _scan(text, marker):
    """Select roots without treating nested objects or quoted braces as roots.

    Missing values are explicitly marked before the library can invent defaults.
    Quotes are delimiters only inside a root, so apostrophes in prose are harmless.
    """
    roots, stack, start, quote, escaped = [], [], None, None, False
    inserts, keys, duplicates = [], [], 0
    token_start = 0
    i = 0
    while i < len(text):
        if i % 4096 == 0:
            remaining_model_seconds()
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if start is not None and text.startswith(("//", "/*"), i):
            end = text.find("\n" if text[i:i+2] == "//" else "*/", i + 2)
            i = len(text) if end < 0 else end + (1 if text[i:i+2] == "//" else 2)
            continue
        if start is not None and ch in "\"'":
            quote = ch
        elif ch in "{[":
            if start is None:
                start = i
                inserts, duplicates = [], 0
            stack.append(ch)
            keys.append(set())
            token_start = i + 1
            if len(stack) > MAX_DEPTH:
                raise JSONOutputError("json_output_depth_limit")
        elif start is not None and ch == ":":
            raw_key = text[token_start:i].strip()
            try:
                key = json.loads(raw_key)
                if not isinstance(key, str):
                    key = raw_key
            except ValueError:
                key = raw_key.strip("\"'")
            if keys and stack[-1] == "{":
                if key in keys[-1]:
                    duplicates += 1
                keys[-1].add(key)
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j == len(text) or text[j] in ",}]":
                inserts.append((j, json.dumps(marker)))
        elif start is not None and ch == ",":
            token_start = i + 1
        elif start is not None and ch in "}]":
            if not stack or stack[-1] != ("{" if ch == "}" else "["):
                raise JSONOutputError("json_output_mismatched_bracket")
            stack.pop()
            keys.pop()
            if not stack:
                roots.append((start, i + 1, inserts, duplicates))
                start = None
                if len(roots) > MAX_CANDIDATES:
                    raise JSONOutputError("json_output_candidate_limit")
        i += 1
    if start is not None:
        roots.append((start, len(text), inserts, duplicates))
    if len(roots) > MAX_CANDIDATES:
        raise JSONOutputError("json_output_candidate_limit")
    if not roots:
        raise JSONOutputError("json_output_no_candidate")
    begin, end, inserts, duplicates = roots[-1]
    candidate = text[begin:end]
    for at, value in reversed(inserts):
        candidate = candidate[:at-begin] + value + candidate[at-begin:]
    return candidate, len(roots), duplicates, bool(text[:begin].strip() or text[end:].strip())


def _finite(value, depth=0):
    if depth > MAX_DEPTH:
        raise JSONOutputError("json_output_depth_limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise JSONOutputError("json_output_nonfinite")
    if isinstance(value, (dict, list)):
        for item in value.values() if isinstance(value, dict) else value:
            _finite(item, depth + 1)


def _resolve(schema, root):
    seen = set()
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/") or ref in seen:
            raise JSONOutputError("json_output_schema_reference")
        seen.add(ref)
        target = root
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        siblings = {k: v for k, v in schema.items() if k != "$ref"}
        schema = {"allOf": [target, siblings]} if siblings else target
    return schema


def _valid(value, schema, root):
    # Keep the root's definitions available to local references in sub-schemas.
    return Draft202012Validator(root).evolve(schema=schema).is_valid(value)


def _clauses(schema, root, depth=0):
    if depth > MAX_DEPTH:
        raise JSONOutputError("json_output_schema_depth_limit")
    schema = _resolve(schema, root)
    if isinstance(schema, bool):
        return [{} if schema else {"not": {}}]
    result = [{k: v for k, v in schema.items() if k != "allOf"}]
    for branch in schema.get("allOf", []):
        result.extend(_clauses(branch, root, depth+1))
    return [node for node in result if node] or [{}]


def _project(value, schema, root, marker, depth=0):
    remaining_model_seconds()
    if depth > MAX_DEPTH:
        raise JSONOutputError("json_output_depth_limit")
    if value == marker:
        for empty in (None, ""):
            if _valid(empty, schema, root):
                return empty, 0, 1
        raise JSONOutputError("json_output_missing_value")
    clauses = _clauses(schema, root)
    for index, node in enumerate(clauses):
        for union in ("anyOf", "oneOf"):
            if union not in node:
                continue
            base = {k: v for k, v in node.items() if k != union}
            options = []
            for branch in node[union]:
                branch_schema = {"allOf": clauses[:index] + clauses[index+1:] + [base, branch]}
                try:
                    v, n, m = _project(deepcopy(value), branch_schema, root, marker, depth+1)
                    if _valid(v, schema, root):
                        options.append((v, n, m))
                except JSONOutputError:
                    pass
            if not options:
                raise JSONOutputError("json_output_schema_invalid")
            best = min(item[1] for item in options)
            options = [item for item in options if item[1] == best]
            if len({json.dumps(item[0], sort_keys=True, ensure_ascii=False) for item in options}) != 1:
                raise JSONOutputError("json_output_schema_ambiguous")
            return options[0]
    removed = filled = 0
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            children, forbidden = [], False
            for node in clauses:
                matches = []
                if key in node.get("properties", {}):
                    matches.append(node["properties"][key])
                matches.extend(sub for pattern, sub in node.get("patternProperties", {}).items() if re.search(pattern, key))
                if not matches:
                    extra = node.get("additionalProperties", True)
                    if extra is False:
                        forbidden = True
                    elif isinstance(extra, dict):
                        matches.append(extra)
                children.extend(matches)
            if forbidden:
                removed += 1
                continue
            child = {"allOf": children} if children else {}
            result[key], n, m = _project(item, child, root, marker, depth+1)
            removed += n
            filled += m
        value = result
    elif isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            children = []
            for node in clauses:
                prefix = node.get("prefixItems", [])
                children.append(prefix[index] if index < len(prefix) else node.get("items", {}))
            v, n, m = _project(item, {"allOf": children}, root, marker, depth+1)
            result.append(v)
            removed += n
            filled += m
        value = result
    return value, removed, filled


def model_json_value(text, *, schema=None, legacy=None, settings=None):
    """Adapter for model-only parsers, with their exact disabled-mode decoder."""
    from harness.config import get_settings
    settings = settings or get_settings()
    if not settings.json_repair_enabled:
        return (legacy or json.loads)(text)
    return decode_model_json(text, schema=schema, settings=settings).value


def validate_model_json(model, text, *, repair_schema=None, **kwargs):
    from harness.config import get_settings
    if not get_settings().json_repair_enabled:
        return model.model_validate_json(text, **kwargs)
    # JSON validation preserves Pydantic's JSON-specific strict conversions.
    result = decode_model_json(text, schema=repair_schema if repair_schema is not None else model.model_json_schema())
    return model.model_validate_json(json.dumps(result.value, ensure_ascii=False), **kwargs)


def decode_model_json(text, *, schema=None, settings=None, metadata=None, summary=None):
    """Decode exactly one effective object. The original response is never mutated."""
    from harness.config import get_settings
    from infra.trace.json_repair_evidence import record_repair
    settings = settings or get_settings()
    started = time.monotonic()
    actions, effective, error = {}, None, None
    try:
        remaining_model_seconds()
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise JSONOutputError("json_output_size_limit")
        marker = "__missing_json_value_" + uuid4().hex
        candidate, count, duplicates, extracted = _scan(text, marker)
        if count > 1:
            actions["last_candidate"] = count - 1
        if extracted:
            actions["extract"] = 1
        if duplicates:
            actions["duplicate_key"] = duplicates
        try:
            parsed_duplicates = 0
            def last_pairs(pairs):
                nonlocal parsed_duplicates
                result = {}
                for key, item in pairs:
                    if key in result:
                        parsed_duplicates += 1
                    result[key] = item
                return result
            value = json.loads(candidate, parse_constant=_reject_constant, object_pairs_hook=last_pairs)
            if parsed_duplicates:
                actions["duplicate_key"] = parsed_duplicates
        except json.JSONDecodeError:
            # Reject bare nonfinite constants before a tolerant parser can turn them into strings.
            outside = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', candidate)
            if re.search(r'(?<![\w])(?:NaN|[+-]?Infinity)(?![\w])', outside):
                raise JSONOutputError("json_output_nonfinite")
            value = repair_json(candidate, return_objects=True, skip_json_loads=True)
            actions["syntax"] = 1
        _finite(value)
        if not isinstance(value, dict):
            raise JSONOutputError("json_output_object_required")
        value, removed, filled = _project(value, schema or {}, schema or {}, marker)
        if removed:
            actions["extra_fields"] = removed
        if filled:
            actions["empty_values"] = filled
        effective = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if schema is not None:
            errors = list(Draft202012Validator(schema).iter_errors(value))[:8]
            if errors:
                names = set()
                def collect(node):
                    if isinstance(node, dict):
                        names.update(node.get("properties", {}))
                        for child in node.values():
                            collect(child)
                    elif isinstance(node, list):
                        for child in node:
                            collect(child)
                collect(schema)
                raise JSONOutputError("json_output_schema_invalid", [{"validator": e.validator,
                    "path": [p if isinstance(p, int) or p in names else "*" for p in e.absolute_path],
                    "missing_fields": [k for k in e.validator_value if k in names and k not in e.instance]
                        if e.validator == "required" and isinstance(e.instance, dict) else []} for e in errors])
        remaining_model_seconds()
        effective = effective if actions else text
        return JSONOutput(value, effective, actions, (time.monotonic()-started)*1000, schema is not None)
    except (ValueError, TypeError, RecursionError, KeyError) as exc:
        error = exc if isinstance(exc, JSONOutputError) else JSONOutputError("json_output_invalid")
        error.effective_text = effective
        raise error from None
    except TimeoutError as exc:
        error = exc
        raise
    finally:
        result = {"status": "failed" if error else "repaired" if actions else "unchanged",
                  "actions": actions, "elapsed_ms": round((time.monotonic()-started)*1000, 3),
                  "effective_content_characters": len(effective) if effective else 0}
        if error or actions:
            result.update(record_repair(settings, text, effective, schema, actions, error, metadata))
            logger.info("json_output_repair status=%s actions=%s elapsed_ms=%s evidence_status=%s evidence_ref=%s",
                        result["status"], actions, result["elapsed_ms"], result.get("evidence_status"),
                        result.get("evidence_ref"))
        if summary is not None:
            summary.update(result)
