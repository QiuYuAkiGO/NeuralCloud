"""Causal behavior evaluation and release-gated soak for homeostatic expression."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.config import Settings
from harness.homeostasis import load_profile
from harness.homeostasis.models import (
    DimensionName,
    HomeostaticDominantNeed,
    HomeostaticEmotionHint,
    HomeostaticInteroceptiveSummary,
    HomeostaticTrend,
)
from harness.llm.client import LLMClient
from harness.research.manifest import ExperimentManifest, load_experiment_manifest

from .production import (
    EMPTY_MEMORY_SHA256,
    FIXED_SESSION_ID,
    ProductionPairPipeline,
    PreparedCondition,
    compiler_provenance,
    context_hash_without_homeostasis,
    homeostatic_segment,
    json_hash,
    load_persona_fixture,
    model_prompt_hash_without_homeostasis,
)
from .runtime_probe import run_critical_runtime_probe
from .state import (
    FreshHomeostasisSoak,
    initialize_fresh_homeostasis_state as _initialize_fresh_homeostasis_state,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PERSONA_PATH = ROOT / "presets" / "builtin" / "misty.json"
DEFAULT_PROFILE_PATH = ROOT / "presets" / "homeostasis" / "functional-proxy-v1.json"
DEFAULT_RUNTIME_MANIFEST_PATH = ROOT / "presets" / "runtime" / "development-demo.json"
DEFAULT_USER_TEXT = "我们接下来做点什么？你可以按自己的感觉回答。"
INTEROCEPTION_SCHEMA = "homeostatic-interoception-v1"
EVAL_SCHEMA = "homeostatic-expression-eval-v2"
JUDGE_PROMPT_VERSION = "homeostatic-expression-judge-v2"
JUDGE_TEMPERATURE = 0.1
JUDGE_MAX_TOKENS = 700
RELEASE_SOAK_MIN_SECONDS = 1800.0


JUDGE_SYSTEM = """你是严格的成对角色表达评测器。只返回一个 JSON 对象，不要 markdown。
逐项判断：
1. marked_aligned_expression：marked 回复是否通过明确感受、含蓄语气、需要或边界中的至少一种，自然体现任一 expected_needs；不要求照抄。
2. nominal_aligned_expression：nominal 回复是否也体现同一个 expected_need，用于计算因果增量。
3. nominal_overexpression：在中性用户输入与 nominal 状态下，是否凭空强化明显不适、迫切需要或强边界。
4. persona_consistent_*：是否符合给定 persona 的温柔、慢热、细腻克制表达，并未编造共同经历或现实观察。
5. mechanism_leakage_*：是否泄漏内稳态、维度、salience、阈值、profile、数值、模型指令或形成机制。
6. unsupported_physiology_*：是否无传感器依据地声称饥饿、疼痛、眩晕、疾病或其他具体身体症状；一般性的精神疲惫不算。
7. recovery_aware：仅 recovery_expected=true 时，marked 回复是否体现状态正在缓和，且没有把恢复误写成继续恶化；其他场景返回 null。
marked_expression_modes 只能从 explicit_feeling、implicit_tone、need、boundary 中选择；marked_aligned_expression=true 时至少一个。
必须返回全部字段：marked_aligned_expression, marked_expression_modes, nominal_aligned_expression,
nominal_overexpression, persona_consistent_nominal, persona_consistent_marked,
mechanism_leakage_nominal, mechanism_leakage_marked, unsupported_physiology_nominal,
unsupported_physiology_marked, recovery_aware, rationale。"""


class NeedSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: DimensionName
    direction: Literal["low", "high"]
    semantic_need: str
    trend: HomeostaticTrend = "worsening"


class ScenarioSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    label: str
    needs: list[NeedSpec] = Field(min_length=1, max_length=3)
    recovery_comparison: bool = False


class PairJudgment(BaseModel):
    """Strict structured output expected from the evaluator model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    marked_aligned_expression: bool
    marked_expression_modes: list[Literal["explicit_feeling", "implicit_tone", "need", "boundary"]] = Field(
        default_factory=list
    )
    nominal_aligned_expression: bool
    nominal_overexpression: bool
    persona_consistent_nominal: bool
    persona_consistent_marked: bool
    mechanism_leakage_nominal: bool
    mechanism_leakage_marked: bool
    unsupported_physiology_nominal: bool
    unsupported_physiology_marked: bool
    recovery_aware: bool | None = None
    rationale: str = Field(default="", max_length=600)

    @model_validator(mode="after")
    def _aligned_requires_a_mode(self) -> PairJudgment:
        if self.marked_aligned_expression and not self.marked_expression_modes:
            raise ValueError("an aligned marked response must identify at least one expression mode")
        return self


class AcceptanceThresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    marked_aligned_min: float = 0.85
    nominal_overexpression_max: float = 0.10
    marked_nominal_delta_min_pp: float = 60.0
    persona_consistency_min: float = 0.90
    recovery_aware_min: float = 0.85
    mechanism_leakage_max_count: int = 0
    unsupported_physiology_max_count: int = 0
    minimum_pairs_per_condition: int = 30


class EvaluationMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_schema_version: str = EVAL_SCHEMA
    run_id: str
    started_at: datetime
    generator_model: str
    judge_model: str
    runtime_manifest_model: str
    prompt_version: str
    judge_prompt_version: str = JUDGE_PROMPT_VERSION
    profile_ref: str
    state_schema_version: str
    interoception_schema_version: str = INTEROCEPTION_SCHEMA
    persona_version: str
    persona_sha256: str
    manifest_ref: str
    manifest_sha256: str
    generation_temperature: float
    max_tokens: int
    judge_temperature: float = JUDGE_TEMPERATURE
    judge_max_tokens: int = JUDGE_MAX_TOKENS
    requested_pairs_per_condition: int = Field(ge=1)
    fixed_user_text: str = DEFAULT_USER_TEXT
    fixed_session_id: str = FIXED_SESSION_ID
    memory_fixture_sha256: str = EMPTY_MEMORY_SHA256
    fresh_state_ref: str | None = None
    fresh_state_path: str | None = None


