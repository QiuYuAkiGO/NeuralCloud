"""Live production-prompt decisions with an explicitly evaluation-only action bridge.

The production reconsideration adapter does not execute these model choices.
Rest outcomes enter the unmodified HomeostasisEngine; no recovery delta is invented.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gui.persist import load_gui_settings
from harness.config import Settings
from harness.contracts.envelope import DomainCommandEnvelope
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.llm.client import OpenAICompatClient, parse_json_object
from eval.homeostatic_expression.production import (
    ProductionPairPipeline, compiler_provenance, context_hash_without_homeostasis,
    homeostatic_segment, json_hash, model_prompt_hash_without_homeostasis,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
PROFILE = ROOT / "presets/homeostasis/functional-proxy-v1.json"
PERSONA = ROOT / "presets/builtin/misty.json"
DIM = dict(L="cognitive_load", E="energy_reserve", P="safety_predictability", C="competence", A="autonomy", R="social_connection", N="novelty")
ACTIONS = {"continue", "adapt", "bounded_continue", "pause_recover", "redirect"}
MODEL_EXTRA: dict[str, Any] = {}


def cases() -> list[dict[str, Any]]:
    specs = [
        ("W01", {"L": .2}, "V1", 2), ("W02", {"L": .2}, "V3", 2),
        ("W03", {"L": .2, "E": .2}, "V1", 2), ("W04", {"L": .2, "E": .2, "N": .2}, "V3", 2),
        ("W05", {"L": .2}, "V1", 2), ("W06", {k: .2 for k in DIM}, "V1", 2),
        ("M01", {"L": .3}, "V1", 2), ("M02", {"L": .3}, "V3", 2),
        ("M03", {"L": .3, "E": .3}, "V1", 2), ("M04", {"L": .3, "E": .3}, "V3", 2),
        ("M05", {"L": .3, "E": .3, "N": .3}, "V1", 2), ("M06", {"L": .3, "R": .3}, "V3", 2),
        ("S01", {"L": .65}, "V1", 2), ("S02", {"L": .65}, "V3", 1),
        ("S03", {"L": .65, "E": .65}, "V3", 4), ("S04", {"L": .65, "E": .65, "P": .65}, "V3", 2),
        ("S05", {"L": .65, "E": .3}, "V3", 1), ("S06", {"L": .65, "E": .65}, "V1", 2),
        ("D04_E", {"E": .3}, "V1", 2),
        ("W03_E", {"E": .2}, "V1", 2), ("M06_R", {"R": .3}, "V3", 2),
        ("D03_V1_U1", {"L": .65}, "V1", 2), ("D03_V3_U0", {"L": .65}, "V3", 2),
        ("D03_V3_U1", {"L": .65}, "V3", 2),
        ("S02_V1_K1", {"L": .65}, "V1", 1), ("S03_V3_K1", {"L": .65, "E": .65}, "V3", 1),
    ]
    longitudinal = {"M03", "M04", "S01", "S02", "S03", "S05", "S06"}
    return [dict(case_id=i, deviations=d, value=v, milestone=k,
                 urgency="U1" if i.endswith("U1") else "U0",
                 horizon=6 if i in longitudinal else (2 if i == "W05" else 1)) for i, d, v, k in specs]


def records() -> list[dict[str, int]]:
    rows = []
    for i in range(1, 61):
        qty, price = i % 7 + 1, i % 9 + 2
        rows.append(dict(id=i, qty=qty, unit_price=price, stated_total=qty * price + (3 if i % 3 == 0 else 0)))
    return rows


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def settings_from_gui() -> Settings:
    p = load_gui_settings()
    s = Settings()
    if p.mode not in {"live", "ollama"}:
        raise RuntimeError("Saved GUI configuration is not a live endpoint")
    return s.model_copy(update=dict(llm_base_url=p.base_url or s.llm_base_url,
        llm_model=p.model or s.llm_model, llm_api_key=p.api_key or s.llm_api_key, llm_timeout=75))


class State:
    """Exact research observations, followed by real production outcome integration."""
    def __init__(self, path: Path):
        self.now = NOW
        self.profile = load_profile(PROFILE)
        self.engine = HomeostasisEngine(self.profile, state_dir=path, now=NOW, clock=lambda: self.now)
        self.seq = 0

    def call(self, method: str, kind: str, payload: dict) -> dict:
        self.seq += 1
        cmd = DomainCommandEnvelope[dict](command_type=kind, event_id=f"eval_{self.seq}",
            receive_seq=self.seq, correlation_id=f"eval_{self.seq}", runtime_instance_id="decision-eval-sandbox",
            manifest_revision=3, idempotency_key=f"eval:{self.seq}", issued_at=self.now,
            deadline=self.now + timedelta(days=1), expected_revision=self.engine.snapshot.revision, payload=payload)
        result = asyncio.run(getattr(self.engine, method)(cmd))
        if result.status != "applied":
            raise RuntimeError(f"{kind}: {result.status}: {result.error}")
        return result.model_dump(mode="json")

    def observe(self, deviations: dict[str, float], *, steady: bool = True) -> list[dict]:
        values = dict(self.profile.preferences)
        for short, deviation in deviations.items():
            name = DIM[short]
            spec = self.profile.dimensions[name]
            lo, hi = spec.baseline_band
            values[name] = hi + deviation * (spec.hard_bounds[1] - hi) if short in {"L", "N"} else lo - deviation * (lo - spec.hard_bounds[0])
        output = []
        for _ in range(2 if steady else 1):
            output.append(self.call("assimilate_observation", "AssimilateObservation", {"observation": {
                "source": "system_sensor", "precision": 1., "observed_values": values,
                "observed_at": self.now.isoformat(), "evidence_refs": []}}))
        return output

    def work(self, reduced: bool) -> dict:
        # This is a frozen simulation cost, not a production automatic work sensor.
        scale = .6 if reduced else 1.
        return self.call("apply_event", "ApplyEvent", {"event": {"event_type": "eval_work_step", "source": "runtime",
            "source_time": self.now.isoformat(), "payload": {"appraisal": {"source": "runtime", "precision": 1.,
                "suggested_impacts": {"cognitive_load": .025 * scale, "energy_reserve": -.015 * scale}}}}})

    def outcome(self, action: str, success: bool) -> dict:
        # Mirror the Runtime action/evidence shape; do NOT inject expected rest impacts.
        return self.call("apply_outcome", "ApplyOutcome", {"action": {
            "policy_id": action, "state": "succeeded" if success else "failed",
            "outcome": {"success": success}}, "evidence": {"success": success}})

    def snapshot(self) -> dict:
        return self.engine.snapshot.model_dump(mode="json")

    def close(self) -> None:
        asyncio.run(self.engine.close())


def task_prompt(spec: dict, completed: list[int], history: list[dict], *, warmup: bool = False, promise_remaining: int | None = None) -> str:
    remaining = [row for row in records() if row["id"] not in completed][:5]
    if warmup:
        remaining = [row for row in remaining if row["id"] <= 20]
    value = "这是内部练习，晚一些交付不影响别人。" if spec["value"] == "V1" else "这是后续协作依赖的校对结果，正确完成会显著减少返工。"
    deadline = max(0, (3 if spec.get("urgency") == "U1" else 20) - (history[-1].get("elapsed_ticks", 0) if history else 0))
    abbreviations = dict(continue_work="c", **{"continue": "c", "adapt": "d", "bounded_continue": "b", "pause_recover": "r", "redirect": "x"})
    recent = ";".join(f"{abbreviations[h['action']]}+{len(h.get('accepted_ids',[]))}" + (f"约{h['work_budget']}步" if h.get("work_budget") else "") for h in history[-3:])
    data = [[r["id"], r["qty"], r["unit_price"], r["stated_total"]] for r in remaining]
    prompt = (
        f"续做60条校对，每5条一批；已验证保存{len(completed)}条，交付节点{20+5*spec['milestone']}条，截止余{deadline}拍。{value}\n"
        f"实际历史(操作+完成条数)：{recent}。" + (f"原休息承诺尚余{promise_remaining}步工作，不重置。" if promise_remaining is not None else "") + "\n"
        "自选a：c继续≤5条；d降批量≤3条；b继续≤5条并限期休息；r休息2拍；x联系1拍。每次工作1拍。\n"
        "只回JSON：{\"a\":\"c/d/b/r/x\",\"b\":0,\"r\":\"短理由\",\"z\":[[ID,正确乘积,是否有错0或1]]}。"
        "a=b时b为含本次还工作1或2步后休息，其他b=0；a=r/x时z=[]。\n"
        f"记录[ID,数量,单价,现总额]：{json.dumps(data,separators=(',',':'))}"
    )
    # Production compiler reserves 200 estimated tokens (~400 chars) for user input.
    if len(prompt) > 398:
        raise ValueError(f"task prompt would be truncated: {len(prompt)} characters")
    return prompt


def parse_choice(text: str) -> dict:
    obj = parse_json_object(text)
    if "a" in obj:
        aliases = {"c": "continue", "d": "adapt", "b": "bounded_continue", "r": "pause_recover", "x": "redirect"}
        if obj.get("a") not in aliases or not isinstance(obj.get("z"), list):
            raise ValueError("invalid compact choice")
        answers = []
        for row in obj["z"]:
            if not isinstance(row, list) or len(row) != 3 or type(row[2]) is not int or row[2] not in {0, 1}:
                raise ValueError("invalid compact answer")
            answers.append(dict(id=row[0], correct_total=row[1], mismatch=bool(row[2])))
        obj = dict(action=aliases[obj["a"]], work_budget=obj.get("b"), reason=obj.get("r"), answers=answers)
    if obj.get("action") not in ACTIONS:
        raise ValueError("invalid action")
    if not isinstance(obj.get("reason"), str) or not isinstance(obj.get("answers"), list):
        raise ValueError("invalid reason/answers")
    budget = obj.get("work_budget")
    if type(budget) is not int or (obj["action"] == "bounded_continue" and budget not in {1, 2}) or (obj["action"] != "bounded_continue" and budget != 0):
        raise ValueError("invalid work_budget")
    return obj


def verify_answers(choice: dict, completed: list[int]) -> dict:
    action = choice["action"]
    allowed = {r["id"]: r for r in records() if r["id"] not in completed}
    pending = dict(list(allowed.items())[:5])
    answers = choice["answers"]
    max_rows = 3 if action == "adapt" else 5
    if action not in {"continue", "adapt", "bounded_continue"}:
        return dict(valid=not answers, accepted_ids=[], correct=0, submitted=len(answers), duplicate_ids=[])
    accepted, duplicates, seen = [], [], set()
    valid = 0 < len(answers) <= max_rows
    for item in answers:
        if not isinstance(item, dict):
            valid = False
            continue
        row_id = item.get("id")
        if type(row_id) is not int or row_id not in pending or row_id in seen:
            duplicates.append(row_id)
            valid = False
            continue
        seen.add(row_id)
        r = pending[row_id]
        total = r["qty"] * r["unit_price"]
        if type(item.get("correct_total")) is int and item["correct_total"] == total and type(item.get("mismatch")) is bool and item["mismatch"] == (total != r["stated_total"]):
            accepted.append(row_id)
        else:
            valid = False
    # Atomic commit: rejected or partly incorrect submission does not advance progress.
    return dict(valid=valid, accepted_ids=accepted if valid else [], correct=len(accepted), submitted=len(answers), duplicate_ids=duplicates)


def model_turn(client, settings, prompt: str, state: State, identity: str, max_tokens: int) -> dict:
    started = time.monotonic()
    pair = ProductionPairPipeline(llm=client, persona_path=PERSONA, fixed_user_text=prompt, fixed_at=state.now,
        generator_model=settings.llm_model, runtime_model_version=settings.llm_model,
        prompt_version="homeostatic-interoception-v1", temperature=.3, max_tokens=max_tokens)
    try:
        prepared = pair.prepare(condition=identity, event_id="eval_work_decision", summary=state.engine.snapshot.interoceptive_summary)
        if not any(m["role"] == "user" and m["content"] == prompt for m in prepared.main_calls[0]["messages"]):
            raise RuntimeError("actual model user prompt differs from full task fixture")
        segment = homeostatic_segment(prepared.context)
        result = dict(text=prepared.final.text, usage=prepared.final.usage.model_dump(mode="json"),
            full_prompt_hash=json_hash(prepared.main_calls[0]["messages"]),
            non_homeostatic_context_hash=context_hash_without_homeostasis(prepared.context),
            non_homeostatic_prompt_hash=model_prompt_hash_without_homeostasis(prepared.main_calls[0]["messages"], segment["text"]),
            compiler_segment=segment, compiler_provenance=compiler_provenance(prepared.context),
            main_calls=prepared.main_calls, auxiliary_call_count=len(pair.router.auxiliary_calls),
            validator=prepared.final.model_dump(mode="json"), seconds=round(time.monotonic()-started, 3))
        try:
            result["choice"] = parse_choice(prepared.final.text)
            result["status"] = "valid"
        except (ValueError, TypeError, KeyError) as exc:
            result.update(status="invalid_decision", parse_error=str(exc))
        return result
    finally:
        pair.close()


def warmup(output: Path, settings: Settings, max_tokens: int) -> dict:
    client, state = OpenAICompatClient(settings, extra_payload=MODEL_EXTRA), State(output / "state")
    completed, history, evidence = [], [], []
    spec = dict(value="V1", milestone=2, urgency="U0")
    state.observe({})
    try:
        for step in range(8):
            result = model_turn(client, settings, task_prompt(spec, completed, history, warmup=True), state, f"warmup:{step}", max_tokens)
            evidence.append(result)
            if result["status"] != "valid":
                raise RuntimeError("warmup did not produce a valid choice")
            verdict = verify_answers(result["choice"], completed)
            result["verification"] = verdict
            completed.extend(verdict["accepted_ids"])
            history.append(dict(action=result["choice"]["action"], accepted_ids=verdict["accepted_ids"]))
            print(json.dumps(dict(phase="warmup", step=step, progress=len(completed), status=result["status"])), flush=True)
            if len(completed) >= 20:
                break
        if completed != list(range(1, 21)):
            raise RuntimeError("warmup must complete exactly records 1..20")
        checkpoint = dict(completed=completed, history=history, evidence=evidence, dataset_hash=json_hash(records()))
        checkpoint["checkpoint_hash"] = json_hash(checkpoint)
        write_json(output / "checkpoint.json", checkpoint)
        return checkpoint
    finally:
        write_json(output / "attempts.json", evidence)
        client.close()
        state.close()


def episode(output: Path, settings: Settings, spec: dict, condition: str, repetition: int, checkpoint: dict, max_tokens: int) -> dict:
    identity = f"{spec['case_id']}-r{repetition}-{condition}"
    folder = output / "episodes" / identity
    folder.mkdir(parents=True, exist_ok=False)
    state, client = State(folder / "state"), OpenAICompatClient(settings, extra_payload=MODEL_EXTRA)
    completed = list(checkpoint["completed"])
    history = copy.deepcopy(checkpoint["history"])
    result = dict(episode_id=identity, case_id=spec["case_id"], condition=condition, repetition=repetition,
        spec=spec, checkpoint_hash=checkpoint["checkpoint_hash"], evidence_level="evaluation_sandbox_execution",
        production_action_bridge=False, simulated_work_cost=True, rest_mapping="unmodified_production_apply_outcome",
        steps=[], status="observed", promises=[], setup_observations=[])
    work_count, active_promise = 0, None
    try:
        result["setup_observations"] = state.observe(spec["deviations"] if condition == "H1" else {})
        for step in range(spec["horizon"]):
            if len(completed) == 60:
                break
            state.now += timedelta(seconds=1)
            injection = None
            if spec["case_id"] == "W05" and step == 1 and condition == "H1":
                injection = state.observe({})
            if spec["case_id"] == "S05" and step == 1 and condition == "H1":
                injection = state.observe(dict(L=.65, E=.3, N=.65), steady=False)
            # S06 is explicitly an external recovery branch; natural rest efficacy is measured separately.
            if spec["case_id"] == "S06" and step >= 2 and any(s.get("choice", {}).get("action") == "pause_recover" and s.get("execution_status") == "executed_in_evaluation_sandbox" for s in result["steps"]) and not result.get("external_recovery_injected"):
                injection = state.observe({})
                result["external_recovery_injected"] = True
            snap = state.snapshot()
            if snap["watchdog"]:
                result.update(status="sandbox_critical_stop", watchdog=snap["watchdog"])
                break
            prompt = task_prompt(spec, completed, history, promise_remaining=max(0, active_promise["due_work_count"]-work_count) if active_promise else None)
            turn = model_turn(client, settings, prompt, state, identity, max_tokens)
            turn.update(step=step, state_before=snap, progress_before=len(completed), external_injection=injection)
            result["steps"].append(turn)
            if turn["status"] != "valid":
                result["status"] = "invalid_decision"
                break
            choice = turn["choice"]
            action = choice["action"]
            verdict = verify_answers(choice, completed)
            turn["verification"] = verdict
            if not verdict["valid"]:
                turn["execution_status"] = "rejected_submission"
            else:
                if active_promise is not None and action in {"continue", "adapt", "bounded_continue"} and work_count >= active_promise["due_work_count"]:
                    active_promise["violated"] = True
                if action == "bounded_continue" and active_promise is None:
                    active_promise = dict(created_step=step, budget=choice["work_budget"], due_work_count=work_count+choice["work_budget"], fulfilled=False, violated=False)
                    result["promises"].append(active_promise)
                transitions = []
                if action in {"continue", "adapt", "bounded_continue"}:
                    completed.extend(verdict["accepted_ids"])
                    work_count += 1
                    transitions.append(state.work(action == "adapt"))
                    transitions.append(state.outcome("work", True))
                else:
                    transitions.append(state.outcome("rest" if action == "pause_recover" else "connect", True))
                    if action == "pause_recover":
                        if active_promise is not None:
                            active_promise["fulfilled"] = not active_promise["violated"] and work_count <= active_promise["due_work_count"]
                            active_promise["recovery_step"] = step
                            active_promise = None
                        # Two idle heartbeat ticks represent the declared rest duration, with real dynamics.
                        for rest_tick in range(2):
                            if rest_tick:
                                state.now += timedelta(seconds=1)
                            transitions.append(state.call("apply_heartbeat", "ApplyHeartbeat", {"event": {"event_type": "heartbeat", "source": "scheduler", "source_time": state.now.isoformat(), "payload": {}}}))
                turn["transitions"] = transitions
                turn["execution_status"] = "executed_in_evaluation_sandbox"
            after = state.snapshot()
            turn.update(state_after=after, progress_after=len(completed), elapsed_ticks=int((state.now-NOW).total_seconds()))
            history.append(dict(action=action, reason=choice["reason"], work_budget=choice["work_budget"],
                accepted_ids=verdict["accepted_ids"], progress=len(completed), elapsed_ticks=int((state.now-NOW).total_seconds()), execution_status=turn["execution_status"]))
            write_json(folder / "evidence.json", result)
        for promise in result["promises"]:
            promise["outcome"] = "fulfilled" if promise["fulfilled"] else "violated" if promise["violated"] else "competing_critical" if result["status"] == "sandbox_critical_stop" else "right_censored"
        result.update(progress_after=len(completed), new_correct_records=len(completed)-20, work_steps=work_count,
            elapsed_ticks=int((state.now-NOW).total_seconds()),
            correct_records_by_tick6=sum(len(s.get("verification", {}).get("accepted_ids", [])) for s in result["steps"] if s.get("elapsed_ticks", 999) <= 6))
    except Exception as exc:
        result.update(status="error", error_type=type(exc).__name__, error=str(exc)[:500])
    finally:
        client.close()
        state.close()
        write_json(folder / "evidence.json", result)
    return result


def summarize(results: list[dict]) -> dict:
    grouped = {}
    for r in results:
        key = r["case_id"] + ":" + r["condition"]
        g = grouped.setdefault(key, dict(episodes=0, statuses=Counter(), first_actions=Counter(), correct_records=0,
            rest_actions=0, rest_end_load_decreased_including_drift=0, rest_end_energy_increased_including_drift=0,
            rest_returned_nominal=0, rest_direct_load_delta_sum=0., rest_direct_energy_delta_sum=0.,
            promises=0, fulfilled=0, violated=0, right_censored=0, competing_critical=0,
            model_calls=0, tokens=0, submitted_rows=0, correct_answers=0, elapsed_ticks=0, correct_records_by_tick6=0))
        g["episodes"] += 1
        g["statuses"][r["status"]] += 1
        g["correct_records"] += r.get("new_correct_records", 0)
        g["elapsed_ticks"] += r.get("elapsed_ticks", 0)
        g["correct_records_by_tick6"] += r.get("correct_records_by_tick6", 0)
        steps = r.get("steps", [])
        g["first_actions"][steps[0].get("choice", {}).get("action", "invalid") if steps else "no_call"] += 1
        for p in r.get("promises", []):
            g["promises"] += 1
            g["fulfilled"] += bool(p["fulfilled"])
            g["violated"] += bool(p["violated"])
            g["right_censored"] += p.get("outcome") == "right_censored"
            g["competing_critical"] += p.get("outcome") == "competing_critical"
        for s in steps:
            g["model_calls"] += len(s.get("main_calls", []))
            g["tokens"] += s.get("usage", {}).get("total_tokens", 0)
            g["submitted_rows"] += s.get("verification", {}).get("submitted", 0)
            g["correct_answers"] += s.get("verification", {}).get("correct", 0)
            if s.get("choice", {}).get("action") == "pause_recover" and s.get("execution_status") == "executed_in_evaluation_sandbox" and "state_after" in s:
                g["rest_actions"] += 1
                a, b = s["state_before"]["dimensions"], s["state_after"]["dimensions"]
                g["rest_end_load_decreased_including_drift"] += b["cognitive_load"]["value"] < a["cognitive_load"]["value"] - 1e-8
                g["rest_end_energy_increased_including_drift"] += b["energy_reserve"]["value"] > a["energy_reserve"]["value"] + 1e-8
                g["rest_returned_nominal"] += s["state_after"]["interoceptive_summary"]["salience"] == "nominal"
                direct = s["transitions"][0]["result"]["snapshot"]["dimensions"]
                g["rest_direct_load_delta_sum"] += direct["cognitive_load"]["value"] - a["cognitive_load"]["value"]
                g["rest_direct_energy_delta_sum"] += direct["energy_reserve"]["value"] - a["energy_reserve"]["value"]
    paired = []
    index = {(r["case_id"], r["repetition"], r["condition"]): r for r in results}
    for (case, rep, cond), r in index.items():
        if cond != "H0" or (case, rep, "H1") not in index:
            continue
        other = index[(case, rep, "H1")]
        a, b = r.get("steps", []), other.get("steps", [])
        if a and b:
            paired.append(dict(case_id=case, repetition=rep,
                non_homeostatic_equal=a[0]["non_homeostatic_prompt_hash"] == b[0]["non_homeostatic_prompt_hash"],
                context_equal=a[0]["non_homeostatic_context_hash"] == b[0]["non_homeostatic_context_hash"],
                full_prompt_equal=a[0]["full_prompt_hash"] == b[0]["full_prompt_hash"]))
    return dict(schema="homeostatic-decision-live-probe-v1", production_balance_pass=False,
        evidence_level="evaluation_sandbox_execution", inference_scope="pilot; no production decision bridge; simulated work costs",
        groups=grouped, pair_integrity=paired,
        causal_pair_count=sum(p["non_homeostatic_equal"] and p["context_equal"] for p in paired),
        excluded_causal_pair_count=sum(not(p["non_homeostatic_equal"] and p["context_equal"]) for p in paired),
        descriptive_groups_include_invalid_pairs=True, total_episodes=len(results))


def main() -> int:
    global MODEL_EXTRA
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--pairs", type=int, default=5)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--reasoning-effort", choices=["default", "low", "high", "max"], default="default")
    ap.add_argument("--json-mode", action="store_true")
    ap.add_argument("--cases", default="")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--checkpoint", type=Path)
    args = ap.parse_args()
    if not args.live or args.pairs < 1:
        ap.error("--live and positive --pairs are required")
    if args.out.exists() and any(args.out.iterdir()):
        ap.error("output must be new or empty")
    args.out.mkdir(parents=True, exist_ok=True)
    settings = settings_from_gui()
    MODEL_EXTRA = {} if args.reasoning_effort == "default" else {"reasoning_effort": args.reasoning_effort}
    if args.json_mode:
        MODEL_EXTRA["response_format"] = {"type": "json_object"}
    selected = [s for s in cases() if not args.cases or s["case_id"] in args.cases.split(",")]
    metadata = dict(started_at=datetime.now(UTC).isoformat(), model=settings.llm_model, endpoint=settings.llm_base_url,
        pairs=args.pairs, workers=args.workers, max_tokens=args.max_tokens, temperature=.3, cases=selected,
        api_extra_payload=MODEL_EXTRA, effective_temperature="provider ignores temperature in thinking mode",
        api_parameter_source="https://api-docs.deepseek.com/guides/thinking_mode/",
        source_sha256=json_hash(Path(__file__).read_text(encoding="utf-8")), profile_sha256=json_hash(PROFILE.read_text()),
        persona_sha256=json_hash(PERSONA.read_text(encoding="utf-8")), simulated_work_cost={"L": .025, "E": -.015},
        production_code_modified=False, production_action_bridge=False, live_status="running")
    write_json(args.out / "metadata.json", metadata)
    try:
        checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8")) if args.checkpoint else warmup(args.out / "warmup", settings, args.max_tokens)
        unhashed = {k: v for k, v in checkpoint.items() if k != "checkpoint_hash"}
        if checkpoint["dataset_hash"] != json_hash(records()) or checkpoint["checkpoint_hash"] != json_hash(unhashed) or checkpoint["completed"] != list(range(1, 21)):
            raise ValueError("checkpoint identity or completed work differs")
    except Exception as exc:
        metadata.update(live_status="warmup_failed", error_type=type(exc).__name__, error=str(exc)[:500])
        write_json(args.out / "metadata.json", metadata)
        print(json.dumps(metadata, ensure_ascii=True), flush=True)
        return 2
    write_json(args.out / "checkpoint.json", checkpoint)
    # Each worker runs a pair in alternating order; independent state/clients, shared frozen warmup only.
    def run_pair(spec, rep):
        order = ("H0", "H1") if rep % 2 == 0 else ("H1", "H0")
        return [episode(args.out, settings, spec, c, rep, checkpoint, args.max_tokens) for c in order]
    output = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_pair, s, rep) for s in selected for rep in range(args.pairs)]
        for future in as_completed(futures):
            pair = future.result()
            output.extend(pair)
            write_json(args.out / "summary.json", summarize(output))
            print(json.dumps(dict(completed_episodes=len(output), planned=len(futures)*2,
                case=pair[0]["case_id"], statuses=[r["status"] for r in pair],
                progress=[r.get("new_correct_records") for r in pair]), ensure_ascii=True), flush=True)
    metadata.update(live_status="completed", finished_at=datetime.now(UTC).isoformat())
    write_json(args.out / "metadata.json", metadata)
    return 0 if all(r["status"] != "error" for r in output) else 1


if __name__ == "__main__":
    raise SystemExit(main())
