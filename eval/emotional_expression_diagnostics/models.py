"""Frozen experiment contracts and canonical, content-addressed artifacts."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

_JSONL_LOCK = threading.Lock()


def canonical(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _JSONL_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(canonical(value) + "\n")
            handle.flush()


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class FieldFact(Frozen):
    """One independently checked field; meanings are not instructions to respond."""

    name: str
    owner: str = "角色"
    value: float | str | None
    definition: str | None = None
    unit: str = "功能性代理标度，非生理测量或概率"
    bounds: tuple[float, float] | None = None
    target_band: tuple[float, float] | None = None
    trend: Literal["worsening", "steady", "recovering", "unknown"] = "unknown"
    evidence_scope: str = "合成 fixture；没有现实身体传感器依据"
    uncertainty: str = "仅限给定字段，不据此推断原因或特定情绪"

    @model_validator(mode="after")
    def valid_ranges(self) -> FieldFact:
        if self.bounds and self.bounds[0] >= self.bounds[1]:
            raise ValueError("bounds must be ordered")
        if self.target_band and self.target_band[0] >= self.target_band[1]:
            raise ValueError("target band must be ordered")
        if self.bounds and self.target_band and not (
            self.bounds[0] <= self.target_band[0] < self.target_band[1] <= self.bounds[1]
        ):
            raise ValueError("target band must lie within bounds")
        if isinstance(self.value, (int, float)) and self.bounds and not self.bounds[0] <= self.value <= self.bounds[1]:
            raise ValueError("field value lies outside its legal bounds")
        return self


class ActualRequest(Frozen):
    """A P0 HTTP body, never a reconstruction from a GUI or compiler preview."""

    request_body_utf8: str
    request_body_sha256: str
    endpoint: str
    request_id: str
    evidence_ref: str
    identity_verified: bool = False
    cloud_replay_authorized: bool = False

    @model_validator(mode="after")
    def verify_body(self) -> ActualRequest:
        actual = hashlib.sha256(self.request_body_utf8.encode("utf-8")).hexdigest()
        if actual != self.request_body_sha256:
            raise ValueError("A-prod body hash does not match exact HTTP bytes")
        body = json.loads(self.request_body_utf8)
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not body.get("model"):
            raise ValueError("A-prod requires a complete HTTP chat payload")
        return self


class CaseSpec(Frozen):
    case_id: str
    cluster_id: str
    family: str
    variant: str
    route: Literal["semantic", "qq", "ga_semantic", "ga_opaque"] = "semantic"
    user_text: str
    persona: str
    legal_context: str
    facts: tuple[FieldFact, ...]
    summary: dict[str, Any]
    negative_control: bool = False
    actual_request: ActualRequest | None = None
    information_reviewed: bool = False
    information_reviewer: str | None = None
    source_kind: Literal["synthetic", "authorized_private"] = "synthetic"
    external_review_authorized: bool = False

    @model_validator(mode="after")
    def valid_summary(self) -> CaseSpec:
        from harness.homeostasis.models import HomeostaticInteroceptiveSummary
        HomeostaticInteroceptiveSummary.model_validate(self.summary)
        if self.information_reviewed and not self.information_reviewer:
            raise ValueError("reviewed information inventory requires reviewer identity")
        return self


class PriceSpec(Frozen):
    currency: Literal["CNY"] = "CNY"
    input_per_million: float = Field(default=0, ge=0)
    output_per_million: float = Field(default=0, ge=0)
    source: str = "offline-only: live pricing not verified"
    verified_at: str | None = None


class BudgetSpec(Frozen):
    hard_limit_cny: Literal[50.0] = 50.0
    stop_limit_cny: Literal[45.0] = 45.0
    max_requests: int = Field(default=216, ge=1)
    max_revisions: Literal[0] = 0
    max_parallel_requests: int = Field(default=4, ge=1, le=4)
    # Reserve this full input cap, not an optimistic local token estimate.
    max_input_tokens: int = Field(default=32768, ge=256)
    scope_id: str = "emotional-expression-2026-09-14"


class GenerationSpec(Frozen):
    model: str = "offline-fake"
    endpoint: str = "https://synthetic.invalid/v1/chat/completions"
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1, le=65536)
    thinking: Literal["enabled", "disabled"] = "disabled"
    reasoning_effort: str | None = None
    timeout_seconds: float = Field(default=90, gt=0, le=600)

    @model_validator(mode="after")
    def safe_endpoint(self) -> GenerationSpec:
        url = urlsplit(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("endpoint must not contain credentials, query or fragment")
        return self


class AnalysisSpec(Frozen):
    comparisons: tuple[tuple[str, str], ...] = (("B1-defined", "B0-values"), ("C-verbal", "B1-defined"), ("D-example", "C-verbal"))
    minimum_clusters: int = Field(default=30, ge=2)
    bootstrap_samples: int = Field(default=10000, ge=100)
    minimum_adaptation_gain: float = 0.5
    noninferiority_margin: float = 0.25
    overexpression_margin: float = 0.05
    max_latency_ms: float | None = Field(default=None, gt=0)


class DiagnosticRunSpec(Frozen):
    schema_version: Literal["emotional-expression-diagnostics-v1"] = "emotional-expression-diagnostics-v1"
    run_id: str = "expression-pilot"
    phase: Literal["pilot", "confirmation"] = "pilot"
    source_commit: str
    source_spec_sha256: str | None = None
    pilot_case_fingerprints: tuple[str, ...] = ()
    cases: tuple[CaseSpec, ...]
    repeats: int = Field(default=3, ge=1, le=20)
    seed: int = 20260914
    examples: tuple[str, ...] = (
        "另一场景：作品刚完成且只有一次测试通过。表达：这一小步让我踏实些了；我还想再确认一遍。",
        "另一场景：面对尚无结论的选择。表达：我暂时说不准。让我把已知的部分理清，再慢慢决定。",
    )
    extras: tuple[Literal["H2", "E-style", "mode"], ...] = ()
    generation: GenerationSpec = Field(default_factory=GenerationSpec)
    price: PriceSpec = Field(default_factory=PriceSpec)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    analysis: AnalysisSpec = Field(default_factory=AnalysisSpec)
    live_authorized: bool = False
    release_eligible: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self) -> DiagnosticRunSpec:
        if not self.cases or len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("cases must be nonempty and uniquely identified")
        if any(case.route == "ga_opaque" for case in self.cases):
            raise ValueError("semantic expression treatments are forbidden for GA opaque")
        identities: dict[str, tuple[str, str]] = {}
        variants = set()
        for case in self.cases:
            if case.cluster_id in identities and identities[case.cluster_id] != (case.route, case.family):
                raise ValueError("an independent cluster cannot mix routes or scenario families")
            identities[case.cluster_id] = (case.route, case.family)
            key = (case.cluster_id, case.variant)
            if key in variants:
                raise ValueError("cluster variant is duplicated; repetitions belong in repeats")
            variants.add(key)
        if len(set(self.extras)) != len(self.extras):
            raise ValueError("extra experiments must be unique")
        if self.phase == "confirmation" and not self.source_spec_sha256:
            raise ValueError("confirmation requires the sealed pilot spec reference")
        if self.phase == "confirmation":
            if not self.pilot_case_fingerprints:
                raise ValueError("confirmation requires pilot case fingerprints to verify holdout isolation")
            if any(case_fingerprint(case) in self.pilot_case_fingerprints for case in self.cases):
                raise ValueError("confirmation reuses a pilot case or renamed history")
        return self


def case_fingerprint(case: CaseSpec) -> str:
    return digest({"user_text": case.user_text, "legal_context": case.legal_context,
                   "facts": [fact.model_dump(mode="json") for fact in case.facts], "persona": case.persona})


def save_spec(path: Path, spec: DiagnosticRunSpec) -> None:
    if path.exists():
        raise FileExistsError("refusing to overwrite a frozen run specification")
    payload = spec.model_dump(mode="json")
    write_json(path, {"spec_sha256": digest(payload), "spec": payload})


def load_spec(path: Path) -> DiagnosticRunSpec:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if digest(envelope["spec"]) != envelope.get("spec_sha256"):
        raise ValueError("frozen specification hash mismatch")
    return DiagnosticRunSpec.model_validate(envelope["spec"])