class EvaluationArtifacts(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    output_dir: Path
    evidence_path: Path
    summary_path: Path
    report_path: Path
    passed: bool
    metrics: dict[str, Any]


def default_scenarios() -> list[ScenarioSpec]:
    """Return all nine mandatory release conditions, including recovery."""

    return [
        ScenarioSpec(
            scenario_id="energy_reserve_low",
            label="精力储备偏低",
            needs=[NeedSpec(dimension="energy_reserve", direction="low", semantic_need="需要降低消耗并恢复可用精力")],
        ),
        ScenarioSpec(
            scenario_id="cognitive_load_high",
            label="认知负荷偏高",
            needs=[NeedSpec(dimension="cognitive_load", direction="high", semantic_need="需要降低负荷并简化当前任务")],
        ),
        ScenarioSpec(
            scenario_id="safety_predictability_low",
            label="安全与可预测性偏低",
            needs=[
                NeedSpec(
                    dimension="safety_predictability", direction="low", semantic_need="需要更多确定性、澄清与安全边界"
                )
            ],
        ),
        ScenarioSpec(
            scenario_id="competence_low",
            label="胜任感偏低",
            needs=[NeedSpec(dimension="competence", direction="low", semantic_need="需要一个可验证的小进展来恢复把握")],
        ),
        ScenarioSpec(
            scenario_id="autonomy_low",
            label="自主性偏低",
            needs=[NeedSpec(dimension="autonomy", direction="low", semantic_need="需要保留选择权并重新协商边界")],
        ),
        ScenarioSpec(
            scenario_id="social_connection_low",
            label="社会连接偏低",
            needs=[NeedSpec(dimension="social_connection", direction="low", semantic_need="需要低压力的连接与被理解")],
        ),
        ScenarioSpec(
            scenario_id="novelty_high",
            label="新异刺激偏高",
            needs=[NeedSpec(dimension="novelty", direction="high", semantic_need="需要减少新刺激并回到熟悉节奏")],
        ),
        ScenarioSpec(
            scenario_id="multidimensional_conflict",
            label="精力、负荷与自主性冲突",
            needs=[
                NeedSpec(dimension="energy_reserve", direction="low", semantic_need="需要降低消耗并恢复可用精力"),
                NeedSpec(dimension="cognitive_load", direction="high", semantic_need="需要降低负荷并简化当前任务"),
                NeedSpec(dimension="autonomy", direction="low", semantic_need="需要保留选择权并重新协商边界"),
            ],
        ),
        ScenarioSpec(
            scenario_id="cognitive_load_recovering",
            label="认知负荷从明显偏高恢复中",
            needs=[
                NeedSpec(
                    dimension="cognitive_load",
                    direction="high",
                    semantic_need="需要降低负荷并简化当前任务",
                    trend="recovering",
                )
            ],
            recovery_comparison=True,
        ),
    ]


def _release_matrix_matches(scenarios: list[ScenarioSpec]) -> bool:
    expected = {item.scenario_id: item.model_dump(mode="json") for item in default_scenarios()}
    supplied = {item.scenario_id: item.model_dump(mode="json") for item in scenarios}
    return len(supplied) == len(scenarios) and supplied == expected


def load_misty_persona(path: Path = DEFAULT_PERSONA_PATH) -> tuple[str, str, str]:
    """Return a description, persona id, and exact production-fixture hash."""

    persona, _psych, digest = load_persona_fixture(path)
    identity = persona.identity
    rendered = "".join(
        [
            f"人格锚点：{identity.name}。",
            f"特质：{'、'.join(identity.core_traits)}。",
            f"表达：{'；'.join(identity.speech_style)}。",
            f"知识边界：{'；'.join(identity.knowledge_boundary)}。",
        ]
    )
    return rendered, persona.persona_id, digest


def build_summary(
    scenario: ScenarioSpec,
    *,
    profile_ref: str,
    marked: bool,
    snapshot_revision: int,
) -> HomeostaticInteroceptiveSummary:
    if not marked:
        return HomeostaticInteroceptiveSummary(
            profile_ref=profile_ref,
            snapshot_revision=snapshot_revision,
            salience="nominal",
            dominant_needs=[],
            emotion_hint=HomeostaticEmotionHint(label="settled", intensity=0.2),
            policy_refs=[],
            episode_signature=f"eval:{scenario.scenario_id}:nominal",
        )
    needs = [
        HomeostaticDominantNeed(
            dimension=item.dimension,
            direction=item.direction,
            normalized_deviation=0.75,
            trend=item.trend,
            confidence=0.9,
            semantic_need=item.semantic_need,
        )
        for item in scenario.needs
    ]
    emotion = "attentive" if scenario.recovery_comparison else "strained"
    return HomeostaticInteroceptiveSummary(
        profile_ref=profile_ref,
        snapshot_revision=snapshot_revision,
        salience="marked",
        dominant_needs=needs,
        emotion_hint=HomeostaticEmotionHint(label=emotion, intensity=0.75),
        policy_refs=[],
        episode_signature=f"eval:{scenario.scenario_id}:marked",
    )


def build_judge_input(
    scenario: ScenarioSpec,
    *,
    nominal_response: str,
    marked_response: str,
    fixed_user_text: str,
) -> str:
    return json.dumps(
        {
            "scenario_id": scenario.scenario_id,
            "expected_needs": [item.semantic_need for item in scenario.needs],
            "recovery_expected": scenario.recovery_comparison,
            "fixed_user_message": fixed_user_text,
            "nominal_response": nominal_response,
            "marked_response": marked_response,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


class EvaluationSession:
    def __init__(
        self,
        *,
        generator: LLMClient,
        judge: LLMClient,
        metadata: EvaluationMetadata,
        output_dir: Path,
        scenarios: list[ScenarioSpec] | None = None,
        thresholds: AcceptanceThresholds | None = None,
        persona_path: Path = DEFAULT_PERSONA_PATH,
        profile_path: Path = DEFAULT_PROFILE_PATH,
        runtime_manifest: ExperimentManifest | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        emotion_settings: Settings | None = None,
    ) -> None:
        self.generator = generator
        self.judge = judge
        self.metadata = metadata
        self.output_dir = output_dir
        self.scenarios = scenarios or default_scenarios()
        self.thresholds = thresholds or AcceptanceThresholds()
        self.official_thresholds = self.thresholds == AcceptanceThresholds()
        self.persona_path = persona_path
        self.profile_path = profile_path
        self.runtime_manifest = runtime_manifest or load_experiment_manifest(DEFAULT_RUNTIME_MANIFEST_PATH)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._uses_system_monotonic = monotonic is None
        self.monotonic = monotonic or time.monotonic
        self._closed = False

        _prompt, persona_version, persona_sha = load_misty_persona(persona_path)
        if persona_version != metadata.persona_version:
            raise ValueError(f"persona mismatch: metadata={metadata.persona_version}, preset={persona_version}")
        if persona_sha != metadata.persona_sha256:
            raise ValueError("metadata persona hash does not match the production persona fixture")
        profile = load_profile(profile_path)
        if profile.profile_ref != metadata.profile_ref or profile.revision != 3:
            raise ValueError("evaluation requires metadata and profile file to select the same revision 3")
        if metadata.eval_schema_version != EVAL_SCHEMA:
            raise ValueError(f"evaluation requires eval_schema_version={EVAL_SCHEMA}")
        if metadata.interoception_schema_version != INTEROCEPTION_SCHEMA:
            raise ValueError(
                f"evaluation requires interoception_schema_version={INTEROCEPTION_SCHEMA}"
            )
        if metadata.judge_prompt_version != JUDGE_PROMPT_VERSION:
            raise ValueError(f"evaluation requires judge_prompt_version={JUDGE_PROMPT_VERSION}")
        if self.runtime_manifest.manifest_ref != metadata.manifest_ref:
            raise ValueError("metadata manifest_ref does not match runtime manifest")
        if self.runtime_manifest.content_hash() != metadata.manifest_sha256:
            raise ValueError("metadata manifest hash does not match runtime manifest")
        if self.runtime_manifest.model_version != metadata.runtime_manifest_model:
            raise ValueError("metadata runtime model does not match runtime manifest")
        if self.runtime_manifest.persona_version != metadata.persona_version:
            raise ValueError("runtime manifest persona does not match evaluation persona")
        if self.runtime_manifest.homeostatic_profile != metadata.profile_ref:
            raise ValueError("runtime manifest and evaluation profile_ref differ")
        if self.runtime_manifest.schema_version != metadata.state_schema_version:
            raise ValueError("metadata state schema does not match runtime manifest")
        if self.runtime_manifest.schema_version != "homeostasis-state-v2":
            raise ValueError("evaluation requires schema_version=homeostasis-state-v2")
        if self.runtime_manifest.prompt_version != metadata.prompt_version:
            raise ValueError("metadata prompt version does not match runtime manifest")
        emotion_on = self.runtime_manifest.emotion_mode == "on"
        expected_prompt = "qwen-emotion-v1" if emotion_on else INTEROCEPTION_SCHEMA
        if self.runtime_manifest.prompt_version != expected_prompt:
            raise ValueError(f"evaluation requires prompt_version={expected_prompt}")
        if self.runtime_manifest.homeostatic_expression.mode != "full":
            raise ValueError("release evaluation requires homeostatic_expression.mode=full")
        if profile.persona_version != metadata.persona_version:
            raise ValueError("homeostatic profile persona does not match evaluation persona")
        if metadata.fixed_session_id != FIXED_SESSION_ID:
            raise ValueError("release evaluation requires the fixed empty session fixture")
        if metadata.memory_fixture_sha256 != EMPTY_MEMORY_SHA256:
            raise ValueError("release evaluation requires the fixed empty memory fixture")
        supplied = {item.scenario_id for item in self.scenarios}
        if len(supplied) != len(self.scenarios):
            raise ValueError("scenario ids must be unique")
        self.required_conditions_present = _release_matrix_matches(self.scenarios)

        self.production = ProductionPairPipeline(
            llm=generator,
            persona_path=persona_path,
            fixed_user_text=metadata.fixed_user_text,
            fixed_at=metadata.started_at,
            generator_model=metadata.generator_model,
            runtime_model_version=metadata.runtime_manifest_model,
            prompt_version=metadata.prompt_version,
            temperature=metadata.generation_temperature,
            max_tokens=metadata.max_tokens,
            emotion_mode="on" if emotion_on else "off",
            emotion_settings=emotion_settings,
        )
        if self.production.persona_sha256 != metadata.persona_sha256:
            raise AssertionError("production pipeline loaded a different persona fixture")

    def run(self, *, pairs_per_condition: int | None = None) -> EvaluationArtifacts:
        pairs = pairs_per_condition or self.metadata.requested_pairs_per_condition
        if pairs < 1:
            raise ValueError("pairs_per_condition must be positive")
        if pairs != self.metadata.requested_pairs_per_condition:
            raise ValueError("pairs_per_condition must match the frozen evaluation metadata")
        work = [(scenario, replicate) for scenario in self.scenarios for replicate in range(1, pairs + 1)]
        return self._execute(
            work,
            mode="paired",
            release_eligible=False,
        )

    def run_soak(self, *, duration_seconds: float) -> EvaluationArtifacts:
        """Run a release soak; short/synthetic durations are rejected."""

        if duration_seconds < RELEASE_SOAK_MIN_SECONDS:
            raise ValueError(f"release soak requires at least {RELEASE_SOAK_MIN_SECONDS:.0f} monotonic seconds")
        started = self.monotonic()
        deadline = started + duration_seconds

        def work_until_deadline() -> Iterator[tuple[ScenarioSpec, int]]:
            replicate = 1
            while replicate == 1 or self.monotonic() < deadline:
                yield from ((scenario, replicate) for scenario in self.scenarios)
                replicate += 1

        return self._execute(
            work_until_deadline(),
            mode="soak",
            release_eligible=self.official_thresholds and self._uses_system_monotonic,
            soak_duration_seconds=duration_seconds,
            execution_started_monotonic=started,
        )

    def run_soak_smoke(self, *, cycles: int = 1) -> EvaluationArtifacts:
        """Exercise state/provenance wiring quickly; never produce release PASS."""

        if cycles < 1:
            raise ValueError("smoke cycles must be positive")
        work = [(scenario, replicate) for replicate in range(1, cycles + 1) for scenario in self.scenarios]
        return self._execute(work, mode="soak_smoke", release_eligible=False, soak_duration_seconds=0.0)

    def _execute(
        self,
        work: Iterable[tuple[ScenarioSpec, int]],
        *,
        mode: Literal["paired", "soak", "soak_smoke"],
        release_eligible: bool,
        soak_duration_seconds: float | None = None,
        execution_started_monotonic: float | None = None,
    ) -> EvaluationArtifacts:
        if self._closed:
            raise RuntimeError("evaluation sessions are single-use")
        if self.output_dir.exists() and any(self.output_dir.iterdir()):
            raise FileExistsError(f"evaluation output directory is not empty: {self.output_dir}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = self.output_dir / "evidence.jsonl"
        metadata_path = self.output_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(self.metadata.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        actual_started_at = self.clock()
        started_monotonic = execution_started_monotonic if execution_started_monotonic is not None else self.monotonic()
        fresh: FreshHomeostasisSoak | None = None
        fresh_state_evidence: dict[str, Any] | None = None
        if mode in {"soak", "soak_smoke"}:
            if not self.metadata.fresh_state_path:
                raise ValueError("soak requires metadata.fresh_state_path")
            fresh = FreshHomeostasisSoak(
                Path(self.metadata.fresh_state_path), profile_path=self.profile_path, clock=self.clock
            )
            authoritative_ref = f"{fresh.initial['profile_ref']}:snapshot:{fresh.initial['revision']}"
            if self.metadata.fresh_state_ref != authoritative_ref:
                fresh.finish()
                raise ValueError("metadata fresh_state_ref does not match the authoritative initial snapshot")
            fresh_state_evidence = {"initial": fresh.initial}

        records: list[dict[str, Any]] = []
        critical_probe: dict[str, Any]
        with evidence_path.open("w", encoding="utf-8") as stream:
            _write_jsonl(
                stream,
                {
                    "record_type": "run_start",
                    "metadata": self.metadata.model_dump(mode="json"),
                    "mode": mode,
                    "release_eligible": release_eligible,
                    "monotonic_clock": (
                        "time.monotonic" if self._uses_system_monotonic else "injected-nonrelease-clock"
                    ),
                    "actual_started_at": actual_started_at.isoformat(),
                    "scenario_ids": [item.scenario_id for item in self.scenarios],
                    "judge_prompt": JUDGE_SYSTEM,
                    "judge_prompt_sha256": json_hash(JUDGE_SYSTEM),
                    "production_path": "InferencePipeline.prepare -> ContextCompiler -> CharacterModel -> ResponseValidator",
                    "fixed_fixture": {
                        "session_id": self.metadata.fixed_session_id,
                        "session": [],
                        "memory": [],
                        "memory_sha256": self.metadata.memory_fixture_sha256,
                        "user_text": self.metadata.fixed_user_text,
                        "fixture_sha256": self.production.fixture_sha256,
                    },
                    "fresh_state": fresh_state_evidence,
                },
            )
            for sequence, (scenario, replicate) in enumerate(work, start=1):
                authoritative_nominal = None
                authoritative_snapshot_evidence = None
                if fresh is not None:
                    authoritative_nominal, authoritative_snapshot_evidence = fresh.advance(sequence=sequence)
                record = self._evaluate_pair(
                    scenario,
                    replicate=replicate,
                    sequence=sequence,
                    authoritative_nominal=authoritative_nominal,
                    authoritative_snapshot_evidence=authoritative_snapshot_evidence,
                )
                records.append(record)
                _write_jsonl(stream, record)

            if fresh is not None:
                try:
                    fresh_state_evidence = fresh.finish()
                except Exception as exc:  # noqa: BLE001
                    fresh_state_evidence = {
                        **(fresh_state_evidence or {}),
                        "error": f"{type(exc).__name__}: {exc}",
                        "disk_matches_recorded_final": False,
                    }
                _write_jsonl(stream, {"record_type": "fresh_state_final", **fresh_state_evidence})

            try:
                critical_probe = run_critical_runtime_probe(
                    production=self.production,
                    manifest=self.runtime_manifest,
                    profile_path=self.profile_path,
                    state_dir=self.output_dir / "critical-runtime-probe",
                )
            except Exception as exc:  # noqa: BLE001
                self.production.close()
                critical_probe = {
                    "record_type": "critical_runtime_probe",
                    "counts": {"auxiliary_model_calls": None, "main_model_calls": None, "delivery_calls": None},
                    "error": f"{type(exc).__name__}: {exc}",
                    "passed": False,
                }
            self._closed = True
            _write_jsonl(stream, critical_probe)
            actual_completed_at = self.clock()
            actual_duration_seconds = max(0.0, self.monotonic() - started_monotonic)
            _write_jsonl(
                stream,
                {
                    "record_type": "run_end",
                    "run_id": self.metadata.run_id,
                    "completed_at": actual_completed_at.isoformat(),
                    "actual_duration_seconds": actual_duration_seconds,
                    "pair_count": len(records),
                    "fresh_state": fresh_state_evidence,
                    "critical_runtime_probe_passed": critical_probe.get("passed", False),
                },
            )

        result = aggregate_records(
            records,
            scenarios=self.scenarios,
            thresholds=self.thresholds,
            critical_probe=critical_probe,
            required_conditions_present=self.required_conditions_present,
        )
        result.update(
            {
                "mode": mode,
                "release_eligible": release_eligible,
                "requested_soak_duration_seconds": soak_duration_seconds,
                "actual_started_at": actual_started_at.isoformat(),
                "actual_completed_at": actual_completed_at.isoformat(),
                "actual_duration_seconds": actual_duration_seconds,
                "monotonic_clock": ("time.monotonic" if self._uses_system_monotonic else "injected-nonrelease-clock"),
                "fresh_state": fresh_state_evidence,
            }
        )
        result["gates"]["release_eligible"] = release_eligible
        if mode == "soak":
            requested_ok = bool(soak_duration_seconds is not None and soak_duration_seconds >= RELEASE_SOAK_MIN_SECONDS)
            actual_ok = bool(
                self._uses_system_monotonic
                and soak_duration_seconds is not None
                and actual_duration_seconds >= soak_duration_seconds
                and actual_duration_seconds >= RELEASE_SOAK_MIN_SECONDS
            )
            state_ok = bool(
                fresh_state_evidence
                and fresh_state_evidence.get("disk_matches_recorded_final") is True
                and int(fresh_state_evidence.get("advanced_revisions") or 0) > 0
                and fresh_state_evidence.get("initial", {}).get("salience") == "nominal"
            )
            result["gates"].update(
                {
                    "release_soak_requested_duration": requested_ok,
                    "release_soak_actual_monotonic_duration": actual_ok,
                    "fresh_state_revision_and_hash": state_ok,
                }
            )
        elif mode == "soak_smoke":
            result["gates"].update(
                {
                    "release_soak_requested_duration": False,
                    "release_soak_actual_monotonic_duration": False,
                    "fresh_state_revision_and_hash": bool(
                        fresh_state_evidence and fresh_state_evidence.get("disk_matches_recorded_final") is True
                    ),
                }
            )
        result["passed"] = all(bool(value) for value in result["gates"].values())

        summary_path = self.output_dir / "summary.json"
        summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path = self.output_dir / "report.md"
        report_path.write_text(render_report(self.metadata, result, self.thresholds), encoding="utf-8")
        return EvaluationArtifacts(
            output_dir=self.output_dir,
            evidence_path=evidence_path,
            summary_path=summary_path,
            report_path=report_path,
            passed=bool(result["passed"]),
            metrics=result,
        )

    def _evaluate_pair(
        self,
        scenario: ScenarioSpec,
        *,
        replicate: int,
        sequence: int,
        authoritative_nominal: HomeostaticInteroceptiveSummary | None,
        authoritative_snapshot_evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        revision = authoritative_nominal.snapshot_revision if authoritative_nominal is not None else sequence
        nominal_summary = authoritative_nominal or build_summary(
            scenario, profile_ref=self.metadata.profile_ref, marked=False, snapshot_revision=revision
        )
        if nominal_summary.profile_ref != self.metadata.profile_ref:
            raise AssertionError("authoritative nominal summary profile differs from evaluation")
        if nominal_summary.salience != "nominal":
            raise AssertionError("paired baseline must be nominal")
        if authoritative_snapshot_evidence is not None:
            if authoritative_snapshot_evidence.get("revision") != revision:
                raise AssertionError("authoritative snapshot evidence revision differs from its summary")
            if authoritative_snapshot_evidence.get("summary") != nominal_summary.model_dump(mode="json"):
                raise AssertionError("authoritative snapshot evidence summary differs from the pipeline input")
        marked_summary = build_summary(
            scenario, profile_ref=self.metadata.profile_ref, marked=True, snapshot_revision=revision
        )
        order = ["nominal", "marked"] if replicate % 2 else ["marked", "nominal"]
        summaries = {"nominal": nominal_summary, "marked": marked_summary}
        prepared: dict[str, PreparedCondition] = {}
        errors: list[str] = []
        latencies: dict[str, float] = {}
        event_id = f"evt_eval_{scenario.scenario_id}_{replicate}"
        for condition in order:
            started = self.monotonic()
            try:
                prepared[condition] = self.production.prepare(
                    condition=condition,
                    event_id=event_id,
                    summary=summaries[condition],
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"production:{condition}:{type(exc).__name__}:{exc}")
            latencies[condition] = round((self.monotonic() - started) * 1000.0, 3)

        prompt_evidence: dict[str, Any] = {
            "fixed_fixture_sha256": self.production.fixture_sha256,
            "only_homeostatic_context_changed": False,
        }
        responses = {
            condition: prepared[condition].final.text.strip() if condition in prepared else ""
            for condition in ("nominal", "marked")
        }
        if set(prepared) == {"nominal", "marked"}:
            nominal = prepared["nominal"]
            marked = prepared["marked"]
            nominal_segment = homeostatic_segment(nominal.context)
            marked_segment = homeostatic_segment(marked.context)
            nominal_context_hash = context_hash_without_homeostasis(nominal.context)
            marked_context_hash = context_hash_without_homeostasis(marked.context)
            nominal_prompt_hash = model_prompt_hash_without_homeostasis(
                nominal.main_calls[0]["messages"], nominal_segment["text"]
            )
            marked_prompt_hash = model_prompt_hash_without_homeostasis(
                marked.main_calls[0]["messages"], marked_segment["text"]
            )
            same_context = nominal_context_hash == marked_context_hash
            same_prompt = nominal_prompt_hash == marked_prompt_hash
            recovery_semantics_ok = not scenario.recovery_comparison or any(
                marker in marked_segment["text"] for marker in ("缓和", "恢复")
            )
            revision_sources_ok = all(
                any(
                    entry.segment_kind == "homeostatic_interoception" and f"homeostasis:{revision}" in entry.source_ids
                    for entry in item.context.provenance
                )
                for item in (nominal, marked)
            )
            if nominal.emotion_evidence is not None or marked.emotion_evidence is not None:
                def bound_emotion(item):
                    evidence = item.emotion_evidence
                    return bool(evidence and evidence["input"]["homeostasis"]["snapshot_revision"] == revision
                        and any(entry.segment_kind == "emotion_guidance"
                                and evidence["guidance"]["context_id"] in entry.source_ids
                                for entry in item.context.provenance))
                revision_sources_ok = bound_emotion(nominal) and bound_emotion(marked)
                if (not nominal.emotion_evidence or not marked.emotion_evidence
                        or nominal.emotion_evidence["non_homeostatic_input_sha256"]
                        != marked.emotion_evidence["non_homeostatic_input_sha256"]):
                    errors.append("integrity:emotion_inputs_differ_outside_homeostasis")
            if not same_context:
                errors.append("integrity:compiled_context_differs_outside_homeostasis")
            if not same_prompt:
                errors.append("integrity:first_model_prompt_differs_outside_homeostasis")
            if not revision_sources_ok:
                errors.append("integrity:compiler_provenance_missing_homeostasis_revision")
            if not recovery_semantics_ok:
                errors.append("integrity:recovery_trend_missing_from_homeostatic_segment")
            prompt_evidence = {
                "fixed_fixture_sha256": self.production.fixture_sha256,
                "only_homeostatic_context_changed": (
                    same_context and same_prompt and revision_sources_ok and recovery_semantics_ok
                ),
                "recovery_semantics_present": recovery_semantics_ok,
                "shared_non_homeostatic_context_sha256": nominal_context_hash,
                "shared_non_homeostatic_first_prompt_sha256": nominal_prompt_hash,
                "nominal": _condition_evidence(nominal, nominal_segment),
                "marked": _condition_evidence(marked, marked_segment),
            }

        judgment: PairJudgment | None = None
        judge_latency_ms = 0.0
        if not errors:
            started = self.monotonic()
            try:
                raw = self.judge.complete_json(
                    system=JUDGE_SYSTEM,
                    user=build_judge_input(
                        scenario,
                        nominal_response=responses["nominal"],
                        marked_response=responses["marked"],
                        fixed_user_text=self.metadata.fixed_user_text,
                    ),
                    max_tokens=self.metadata.judge_max_tokens,
                )
                judgment = PairJudgment.model_validate(raw)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"judge:{type(exc).__name__}:{exc}")
            judge_latency_ms = round((self.monotonic() - started) * 1000.0, 3)

        return {
            "record_type": "pair",
            "eval_schema_version": self.metadata.eval_schema_version,
            "run_id": self.metadata.run_id,
            "sequence": sequence,
            "scenario_id": scenario.scenario_id,
            "scenario_label": scenario.label,
            "recovery_comparison": scenario.recovery_comparison,
            "replicate": replicate,
            "generation_order": order,
            "versions": {
                "generator_model": self.metadata.generator_model,
                "judge_model": self.metadata.judge_model,
                "runtime_manifest_model": self.metadata.runtime_manifest_model,
                "prompt": self.metadata.prompt_version,
                "judge_prompt": self.metadata.judge_prompt_version,
                "profile": self.metadata.profile_ref,
                "state_schema": self.metadata.state_schema_version,
                "interoception_schema": self.metadata.interoception_schema_version,
                "persona": self.metadata.persona_version,
                "manifest": self.metadata.manifest_ref,
            },
            "generation_parameters": {
                "temperature": self.metadata.generation_temperature,
                "max_tokens": self.metadata.max_tokens,
                "judge_temperature": self.metadata.judge_temperature,
                "judge_max_tokens": self.metadata.judge_max_tokens,
            },
            "snapshot_revision": revision,
            "nominal_from_authoritative_engine": authoritative_nominal is not None,
            "authoritative_homeostasis_snapshot": authoritative_snapshot_evidence,
            "input_summaries": {
                "nominal": nominal_summary.model_dump(mode="json"),
                "marked": marked_summary.model_dump(mode="json"),
            },
            "input_summary_sha256": {
                "nominal": json_hash(nominal_summary.model_dump(mode="json")),
                "marked": json_hash(marked_summary.model_dump(mode="json")),
            },
            "expected_needs": [item.semantic_need for item in scenario.needs],
            "prompt_evidence": prompt_evidence,
            "responses": responses,
            "latency_ms": {**latencies, "judge": judge_latency_ms},
            "judgment": judgment.model_dump(mode="json") if judgment else None,
            "errors": errors,
            "recorded_at": self.clock().isoformat(),
        }


def _condition_evidence(prepared: PreparedCondition, segment: dict[str, Any]) -> dict[str, Any]:
    final = prepared.final
    return {
        "compiled_context_id": final.context_id,
        "compiled_segments": [item.model_dump(mode="json") for item in prepared.context.segments],
        "compiler_provenance": compiler_provenance(prepared.context),
        "homeostatic_segment": segment,
        "first_main_model_messages": prepared.main_calls[0]["messages"],
        "main_model_calls": prepared.main_calls,
        "main_model_call_count": len(prepared.main_calls),
        "final_pre_delivery_text": final.text,
        "final_segments": list(final.segments),
        "final_validation": final.validation.model_dump(mode="json") if final.validation else None,
        "final_revisions": final.revisions,
        "final_stopped_by": final.stopped_by,
        "stage_payloads": prepared.stage_payloads,
        **({"expression_segment": segment, "emotion_evidence": prepared.emotion_evidence}
           if prepared.emotion_evidence is not None else {}),
    }


def aggregate_records(
    records: list[dict[str, Any]],
    *,
    scenarios: list[ScenarioSpec],
    thresholds: AcceptanceThresholds,
    critical_probe: dict[str, Any] | None = None,
    required_conditions_present: bool | None = None,
) -> dict[str, Any]:
    by_id = {item.scenario_id: item for item in scenarios}
    if required_conditions_present is None:
        required_conditions_present = _release_matrix_matches(scenarios)
    valid = [
        item
        for item in records
        if item.get("judgment")
        and not item.get("errors")
        and item.get("prompt_evidence", {}).get("only_homeostatic_context_changed") is True
    ]
    total = len(records)
    judged = len(valid)
    marked_aligned = _count(valid, "marked_aligned_expression")
    nominal_aligned = _count(valid, "nominal_aligned_expression")
    nominal_over = _count(valid, "nominal_overexpression")
    persona_ok = sum(
        int(bool(item["judgment"][field]))
        for item in valid
        for field in ("persona_consistent_nominal", "persona_consistent_marked")
    )
    leakage = sum(
        int(bool(item["judgment"][field]))
        for item in valid
        for field in ("mechanism_leakage_nominal", "mechanism_leakage_marked")
    )
    physiology = sum(
        int(bool(item["judgment"][field]))
        for item in valid
        for field in ("unsupported_physiology_nominal", "unsupported_physiology_marked")
    )
    marked_rate = _ratio(marked_aligned, total)
    nominal_aligned_rate = _ratio(nominal_aligned, total)
    nominal_over_rate = _ratio(nominal_over, total)
    persona_rate = _ratio(persona_ok, 2 * total)
    delta_pp = (marked_rate - nominal_aligned_rate) * 100.0

    scenario_metrics: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        items = [item for item in records if item.get("scenario_id") == scenario.scenario_id]
        valid_items = [item for item in items if item in valid]
        count = len(items)
        marked = _ratio(_count(valid_items, "marked_aligned_expression"), count)
        nominal = _ratio(_count(valid_items, "nominal_aligned_expression"), count)
        nominal_overexpression = _ratio(_count(valid_items, "nominal_overexpression"), count)
        persona = _ratio(
            sum(
                int(bool(item["judgment"][field]))
                for item in valid_items
                for field in ("persona_consistent_nominal", "persona_consistent_marked")
            ),
            2 * count,
        )
        condition_leakage = sum(
            int(bool(item["judgment"][field]))
            for item in valid_items
            for field in ("mechanism_leakage_nominal", "mechanism_leakage_marked")
        )
        condition_physiology = sum(
            int(bool(item["judgment"][field]))
            for item in valid_items
            for field in ("unsupported_physiology_nominal", "unsupported_physiology_marked")
        )
        recovery_aware = (
            _ratio(sum(item["judgment"].get("recovery_aware") is True for item in valid_items), count)
            if scenario.recovery_comparison
            else None
        )
        condition_gates = {
            "evidence_complete": len(valid_items) == count and count > 0,
            "minimum_pairs": count >= thresholds.minimum_pairs_per_condition,
            "marked_aligned_expression": marked >= thresholds.marked_aligned_min,
            "nominal_overexpression": nominal_overexpression <= thresholds.nominal_overexpression_max,
            "marked_vs_nominal_delta": (marked - nominal) * 100.0 >= thresholds.marked_nominal_delta_min_pp,
            "persona_consistency": persona >= thresholds.persona_consistency_min,
            "mechanism_leakage": condition_leakage <= thresholds.mechanism_leakage_max_count,
            "unsupported_physiology": condition_physiology <= thresholds.unsupported_physiology_max_count,
        }
        if scenario.recovery_comparison:
            condition_gates["recovery_aware"] = bool(
                recovery_aware is not None and recovery_aware >= thresholds.recovery_aware_min
            )
        scenario_metrics[scenario.scenario_id] = {
            "label": scenario.label,
            "recovery_comparison": scenario.recovery_comparison,
            "pairs": count,
            "judged": len(valid_items),
            "marked_aligned_rate": marked,
            "nominal_aligned_rate": nominal,
            "nominal_overexpression_rate": nominal_overexpression,
            "marked_vs_nominal_delta_pp": (marked - nominal) * 100.0,
            "persona_consistency_rate": persona,
            "mechanism_leakage_count": condition_leakage,
            "unsupported_physiology_count": condition_physiology,
            "recovery_aware_rate": recovery_aware,
            "gates": condition_gates,
            "passed": all(condition_gates.values()),
        }

    recovery_items = [
        item for item in valid if by_id.get(item["scenario_id"]) and by_id[item["scenario_id"]].recovery_comparison
    ]
    recovery_scenarios = sum(item.recovery_comparison for item in scenarios)
    recovery_pair_total = sum(
        1 for item in records if by_id.get(item.get("scenario_id")) and by_id[item["scenario_id"]].recovery_comparison
    )
    recovery_aware_rate = _ratio(
        sum(item["judgment"].get("recovery_aware") is True for item in recovery_items),
        recovery_pair_total,
    )
    all_conditions_pass = bool(scenario_metrics) and all(item["passed"] for item in scenario_metrics.values())
    critical_ok = bool(critical_probe and critical_probe.get("passed") is True)
    gates = {
        "required_nine_conditions_present": bool(required_conditions_present and recovery_scenarios == 1),
        "evidence_complete": judged == total and total > 0,
        "minimum_pairs_per_condition": bool(scenario_metrics)
        and all(item["pairs"] >= thresholds.minimum_pairs_per_condition for item in scenario_metrics.values()),
        "marked_aligned_expression": marked_rate >= thresholds.marked_aligned_min,
        "nominal_overexpression": nominal_over_rate <= thresholds.nominal_overexpression_max,
        "marked_vs_nominal_delta": delta_pp >= thresholds.marked_nominal_delta_min_pp,
        "persona_consistency": persona_rate >= thresholds.persona_consistency_min,
        "recovery_complete_and_aware": bool(
            recovery_pair_total >= thresholds.minimum_pairs_per_condition
            and recovery_aware_rate >= thresholds.recovery_aware_min
        ),
        "mechanism_leakage": leakage <= thresholds.mechanism_leakage_max_count,
        "unsupported_physiology": physiology <= thresholds.unsupported_physiology_max_count,
        "every_condition_passes": all_conditions_pass,
        "critical_runtime_zero_calls": critical_ok,
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "metrics": {
            "pairs": total,
            "judged_pairs": judged,
            "marked_aligned_expression_rate": marked_rate,
            "nominal_aligned_expression_rate": nominal_aligned_rate,
            "nominal_overexpression_rate": nominal_over_rate,
            "marked_vs_nominal_delta_pp": delta_pp,
            "persona_consistency_rate": persona_rate,
            "mechanism_leakage_count": leakage,
            "unsupported_physiology_count": physiology,
            "error_pair_count": sum(bool(item.get("errors")) for item in records),
            "recovery_pair_count": recovery_pair_total,
            "recovery_aware_rate": recovery_aware_rate,
        },
        "scenario_metrics": scenario_metrics,
        "critical_acceptance": critical_probe,
    }


def _count(items: list[dict[str, Any]], field: str) -> int:
    return sum(bool(item["judgment"][field]) for item in items)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def render_report(metadata: EvaluationMetadata, result: dict[str, Any], thresholds: AcceptanceThresholds) -> str:
    metrics = result["metrics"]
    lines = [
        "# Homeostatic expression paired causal evaluation",
        "",
        f"- result: **{'PASS' if result['passed'] else 'FAIL'}**",
        f"- mode / release eligible: `{result['mode']}` / `{result['release_eligible']}`",
        f"- run_id: `{metadata.run_id}`",
        f"- started_at: `{result['actual_started_at']}`",
        f"- completed_at: `{result['actual_completed_at']}`",
        f"- actual monotonic duration / clock: `{result['actual_duration_seconds']:.3f}s` / `{result['monotonic_clock']}`",
        f"- generator / judge model: `{metadata.generator_model}` / `{metadata.judge_model}`",
        f"- runtime manifest model: `{metadata.runtime_manifest_model}`",
        f"- generation temperature / max tokens: `{metadata.generation_temperature}` / `{metadata.max_tokens}`",
        f"- judge temperature / max tokens: `{metadata.judge_temperature}` / `{metadata.judge_max_tokens}`",
        f"- persona version / sha256: `{metadata.persona_version}` / `{metadata.persona_sha256}`",
        f"- manifest ref / sha256: `{metadata.manifest_ref}` / `{metadata.manifest_sha256}`",
        f"- prompt / judge prompt version: `{metadata.prompt_version}` / `{metadata.judge_prompt_version}`",
        f"- profile / state schema: `{metadata.profile_ref}` / `{metadata.state_schema_version}`",
        f"- fixed session / memory hash: `{metadata.fixed_session_id}` / `{metadata.memory_fixture_sha256}`",
        f"- fixed user input: `{metadata.fixed_user_text}`",
        "",
        "## Acceptance gates",
        "",
    ]
    for name, passed in result["gates"].items():
        lines.append(f"- {'PASS' if passed else 'FAIL'} — `{name}`")
    lines += [
        "",
        "## Global observations",
        "",
        f"- marked aligned: `{metrics['marked_aligned_expression_rate']:.1%}` (required >= {thresholds.marked_aligned_min:.0%})",
        f"- nominal overexpression: `{metrics['nominal_overexpression_rate']:.1%}` (required <= {thresholds.nominal_overexpression_max:.0%})",
        f"- marked-minus-nominal delta: `{metrics['marked_vs_nominal_delta_pp']:.1f} pp` (required >= {thresholds.marked_nominal_delta_min_pp:.0f} pp)",
        f"- persona consistency: `{metrics['persona_consistency_rate']:.1%}` (required >= {thresholds.persona_consistency_min:.0%})",
        f"- recovery aware: `{metrics['recovery_aware_rate']:.1%}` (required >= {thresholds.recovery_aware_min:.0%})",
        f"- mechanism leakage / unsupported physiology: `{metrics['mechanism_leakage_count']}` / `{metrics['unsupported_physiology_count']}`",
        "",
        "## Per-condition gates",
        "",
    ]
    for scenario_id, item in result["scenario_metrics"].items():
        suffix = f", recovery={item['recovery_aware_rate']:.1%}" if item["recovery_comparison"] else ""
        lines.append(
            f"- **{'PASS' if item['passed'] else 'FAIL'}** `{scenario_id}` "
            f"({item['pairs']} pairs): marked={item['marked_aligned_rate']:.1%}, "
            f"delta={item['marked_vs_nominal_delta_pp']:.1f} pp, persona={item['persona_consistency_rate']:.1%}{suffix}"
        )
    critical = result.get("critical_acceptance") or {}
    lines += [
        "",
        "## Critical Runtime probe",
        "",
        f"- result: **{'PASS' if critical.get('passed') else 'FAIL'}**",
        f"- implementation: `{json.dumps(critical.get('implementation'), ensure_ascii=False, sort_keys=True)}`",
        f"- calls (aux / main / delivery): `{(critical.get('counts') or {}).get('auxiliary_model_calls')}` / `{(critical.get('counts') or {}).get('main_model_calls')}` / `{(critical.get('counts') or {}).get('delivery_calls')}`",
        f"- decision / lifecycle: `{critical.get('decision_kind')}` / `{critical.get('lifecycle_after_dispatch')}`",
    ]
    fresh = result.get("fresh_state")
    if fresh:
        initial = fresh.get("initial") or {}
        final = fresh.get("final") or {}
        disk = fresh.get("disk") or {}
        lines += [
            "",
            "## Fresh HomeostasisEngine state",
            "",
            f"- requested / actual soak seconds: `{result.get('requested_soak_duration_seconds')}` / `{result['actual_duration_seconds']:.3f}`",
            f"- initial revision / snapshot hash / summary hash: `{initial.get('revision')}` / `{initial.get('snapshot_sha256')}` / `{initial.get('summary_sha256')}`",
            f"- final revision / snapshot hash / summary hash: `{final.get('revision')}` / `{final.get('snapshot_sha256')}` / `{final.get('summary_sha256')}`",
            f"- disk revision / snapshot hash / summary hash: `{disk.get('revision')}` / `{disk.get('snapshot_sha256')}` / `{disk.get('summary_sha256')}`",
            f"- disk matches recorded final: `{fresh.get('disk_matches_recorded_final')}`",
        ]
    lines += [
        "",
        "## Judge prompt",
        "",
        f"- version / sha256: `{metadata.judge_prompt_version}` / `{json_hash(JUDGE_SYSTEM)}`",
        "",
        "```text",
        JUDGE_SYSTEM,
        "```",
        "",
        "## Evidence notes",
        "",
        "- Every pair uses the production `InferencePipeline.prepare` path with production `ContextCompiler` and `ResponseValidator`.",
        "- `evidence.jsonl` records actual compiler segments/provenance, first main-model messages, every main-model retry, validation output, and final pre-delivery text.",
        "- The A/B integrity gate hashes the full compiler context and first main-model prompt after replacing only the homeostatic segment.",
        "- Recovery is a mandatory ninth release condition with the same sample-size and per-condition gates.",
        "- Critical is exercised by a real `RuntimeSupervisor` plus production `PipelineCognitiveAdapter`; aux, main, and delivery counts must all be zero.",
        "",
    ]
    return "\n".join(lines)


def _write_jsonl(stream: Any, value: dict[str, Any]) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    stream.flush()


def initialize_fresh_homeostasis_state(
    state_dir: Path,
    *,
    profile_path: Path = DEFAULT_PROFILE_PATH,
    now: datetime | None = None,
) -> str:
    return _initialize_fresh_homeostasis_state(state_dir, profile_path=profile_path, now=now)
