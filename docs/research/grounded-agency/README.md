# Grounded Agency 研究实施路线

> 状态：GA-01 Implemented；GA-02 至 GA-08 Planned
> 开发分支：`codex/grounded-agency`（上游：本地 `codex/homeostasis-stability`）
> 跨仓库依赖：`QiuYuAkiGO/MistyAI`  
> 文档用途：把后续研究拆成可由单轮 Coding Agent 独立实现、测试和提交的阶段。

## 1. 总目标

本路线不以“实现关系陪伴模块”为直接目标，也不预设角色必须信任、依恋、主动联系或照顾某个对象。

系统首先建立以下一般能力：

1. 持续、可验证且不可由角色任意修改的身体事实；
2. 不携带人类化结论的内部感知通道；
3. 由主模型自己完成的结构化观察、等待、行动与目标选择；
4. 基于真实结果形成并修订因果 belief 的能力；
5. 跨时间维持、暂停、恢复和重规划自身目标的能力；
6. 在开放社会环境中形成对象特异、可逆且有行为后果的长期判断。

当这些能力在没有固定 `trust reward`、`care drive`、关系状态机或陪伴提示词的条件下共同运行时，再观察是否自然出现：

- 稳定的对象区分与关系预期；
- 自主接近、回避、等待、求助、帮助或修复行为；
- 具有机会成本的他者导向目标；
- 持续、非模板化且符合共同经历的情感陪伴表现。

这些结果属于后验观察与离线分类，不进入运行时奖励、Homeostasis 维度或固定策略表。

## 2. 跨阶段不变量

所有阶段都必须保持：

- **事实与解释分离**：MistyAI 提供身体和环境事实，NeuralCloud 形成 belief 与决策。
- **实验条件显式化**：模型、Prompt、Persona、环境、传感变换、初始状态和随机种子均由 manifest 冻结。
- **允许不行动**：`OBSERVE / WAIT / DEFER / NO_ACTION` 与执行动作同等合法。
- **不由规则代替模型选择**：核心研究条件不得自动执行最低 EFE、固定恢复策略或最高排名 policy。
- **不把社会结果写入目标函数**：不新增 `care`、`attachment`、`user_special` 等 drive。
- **以行为和因果轨迹为主证据**：自我报告只作为附加数据，不作为主要达成条件。
- **每个阶段独立可回滚**：新功能默认关闭，必须通过显式 manifest condition 启用。

## 3. 单轮 Agent 执行规范

每份阶段文档都是一个独立 Coding Agent 任务。执行时必须遵守：

1. 只实现该文档的“本轮代码范围”，不得提前实现下一阶段。
2. 修改应集中在文档列出的模块；发现结构问题时用 TODO 或后续文档记录，不做无关重构。
3. 每轮必须同时交付实现、自动化测试和文档状态更新。
4. 单轮建议控制在 4–12 个核心源码/测试文件；生成 fixture、schema 快照和迁移文件不计入核心文件数。
5. 所有测试必须可在无真实 LLM 的条件下用 deterministic fake 完成；需要真实模型的验证只能作为显式 opt-in smoke。
6. 完成后在阶段文档中把状态从 `Planned` 改为 `Implemented`，记录提交 SHA、测试命令和已知限制。
7. 若任一强制验收项未通过，不得将阶段标记为完成。

## 4. 阶段顺序

| 顺序 | 仓库 | 阶段文档 | 核心结果 |
| --- | --- | --- | --- |
| 0 | NeuralCloud | `docs/HOMEOSTASIS_STABILITY_PLAN.md` | 冻结可重复的内稳态与 Runtime 基线 |
| 1 | MistyAI | `docs/design/0005-physiology-authority-convergence.md` | 统一身体资源事实源与采样语义 |
| 2 | MistyAI | `docs/design/0006-energy-station-charging-loop.md` | 角色可通过普通环境行为恢复资源 |
| 3 | MistyAI | `docs/design/0007-experiment-intervention-replay.md` | 可重复的真值干预、故障注入与快照重放 |
| 4 | NeuralCloud | `01-neutral-experiment-contract.md` | 显式冻结研究 treatment 并阻断条件混淆 |
| 5 | NeuralCloud | `02-opaque-interoception-channel.md` | 建立不携带恢复结论的中性内部通道 |
| 6 | NeuralCloud | `03-model-owned-structured-decision.md` | 主模型自行输出结构化行动选择 |
| 7 | NeuralCloud | `04-gmv1-scenario-runner.md` | 可批量运行 Grounded Motivation 场景 |
| 8 | NeuralCloud | `05-gmv1-causal-metrics-and-gates.md` | 用反事实和消融区分语义模仿与功能动机 |
| 9 | NeuralCloud | `06-body-causal-belief.md` | 从结果证据形成可修订的身体因果 belief |
| 10 | NeuralCloud | `07-long-horizon-agency.md` | 形成跨任务的提前规划、持续目标与恢复能力 |
| 11 | NeuralCloud | `08-open-social-generalization.md` | 在开放社会环境中观察一般能力的自然泛化 |

阶段 1–3 与阶段 4 的部分工作可以并行；阶段 5 之后必须使用阶段 1–4 已冻结的契约。阶段 8 未通过前，不进入阶段 9–11。

## 5. 分支与提交建议

本路线在 `codex/grounded-agency` 上按阶段独立实现与提交；该分支从
`codex/homeostasis-stability` 的 `1334c43049f13c557abda1716582185d117e5e0e` 创建并跟踪它。
需要并行开发时，可从研究开发分支创建阶段子分支，例如：

```text
codex/ga-01-neutral-contract
codex/ga-02-opaque-interoception
codex/ga-03-model-owned-decision
...
```

阶段子分支完成后先合并回 `codex/grounded-agency`，整体验收后再合并回
`codex/homeostasis-stability`，不得直接推送到 `master`。

MistyAI 每阶段也建议使用独立子分支，完成后合并回当前主开发分支。跨仓库阶段必须在 manifest 和评测报告中同时记录两侧提交 SHA。

## 6. 最终证据链

路线完成后，系统应能提供以下连续证据，而不是只展示一段看似有情感的文本：

```text
Body Truth
→ Opaque Sensor Observation
→ Agent Belief / Uncertainty
→ Model-owned Structured Decision
→ Verified Action Outcome
→ Body / World Consequence
→ Belief Revision
→ Long-horizon Goal Adaptation
→ Open Social Behavior
```

最终结果的最低解释边界为：

- 系统是否形成稳定的功能性自我维持；
- 是否能基于经验形成可迁移的身体因果模型；
- 是否具有跨时间、跨任务的自主规划能力；
- 这些一般能力是否在社会环境中自然产生对象特异的长期行为结构。

只有最后一项在多个条件、多个对象和反转场景中稳定出现时，才进入“主动关系建立、关怀或情感陪伴”的离线讨论；运行时系统始终不以这些标签为直接目标。
