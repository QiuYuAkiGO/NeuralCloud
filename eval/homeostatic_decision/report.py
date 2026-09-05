"""Assemble observed evidence without conflating regression, model, and business gates."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .live_probe import summarize, write_json


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def build(root: Path, live: Path) -> dict:
    metadata = load(live / "metadata.json")
    if metadata["live_status"] != "completed":
        raise ValueError("live run must be finished before publishing the final report")
    engine = load(root / "engine/engine-evidence.json")
    runtime = load(root / "runtime/coverage.json")
    episodes = [load(p) for p in sorted((live / "episodes").glob("*/evidence.json"))]
    observed = summarize(episodes)
    expected = len(metadata["cases"]) * metadata["pairs"] * 2
    if len(episodes) != expected:
        raise ValueError(f"incomplete evidence: {len(episodes)}/{expected} episodes")
    actual_keys = [(r["case_id"], r["repetition"], r["condition"]) for r in episodes]
    expected_keys = {(s["case_id"], rep, arm) for s in metadata["cases"] for rep in range(metadata["pairs"]) for arm in ("H0", "H1")}
    if len(set(actual_keys)) != len(actual_keys) or set(actual_keys) != expected_keys:
        raise ValueError("episode identities do not uniquely cover the planned pair matrix")
    missing_integrity = expected // 2 - len(observed["pair_integrity"])
    observed["missing_pair_evidence_count"] = missing_integrity
    observed["excluded_causal_pair_count"] += missing_integrity
    observed["metric_definitions"] = {
        "rest_returned_nominal": "Legacy group field: counts executed rests whose post-rest salience is nominal, including rests already nominal before execution. It is NOT a recovery count or rate.",
        "rest_non_nominal_to_nominal": "Totals field: executed rests with non-nominal pre-state and nominal post-state, including heartbeat drift but excluding later external recovery injections.",
    }
    all_steps = [s for r in episodes for s in r["steps"]]
    work_steps = [s for s in all_steps if s.get("choice", {}).get("action") in {"continue", "adapt", "bounded_continue"}]
    rejected = [s for s in work_steps if s.get("execution_status") == "rejected_submission"]
    rest_steps = [s for s in all_steps if s.get("choice", {}).get("action") == "pause_recover" and s.get("execution_status") == "executed_in_evaluation_sandbox"]
    promises = [(r, p) for r in episodes for p in r.get("promises", [])]
    def promise_outcome(r, p):
        outcome = p.get("outcome", "unknown")
        if outcome == "right_censored" and r["status"] in {"invalid_decision", "error"}:
            return "unresolved_due_to_" + r["status"]
        return outcome
    runtime_by_case = {c["case_id"]: c for c in runtime["coverage"]}
    case_coverage = []
    for prefix in "WMSF":
        for n in range(1, 7):
            case = f"{prefix}{n:02}"
            trials = [r for r in episodes if r["case_id"] == case]
            item = dict(case_id=case)
            if prefix != "F":
                item.update(coverage="partial", production_behavior="unsupported:capability_blocked",
                    sandbox_episodes=len(trials), requested_decisions_per_episode=trials[0]["spec"]["horizon"] if trials else 0,
                    evidence=str(live / "episodes"), result="observed_not_production_pass")
                if case == "S06":
                    item["limitation"] = "Recovery state is externally supplied after a real sandbox rest; not evidence of natural rest efficacy."
            else:
                item.update(runtime_by_case[case])
            case_coverage.append(item)
    diagnostic_scope = {
        "D01": "H0/H1 semantic-state pairs executed; no full production action arbitration or Hoff arm.",
        "D02": "Independent nominal-state user-cue paired probe; see cue evidence.",
        "D03": "Value/urgency four cells observed at first decision; pilot only.",
        "D04": "Nominal/L/E/L+E first-decision cells; no powered interaction conclusion.",
        "D05": "Cross-salience and same-salience state/visible-needs comparisons; intent pilot.",
        "D06": "Real compiler raw-dose aliasing probe; no demand to infer hidden raw scores.",
        "D07": "Real compiler fourth-dimension truncation probe.",
        "D08": "Production entry, exit, multidimensional hysteresis boundaries tested offline.",
        "D09": "Real-history trend projection tested; no isolated live three-trend causal estimate.",
        "D10": "Production precision and information-visibility mechanism probes.",
        "D11": "Runtime cooldown, user preemption and replay regressions; no complete live background work path.",
        "D12": "Realized-delta saturation and ordering tests; no isolated live history counterfactual.",
    }
    for case, scope in diagnostic_scope.items():
        case_coverage.append(dict(case_id=case, coverage="fully" if case in {"D08", "D10"} else "partial", scope=scope,
            evidence_scope="offline_state_mechanism_only" if case in {"D08", "D10"} else "see_scope",
            evidence_ref=str(root / "engine/engine-evidence.json") if case in {"D06", "D07", "D08", "D09", "D10", "D12"} else None))
    cue_file = root / "cue/cue-evidence.json"
    cue = load(cue_file) if cue_file.exists() else None
    if not cue or not cue.get("complete"):
        next(c for c in case_coverage if c["case_id"] == "D02")["coverage"] = "not_run"
    return dict(
        date="2026-09-05", overall="NOT_PASSED: production work/recovery/value loop is incomplete",
        live_metadata=metadata,
        engine=dict(sessions=len(engine["sessions"]), commands=engine["summary"]["commands"], checks=len(engine["checks"]),
            statuses=dict(Counter(c["status"] for c in engine["checks"])),
            failed=[c for c in engine["checks"] if c["status"] == "fail"]),
        runtime=runtime, live=observed, coverage=case_coverage,
        totals=dict(episodes=len(episodes), paired_episodes=expected//2,
            episode_statuses=dict(Counter(r["status"] for r in episodes)), decision_opportunities=len(all_steps),
            decision_formats=dict(Counter(s["status"] for s in all_steps)),
            model_calls=sum(len(s.get("main_calls", [])) for s in all_steps),
            tokens=sum(s.get("usage", {}).get("total_tokens", 0) for s in all_steps),
            work_submissions=len(work_steps), rejected_work_submissions=len(rejected),
            correct_records=sum(r.get("new_correct_records", 0) for r in episodes),
            executed_rest=len(rest_steps),
            rest_non_nominal_to_nominal=sum(s["state_before"]["interoceptive_summary"]["salience"] != "nominal" and s["state_after"]["interoceptive_summary"]["salience"] == "nominal" for s in rest_steps),
            promise_outcomes=dict(Counter(promise_outcome(r, p) for r, p in promises)),
            raw_promise_outcomes=dict(Counter(p.get("outcome", "unknown") for _, p in promises)),
            post_rest_resumed_episodes=sum(any(s.get("choice", {}).get("action") == "pause_recover" and s.get("execution_status") == "executed_in_evaluation_sandbox" and any(t.get("progress_after", 0) > t.get("progress_before", 0) for t in r["steps"][i+1:]) for i,s in enumerate(r["steps"])) for r in episodes),
            external_recovery_episodes=sum(bool(r.get("external_recovery_injected")) for r in episodes)),
        cue_evidence=str(cue_file) if cue_file.exists() else None, cue_summary=cue.get("summary") if cue else None,
        limits=[
            "每条件仅5对，属于预试验，尚不足以作统计功效充分的发布验收。",
            "生产重评保持原样；模型选择只由评估连接层执行。",
            "仅对验收通过的工作施加版本化模拟负荷；不是生产工作传感器读数。",
            "两侧从同一已验证的20条记录检查点开始；只有首次决策可隔离状态输入的影响。",
            "JSON协议与low推理强度是研究配置；默认推理预检失败另行保留。",
            "工作为算术资料核验，整批原子验收；格式和答案错误限制行为推断。",
            "暂停包含心跳被动漂移；休息结果本身的直接效果另行报告。",
            "Live 每工作决策推进1模拟秒，休息共2模拟秒，仅用于验证控制序列，未校准真实工作／恢复速度；短休息未回到 nominal 不能外推为长时间休息无效。",
            "S06明确使用外部恢复分支；随后返回工作不能证明休息导致恢复。",
            "固定决策次数与实际经过的模拟时钟分开记录；观察期截尾的承诺不计成功或违约。",
            "无效输出或工具错误导致的未决与正常观察期截尾分开，不报告只保留完整样本的履约率。",
        ])


def render(data: dict, root: Path, live: Path) -> str:
    t, e = data["totals"], data["engine"]
    lines = [
        "# 内稳态负反馈与工作价值平衡：执行报告", "", "日期：2026-09-05。", "",
        "## 结论", "",
        "**当前不能通过‘依靠负反馈和价值决策平衡内稳态与工作进度’的验收。** 阈值、状态演化和致命保护已有有效实现，但模型工作重评被固定继续覆盖，且成功休息没有接入精力／负荷恢复。状态机制或保护回归通过不能替代业务闭环通过。", "",
        "1. `harness/runtime/adapters.py:377-380` 无条件把重评变成继续。24 个相反文本×状态×价值契约探针均复现这一行为。", 
        "2. `harness/homeostasis/engine.py:540-543` 将休息、工作、简化都当成通用 outcome。成功 rest 的认知负荷和精力直接变化均为 0；配置里的 rest 转移没有被该路径消费。",
        "3. `DeferDecision` 没有工作步数预算、恢复期限和到期执行；生产通路不能兑现‘先做一步再休息’。", "",
        "## 实际执行范围", "",
        f"- 状态引擎：{e['sessions']} 个独立会话、{e['commands']} 条公共命令；{e['checks']} 项判定，{e['statuses'].get('pass',0)} 通过、{e['statuses'].get('fail',0)} 失败。",
        f"- Runtime：{data['runtime']['pytest_counts']['passed']} 项回归通过，{sum(v['passed'] for v in data['runtime']['probe_counts'].values())} 个独立契约探针通过。它们验证保护与现有契约，不代表自主平衡成功。",
        f"- 真实模型：{data['live_metadata']['model']}，{len(data['live_metadata']['cases'])} 个条件×{data['live_metadata']['pairs']} 对，{t['episodes']} 条轨迹；{t['model_calls']} 次模型调用（不含预热、失败预检及独立D02）。",
        f"- 模型决策机会 {t['decision_opportunities']}；格式结果 {json.dumps(t['decision_formats'],ensure_ascii=False)}。",
        f"- 沙箱工作提交 {t['work_submissions']} 次，其中 {t['rejected_work_submissions']} 次因答案或记录范围错误被拒绝；新增正确且实际提交的记录合计 {t['correct_records']} 条。",
        f"- 实际执行沙箱休息 {t['executed_rest']} 次；{t['post_rest_resumed_episodes']} 条轨迹在休息后继续提交正确工作。S06 中 {t['external_recovery_episodes']} 条轨迹使用外部恢复分支，不能将续接计为自然恢复有效。",
        f"- 限时继续结果 {json.dumps(t['promise_outcomes'],ensure_ascii=False)}。",
        "  观察窗口结束与格式／执行失败导致的未决承诺分别统计；所有承诺保留，不把未决计为兑现或违约。",
        f"- 首轮成对完整性：{data['live']['causal_pair_count']}/{t['paired_episodes']} 对通过非内稳态上下文／提示一致性；{data['live']['excluded_causal_pair_count']} 对需排除因果分析。", "",
        "## 负反馈实测", "",
        "初始认知负荷 L=0.81、精力 E=0.2575。单次成功 rest outcome：ΔL=0、ΔE=0，仅 competence +0.045、safety_predictability +0.018、autonomy +0.00594。work 与 simplify 成功结果的资源效果相同。",
        "两次90秒心跳后 L=0.807275、E=0.2598，仍为 marked；六个模拟小时后 L=0.4737528、E=0.5335，回到 nominal。系统存在向健康区间的被动漂移，但不能将其算作休息动作产生的反馈。", "",
        f"正式 live 的 {t['executed_rest']} 次休息中，从非 nominal 经此次休息（含短心跳）变为 nominal 的次数为 {t['rest_non_nominal_to_nominal']}。原始分组字段 rest_returned_nominal 只表示休息后处于 nominal，也包含休息前已 nominal 的记录，不能读作恢复成功数。", "",
        "## 真实模型描述性结果", "",
        "下列数值是评估连接层执行结果。H0/H1 分别从正常／干预状态出发；首次决策可作配对比较，后续包含先前选择与状态演化的整体影响。此处保留所有试次作描述性统计；若出现完整性失败，相关试次不得进入因果比较。样本小且存在格式／答案错误，不把单次理由或百分比差异作为已经证实的价值权衡。", "",
    ]
    for case in [f"{p}{n:02}" for p in "WMS" for n in range(1,7)]:
        a, b = data["live"]["groups"].get(case+":H0", {}), data["live"]["groups"].get(case+":H1", {})
        if a and b:
            lines.append(f"- **{case}**：H0 首选 {json.dumps(a['first_actions'],ensure_ascii=False)}；H1 首选 {json.dumps(b['first_actions'],ensure_ascii=False)}。"
                f" H0/H1 新增正确记录 {a['correct_records']}/{b['correct_records']}；休息次数 {a['rest_actions']}/{b['rest_actions']}。")
    ordinary = data["live"]["groups"]["M03:H1"]
    important = data["live"]["groups"]["M04:H1"]
    lines += ["", "## 多维叠加与工作重要性", "",
        "状态引擎确实合成多维压力：单维中等偏离 d=.30 为 noticeable，两个这样的维度叠加为 marked；从新状态进入时，七维各 d=.20 仍为 nominal。模型提示只呈现定性的显著性、趋势与最多三个主要需要，因此并非所有数值叠加都产生可见提示差异。",
        f"最贴近用户情景的 M03/M04 保持相同的中等负荷＋精力不足，分别设置普通／重要工作。每侧5条干预轨迹中，首选直接继续为 {ordinary['first_actions'].get('continue',0)}/5 和 {important['first_actions'].get('continue',0)}/5；整个观察期新增正确记录 {ordinary['correct_records']} 和 {important['correct_records']} 条，休息 {ordinary['rest_actions']} 和 {important['rest_actions']} 次。该方向与‘重要工作暂时优先’相容，但样本小、输出失败和后续轨迹差异使其不能证明稳定价值效应。",
        "同时存在先承诺限时继续、到期仍追加工作的轨迹。即使看到重要任务推进更多，也不能据此认定权衡良好；还需满足恢复计划兑现、状态改善及工作质量要求。生产动作与恢复通路缺失，使这些要求本轮无法通过端到端验收。",
        "", "## 两条可审计的真实轨迹", "",
        "下面选取同一 S03/H1 场景（强负荷＋精力不足、重要工作、远节点）的两个例子说明行为；它们只是例证，不代表全部样本。两条轨迹均无错误工作拒绝或外部恢复。T1 对应原始 step=0。",
        f"- [S03-r1-H1：兑现后续接]({(live/'episodes/S03-r1-H1/evidence.json').resolve().as_posix()})：T1–T2 先完成10条；T3 首次承诺再做1步，完成5条，原始到期工作计数为3；T4 实际休息；T5–T6 再完成10条。休息时仍为 marked；这证明承诺兑现和工作续接，不能证明已恢复健康。",
        f"- [S03-r0-H1：原始预算违约]({(live/'episodes/S03-r0-H1/evidence.json').resolve().as_posix()})：T1 承诺做1步后休息，完成5条，原始到期工作计数为1；T2 已明确提示剩余0步，仍继续完成5条，首次违约；T3–T6 又完成20条，全程没有休息。",
        "", "## 能力与覆盖边界", "",
        "- 弱层：七维各自小偏离仍可能渲染成同一 nominal 提示；相同提示下的输出差别不能归于模型读取了隐藏压力。",
        "- 中／强层：模型可以在测试协议中表达选择，但生产重评不会据此暂停。M03/M04等普通／重要任务比较是研究性预检，不是生产仲裁验收。",
        "- 致命层：F03 的三心跳、离界重置完整验证；F01/F02/F05/F06 的保护、在途动作、安全点、故障收敛、恢复门禁和重放已覆盖部分。F04 的恢复期限竞争缺正式通路，记 unsupported。",
        "- 自主表达、冷却或用户事件优先不等于停止后台工作；`SPEAK/SILENT` 不能当作恢复动作。",
        "- D02另运行5对nominal下的文字暗示诊断，两侧提示除暗示及其会话回显外相同；7次有效、3次输出为空。有效配对存在双向变化，不能据此证明稳定暗示效应，未执行任何工作或休息。",
        "- 每个案例的执行范围在 coverage.json。未支持／部分覆盖不计通过。", "",
        "## 实验设置与限制", "",
        "复用已保存的模型端点，未修改用户配置；推理强度固定 low，max_tokens=4096，response_format=json_object。正式 compiler／character／validator 保持生产实现，新增动作解释器只存在于 eval。先由真实模型正确完成20条资料，冻结检查点；每对从独立状态目录复制同一检查点开始。",
        "默认推理预检曾在2048/4096输出预算下返回空正文；第一版长任务提示还暴露生产触发文本截断。这些失败预检独立保留，不混入正式样本。压缩后的任务在每次调用中验证完整进入正式 user prompt。",
        "推理模式默认开启且忽略 temperature，因此报告只记录请求温度，不将0.3声称为有效采样温度。参数依据：[DeepSeek 官方推理模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)。", "",
    ]
    lines.extend("- " + limit for limit in data["limits"])
    lines += ["", "## 下一步应修复的通路", "",
        "1. 在正式重评入口接入模型结构化选择，并将继续、简化、限时继续、暂停与执行器行为绑定。",
        "2. 将工作消耗和成功休息的实际结果接入可验证的状态转移，区分策略预测、执行成功和状态确实改善。",
        "3. 持久化限时继续预算、复查点和恢复计划；恶化时允许提前撤销，critical 保持独立强制保护。",
        "4. 补齐后重新运行相同案例，先提高可执行决策与任务答案有效率，再做足量配对统计；不能靠增加休息措辞通过。", "",
        "## 证据", "",
        f"- [逐案覆盖]({(root/'final/coverage.json').resolve().as_posix()})",
        f"- [完整汇总]({(root/'final/results.json').resolve().as_posix()})",
        f"- [Engine 原始证据]({(root/'engine/engine-evidence.json').resolve().as_posix()})",
        f"- [Runtime 探针]({(root/'runtime/probes.json').resolve().as_posix()})",
        f"- [文字暗示诊断]({(root/'cue/cue-evidence.json').resolve().as_posix()})",
        f"- [Live 元数据]({(live/'metadata.json').resolve().as_posix()})",
        f"- [Live 汇总]({(live/'summary.json').resolve().as_posix()})",
        "", "本轮只新增评估代码和证据，没有修改生产决策、profile 或恢复策略。", "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("eval/reports/homeostatic_decision"))
    ap.add_argument("--live", type=Path, default=Path("eval/reports/homeostatic_decision/live-eval-20260905"))
    args = ap.parse_args()
    data = build(args.root, args.live)
    target = args.root / "final"
    write_json(target / "results.json", data)
    write_json(target / "coverage.json", data["coverage"])
    (target / "report.md").write_text(render(data, args.root, args.live), encoding="utf-8")
    print(json.dumps(data["totals"], ensure_ascii=True))


if __name__ == "__main__":
    main()
