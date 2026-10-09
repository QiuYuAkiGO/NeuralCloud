"""Bounded, advisory expression planning. None of these objects grant authority."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

EXPRESSION_VERSION = "reply-internalization-v1"
Reference = Annotated[str, Field(min_length=1, max_length=160)]


class ReplyConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subject: str = Field(min_length=1, max_length=120)
    handling: Literal["omit", "qualify", "clarify", "verify"]
    source_ids: list[Reference] = Field(default_factory=list, max_length=2)


class DisclosureRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subject: str = Field(min_length=1, max_length=120)
    reason: Literal["user_requested", "answer_material", "execution_blocked"]


class ResponseExpressionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["reply-internalization-v1"] = EXPRESSION_VERSION
    mode: Literal["observe", "enforce"]
    audience: str = Field(min_length=1)
    # Four new entries plus an optional retained legacy boundary during migration.
    internal_constraints: list[ReplyConstraint] = Field(default_factory=list, max_length=5)
    disclosure_requirements: list[DisclosureRequirement] = Field(default_factory=list, max_length=3)
    # Verified deliveries only. Excerpts describe style, never historical facts.
    recent_replies: list[Annotated[str, Field(max_length=240)]] = Field(default_factory=list, max_length=8)

    def admitted(self, evidence):
        allowed = {e.source_id for e in evidence.entries if e.audience == self.audience} if evidence else set()
        return self.model_copy(update={"internal_constraints": [c for c in self.internal_constraints
            if not c.source_ids or set(c.source_ids) <= allowed]})


def mode_for(settings, scope):
    scopes = getattr(settings, "reply_internalization_conversations", [])
    return getattr(settings, "reply_internalization_mode", "off") if scopes is None or scope in scopes else "off"


def qq_context(settings, *, group_id, self_member_id, plan=None, style_window=None):
    from harness.qq.conversation import conversation_audience
    mode = mode_for(settings, f"{self_member_id}/{group_id}")
    if mode == "off":
        return None
    constraints = list(plan.internal_constraints) if plan else []
    if plan and plan.necessary_boundary and not any(c.subject == plan.necessary_boundary for c in constraints):
        constraints.append(ReplyConstraint(subject=plan.necessary_boundary, handling="qualify"))
    return ResponseExpressionContext(mode=mode, audience=conversation_audience(group_id),
        internal_constraints=constraints,
        disclosure_requirements=list(plan.disclosure_requirements) if plan else [],
        recent_replies=[row["style_excerpt"][:240] for row in (style_window or {}).get("turns", [])[-8:]])


EXPRESSION_ISSUE_KINDS = {"unnecessary_disclaimer", "rule_narration", "repeated_boundary"}


class ExpressionIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["unnecessary_disclaimer", "rule_narration", "repeated_boundary"]
    quote: str = Field(min_length=1, max_length=1200)
    reason: str = Field(min_length=1, max_length=300)


class ExpressionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["reviewed", "uncertain"]
    issues: list[ExpressionIssue] = Field(max_length=3)


def expression_diagnostics(raw, text, context):
    if context is None:
        return None
    try:
        report = ExpressionReview.model_validate(raw)
        if report.status == "uncertain" and report.issues:
            raise ValueError("uncertain expression report has issues")
        if any(i.quote not in text or (i.kind == "repeated_boundary" and not context.recent_replies)
               for i in report.issues):
            raise ValueError("expression report lacks support")
        return {"version": context.version, "mode": context.mode, **report.model_dump(mode="json")}
    except (ValueError, TypeError):
        return {"version": context.version, "mode": context.mode, "status": "unavailable", "issues": []}
