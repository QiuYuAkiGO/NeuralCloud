"""Plain-text, small-model-first assessment for autonomous speech."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from harness.compiler.compiler import emotion_enabled, render_emotion_guidance, render_homeostatic_interoception
from harness.homeostasis.models import HomeostaticInteroceptiveSummary
from harness.llm.client import ChatResult, resolve_aux_endpoint
from harness.validator.validator import contains_homeostatic_mechanism_leak

AUTONOMOUS_PROMPT_VERSION = "autonomous-assessment-v2"
_FINAL_BODY_MARKER = "--- FINAL BODY ---"
_ATTEMPTS_MARKER = "--- ATTEMPTS ---"

_SYSTEM = """你负责判断角色是否应在没有用户新消息时主动开口。
这不是让你代替角色说话，而是一次私下、克制的内部判断。
只有当现在开口对关系、边界、安全或明确的内在需要确有价值时才选择 SPEAK；
普通的时间流逝、轻微波动、无明确对象的冲动都选择 SILENT。
较明显的内在需要可以成为开口理由，但并不强制 SPEAK；仍需判断此刻表达感受、需要或边界是否自然且有价值。
不要在理由中复述任何内部标签、指标、数值、策略编号或形成机制。

输出必须是纯文本：首个非空行只能是 SILENT 或 SPEAK。
之后用简短自然语言记录判断理由。不要输出 JSON、Markdown 代码块或准备发送给用户的话。
"""


@dataclass(frozen=True)
class AssessmentAttempt:
    evaluator: str
    status: Literal["valid", "invalid", "error", "unavailable"]
    output: str = ""
    error: str = ""


@dataclass(frozen=True)
class AssessmentRun:
    assessment_id: str
    decision: Literal["silent", "speak"]
    body: str
    evaluator: Literal["aux", "main_fallback", "forced_silent"]
    model_version: str
    prompt_version: str
    input_hash: str
    evaluated_at: datetime
    record_ref: str
    attempts: tuple[AssessmentAttempt, ...] = ()


def assessment_id_for(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"assessment_{digest}"


def assessment_record_path(log_dir: Path, assessment_id: str, evaluated_at: datetime) -> Path:
    day = evaluated_at.astimezone(UTC).strftime("%Y%m%d")
    return (log_dir / day / f"{assessment_id}.txt").resolve()


def parse_assessment_output(text: str) -> tuple[Literal["silent", "speak"], str] | None:
    lines = text.replace("\ufeff", "").splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return None
    marker = lines[first].strip().casefold()
    if marker not in {"silent", "speak"}:
        return None
    body = "\n".join(lines[first + 1 :]).strip()
    if marker == "speak" and not body:
        return None
    if contains_homeostatic_mechanism_leak(body):
        return None
    return marker, body  # type: ignore[return-value]


def run_or_load_assessment(
    *,
    pipeline: Any,
    log_dir: Path,
    assessment_id: str,
    evaluated_at: datetime,
    session_id: str,
    channel: str,
    homeostasis: dict[str, Any],
    policies: list[dict[str, Any]],
    homeostatic_interoception: HomeostaticInteroceptiveSummary | dict | None = None,
    emotion_guidance: Any = None,
) -> AssessmentRun:
    target = assessment_record_path(log_dir, assessment_id, evaluated_at)
    messages = _assessment_messages(
        pipeline=pipeline,
        evaluated_at=evaluated_at,
        session_id=session_id,
        channel=channel,
        homeostasis=homeostasis,
        policies=policies,
        homeostatic_interoception=homeostatic_interoception,
        emotion_guidance=emotion_guidance,
    )
    input_hash = hashlib.sha256(
        json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if target.is_file():
        try:
            cached = load_assessment_record(target)
        except (KeyError, TypeError, ValueError):
            cached = None
        if cached is not None:
            if (
                cached.prompt_version == AUTONOMOUS_PROMPT_VERSION
                and cached.input_hash == input_hash
            ):
                return cached
    attempts: list[AssessmentAttempt] = []
    decision: Literal["silent", "speak"] = "silent"
    body = "判定模型不可用，保持静默。"
    evaluator: Literal["aux", "main_fallback", "forced_silent"] = "forced_silent"
    model_version = pipeline.settings.llm_model

    aux_method = getattr(pipeline.llm, "complete_aux_text_result", None)
    aux_distinct = bool(getattr(pipeline.llm, "aux_distinct", False))
    if callable(aux_method) and aux_distinct:
        try:
            result: ChatResult = aux_method(
                messages,
                temperature=0.1,
                max_tokens=pipeline.settings.llm_json_max_tokens,
            )
            parsed = parse_assessment_output(result.text)
            if parsed is not None:
                decision, body = parsed
                evaluator = "aux"
                model_version = resolve_aux_endpoint(pipeline.settings)[1]
                attempts.append(AssessmentAttempt("aux", "valid", output=result.text))
            else:
                attempts.append(AssessmentAttempt("aux", "invalid", output=result.text))
        except Exception as exc:
            attempts.append(AssessmentAttempt("aux", "error", error=f"{type(exc).__name__}: {exc}"))
    else:
        attempts.append(
            AssessmentAttempt("aux", "unavailable", error="distinct auxiliary model is not configured")
        )

    if evaluator != "aux":
        try:
            result = pipeline.llm.complete_chat_result(
                messages,
                temperature=0.1,
                stream=False,
                max_tokens=pipeline.settings.llm_json_max_tokens,
            )
            parsed = parse_assessment_output(result.text)
            if parsed is not None:
                decision, body = parsed
                evaluator = "main_fallback"
                model_version = pipeline.settings.llm_model
                attempts.append(AssessmentAttempt("main_fallback", "valid", output=result.text))
            else:
                attempts.append(AssessmentAttempt("main_fallback", "invalid", output=result.text))
        except Exception as exc:
            attempts.append(
                AssessmentAttempt(
                    "main_fallback",
                    "error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    run = AssessmentRun(
        assessment_id=assessment_id,
        decision=decision,
        body=body,
        evaluator=evaluator,
        model_version=model_version,
        prompt_version=AUTONOMOUS_PROMPT_VERSION,
        input_hash=input_hash,
        evaluated_at=evaluated_at,
        record_ref=str(target),
        attempts=tuple(attempts),
    )
    _write_assessment_record(target, run)
    return run


def load_assessment_record(path: Path) -> AssessmentRun:
    text = path.read_text(encoding="utf-8")
    if _FINAL_BODY_MARKER not in text or _ATTEMPTS_MARKER not in text:
        raise ValueError(f"invalid autonomous assessment record: {path}")
    header, remainder = text.split(_FINAL_BODY_MARKER, 1)
    body_text, attempts_text = remainder.split(_ATTEMPTS_MARKER, 1)
    metadata: dict[str, str] = {}
    for line in header.splitlines()[1:]:
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip()
    decision = metadata.get("decision")
    evaluator = metadata.get("evaluator")
    if decision not in {"silent", "speak"}:
        raise ValueError(f"invalid assessment decision in {path}")
    if evaluator not in {"aux", "main_fallback", "forced_silent"}:
        raise ValueError(f"invalid assessment evaluator in {path}")
    evaluated_at = datetime.fromisoformat(metadata["evaluated_at"])
    body = body_text.strip()
    if (decision == "speak" and not body) or contains_homeostatic_mechanism_leak(body):
        raise ValueError(f"unsafe assessment body in {path}")
    return AssessmentRun(
        assessment_id=metadata["assessment_id"],
        decision=decision,  # type: ignore[arg-type]
        body=body,
        evaluator=evaluator,  # type: ignore[arg-type]
        model_version=metadata["model_version"],
        prompt_version=metadata["prompt_version"],
        input_hash=metadata.get("input_hash", ""),
        evaluated_at=evaluated_at,
        record_ref=str(path.resolve()),
        attempts=(AssessmentAttempt("record", "valid", output=attempts_text.strip()),),
    )


def _write_assessment_record(path: Path, run: AssessmentRun) -> None:
    attempts: list[str] = []
    for attempt in run.attempts:
        attempts.append(f"=== {attempt.evaluator} | {attempt.status} ===")
        if attempt.output:
            attempts.append(attempt.output.rstrip())
        if attempt.error:
            attempts.append(attempt.error.rstrip())
    content = "\n".join(
        [
            "AUTONOMOUS_ASSESSMENT_V2",
            f"assessment_id: {run.assessment_id}",
            f"evaluated_at: {run.evaluated_at.isoformat()}",
            f"decision: {run.decision}",
            f"evaluator: {run.evaluator}",
            f"model_version: {run.model_version}",
            f"prompt_version: {run.prompt_version}",
            f"input_hash: {run.input_hash}",
            _FINAL_BODY_MARKER,
            run.body,
            _ATTEMPTS_MARKER,
            *attempts,
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _assessment_messages(
    *,
    pipeline: Any,
    evaluated_at: datetime,
    session_id: str,
    channel: str,
    homeostasis: dict[str, Any],
    policies: list[dict[str, Any]],
    homeostatic_interoception: HomeostaticInteroceptiveSummary | dict | None = None,
    emotion_guidance: Any = None,
) -> list[dict[str, str]]:
    snapshot = pipeline.memory.get_state(now=evaluated_at)
    persona = snapshot.persona.identity if snapshot.persona else None
    recent = pipeline.session_store.recent(session_id)[-6:]
    lines = [
        f"时间：{evaluated_at.isoformat()}",
        f"沟通渠道：{channel}",
        (
            "角色："
            + persona.name
            + "；特质："
            + "、".join(persona.core_traits)
            + "；价值观："
            + "、".join(persona.values)
            if persona
            else "角色：未知"
        ),
    ]
    use_emotion = emotion_enabled(pipeline.settings)
    if snapshot.relationship is not None and not use_emotion:
        rel = snapshot.relationship
        lines.append(
            f"关系：对象={rel.target}，亲近={rel.closeness:.2f}，信任={rel.trust:.2f}，"
            f"张力={rel.recent_tension:.2f}"
        )
    if snapshot.psych is not None and snapshot.psych.short and not use_emotion:
        lines.append("近期心理：" + _plain_mapping(snapshot.psych.short))
    if snapshot.world is not None:
        lines.append(
            f"环境：presence={snapshot.world.user_presence}，activity={snapshot.world.user_activity}"
        )
    if use_emotion:
        lines.append(render_emotion_guidance(emotion_guidance, homeostasis=homeostatic_interoception))
    elif homeostatic_interoception is not None:
        lines.append(
            "角色的内在表达线索："
            + render_homeostatic_interoception(
                homeostatic_interoception,
                purpose="assessment",
            )
        )
    if recent:
        dialogue = "\n".join(f"{turn.speaker or turn.role}：{turn.text}" for turn in recent)
        lines.append("最近真实对话：\n" + dialogue)
    else:
        lines.append("最近真实对话：无")
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": "\n\n".join(lines)},
    ]


def _plain_mapping(value: dict[str, Any]) -> str:
    return "，".join(f"{key}={item}" for key, item in value.items())
