# 内稳态状态与负反馈离线实测

日期：2026-09-05；profile：functional-proxy-v1@3。

执行 89 个独立状态会话、544 条真实 Engine 命令，90 项判定：87 通过、3 失败。模型调用为 0。

结论：阈值、迟滞、趋势、来源精度、实际 delta 耦合与被动恢复机制可运行；生产工作/休息闭环尚不能判定为通过。

## 核心缺口

成功 rest outcome 的实际变化为 {"energy_reserve": 0.0, "cognitive_load": 0.0, "safety_predictability": 0.018000000000000016, "competence": 0.04500000000000004, "autonomy": 0.005940000000000056, "social_connection": 0.0, "novelty": 0.0}。恢复判定：失败。

当前配置的 transition_model.rest 未由 apply_outcome 消费，rest/work/simplify 作为通用成功 outcome 处理。人为显式提交 rest appraisal 可改变状态，但这只是状态积分正对照，不能算生产恢复动作成功。

## 可验证结果

- 弱单维、双维、三维、七维从 nominal 进入仍 nominal，真实 compiler messages 与基线一致。七维弱偏离不能直接相加视为强压力。
- 中等单维 noticeable，中等双维 marked；强单维、双维均 marked，但需要列表仍能使提示不同。
- L(.65)/L(.75) compiler messages 相同；第四维未进入前三时可完全不可见。
- .249/.250/.251 与 .499/.500/.501 在全部七维进入边界符合预期；历史退出与多维 .15 条件、连续迟滞轨迹单独实测。
- 同一 .81 当前负荷由真实历史产生 steady/worsening/recovering，三种提示不同。来源精度影响事件同化；值与趋势匹配后仅置信度变化不进提示。
- 不饱和线性更新可有相同最终数值；边界截断产生路径依赖；已饱和来源不会因请求 delta 而产生虚假耦合。

## 被动恢复与工作代理

- 初始：L=0.81000000，E=0.25750000，salience=marked。
- 2 次 90 秒心跳：L=0.80727500，E=0.25980000，salience=marked。
- 6 个模拟小时：L=0.47375280，E=0.53350000，salience=nominal。

心跳向健康区间中心漂移，不依赖是否休息；不能将其全部归因于休息策略。普通 perception 可增加负荷并经耦合消耗精力，但不是对实际完成工作量的测量。

## 失败判定

- rest_action_recovery：successful rest outcome restores energy and reduces cognitive load。
- rest_vs_work_differential：rest and work outcomes have distinct energy/load effects。
- work_outcome_load_growth：successful work outcome creates workload/resource costs。

## 证据与复现

完整命令、前后值、realized delta、transition trace、summary、compiler 文本与消息哈希见 engine-evidence.json。

`python -m eval.homeostatic_decision.engine_probe --output eval/reports/homeostatic_decision/engine`

该探针没有调用模型、执行校对动作或验收限时继续计划。compiler.render_messages 哈希用于投影可见性，不冒充完整 character 首轮生成提示。
