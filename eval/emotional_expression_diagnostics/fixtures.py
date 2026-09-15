"""Deterministic information-matched treatments; no live calls or state writes."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from harness.compiler.compiler import render_homeostatic_interoception
from harness.homeostasis import load_profile
from eval.homeostatic_expression.evaluation import (
    DEFAULT_PROFILE_PATH, build_summary, default_scenarios, load_misty_persona,
)

from .models import CaseSpec, DiagnosticRunSpec, FieldFact, canonical, digest


STYLE = "保持慢热、轻柔和克制，但让不同情绪在措辞、节奏与关注点上可辨；强度服从情境，允许平静。"
COMMON = "请以给定角色回应用户。只输出角色正文，保持事实和行动边界；未执行的动作不能说成已经完成。允许平静，不要求特定情绪。"
PROBE = (
    "这是独立的字段理解测量，不生成角色回复，不给出思维链。只返回JSON，包含owner、"
    "directions（字段到low/in_band/high/unknown的映射）、trends（字段到worsening/steady/recovering/unknown）、"
    "unknown_fields（字段名列表）、confirmed_causes（列表）。"
    "directions只判断数值相对可见目标带的位置：低于下界为low、带内为in_band、高于上界为high，"
    "无数值或目标带时为unknown；这个算术判断不要求知道字段语义。"
    "目标带包含两端点。owner为字段资料中的归属字符串；本批所有字段归属相同。"
    "unknown_fields列出无观测、明确无项目定义或证据不足的字段；解释字典未提供不等于数值无效。"
    "confirmed_causes只列已证实的因果关系，没有则返回空列表[]。"
    "不得据字段名补造项目定义，也不得据数值编造原因。"
)
TREND_ZH = {"worsening": "相对目标带的偏离正在增强", "steady": "相对目标带的偏离保持稳定",
            "recovering": "相对目标带的偏离正在缓和", "unknown": "变化未知"}
DIRECTION_CONTRACT = "有目标带时，低于下界为low、带内为in_band、高于上界为high；无目标带则unknown"


@dataclass(frozen=True)
class Treatment:
    name: str
    payload: dict[str, Any]
    information_sha256: str
    factor: str
    baseline_kind: str
    exact_body: str | None = None
    measurement_input: str | None = None


def default_cases(*, all_families: bool = False) -> tuple[CaseSpec, ...]:
    """Synthetic cases are plainly marked and are never called actual A-prod."""
    profile = load_profile(DEFAULT_PROFILE_PATH)
    persona, _, _ = load_misty_persona()
    scenarios = {item.scenario_id: item for item in default_scenarios()}
    definitions = {
        "energy_reserve": "当前可用的功能性精力储备", "cognitive_load": "当前处理任务的功能性负荷",
        "safety_predictability": "当前情境的安全与可预测程度", "competence": "当前对完成任务的把握",
        "autonomy": "当前保留选择权的程度", "social_connection": "当前功能性社会连接水平；偏低表示连接不足，偏高表示社交密度过高",
        "novelty": "当前新异刺激水平",
    }
    families = {
        1: ("energy", "energy_reserve_low"), 2: ("load", "cognitive_load_high"),
        3: ("predictability", "safety_predictability_low"), 4: ("competence", "competence_low"),
        5: ("autonomy", "autonomy_low"), 6: ("connection", "social_connection_low"),
        7: ("novelty", "novelty_high"), 8: ("conflict", "multidimensional_conflict"),
        9: ("recovery", "cognitive_load_recovering"), 10: ("intensity", "cognitive_load_high"),
        11: ("history", "safety_predictability_low"), 12: ("negative", "cognitive_load_high"),
    }
    selected = tuple(families) if all_families else (2, 6, 8, 9, 10, 12)
    cases: list[CaseSpec] = []
    for number in selected:
        family, scenario_id = families[number]
        scenario = scenarios[scenario_id]
        for variant in (0, 1):
            marked = (variant == 1 or number in {6, 7, 8, 9, 10, 11}) and number != 12
            summary = build_summary(scenario, profile_ref=profile.profile_ref, marked=marked, snapshot_revision=1)
            data = summary.model_dump(mode="json")
            for need in data["dominant_needs"]:
                need["normalized_deviation"] = 0.5
            if number == 6 and variant == 1:
                data["dominant_needs"][0].update(direction="high", semantic_need="需要留出独处空间并降低社交密度")
            if number == 7 and variant == 0:
                data["dominant_needs"][0].update(direction="low", semantic_need="需要一点轻量的新鲜感或探索")
            if number == 8:
                # The research plan asks for depleted energy while willing to connect.
                # Keep the connection state fixed; only the energy trend changes.
                energy = data["dominant_needs"][0]
                energy["trend"] = "steady" if variant == 0 else "recovering"
                connection = build_summary(scenarios["social_connection_low"], profile_ref=profile.profile_ref,
                                           marked=True, snapshot_revision=1).model_dump(mode="json")["dominant_needs"][0]
                connection["normalized_deviation"] = 0.5
                data["dominant_needs"] = [energy, connection]
            if number == 9:
                data["dominant_needs"][0]["trend"] = "steady" if variant == 0 else "recovering"
            if number == 10:
                data["emotion_hint"]["intensity"] = (0.55, 0.85)[variant]
            facts = []
            dimensions = [need["dimension"] for need in data["dominant_needs"]] if marked else [need.dimension for need in scenario.needs]
            for dimension in dimensions:
                bounds = profile.dimensions[dimension].hard_bounds
                band = profile.dimensions[dimension].baseline_band
                value = sum(band) / 2
                summary_need = next((item for item in data["dominant_needs"] if item["dimension"] == dimension), None)
                if marked:
                    direction = summary_need["direction"]
                    value = (bounds[0] + band[0]) / 2 if direction == "low" else (band[1] + bounds[1]) / 2
                trend = summary_need["trend"] if summary_need else "steady"
                facts.append(FieldFact(name=dimension, value=round(value, 4), definition=definitions[dimension],
                                       bounds=bounds, target_band=band, trend=trend))
            facts.append(FieldFact(name="hint_intensity", value=data["emotion_hint"]["intensity"],
                                   definition="派生状态线索的强度，不表示任何特定人类情绪或真实生理感受", bounds=(0, 1)))
            if number == 12 and variant:
                facts.append(FieldFact(name="undefined_field", value=None, definition=None, uncertainty="无定义且无观测"))
            context = "双方正在讨论接下来的一件小事；没有已完成行动、身体传感器或额外关系事实。"
            if number == 8:
                context += "角色明确愿意继续交流；这不证明任何特定情绪、亲密关系或身体症状。"
            if number == 11:
                context = f"给定客观历史：对方未完成已约定事项的次数为{1 if variant == 0 else 3}；原因未知。"
            cases.append(CaseSpec(
                case_id=f"synthetic-f{number:02d}-v{variant}", cluster_id=f"synthetic-f{number:02d}", family=family,
                variant=str(variant), route="qq", user_text="我们接下来做点什么？你可以按自己的感觉回答。", persona=persona,
                legal_context=context, facts=tuple(facts), summary=data, negative_control=number == 12,
            ))
    return tuple(cases)


def values(case: CaseSpec) -> list[dict[str, Any]]:
    return [fact.model_dump(mode="json", exclude={"definition"}) for fact in case.facts]


def definitions(case: CaseSpec) -> dict[str, Any]:
    return {fact.name: {"definition": fact.definition or "未知；没有项目定义", "direction_contract":
                        DIRECTION_CONTRACT, "trend_contract": TREND_ZH}
            for fact in case.facts}


def verbal(case: CaseSpec) -> str:
    rows = []
    for fact in case.facts:
        rows.append(
            f"字段{fact.name}属于{fact.owner}，含义为{fact.definition or '未知；没有项目定义'}；"
            f"当前值{canonical(fact.value)}；单位{fact.unit}；量程{canonical(fact.bounds)}；目标带{canonical(fact.target_band)}；"
            f"趋势{fact.trend}（{TREND_ZH[fact.trend]}）；证据范围{fact.evidence_scope}；不确定性{fact.uncertainty}。"
        )
    rows.append(DIRECTION_CONTRACT + "。")
    rows.append("趋势定义：" + "；".join(f"{key}表示{value}" for key, value in TREND_ZH.items()) + "。")
    return "\n".join(rows)


def base_payload(spec: DiagnosticRunSpec, messages: list[dict[str, str]]) -> dict[str, Any]:
    options = spec.generation
    payload: dict[str, Any] = {"model": options.model, "messages": messages, "temperature": options.temperature,
                               "max_tokens": options.max_tokens, "thinking": {"type": options.thinking}}
    if options.reasoning_effort:
        payload["reasoning_effort"] = options.reasoning_effort
    return payload


def treatments(spec: DiagnosticRunSpec, case: CaseSpec) -> list[Treatment]:
    if case.route == "ga_opaque":
        raise ValueError("opaque cases cannot receive semantic diagnostic treatments")
    header = COMMON + "\n" + case.persona + "\n合法情境：" + case.legal_context
    def payload(text: str) -> dict[str, Any]:
        return base_payload(spec, [{"role": "system", "content": header + "\n" + text},
                                   {"role": "user", "content": case.user_text}])
    def measurement(text: str) -> str:
        # Preserve the information treatment and legal evidence, without the competing
        # role-playing task, persona instructions, examples, or generated answers.
        return "合法情境：" + case.legal_context + "\n" + text
    info_hash = digest([fact.model_dump(mode="json") for fact in case.facts])
    b0 = "字段和值：" + canonical(values(case))
    b1 = b0 + "\n解释字典：" + canonical(definitions(case))
    c = verbal(case)
    projection = render_homeostatic_interoception(case.summary)
    if case.actual_request is not None:
        request = case.actual_request
        if not request.identity_verified:
            raise ValueError("unverified HTTP request cannot be labeled A-prod")
        first = Treatment("A-prod", json.loads(request.request_body_utf8), digest(case.summary), "actual_replay", "actual_http", request.request_body_utf8)
    else:
        first = Treatment("A-synthetic-proxy", payload(projection), digest(case.summary), "synthetic_production_renderer", "synthetic_proxy")
    out = [first, Treatment("B0-values", payload(b0), info_hash, "values", "manipulated_pending_http", measurement_input=measurement(b0)),
           Treatment("B1-defined", payload(b1), info_hash, "definitions_only", "manipulated_pending_http", measurement_input=measurement(b1)),
           Treatment("C-verbal", payload(c), info_hash, "encoding_only", "manipulated_pending_http", measurement_input=measurement(c)),
           Treatment("D-example", payload(c + "\n其他场景的冻结表达示范：\n" + "\n".join(spec.examples)), info_hash, "examples_only", "manipulated_pending_http")]
    if "H2" in spec.extras:
        lost = {"intensity": (case.summary.get("emotion_hint") or {}).get("intensity"),
                "normalized_deviations": [need["normalized_deviation"] for need in case.summary.get("dominant_needs", [])]}
        out.extend([Treatment("H2-current", payload(projection), digest(case.summary), "current_projection", "manipulated_pending_http"),
                    Treatment("H2-enhanced", payload(projection + "\n仅补入原投影未表达的信息：" + canonical(lost)), digest([case.summary, lost]), "added_information", "manipulated_pending_http")])
    if "E-style" in spec.extras:
        out.append(Treatment("E-style", payload(c + "\n" + STYLE), info_hash, "style_only", "manipulated_pending_http"))
    if "mode" in spec.extras:
        for mode in ("enabled", "disabled"):
            body = copy.deepcopy(payload(c))
            body["thinking"] = {"type": mode}
            out.append(Treatment("mode-" + mode, body, info_hash, "thinking_only", "manipulated_pending_http"))
    return out


def probe_payload(treatment: Treatment) -> dict[str, Any]:
    # No generated output is accepted as an argument, preventing answer feedback.
    if treatment.name not in {"B0-values", "B1-defined", "C-verbal"} or treatment.measurement_input is None:
        raise ValueError("independent probes require a frozen B0/B1/C information input")
    result = copy.deepcopy(treatment.payload)
    result["messages"] = [{"role": "system", "content": PROBE}, {"role": "user", "content": treatment.measurement_input}]
    result["response_format"] = {"type": "json_object"}
    return result


def probe_answer_key(case: CaseSpec) -> dict[str, Any]:
    directions = {}
    for fact in case.facts:
        direction = "unknown"
        if isinstance(fact.value, (int, float)) and fact.target_band:
            direction = "low" if fact.value < fact.target_band[0] else "high" if fact.value > fact.target_band[1] else "in_band"
        directions[fact.name] = direction
    return {"owner": "角色", "directions": directions, "trends": {fact.name: fact.trend for fact in case.facts},
            "unknown_fields": [fact.name for fact in case.facts if not fact.definition or fact.value is None], "confirmed_causes": []}


def evaluate_probe(case: CaseSpec, content: str) -> dict[str, Any]:
    try:
        actual = json.loads(content)
        if not isinstance(actual, dict):
            raise ValueError("probe is not an object")
    except (ValueError, TypeError):
        return {"valid": False, "correct_fields": 0, "total_fields": 5, "error": "invalid_probe_json"}
    expected = probe_answer_key(case)
    correctness = {key: actual.get(key) == value for key, value in expected.items()}
    return {"valid": set(actual) == set(expected), "correct_fields": sum(correctness.values()),
            "total_fields": len(correctness), "field_correctness": correctness}
