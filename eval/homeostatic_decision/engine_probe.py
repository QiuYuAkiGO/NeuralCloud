"""Offline, real-engine evidence for homeostatic decision prerequisites.

This does not execute a model or a production work/recovery action loop. Absolute
state interventions are explicitly labelled sensor fixtures. Every transition
is committed by a public HomeostasisEngine command; snapshots are never edited.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from harness.compiler.compiler import ContextCompiler, render_homeostatic_interoception
from harness.config import Settings
from harness.contracts.envelope import DomainCommandEnvelope
from harness.core.inference import SituationActor, SituationState, SituationTrigger
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.homeostasis.models import DIMENSIONS, HomeostaticProfile, normalized_homeostatic_deviation


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
ALIASES = dict(L="cognitive_load", E="energy_reserve", P="safety_predictability",
               C="competence", A="autonomy", R="social_connection", N="novelty")


def sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def values_for(profile: HomeostaticProfile, deviations: dict[str, float]) -> dict[str, float]:
    values = dict(profile.preferences)
    for alias, deviation in deviations.items():
        name = ALIASES.get(alias, alias)
        low, high = profile.dimensions[name].baseline_band
        hard_low, hard_high = profile.dimensions[name].hard_bounds
        values[name] = (high + deviation * (hard_high - high) if name in
                        {"cognitive_load", "novelty"} else low - deviation * (low - hard_low))
    return values


def state_evidence(engine: HomeostasisEngine) -> dict[str, Any]:
    snapshot = engine.snapshot
    summary = snapshot.interoceptive_summary
    assert summary is not None
    state = {
        "revision": snapshot.revision,
        "values": {key: factor.value for key, factor in snapshot.dimensions.items()},
        "velocities": {key: factor.velocity for key, factor in snapshot.dimensions.items()},
        "deviations": {key: normalized_homeostatic_deviation(engine.profile, factor)[1]
                       for key, factor in snapshot.dimensions.items()},
        "precisions": {key: factor.precision for key, factor in snapshot.dimensions.items()},
        "posterior": {key: factor.posterior for key, factor in snapshot.dimensions.items()},
        "salience": summary.salience,
        "watchdog": snapshot.watchdog,
        "summary": summary.model_dump(mode="json"),
        "candidates": [item.model_dump(mode="json") for item in snapshot.candidates],
    }
    if summary.salience == "critical":
        state["compiler_status"] = "blocked_before_model"
        return state
    situation = SituationState(
        situation_id="situation_engine_probe", trigger=SituationTrigger(ref="event_fixture"),
        utterance="继续校对已完成四批的十二批资料；每批保存结果。",
        actor=SituationActor(),
    )
    context = ContextCompiler(Settings(context_token_budget=3000)).compile(
        situation, homeostatic_interoception=summary,
    )
    state["compiler_text"] = next(s.text for s in context.segments if s.kind == "homeostatic_interoception")
    state["assessment_text"] = render_homeostatic_interoception(summary, purpose="assessment")
    state["compiler_messages"] = context.render_messages()
    state["compiler_messages_sha256"] = sha256(state["compiler_messages"])
    state["compiler_text_sha256"] = sha256(state["compiler_text"])
    state["prompt_scope"] = "actual compiler.render_messages; not full character generation prompt"
    return state


class ProbeSession:
    """Public-command session with a fixed advancing clock and complete evidence."""

    def __init__(self, root: Path, name: str, profile: HomeostaticProfile | None = None):
        self.profile = profile or load_profile(ROOT / "presets/homeostasis/functional-proxy-v1.json")
        self.now = NOW
        self.engine = HomeostasisEngine(self.profile, state_dir=root / name, now=NOW, clock=lambda: self.now)
        self.name = name
        self.records: list[dict[str, Any]] = []

    async def command(self, method: str, command_type: str, payload: dict, label: str,
                      seconds: float = 0.0) -> dict[str, Any]:
        self.now += timedelta(seconds=seconds)
        sequence = self.engine.snapshot.revision + 1
        command = DomainCommandEnvelope[dict](
            command_id=f"command_{self.name}_{sequence}", command_type=command_type,
            event_id=f"event_{self.name}_{sequence}", receive_seq=sequence,
            correlation_id=f"correlation_{self.name}", runtime_instance_id="engine_offline_probe",
            manifest_revision=1, idempotency_key=f"{self.name}:{sequence}",
            issued_at=self.now, deadline=self.now + timedelta(days=1),
            expected_revision=self.engine.snapshot.revision, payload=payload,
        )
        before = {key: f.value for key, f in self.engine.snapshot.dimensions.items()}
        result = await getattr(self.engine, method)(command)
        if result.status != "applied":
            raise RuntimeError(f"{self.name}/{label}: {result.status}: {result.error}")
        evidence = state_evidence(self.engine)
        record = {"label": label, "command": command.model_dump(mode="json"),
                  "status": result.status, "values_before": before,
                  "actual_delta": {k: evidence["values"][k] - before[k] for k in DIMENSIONS},
                  "trace": result.result["trace"], "state": evidence}
        self.records.append(record)
        return record

    async def observe(self, values: dict[str, float], label: str, precision: float = 1.0) -> dict:
        return await self.command("assimilate_observation", "AssimilateObservation", {
            "observation": {"source": "system_sensor", "precision": precision,
                            "observed_values": values, "evidence_refs": [], "observed_at": self.now}}, label)

    async def initialize(self, deviations: dict[str, float] | None = None, *, steady: bool = True):
        await self.observe(dict(self.profile.preferences), "fixture_preferences")
        if deviations:
            await self.observe(values_for(self.profile, deviations), "fixture_intervention")
        if steady:
            await self.observe({k: f.value for k, f in self.engine.snapshot.dimensions.items()}, "fixture_steady")
        return self

    async def event(self, impacts: dict[str, float], label: str, *, source: str = "system_sensor",
                    precision: float | None = None) -> dict:
        appraisal = {"source": source, "suggested_impacts": impacts}
        if precision is not None:
            appraisal["precision"] = precision
        return await self.command("apply_event", "ApplyEvent", {
            "event": {"event_type": "probe_appraisal", "source_time": self.now,
                      "payload": {"appraisal": appraisal}}}, label)

    async def outcome(self, policy_id: str, label: str) -> dict:
        # Mirrors supervisor._commit_action_outcome's action/evidence envelope.
        # No explicit appraisal: tests whether action identity drives recovery.
        return await self.command("apply_outcome", "ApplyOutcome", {
            "action": {"action_id": f"action_{policy_id}", "state": "succeeded",
                       "policy_id": policy_id, "outcome": {"success": True}},
            "evidence": {"success": True, "summary": f"{policy_id} completed"}}, label)

    async def heartbeat(self, seconds: float, label: str) -> dict:
        at = self.now + timedelta(seconds=seconds)
        return await self.command("apply_heartbeat", "ApplyHeartbeat", {
            "event": {"event_type": "heartbeat", "source": "scheduler", "source_time": at}}, label, seconds)


async def run_probe(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    profile_path = ROOT / "presets/homeostasis/functional-proxy-v1.json"
    profile = load_profile(profile_path)
    report: dict[str, Any] = {
        "schema_version": "homeostatic-decision-engine-probe-v1", "profile_ref": profile.profile_ref,
        "fixed_start": NOW.isoformat(), "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        "scope": "offline real HomeostasisEngine public commands and production compiler; zero model calls",
        "limitations": ["Sensor fixtures intervene on state; no production work action executes.",
                        "Compiler hashes are not full character generation prompts.",
                        "Passing prerequisites does not establish decision or work/recovery balance."],
        "checks": [], "sessions": {}, "observations": {},
    }
    checks = report["checks"]

    def check(cid: str, claim: str, condition: bool, *, scope: str = "state_mechanism"):
        checks.append({"id": cid, "claim": claim, "status": "pass" if condition else "fail", "scope": scope})

    with tempfile.TemporaryDirectory(prefix="engine-state-", dir=output) as temporary:
        root = Path(temporary)
        sessions: dict[str, ProbeSession] = {}

        async def fixture(name: str, deviations: dict[str, float] | None = None):
            session = ProbeSession(root, name, profile)
            sessions[name] = session
            return await session.initialize(deviations)

        nominal = await fixture("nominal")
        baseline = state_evidence(nominal.engine)
        conditions = {
            "W01_W02": ({"L": .20}, "nominal"),
            "W03": ({"L": .20, "E": .20}, "nominal"),
            "W04": ({"L": .20, "E": .20, "N": .20}, "nominal"),
            "W06": ({k: .20 for k in ALIASES}, "nominal"),
            "M01_M02": ({"L": .30}, "noticeable"),
            "M_E_only": ({"E": .30}, "noticeable"),
            "M03_M04": ({"L": .30, "E": .30}, "marked"),
            "S01_S02": ({"L": .65}, "marked"),
            "S_E_only": ({"E": .65}, "marked"),
            "S03_S04": ({"L": .65, "E": .65}, "marked"),
        }
        states = {}
        for name, (deviations, expected) in conditions.items():
            session = await fixture(name, deviations)
            state = state_evidence(session.engine)
            states[name] = state
            check(name, f"fresh salience = {expected}", state["salience"] == expected)
            if name.startswith("W"):
                check(name + "_visibility", "weak intervention compiler messages equal nominal",
                      state["compiler_messages_sha256"] == baseline["compiler_messages_sha256"], scope="projection")
        report["observations"]["four_cell_visibility"] = {
            "D04_D05_medium": {k: (states[k] if k != "nominal" else baseline)["compiler_messages_sha256"]
                              for k in ("nominal", "M01_M02", "M_E_only", "M03_M04")},
            "D05_strong": {k: states[k]["compiler_messages_sha256"] for k in ("S01_S02", "S_E_only", "S03_S04")},
        }
        check("D04", "medium four cells have distinct compiler messages", len(set(report["observations"]["four_cell_visibility"]["D04_D05_medium"].values())) == 4, scope="projection")
        check("D05", "same-marked strong single and dual needs remain visible", len(set(report["observations"]["four_cell_visibility"]["D05_strong"].values())) == 3, scope="projection")

        for deviation in (.65, .75):
            await fixture(f"D06_{deviation}", {"L": deviation})
        same_numeric = [state_evidence(sessions[f"D06_{d}"].engine) for d in (.65, .75)]
        check("D06", "different strong values can produce identical actual compiler messages", same_numeric[0]["compiler_messages_sha256"] == same_numeric[1]["compiler_messages_sha256"], scope="projection")
        for name, dvs in {"D07_three": {"L": .70, "E": .65, "P": .60},
                          "D07_four": {"L": .70, "E": .65, "P": .60, "A": .30},
                          "D07_A_visible": {"L": .70, "E": .65, "P": .60, "A": .80}}.items():
            await fixture(name, dvs)
        d7 = [state_evidence(sessions[name].engine) for name in ("D07_three", "D07_four", "D07_A_visible")]
        check("D07_hidden", "fourth need omitted with identical compiler messages", d7[0]["compiler_messages_sha256"] == d7[1]["compiler_messages_sha256"], scope="projection")
        check("D07_visible", "autonomy entering top three changes compiler messages", d7[0]["compiler_messages_sha256"] != d7[2]["compiler_messages_sha256"], scope="projection")

        # Fresh boundary cases use independent engines for every dimension.
        for alias in ALIASES:
            for d, expected in ((.249, "nominal"), (.250, "noticeable"), (.251, "noticeable"),
                                (.499, "noticeable"), (.500, "marked"), (.501, "marked")):
                name = f"D08_fresh_{alias}_{d}"
                session = await fixture(name, {alias: d})
                check(name, f"fresh {alias}({d}) -> {expected}", session.engine.snapshot.interoceptive_summary.salience == expected)
        for prior, deviations, expected in (
            ("marked", {"L": .351}, "marked"), ("marked", {"L": .350}, "marked"),
            ("marked", {"L": .349}, "noticeable"),
            ("noticeable", {"L": .151}, "noticeable"), ("noticeable", {"L": .150}, "noticeable"),
            ("noticeable", {"L": .149}, "nominal"),
            ("marked", {"L": .151, "E": .151}, "marked"),
            ("marked", {"L": .150, "E": .150}, "marked"),
            ("marked", {"L": .151, "E": .149}, "noticeable"),
            ("marked", {"L": .149, "E": .149}, "nominal"),
        ):
            name = f"D08_history_{prior}_{len(sessions)}"
            session = await fixture(name, {"L": .65 if prior == "marked" else .30})
            await session.observe(values_for(profile, deviations), "recover_to_boundary")
            check(name, f"previous={prior}; {deviations} -> {expected}", session.engine.snapshot.interoceptive_summary.salience == expected)
        timeline = await fixture("D08_timeline")
        actual = []
        for d in (.249, .250, .249, .149, .501, .351, .349, .151, .149):
            await timeline.observe(values_for(profile, {"L": d}), f"timeline_{d}")
            actual.append(timeline.engine.snapshot.interoceptive_summary.salience)
        check("D08_timeline", "continuous trajectory enters/holds/exits without threshold jitter",
              actual == ["nominal", "noticeable", "noticeable", "nominal", "marked", "marked", "noticeable", "noticeable", "nominal"])

        trend_states = {}
        for trend, previous_d in (("steady", .65), ("worsening", .55), ("recovering", .75)):
            session = await fixture("D09_" + trend, {"L": previous_d})
            await session.observe(values_for(profile, {"L": .65}), "same_final_value_valid_history")
            trend_states[trend] = state_evidence(session.engine)
            check("D09_" + trend, "same final .81 value with history-derived " + trend,
                  abs(trend_states[trend]["values"]["cognitive_load"] - .81) < 1e-10 and
                  trend_states[trend]["summary"]["dominant_needs"][0]["trend"] == trend)
        check("D09_visible", "all three valid trends produce different compiler messages",
              len({s["compiler_messages_sha256"] for s in trend_states.values()}) == 3, scope="projection")

        precision_states = {}
        for source in ("system_sensor", "llm_appraisal"):
            session = await fixture("D10_" + source, {"L": .30})
            record = await session.event({"cognitive_load": .12}, "same_impact_different_source", source=source)
            state = record["state"]
            precision_states[source] = state
            expected = .67 + .12 * profile.source_precision[source]
            check("D10_" + source, "source precision attenuates realized event delta",
                  abs(state["values"]["cognitive_load"] - expected) < 1e-10)
        check("D10_threshold_visible", "different precision changes final salience and compiler messages",
              precision_states["system_sensor"]["salience"] == "marked" and
              precision_states["llm_appraisal"]["salience"] == "noticeable", scope="projection")
        matched = []
        for precision in (.95, .35):
            session = await fixture(f"D10_same_value_{precision}", {"L": .65})
            await session.observe({"cognitive_load": .81}, "same_value_different_precision", precision)
            matched.append(state_evidence(session.engine))
        check("D10_hidden", "confidence alone with matched values/trends is absent from compiler messages",
              matched[0]["compiler_messages_sha256"] == matched[1]["compiler_messages_sha256"], scope="projection")

        outcomes = {}
        for policy in ("rest", "work", "simplify"):
            session = await fixture("outcome_" + policy, {"L": .65, "E": .65})
            record = await session.outcome(policy, policy + "_success")
            outcomes[policy] = record
        report["observations"]["production_outcomes"] = outcomes
        rest_delta = outcomes["rest"]["actual_delta"]
        check("rest_action_recovery", "successful rest outcome restores energy and reduces cognitive load",
              rest_delta["energy_reserve"] > 0 and rest_delta["cognitive_load"] < 0, scope="production_capability")
        check("rest_vs_work_differential", "rest and work outcomes have distinct energy/load effects",
              any(abs(rest_delta[k] - outcomes["work"]["actual_delta"][k]) > 1e-10
                  for k in ("energy_reserve", "cognitive_load")), scope="production_capability")
        check("work_outcome_load_growth", "successful work outcome creates workload/resource costs",
              outcomes["work"]["actual_delta"]["cognitive_load"] > 0 and
              outcomes["work"]["actual_delta"]["energy_reserve"] < 0, scope="production_capability")

        heartbeat = await fixture("negative_feedback_heartbeat", {"L": .65, "E": .65})
        start = state_evidence(heartbeat.engine)
        for n in range(2):
            await heartbeat.heartbeat(90, f"heartbeat_recovery_{n + 1}")
        two = state_evidence(heartbeat.engine)
        for n in range(238):
            await heartbeat.heartbeat(90, f"heartbeat_recovery_{n + 3}")
        end = state_evidence(heartbeat.engine)
        report["observations"]["passive_recovery"] = {
            "start": start, "after_two_90s_heartbeats": two, "after_six_simulated_hours": end,
            "note": "Heartbeat drift is independent of action/work policy. Simulated elapsed time; no sleeping.",
        }
        check("heartbeat_negative_feedback", "heartbeat drift restores depleted energy and lowers high cognitive load",
              two["values"]["energy_reserve"] > start["values"]["energy_reserve"] and
              two["values"]["cognitive_load"] < start["values"]["cognitive_load"])
        check("heartbeat_six_hour_recovery", "six simulated hours eventually exits marked to nominal",
              end["salience"] == "nominal")
        perception = await fixture("work_proxy_perception", {"L": .65, "E": .65})
        rec = await perception.command("apply_event", "ApplyEvent", {
            "event": {"event_type": "perception", "source": "runtime", "source_time": perception.now,
                      "payload": {"summary": "校对一批资料"}}}, "perception_proxy_not_measured_work")
        report["observations"]["perception_work_proxy"] = rec
        check("perception_proxy_growth", "generic perception adds load and coupled energy cost",
              rec["actual_delta"]["cognitive_load"] > 0 and rec["actual_delta"]["energy_reserve"] < 0)
        explicit_rest = await fixture("explicit_transition_appraisal", {"L": .65, "E": .65})
        rec = await explicit_rest.event(dict(profile.transition_model["rest"]), "manual_transition_positive_control", precision=1.0)
        report["observations"]["manual_rest_positive_control"] = rec
        check("manual_rest_positive_control", "explicit externally supplied rest deltas integrate correctly",
              rec["actual_delta"]["energy_reserve"] > 0 and rec["actual_delta"]["cognitive_load"] < 0)

        sequence_results = {}
        for seed, deviations in (("nominal", {}), ("near_bounds", {"L": .975, "E": .978})):
            results = {}
            for order in ("simultaneous", "L_then_E", "E_then_L"):
                session = await fixture(f"D12_{seed}_{order}", deviations)
                events = ([{"cognitive_load": .12, "energy_reserve": -.12}] if order == "simultaneous" else
                          [{"cognitive_load": .12}, {"energy_reserve": -.12}] if order == "L_then_E" else
                          [{"energy_reserve": -.12}, {"cognitive_load": .12}])
                for i, impacts in enumerate(events):
                    await session.event(impacts, f"ordered_event_{i}", precision=1.0)
                results[order] = state_evidence(session.engine)
            sequence_results[seed] = results
        report["observations"]["D12_sequence"] = sequence_results
        nom = sequence_results["nominal"]
        check("D12_unclipped_values", "unclipped simultaneous and sequential linear updates match final values",
              all(abs(nom[o]["values"][k] - nom["simultaneous"]["values"][k]) < 1e-10
                  for o in ("L_then_E", "E_then_L") for k in DIMENSIONS))
        near = sequence_results["near_bounds"]
        check("D12_clipped_path_dependence", "clipping creates measured path dependence rather than pure order-invariance",
              any(abs(near[o]["values"][k] - near["simultaneous"]["values"][k]) > 1e-10
                  for o in ("L_then_E", "E_then_L") for k in DIMENSIONS))
        plateau = await fixture("D12_zero_realized_delta", {"L": 1.0})
        rec = await plateau.event({"cognitive_load": 1.0}, "positive_request_at_upper_bound", precision=1.0)
        report["observations"]["D12_saturated_source"] = rec
        check("D12_zero_realized_coupling", "saturated load creates zero realized delta and zero energy coupling",
              abs(rec["actual_delta"]["cognitive_load"]) < 1e-10 and abs(rec["actual_delta"]["energy_reserve"]) < 1e-10)
        report["sessions"] = {name: session.records for name, session in sessions.items()}

    report["summary"] = {"checks": len(checks), **dict(Counter(c["status"] for c in checks)),
                         "sessions": len(report["sessions"]),
                         "commands": sum(len(records) for records in report["sessions"].values()),
                         "model_calls": 0, "production_balance_verdict": "unsupported",
                         "rest_action_recovery_verdict": next(c["status"] for c in checks if c["id"] == "rest_action_recovery")}
    (output / "engine-evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "engine-summary.md").write_text(render_report(report), encoding="utf-8")
    return report


def render_report(report: dict[str, Any]) -> str:
    obs = report["observations"]
    delta = obs["production_outcomes"]["rest"]["actual_delta"]
    recovery = obs["passive_recovery"]
    s = report["summary"]
    rest_passed = s["rest_action_recovery_verdict"] == "pass"
    mechanism_passed = all(c["status"] == "pass" for c in report["checks"] if c["scope"] != "production_capability")
    lines = ["# 内稳态状态与负反馈离线实测", "", f"日期：2026-09-05；profile：{report['profile_ref']}。", "",
             f"执行 {s['sessions']} 个独立状态会话、{s['commands']} 条真实 Engine 命令，{s['checks']} 项判定：{s.get('pass', 0)} 通过、{s.get('fail', 0)} 失败。模型调用为 0。", "",
             ("结论：阈值、迟滞、趋势、来源精度、实际 delta 耦合与被动恢复机制可运行；生产工作/休息闭环尚不能判定为通过。" if mechanism_passed else
              "结论：部分状态/投影判定失败，详见下方；本探针未执行生产工作/休息闭环。"), "",
             "## 核心缺口", "",
             f"成功 rest outcome 的实际变化为 {json.dumps(delta, ensure_ascii=False)}。恢复判定：{'通过' if rest_passed else '失败'}。", "",
             ("rest outcome 能改变目标资源状态；仍需真实动作执行与后续工作的证据。" if rest_passed else
              "当前配置的 transition_model.rest 未由 apply_outcome 消费，rest/work/simplify 作为通用成功 outcome 处理。人为显式提交 rest appraisal 可改变状态，但这只是状态积分正对照，不能算生产恢复动作成功。"), "",
             "## 可验证结果", "",
             "- 弱单维、双维、三维、七维从 nominal 进入仍 nominal，真实 compiler messages 与基线一致。七维弱偏离不能直接相加视为强压力。",
             "- 中等单维 noticeable，中等双维 marked；强单维、双维均 marked，但需要列表仍能使提示不同。",
             "- L(.65)/L(.75) compiler messages 相同；第四维未进入前三时可完全不可见。",
             "- .249/.250/.251 与 .499/.500/.501 在全部七维进入边界符合预期；历史退出与多维 .15 条件、连续迟滞轨迹单独实测。",
             "- 同一 .81 当前负荷由真实历史产生 steady/worsening/recovering，三种提示不同。来源精度影响事件同化；值与趋势匹配后仅置信度变化不进提示。",
             "- 不饱和线性更新可有相同最终数值；边界截断产生路径依赖；已饱和来源不会因请求 delta 而产生虚假耦合。", "",
             "## 被动恢复与工作代理", ""]
    for key, label in (("start", "初始"), ("after_two_90s_heartbeats", "2 次 90 秒心跳"),
                       ("after_six_simulated_hours", "6 个模拟小时")):
        state = recovery[key]
        lines.append(f"- {label}：L={state['values']['cognitive_load']:.8f}，E={state['values']['energy_reserve']:.8f}，salience={state['salience']}。")
    lines += ["", "心跳向健康区间中心漂移，不依赖是否休息；不能将其全部归因于休息策略。普通 perception 可增加负荷并经耦合消耗精力，但不是对实际完成工作量的测量。", "",
              "## 失败判定", ""]
    lines.extend(f"- {c['id']}：{c['claim']}。" for c in report["checks"] if c["status"] == "fail")
    lines += ["", "## 证据与复现", "", "完整命令、前后值、realized delta、transition trace、summary、compiler 文本与消息哈希见 engine-evidence.json。",
              "", "`python -m eval.homeostatic_decision.engine_probe --output eval/reports/homeostatic_decision/engine`", "",
              "该探针没有调用模型、执行校对动作或验收限时继续计划。compiler.render_messages 哈希用于投影可见性，不冒充完整 character 首轮生成提示。", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "eval/reports/homeostatic_decision/engine")
    args = parser.parse_args()
    report = asyncio.run(run_probe(args.output.resolve()))
    print(json.dumps(report["summary"], ensure_ascii=False))
    for check in report["checks"]:
        if check["status"] != "pass":
            print(json.dumps(check, ensure_ascii=False))


if __name__ == "__main__":
    main()
