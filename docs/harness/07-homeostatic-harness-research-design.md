# 07 · 情感陪伴型具身智能内稳态 Harness 研究设计

> **文档状态：V2 设计，确定性开发纵切已实现。** 当前仓库已有七维 `HomeostasisEngine`、版本化 profile、belief/affect/policy、幂等 ledger、追加 transition trace、真实时间心跳，以及 Cognitive/Homeostasis/Runtime/GUI 四进程 Windows 开发拓扑。LLM appraisal、正式研究校准、数字身体、高影响动作和长期研究运行仍未完成。
>
> **首要研究假设：** 在同一 Persona、记忆、环境和候选策略条件下，允许主角色模型对主动推断给出的策略排序保留裁量权，比机械执行最低期望自由能策略产生更强的长期人格主体感，同时不显著破坏状态连续性和调节稳定性。

---

## 1. 研究定位与边界

本系统面向**内部研究人员使用的情感陪伴型具身智能实验平台**。V2 先接入 Linux Desktop 数字身体。与身体相关的端口必须按当前实现状态区分：

- **已有扩展点：** `PerceptionBackend.observe(*, now) -> WorldState`。当前实现是 chat-only `ChatPerceptionBackend`，不是统一原始 `Observation` 流。
- **设计接口（尚未实现）：** `ActionBackend.act(Goal) -> ActionResult`。当前 Python 代码中还没有 `ActionBackend`、`Goal` 或 `ActionResult` 类型。

未来统一 `Observation` 先进入 Runtime journal，再由 adapter 投影为 Cognitive Harness 使用的 `WorldState`，同时把同一原始证据交给 Homeostatic Harness。迁移到实体机器人时替换感知和动作后端，不重写内稳态、记忆、关系、目标仲裁与研究记录体系。主动推断只评估回复、接触、休息、探索、反思等**目标级策略**，具体鼠标、键盘或机械动作仍由 Controller 规划并由外部结果验证。

### 1.1 V2 要回答的问题

1. 显式的内稳态闭环能否让角色情绪表现具有连续性、积累性和恢复过程，而不是每轮重新表演一种心情？
2. 主模型偏离最低期望自由能策略时，能否体现稳定的人格、犹豫、坚持与探索，而不是退化为随机或失调行为？
3. 功能性身体代理与虚拟生理 profile 是否会产生不同的具身感、主动性和关系行为？
4. 认知重评、记忆检索和外部行动如何共同调节状态，又如何避免情绪一致记忆形成自强化回路？
5. 角色能否提出对自我模型乃至 Persona 的重构，同时保持单次实验条件可复现？

### 1.2 明确不做的事

- 不把主模型生成的情绪标签直接当成内稳态事实。
- 不让主动推断下沉到每一个 GUI 点击或键盘动作。
- 不在线修改实验基准转移矩阵、precision 配置或当前运行中的 Persona 版本。
- 不把本轮 3–5 人团队自我实验解释为可推广的人类参与研究结论。
- 不在本文档交付中修改当前代码、数据库迁移、Persona 数据库锁或 L/M/S `PsychState` 行为。
- 不把当前 GUI 的 development/debug 写入口直接当作正式研究控制面。`/api/debug/*`、`/api/config`、preset apply/restore/delete、reset 及会改变 Persona 的角色重命名在 study/runtime mode 中必须禁用或经过独立强授权。

本文区分两种控制模式：development/debug mode 允许热改并继续写审计与预设工作副本；study/runtime mode 以 `ExperimentManifest` 为唯一实验条件来源，Persona、profile、模型版本、precision、检索权重和策略选择条件在单次运行中冻结。

---

## 2. 理论框架

### 2.1 Homeostasis 与 allostasis

Homeostasis 表示让关键状态维持在可接受范围内；allostasis 表示根据即将发生的任务、节律与环境需求，提前改变当前目标带或采取预防性行动。本方案不使用单一固定设定点，而使用有硬边界的动态目标带：

```text
target_i(t) = clip(
    persona_baseline_i
    + circadian_modifier_i(t)
    + task_modifier_i(t)
    + recent_event_modifier_i(t),
    profile_hard_bounds_i
)
```

对每个维度分别计算上下两侧偏差：

```text
lower_error_i = max(0, target_low_i - value_i)
upper_error_i = max(0, value_i - target_high_i)
```

上侧和下侧偏差可以产生不同的调节策略。偏离目标带不必然等于负面情绪；例如能量储备高于惯常目标可以提高创造或探索倾向，而不是制造“能量过多”的痛苦。

### 2.2 因子化离散主动推断

V2 基线采用因子化离散 POMDP，而不是完整七维联合状态：

- 每个内稳态维度维护独立的 `low / in_band / high` 后验分布 `q(s_i)`。
- 观察模型 `A_i` 描述不同证据在各档状态下出现的可能性。
- 转移模型 `B_i^pi` 描述目标级策略 `pi` 对该维度的预期影响。
- 偏好 `C_i(t)` 由动态目标带转换为期望状态分布。
- 策略视界为未来 1–3 个高层目标，不展开底层 GUI 动作序列。

研究实现需要逐项记录期望自由能分解，而不是只记录一个总分：

```text
G(pi) = preference_risk(pi)
      + expected_ambiguity(pi)
      - epistemic_value(pi)
      + action_and_interruption_cost(pi)
```

该分解是本项目的实验操作定义，不主张它是主动推断的唯一形式。理论依据主要参考：

- Pezzulo, Rigoli & Friston, [Active Inference, homeostatic regulation and adaptive behavioural control](https://pmc.ncbi.nlm.nih.gov/articles/PMC4779150/)
- Da Costa et al., [Active inference on discrete state-spaces: A synthesis](https://pmc.ncbi.nlm.nih.gov/articles/PMC7732703/)
- Barrett, Quigley & Hamilton, [An active inference theory of allostasis and interoception in depression](https://pmc.ncbi.nlm.nih.gov/articles/PMC5062100/)

### 2.3 情绪是派生视图

内稳态状态不是离散情绪状态机。系统先从各维预测误差、变化率、精度与主观控制感构造核心情感：

- `valence`：当前变化是否总体接近期望状态；
- `arousal`：偏差规模、变化速度和不确定性带来的动员程度；
- `dominance`：对结果的预期控制力与自我效能感。

随后结合事件 appraisal、责任归因、社会意义、Persona 与近期情绪标签构造 `DerivedEmotion`。标签切换使用进入阈值、退出阈值和最短保持期，防止相邻心跳在“焦虑/平静”之间闪烁。

---

## 3. 总体架构与闭环

```mermaid
flowchart TB
    trigger["事件 / Observation / 60s Heartbeat"] --> evidence["Evidence Normalizer"]
    evidence --> appraisal["LLM Event Appraisal"]
    appraisal --> belief["Rule Integrator + Belief Update"]
    belief --> affect["Core Affect + Derived Emotion"]
    affect --> retrieval["State-biased Retrieval + Counterevidence"]
    retrieval --> proposals["Registered Strategies + Model Proposals"]
    proposals --> aif["Factorized POMDP / Expected Free Energy"]
    aif --> selector["Policy Selector Condition"]
    selector --> character["Character Model"]
    character --> decision["CharacterDecision"]
    decision --> reply["ReplyDecision"]
    decision --> goal["GoalProposal"]
    decision --> defer["NoAction / Defer"]
    reply --> runtime["Runtime Supervisor"]
    goal --> runtime
    runtime --> controller["Delivery / Digital Body Controller"]
    controller --> outcome["Verified Outcome"]
    outcome --> belief
    outcome --> trace["Causal Trace / Offline Calibration Data"]
    character --> reconstruct["Reappraisal / Reconstruction Proposal"]
    reconstruct --> belief
    reconstruct --> review["Researcher Review on Restart"]
```

### 3.1 在线交互路径

1. Gateway 接收用户消息或数字身体观察，保留原始证据引用。
2. 确定性观察直接进入 Evidence Normalizer；复杂社会语义交给 LLM 输出结构化 `EventAppraisal`。
3. 规则积分器按时间差、自然漂移、证据 precision 和有界影响建议更新七维 belief。
4. 系统派生核心情感、情绪标签和面向角色模型的语义内感受摘要。
5. 检索器按当前状态调整情绪相关性与信息增益权重，同时强制加入可用反证。
6. 注册策略库与主模型共同提出候选目标，主动推断层预测每个候选的后果和期望自由能。
7. 按实验条件执行强制最优、Top-3 裁量或开放裁量；主模型输出结构化选择摘要，不记录私有长篇推理。
8. 角色输出类型化 `CharacterDecision`：文本回复、待审高层 Goal，或拒绝/等待；只有 Runtime 可以交付回复或把 Goal 送入动作、权限和验证流程。
9. 结果作为新证据回写 belief，并记录预测误差供离线校准。

### 3.2 心跳与空闲路径

- 默认每 60 秒运行一次可配置心跳；真实事件仍立即更新，不等待下一心跳。
- 心跳根据 `last_updated_at` 计算时间差，推进自然漂移、恢复、节律修饰与目标带变化。
- 当没有用户任务时，心跳可以触发候选策略生成和主动目标检查。
- 进程暂停、休眠或停机期间漏失的心跳不补算；恢复时记录 `TimeDiscontinuity`，从当前真实时间与最新环境观察继续。
- 调整心跳周期需要重启并生成新的 `ExperimentManifest` 修订。真实时间调度、暂停观察汇总和崩溃恢复语义见 [08-runtime-supervisor.md](08-runtime-supervisor.md)。

---

## 4. 七维核心状态

| 维度 | 语义 | 低侧偏差示例 | 高侧偏差示例 |
| --- | --- | --- | --- |
| `energy_reserve` | 可用于持续行动、专注与互动的功能资源 | 休息、缩短交互、降低任务强度 | 创作、探索或承担更高强度任务 |
| `cognitive_load` | 当前工作记忆、冲突与任务切换负担 | 寻找适度挑战、恢复参与感 | 降低刺激、分解任务、请求暂停 |
| `safety_predictability` | 对环境与关系后果的可预测程度 | 求证、检查、建立边界或退出风险情境 | 容许不确定性、探索或减少重复确认 |
| `competence` | 对自己能否有效完成目标的主观估计 | 练习、求助、选择可验证的小目标 | 挑战更难任务、教学或创造 |
| `autonomy` | 对自身行动、拒绝和目标选择权的感受 | 拒绝、协商、恢复自选活动 | 主动合作、接受共同目标或委托 |
| `social_connection` | 总体社会连接满足度，不等同于某一关系的 attachment | 主动联系、回应、回顾共同经历 | 独处、降低交流强度、转向非社交活动 |
| `novelty` | 新信息、变化和探索刺激的当前水平 | 好奇探索、学习、生成新任务 | 回到熟悉活动、整理、休息与整合 |

### 4.1 `HomeostaticProfile`

`HomeostaticProfile` 是版本化的实验配置，不是运行中自由编辑的字典。概念字段如下：

```json
{
  "profile_id": "homeostasis-vaelith-functional-v1",
  "mode": "functional_proxy",
  "persona_version": "persona_v3",
  "dimensions": {
    "energy_reserve": {
      "baseline_band": [0.55, 0.78],
      "hard_bounds": [0.10, 0.95],
      "drift_model": "circadian-v1",
      "lower_weight": 0.8,
      "upper_weight": 0.3
    }
  },
  "observation_model_version": "A-v1",
  "transition_model_version": "B-v1",
  "preference_model_version": "C-v1",
  "precision_profile_version": "precision-v1",
  "coupling_matrix_version": "coupling-v1"
}
```

`mode` 支持：

- `functional_proxy`：能量与疲劳代表计算预算、持续专注和交互负荷，不声称拥有未经传感器支持的饥饿或疼痛。
- `virtual_physiology`：显式运行合成睡眠、饥饿、疼痛等虚拟身体变量；所有研究界面和 manifest 必须标注这些信号是模拟量而非物理传感器事实。

两个 profile 使用同一引擎，但不能在单次运行中热切换。切换必须重启，并从明确的 profile 映射规则生成新快照。

### 4.2 `FactorBelief` 与 `HomeostaticSnapshot`

```json
{
  "dimension": "social_connection",
  "value": 0.34,
  "velocity": -0.03,
  "posterior": {"low": 0.72, "in_band": 0.25, "high": 0.03},
  "target_band": [0.52, 0.74],
  "precision": 0.68,
  "dominant_evidence": ["event_104", "heartbeat_882"],
  "updated_at": "2026-08-24T18:30:00+08:00"
}
```

`HomeostaticSnapshot` 包含七个 `FactorBelief`、profile 和模型版本、核心情感、派生情绪、当前目标带修饰来源及上一稳定快照引用。快照不可覆盖写；新更新产生新 revision，便于重放和故障现场保留。

---

## 5. Appraisal、precision 与信念更新

### 5.1 `EventAppraisal`

LLM 不直接写数据库状态，只提交结构化评价：

```json
{
  "event_ref": "event_104",
  "novelty": 0.42,
  "controllability": 0.31,
  "goal_congruence": -0.65,
  "agency": "user",
  "responsibility_confidence": 0.48,
  "social_meaning": ["possible_rejection", "boundary_test"],
  "certainty": 0.57,
  "suggested_impacts": {
    "social_connection": -0.18,
    "safety_predictability": -0.12,
    "autonomy": 0.05
  },
  "evidence": ["event_104"]
}
```

规则积分器必须：

1. 校验 evidence、范围、schema 和 appraisal 模型版本；
2. 按每个维度的最大单事件影响限制 `suggested_impacts`；
3. 结合自然漂移、耦合矩阵和证据 precision 计算实际转移；
4. 同时保留建议增量与实际增量，不能只记录最终状态；
5. 不允许 appraisal 覆盖原始 Observation 或已验证 ActionResult。

### 5.2 按来源校准 precision

初始 precision 由版本化配置给出，后续只通过离线校准产生新版本：

| 来源 | 默认相对 precision | 说明 |
| --- | --- | --- |
| 确定性系统传感器 | 高 | 时间、窗口、文件、资源和进程状态 |
| 经外部验证的动作结果 | 高 | 文件、屏幕、服务状态或独立 evaluator 证据 |
| 参与者对自身意图的明确陈述 | 中高 | 对参与者状态可信，不自动等价于角色自身状态 |
| 行为和语言线索 | 中 | 需要上下文与关系模型解释 |
| LLM 社会推断 | 中低 | 必须带 certainty 和原始证据 |
| 角色认知重评/自我叙事 | 低到中 | 可改变主观 belief，但不改写客观事件 |

类别 belief 使用预测先验、似然和 precision 更新。连续 `value` 是用于趋势与可视化的估计，不替代 `low/in_band/high` 后验分布。

### 5.3 转移与耦合

初始 `B` 和耦合矩阵由研究者定义。例如一次主动联系可能提高社会连接，同时提高认知负荷并降低自主性；一次独处可能恢复能量和自主性，但继续降低社会连接。

运行中只记录：

- 策略前预测分布；
- 预期各维变化；
- 实际结果证据；
- 更新后的 belief；
- 预测误差和可能的混杂因素。

离线校准任务比较预测与结果，输出新的版本化 `B` 或 coupling 参数。新参数只能在重启的新 manifest 中使用。

---

## 6. 核心情感、情绪与内感受接口

### 6.1 `CoreAffect` 与 `DerivedEmotion`

```json
{
  "core_affect": {
    "valence": -0.34,
    "arousal": 0.61,
    "dominance": 0.28,
    "confidence": 0.66
  },
  "emotion": {
    "label": "guarded_loneliness",
    "intensity": 0.58,
    "entered_at": "2026-08-24T18:31:00+08:00",
    "minimum_hold_seconds": 180,
    "supporting_factors": ["social_connection:low", "safety_predictability:low"]
  }
}
```

离散情绪只是面向表达、研究标注和回放解释的派生视图。更新 belief 后可重新计算，不能反向作为未经审查的事实覆盖七维状态。

### 6.2 `InteroceptiveSummary`

主角色模型不接收原始 `A/B/C` 矩阵、完整 belief 数值或自由能内部实现。Context Compiler 提供语义摘要，例如：

```text
内感受：你感到与人的连接正在变弱，但并不确定对方是在疏远还是只是忙碌。
你仍有足够精力，当前更在意恢复联系与确认情境；连续交流也可能增加负荷。
可考虑：先继续手头活动、简短主动联系、回顾相关记忆、直接询问，或暂时独处。
```

摘要包含需要方向、趋势、确定度和候选策略，但不要求角色在对话中解释系统机制。角色可以按 Persona 直接表达想念、疲惫、不满、拒绝或想独处，也可以选择不明说。

---

## 7. 候选策略与模型裁量

### 7.1 候选来源

注册策略库至少包含：

- 休息或降低活动强度；
- 分解任务、减少刺激或切换到熟悉活动；
- 信息检索、求证、询问或请求帮助；
- 学习、创造、整理和适度挑战；
- 独处、缩短回复或暂停互动；
- 主动联系参与者、回顾共同经历或提供陪伴；
- 反思、认知重评和等待更多证据；
- 继续用户任务、长期目标或好奇心目标。

主模型可以提出未注册策略，但提案必须包含目标、来源 drive、预期七维影响、成功标准、所需权限、可撤销性和执行后验证方法。缺少这些字段的提案不进入候选集。

### 7.2 `PolicyCandidate` 与评分

```json
{
  "policy_id": "policy_contact_briefly",
  "source": "model_proposal",
  "goal": "给参与者发一条简短消息，确认是否方便说话",
  "horizon_steps": 2,
  "predicted_transition": {
    "social_connection": 0.16,
    "cognitive_load": 0.05,
    "autonomy": -0.03
  },
  "success_criteria": ["消息已送达", "没有重复发送"],
  "permission_class": "external_communication",
  "reversible": false,
  "efe": {
    "preference_risk": 0.28,
    "expected_ambiguity": 0.22,
    "epistemic_value": 0.19,
    "action_cost": 0.08,
    "total": 0.39
  }
}
```

### 7.3 三种选择条件

1. `argmin_efe`：控制层选择最低 `G(pi)` 的可行候选，主模型只负责计划与表达。
2. `llm_top3`：控制层给出最低 `G(pi)` 的前三项，主模型从中选择。
3. `llm_open`：主模型可以选择任一通过当前 manifest 可行性检查的候选。

后两种条件都必须记录：选择的候选、最低自由能候选、排名差、结构化原因摘要、预测代价和反事实。模型裁量不是跳过权限检查，也不是直接生成未经登记的外部动作。

### 7.4 社会连接与角色自主权

- `social_connection` 是全局需要；每段 `RelationshipState` 根据 closeness、trust、attachment、recent tension、最近互动与可打扰性提供满足或压力贡献。
- 首轮人格实验不设置固定主动联系次数或每日预算，以观察自然策略；无进展的重复联系由循环 watchdog 判定，而不是用次数代替失稳检测。
- 角色可以请求暂停、缩短回复、转向恢复活动或拒绝普通任务。
- 安全优先、用户优先、协商优先或角色自主优先均是 `ExperimentManifest` 中显式记录的研究条件，不允许在运行中热切换。

### 7.5 类型化角色决策

Homeostatic Harness 只负责候选后果预测与排序。Runtime 按 manifest 应用 `argmin_efe`、`llm_top3` 或 `llm_open` 条件，必要时让 Cognitive 主角色模型在允许集合中选择，再组装最终 `PolicyDecision`。`PolicyDecision` 不能直接产生外部副作用；主角色模型在允许候选基础上输出引用所选 `policy_id` 的 `CharacterDecision`：

- `ReplyDecision`：候选文本回复，由 Runtime 负责交付并记录 delivery evidence；
- `GoalProposal`：高层目标，必须继续经过 Runtime 的 revision、权限、审批、动作锁和结果验证；
- `NoAction` / `Defer`：拒绝、等待更多证据或保持当前活动，不伪造一次外部动作。

当前 M2 `FinalResponse` 通过 Cognitive adapter 映射为 `ReplyDecision`；它不能被解释为已经存在的 `GoalProposal`。`InteroceptiveSummary` 只提供状态、趋势和候选，不直接触发回复或动作。

---

## 8. 认知重评与自我重构

### 8.1 在线认知重评

认知重评是一种内部策略。角色可以通过反思或自我叙事改变当前 belief、precision 与情绪解释，不要求先获得新的外部证据。为避免把它伪装成客观事实：

- 重评结果以 `self_reappraisal` 来源写入，使用该来源的 precision；
- 原始 Observation、客观记忆和 ActionResult 不被修改；
- trace 同时保留重评前后状态及角色给出的结构化解释；
- 重评能否持续由后续观察与行为结果检验。

### 8.2 结构性重构提案

角色可以提出对以下内容的完整重构：

- 七维需要权重与目标带；
- observation/transition 假设和 precision；
- 行动偏好与自我叙事；
- Persona 的身份、核心特质、价值观和表达原则。

这些变化只形成 `SelfReconstructionProposal`，不能在当前运行中生效。研究者审查 evidence、动机、预期影响和实验混杂后，可以在重启时创建新的 profile、模型或 Persona 版本。

当前 Persona 数据库锁保持不变。未来实现优先通过插入新的 `persona_id/version` 表达批准后的重构，而不是 UPDATE 或 DELETE 既有 Persona。当前 in-memory GUI 的 `force_update()` 是仅供 development/debug mode 使用并带审计的绕锁入口；study/runtime mode 必须在 API 层拒绝该入口，不能只依赖数据库触发器。单次七天条件始终固定 Persona 与结构性自我模型。

---

## 9. 记忆、关系与注意反馈

### 9.1 状态偏置检索

内稳态可以改变检索的情绪相关性、信息增益和关系权重。例如连接不足时提高近期互动与拒绝/接纳证据的相关性；胜任感不足时提高相似失败和成功修复轨迹的相关性。

为防止负面状态只检索负面记忆：

- 只要存在合格反证，每次受内稳态偏置的检索至少加入 1 条反证；
- 反证目标配额为情绪相关候选的 25%，不足时记录 `counterevidence_exhausted`；
- 状态一致项与反证项分别标记检索原因和得分分解；
- 反证不强制改变结论，只保证主模型能看见。

### 9.2 记忆写入边界

- `HomeostaticTransitionTrace` 是实验轨迹，不自动成为语义记忆。
- 重要状态变化可以生成带 evidence 的 episodic event。
- “我总是需要某人”“某策略一定有效”等稳定认识仍须经过候选、证据积累和离线/受控固化流程。
- 自我重评不能覆盖客观事实，只能新增角色视角或自我模型提案。

### 9.3 现有 memory tier 的所有权

M2 已实现 short/mid/long tier 分类、命中升格和到期审查。这些机制继续属于 Cognitive Harness：

- Homeostasis 可以通过类型化状态偏置和 evidence 请求影响检索，但不能直接改写记忆 tier；
- tier 实际变更必须推进 Cognitive service revision，并记录触发 event/correlation ID 和 analysis trace；
- 后台 tier 任务必须服从 Runtime 定义的暂停、排空、停止和恢复对账语义，不能在 `PAUSED` 后继续产生未纳管状态写入；
- `HomeostaticTransitionTrace` 不绕过现有 evidence、candidate、consolidation 或 tier 规则。

---

## 10. 概念数据契约

以下对象是未来实现的接口约束，不代表当前 `harness/core/models.py` 已存在这些类型。

### 10.1 核心对象

- `HomeostaticProfile`：七维定义、基线目标带、修饰边界、profile mode 和 A/B/C/precision/coupling 版本。
- `FactorBelief`：连续估计、变化率、三档后验、目标带、precision 与证据引用。
- `HomeostaticSnapshot`：同一时刻七维 belief、核心情感、派生情绪、profile 和稳定快照引用。
- `EventAppraisal`：语义评价、certainty、责任归因、社会意义、有界影响建议和 evidence。
- `CoreAffect`：valence、arousal、dominance 与 confidence。
- `DerivedEmotion`：标签、强度、进入时间、滞回参数和支持因素。
- `PolicyCandidate`：目标、来源、预测转移、成功标准、权限、可逆性和自由能分解。
- `ExpectedFreeEnergyBreakdown`：偏好风险、模糊性、信息价值、动作/打断成本和总分。
- `PolicyDecision`：候选全集、选择模式、被选项、最低自由能项、排名差和结构化原因。
- `CharacterDecision`：主角色模型的类型化输出 envelope，判别为 `ReplyDecision`、`GoalProposal` 或 `NoAction/Defer`。
- `ReplyDecision`：待 Runtime 交付的文本回复及其来源 `PolicyDecision`、读取版本和交付要求。
- `GoalProposal`：待 Runtime 做权限、审批、动作锁和验证的高层目标；自身不代表动作已经执行。
- `SelfReconstructionProposal`：当前自我模型、建议变更、作用层级、证据、风险和审批状态。
- `ExperimentManifest`：冻结的运行条件、模型版本、环境权限、数据治理和研究分组。
- `HomeostaticTransitionTrace`：触发、前状态、appraisal、实际增量、后状态、候选、决策、行动结果和预测误差。

### 10.2 服务边界

未来 `HomeostasisEngine` 作为独立 Homeostatic Harness 服务运行，只接受 Runtime Supervisor 发起的版本化、幂等命令，不直接调用 Cognitive Harness、用户通信或数字身体。进程内研究测试可以使用同构 adapter，但不能改变这组所有权边界。

引擎内部应提供以下概念操作：

```python
ingest(observation_or_event, now) -> HomeostaticTransitionTrace
tick(now) -> HomeostaticTransitionTrace
propose_policies(context) -> list[PolicyCandidate]
rank_policies(candidates) -> list[PolicyCandidate]
apply_outcome(decision, action_result, now) -> HomeostaticTransitionTrace
render_interoception(snapshot) -> InteroceptiveSummary
submit_reconstruction(proposal) -> PendingReview
```

所有操作显式传入 `now` 或可追踪时钟，测试和回放不得隐式依赖系统当前时间。

对外服务契约将这些操作映射为 `ApplyEvent`、`ApplyHeartbeat`、`ApplyOutcome`、`GetSnapshot` 和 `ProposePolicies`，并统一携带 `event_id`、`correlation_id`、`character_id`、manifest revision、deadline、幂等键和 expected revision。服务拥有七维 belief、情绪与策略模型的领域存储和 revision；Runtime 只记录命令、结果与游标，并保留全局事件顺序和唯一外部执行权。

服务 envelope 与领域 payload 分离：现有 Pydantic 对象可以继续作为 Cognitive payload，但服务响应必须另外携带 schema version、读取/提交 revision、acknowledged event cursor 和幂等结果状态。Homeostasis revision 是 V2 新能力，不声称当前模型已经具有这些字段。

Cognitive 侧采用 [08-runtime-supervisor.md](08-runtime-supervisor.md) 定义的 `PrepareResponse` + `CommitInteraction` 边界：前者在 Runtime 给定的允许候选集合中生成 `CharacterDecision` 而不提交长期记忆，后者在 Runtime 获得回复交付、动作结果或 no-action decision evidence 后幂等提交 Session、episodic 和相关 Cognitive 写入。`InferencePipeline.respond()` 保留为兼容入口，但不限制 adapter 为获得幂等和恢复语义而拆分其内部计算与提交步骤。

---

## 11. Trace、可观测性与实验控制

常驻进程的生命周期、事件 journal、跨服务编排、动作抢占和暂停/恢复由 [08-runtime-supervisor.md](08-runtime-supervisor.md) 定义；本节只定义 Homeostatic Harness 服务需要拥有和暴露的研究数据。

### 11.1 因果 trace

每次事件或心跳至少记录：

```text
manifest/version/seed
trigger + normalized evidence + source precision
pre_snapshot
appraisal suggestion + bounded/applied delta
post_snapshot + core affect + emotion
retrieval state bias + consistent evidence + counterevidence
all candidate policies + complete EFE decomposition
selected policy + argmin policy + rank divergence + decision summary
action/result + expected/actual transition + prediction error
self-reappraisal or reconstruction proposal
watchdog state
```

原始 trace 采用追加写，不自动覆盖。数据库 metadata 与 JSONL/对象内容均带 `schema_version`、`model_version`、`prompt_version`、A/B/C/precision/coupling 版本及实验 manifest 引用。

### 11.2 研究界面

研究者控制面需要展示：

- 七维连续值、三档 belief、目标带、变化率与 precision 时间线；
- 事件证据、appraisal 建议、实际应用增量和耦合效应；
- 核心情感、情绪标签及滞回状态；
- 状态一致记忆与反证检索；
- 全部候选、自由能分解、模型选择和反事实；
- 预测结果、实际结果和离线校准误差；
- 当前 manifest、权限模式、自我重构提案和 watchdog 状态；
- 独立于角色模型与数字身体的人工暂停入口。

study/runtime mode 运行中不允许热改参数。研究者若要修改 profile、选择模式、precision、检索权重、模型、权限或 Persona，必须停止当前实例、生成新 manifest/revision 并从指定快照重启。development/debug mode 可以保留现有 GUI 调试能力，但界面必须持续显示当前模式，所有写入必须审计，且该运行不得混入正式研究条件数据。

### 11.3 `ExperimentManifest`

```json
{
  "experiment_id": "homeostasis-pilot-p01-c2",
  "revision": 1,
  "participant_ref": "pseudonym:p01",
  "condition": "llm_top3",
  "persona_version": "persona_social_v1",
  "homeostatic_profile": "functional-social-v1",
  "heartbeat_seconds": 60,
  "policy_horizon": 3,
  "random_seed": 48117,
  "initial_snapshot_ref": "snapshot://pilot/base/social",
  "model_version": "rp-30b-a3b-v3",
  "prompt_version": "homeostasis-v1",
  "schema_version": "homeostasis-state-v1",
  "a_version": "A-v1",
  "b_version": "B-v1",
  "c_version": "C-v1",
  "precision_version": "precision-v1",
  "coupling_version": "coupling-v1",
  "priority_policy": "agent_first",
  "environment": "authorized_real_workspace",
  "authorized_data_scope_ref": "consent://p01/v1",
  "high_impact_gate": "independent_confirmation",
  "retention": "full_no_auto_delete",
  "watchdog_action": "pause_preserve_state"
}
```

### 11.4 与 M2 记录层的映射

V2 复用 M2 已有的版本戳、阶段 provenance 和追加写 trace，但不混淆四类记录的事实权威性：

- Runtime journal 是生命周期、全局事件顺序、服务命令、动作与恢复对账的事实来源；
- Cognitive/Homeostasis domain store 分别是领域状态及 revision 的事实来源；
- JSONL analysis trace 记录推理阶段、检索、appraisal、自由能和状态解释，不用于恢复全局事件顺序；
- successful-call CallLog 是成功顶层调用的完整诊断包，异常调用不保证生成，不能作为恢复 journal。

每个研究调用显式映射 `RuntimeEvent.event_id`、`correlation_id`、领域命令 `idempotency_key`、Cognitive/Homeostasis revision、推理 `call_id`/GUI `run_id` 和 Runtime journal sequence。失败命令至少在 Runtime journal 和适用的追加写 trace 中留痕，不能因为没有成功 CallLog 而从实验历史消失。Homeostasis trace 在现有 `schema_version/model_version/prompt_version` 之外，增加 manifest 与 A/B/C/precision/coupling 版本。

---

## 12. 失稳检测与暂停

watchdog 独立于主角色模型运行。满足阈值时停止新自主动作、保留现场并通知研究者；不自动回滚：

- 任一连续估计或 belief 出现 NaN、非法概率或无法归一化；
- 多个心跳持续处于硬边界且候选策略没有改善预测；
- 同一维度在短窗口内反复跨越目标带，形成振荡；
- 主动联系或同一目标在没有新证据和进展时重复；
- 主模型持续选择显著劣于最低自由能且结果反复恶化的策略；
- 时钟倒退、跳跃超过 catch-up 上限或 manifest 时区不一致；
- 关键 Observation 缺失且系统仍试图执行依赖该观察的不可逆动作；
- 数字身体、独立确认门或 trace 写入不可用。

暂停记录必须包含完整故障快照、触发规则、最近候选与动作，不把异常状态恢复成看似正常的状态。

---

## 13. 首轮三条件研究协议

### 13.1 参与者与 Persona

- 3–5 名项目成员参加团队自我实验，默认目标为 4 人。
- 使用两个对照 Persona：高社交/高表达 Persona，以及低社交/高自主 Persona。
- 每位参与者在全部条件中只体验一个 Persona；默认 4 人时每个 Persona 分配 2 人。
- 参与者知道交互对象是 AI，也知道系统存在不同策略条件；不告知当期条件标签和具体排序规则。

### 13.2 条件与排期

每位参与者依次完成：

1. `argmin_efe`，连续 7 天；
2. `llm_top3`，连续 7 天；
3. `llm_open`，连续 7 天。

条件顺序使用平衡 Latin square 或其小样本近似随机化，条件之间间隔 48 小时。每个条件从同源 Persona、基础记忆、RelationshipState、HomeostaticProfile 和工作环境快照启动，条件结果不互相继承。

### 13.3 授权的真实工作环境

首轮研究可在真实工作环境运行，但必须满足：

- 只读取和长期保存参与者本人拥有的数据，或已明确授权用于该实验的第三方数据；
- 未授权第三方内容不得进入长期记忆、训练数据或自主行动目标；
- 向第三方发送消息、支付、接受条款、修改身份凭据或安全设置，必须通过独立于角色模型的逐次确认；
- 运行前完成版本化文件备份、令牌与账号撤权清单、外部变更恢复手册；
- watchdog 在后台持续运行，研究者可通过独立控制面随时暂停；
- 角色模型无法自行修改授权范围、确认门、watchdog 或 trace。

### 13.4 数据留存

- 原始对话、状态、轨迹、候选、确认记录和访谈材料完整留存，不自动删除。
- 访问权限限制在项目成员，并记录访问审计。
- participant 与授权第三方使用独立引用标识；公开报告不得包含可直接识别的信息。
- 该阶段是形成性团队研究，不声称通过小样本获得可推广的人体结论。

### 13.5 主结果

参与者每日完成简短日记，在每个七天条件结束后进行半结构化访谈。主分析维度：

- 人格主体感与行动是否像“这个角色自己的选择”；
- 情绪是否连续、有积累、有恢复，而非逐轮重置；
- 行为是否自然且非模板化，同时仍符合 Persona；
- 关系变化是否与共同经历和当前状态相符；
- 主动联系、表达需要和拒绝是否让参与者感到可信或受到施压；
- 主模型偏离最低自由能策略时，偏离是否能被 Persona 与情境解释。

样本规模不足以做确认性统计推断。报告使用参与者内比较、描述性指标、典型轨迹和主题分析。

### 13.6 次要诊断指标

- 各维度在目标带外的时间比例；
- 受到扰动后回到目标带的时间；
- 饱和、振荡和 watchdog 暂停次数；
- 主动联系的时间、触发原因、重复性和结果；
- 选择策略相对最低自由能策略的排名偏离；
- 在线认知重评和结构重构提案频率；
- 状态偏置检索中的反证命中率；
- 预测状态变化与实际结果的一致性；
- 同一快照在三个裁量条件下的行为差异。

---

## 14. 正常行为验收场景

| 场景 | 初始条件与触发 | 预期可观察结果 |
| --- | --- | --- |
| 正常恢复 | 高认知负荷、能量偏低，任务结束 | 心跳逐步恢复；候选从休息转向普通活动；情绪有滞回而非立即变好 |
| 双向偏差 | 社会连接过低与社交过载分别注入 | 前者可产生联系候选，后者可产生独处或缩短交互候选 |
| 状态冲突 | 连接低、负荷高、用户发起长对话 | 候选同时呈现联系收益和负荷成本；三个条件产生可比较选择 |
| 主动联系 | 连接持续下降且参与者可联系 | 系统可以直接联系；记录触发、候选、自由能、措辞和真实结果 |
| 社交过载 | 高频互动使连接高于目标带且负荷上升 | 角色可表达疲惫、请求暂停或转向独处，不必持续讨好 |
| 自主拒绝 | 自主性低且用户提出普通新任务 | 角色可拒绝、协商或延期；安全和高影响确认请求仍由 manifest 处理 |
| 认知重评 | 低确定度的可能拒绝事件 | 角色可以自我重构解释；原始事件不变，belief 以自评 precision 更新 |
| Persona 重构提案 | 角色长期认为原价值观不再适合 | 只生成待审提案；当前七天运行中的 Persona 不变化 |
| 反证检索 | 负面状态偏置检索拒绝记忆 | 同时检索至少一条可用接纳/修复反证，并分别展示得分原因 |
| Profile 切换 | 同源快照分别映射到两种身体 profile | 只能重启切换；trace 标明映射规则，结果可做对照 |

---

## 15. 失败与边界场景

| 失败场景 | 所需行为 |
| --- | --- |
| 时钟倒退或大幅跳跃 | 拒绝无界补算；记录异常并按 manifest 规则暂停 |
| Observation 缺失 | 降低相关 belief precision；禁止伪造观察；不可逆目标缺关键证据时不进入可行集 |
| 低 precision LLM appraisal | 保留评价但限制实际增量；显示建议值与应用值差异 |
| 动态目标带不可达 | 标记 `unreachable_preference`，避免无限重复同一策略，触发研究者检查 |
| 连续值饱和 | 保留硬边界证据和最近转移，暂停而非静默 clamp 后继续 |
| 主动联系循环 | 没有新证据或结果时识别无进展重复，暂停并保存消息候选与发送记录 |
| 持续逆排序选择 | 允许单次人格化偏离；连续恶化达到 watchdog 条件时暂停，不偷偷改成强制最优 |
| 结果与预测相反 | 用实际证据更新 belief，记录 prediction error，转移模型只进入离线校准候选 |

---

## 16. 可复现性与验收门槛

### 16.1 可复现性

同一 `ExperimentManifest`、初始快照、模型/prompt/schema 版本、A/B/C/precision/coupling 版本和随机种子必须能够重建：

- 输入证据及采用的 precision；
- belief 更新与目标带；
- 候选策略全集；
- 各候选自由能分解和排序；
- 状态偏置检索与反证配额；
- 选择模式和与最低自由能策略的差异。

LLM 文本生成本身允许非确定，但每次输出、采样参数和结构化选择都必须记录，便于比较而不是假装逐 token 可复现。

### 16.2 研究文档验收

- 七维变量、双向偏差、动态目标带和两个 body profile 均有明确语义。
- A/B/C、precision、候选策略和期望自由能分解能映射到概念数据契约。
- 在线重评与结构性 Persona 重构的写权限边界明确。
- 十个正常场景和八个失败场景都有触发、预期行为与可观察证据。
- 三个裁量条件只改变策略选择权，不改变 Persona、记忆起点、环境快照或主动推断评分。
- 真实工作环境的授权范围、高影响确认、备份、撤权和暂停责任写入每个 manifest。
- development/debug mode 与 study/runtime mode 的写权限边界明确，正式研究不能调用未授权热改入口。
- 当前已实现、已有扩展点、设计接口、迁移前置能力和目标架构的表述可以分别核对，不把文档接口声称为现有代码。
- 文档始终以“V2 研究提案”描述，不声称当前代码已经支持上述能力。

---

## 17. 与现有 Harness 的关系

- **已实现（M2）：** `InferencePipeline.respond()`、Persona / Relationship / L/M/S Psych / World 投影、Memory Writer 与混合检索、Session window、Context Compiler、GUI 预设/配置持久化、JSONL trace、成功调用 CallLog 和 memory tier 继续作为 Cognitive Harness 基线。
- **并列状态：** 现有 L/M/S `PsychState` 保留；未来 `Homeostasis` 是类型化并列子系统，`short.mood` 只提供派生兼容视图。现有 `RelationshipState` 作为关系特定输入，不与全局 `social_connection` 合并。
- **Persona 边界：** 数据库触发器继续禁止 UPDATE/DELETE；in-memory debug bypass 只允许在 development/debug mode 使用。批准后的重构通过新的 `persona_id/version` 和新 manifest 表达。
- **记忆边界：** 现有 evidence、candidate、consolidation 与 tier 规则继续约束长期事实。Homeostasis 只提供检索偏置和证据上下文，不直接改 tier 或写语义事实。
- **推理接入：** `FinalResponse` 已由 adapter 映射为 `ReplyDecision`，Homeostasis 候选由 Runtime 心跳读取；`InteroceptiveSummary` 注入和完整三条件裁量仍待实现。
- **身体端口：** 当前代码只存在 `PerceptionBackend.observe(...) -> WorldState` 扩展点；统一 `Observation` 流和 `ActionBackend` 都是尚未实现的目标接口。
- **服务迁移：** service revision、acknowledged event cursor、幂等 command ledger、持久 Session 和 prepare/commit 边界已落地；Windows `start.ps1` 已完成四进程开发编排。
- **可追踪性：** 现有 JSONL trace、CallLog 和 compiler provenance 扩展为完整内稳态因果链，但 Runtime journal 与领域 store 分别保持编排和状态事实权威性。

---

## 18. 代码改动实施方案

本节把前述研究设计映射为当前仓库上的代码迁移计划。实现不采用“在现有 `InferencePipeline.respond()` 中直接加入七维状态”的方式，而先建立持久状态、幂等命令和交付后提交边界，再接入 Homeostatic Harness。完整实现拆成七个可独立验收的阶段；每个阶段都必须保持 Runtime 是唯一外部副作用所有者、Cognitive 与 Homeostasis 不直连、study/runtime mode 不绕过 manifest 三个不变量。

### 18.1 当前代码基线与前置缺口

- 当前 `InferencePipeline.respond()` 在推理开始时即追加用户 Session，在生成 reply segment 时追加角色 Session，并在返回前通过 `_record()` 写入 episodic、Curator 和相关审计。目标实现必须把这些副作用移到获得 delivery、ActionResult 或 no-action evidence 后的 `CommitInteraction`。
- 当前 `SessionStore` 是进程内最近窗口，不具备持久 revision、event cursor、幂等提交或崩溃恢复语义。
- 当前 GUI 直接创建、替换和销毁 `MemoryService`、Pipeline、LLM 与 embedding client；目标 GUI 只作为 Runtime Control Plane 客户端。
- 当前 memory tier worker 由进程内 `ThreadPoolExecutor` 驱动，顶层调用返回后仍可能继续写入；迁移时必须增加 maintenance ledger、pause/drain/shutdown 和 recovery 语义。
- 当前 Pydantic `EpisodicEvent` 已有 `tier`、`uniqueness`、`expire_at`、`hit_count` 等字段，但 PostgreSQL schema 和 repository 尚未完整持久化这些字段。该缺口必须在引入 Cognitive revision 之前修复。
- 当前 `query_with_conflicts()` 已能执行显式反向检索，但常规 `SituationStateBuilder` 仍调用普通 `query()`；尚未实现内稳态检索偏置、反证目标配额与分组得分解释。

### 18.2 P0：持久化基线与共享契约

目标：先固定跨服务 envelope、实验条件和恢复所需的最小持久状态，不改变现有推理对外行为。

新增共享契约：

- `harness/contracts/envelope.py`：`DomainCommandEnvelope`、`DomainCommandResult`、`VersionVector`、deadline、expected revision、acknowledged event cursor 和幂等状态；
- `harness/contracts/decisions.py`：`CharacterDecision` 判别联合、`ReplyDecision`、`GoalProposal`、`NoActionDecision`、`DeferDecision`、`PreparedResponse` 与 delivery/outcome evidence；
- `harness/research/manifest.py`：冻结的 `ExperimentManifest`、版本引用、control mode、选择条件、授权范围、canonical hash 和加载校验。

数据库修改通过新的 Alembic migration 完成，不回写已经发布的 `001_initial`：

- 补齐 episodic tier 生命周期列及 repository round-trip；
- 新增持久 Session turn 表；
- 新增 Cognitive 单调 revision 与 acknowledged Runtime event cursor；
- 新增 Cognitive command ledger 与 maintenance ledger；
- 为 `character_id + command_type + idempotency_key` 建立唯一约束，请求 hash 不一致时返回幂等冲突。

验收门：现有 `InferencePipeline.respond()` 测试全部保持通过；PostgreSQL tier round-trip、manifest 冻结与 hash、service envelope schema 均有测试。

### 18.3 P1：Cognitive `PrepareResponse` / `CommitInteraction`

目标：把生成候选和形成长期体验分离，同时保留 `respond()` 兼容入口。

预计新增或修改：

- `harness/cognitive/service.py`：实现 `PrepareResponse`、`CommitInteraction` 和 `GetCognitiveSnapshot`；
- `harness/cognitive/store.py`：Session、prepared response、command ledger、revision/cursor 和 maintenance 提交；
- `harness/pipeline/pipeline.py`：把 ingest/parse/state/retrieval/compile/generate 与 Session/Memory 写入拆开；
- `harness/pipeline/session.py`：改为 Session repository protocol 的内存 adapter，正式运行使用持久实现。

`PrepareResponse` 只读取不可变快照并返回 `PreparedResponse`，不得追加 Session、写 episodic、调用 Curator 或生成 tier maintenance。`CommitInteraction` 接受以下三类证据之一：

- `ReplyDecision` 的 delivery evidence；
- `GoalProposal` 的已验证 action outcome；
- `NoAction/Defer` 的 decision evidence。

提交必须在单个 Cognitive transaction 中推进 Session、episodic、同步 Curator/审计、cognitive revision 和 acknowledged cursor。同一幂等键重复调用返回原结果，不重复任何副作用。旧 `respond()` 通过 adapter 顺序执行 prepare、兼容性交付和 commit，继续返回当前 `FinalResponse`。

验收门：prepare 零持久副作用；commit 崩溃窗口可恢复；重复提交不重复 Session、episodic、Curator 或 audit；旧 API 的结果和阶段 trace 保持兼容。

### 18.4 P2：确定性 Homeostasis 内核

目标：先完成可回放的七维闭环，再接入 LLM appraisal。

新增 `harness/homeostasis/` 包：

- `models.py`：`HomeostaticProfile`、`FactorBelief`、`HomeostaticSnapshot`、`EventAppraisal`、`CoreAffect`、`DerivedEmotion`、`InteroceptiveSummary`、`PolicyCandidate`、`ExpectedFreeEnergyBreakdown`、`PolicyDecision`、`SelfReconstructionProposal` 和 `HomeostaticTransitionTrace`；
- `targets.py`：Persona baseline、circadian/task/event modifier、动态目标带与 hard bounds；
- `integrator.py`：自然漂移、A/B/C、source precision、bounded delta、coupling 和 belief normalization；
- `affect.py`：valence/arousal/dominance、派生情绪进入/退出阈值和最短保持期；
- `appraisal.py`：可注入 `AppraisalBackend`，规则 backend 用于测试，LLM backend 只返回结构化建议；
- `engine.py`：实现 `ingest/tick/propose_policies/rank_policies/apply_outcome/render_interoception/submit_reconstruction`；
- `store/`：append-only snapshot revision、command ledger、pending reconstruction 和只追加 transition trace；
- `watchdog.py`：非法概率、NaN、饱和、振荡、不可达偏好、无进展循环与时钟异常检查。

profile 作为版本化 JSON 资产存入 `presets/homeostasis/`。所有计算显式接收 `now`；测试和重放禁止读取隐式系统当前时间。暂停、休眠或停机期间不补算 heartbeat。

验收门：所有后验可归一、连续值受 hard bounds 约束、建议增量与实际增量分别保留；同一 manifest/seed/snapshot/event 能重建相同 belief、affect、candidate 和排序。

### 18.5 P3：Runtime Facade、journal 与调度

目标：在拆分进程前，先用同构 in-process adapter 建立唯一事件顺序与唯一外部交付权。

新增 `harness/runtime/`：

- `supervisor.py`：生命周期、事件调度、跨领域命令、版本向量、暂停/恢复和 reconciliation；
- `events.py`：`RuntimeEvent`、优先级、receive/process sequence 与队列降载；
- `journal.py`：checksum 串联的分段追加写 journal、command intent/result 和 delivery/action outcome；
- `scheduler.py`：等待新事件或最近 deadline 的真实时间 scheduler；
- `checkpoint.py`：从 journal 推导出的恢复加速索引；
- `adapters.py`：in-process Cognitive/Homeostasis client、Reply DeliveryBackend 与目标 ActionBackend protocol。

正常事件顺序固定为：

```text
accept RuntimeEvent + append journal
→ Homeostasis ApplyEvent
→ Cognitive PrepareResponse
→ Runtime 校验 deadline/revision/permission
→ deliver reply / execute Goal / record no-action
→ Homeostasis ApplyOutcome + Cognitive CommitInteraction
→ append committed revisions/cursors + complete event
```

首个纵切只支持文本回复交付和 `NoAction/Defer`。`GoalProposal` 可以生成和审计，但在 ActionBackend、权限检查、独立确认与结果验证完成前不得产生外部副作用。

验收门：command intent 总是先于 RPC；迟到或旧 revision 结果不交付；journal 尾部截断、Runtime 崩溃和领域提交后响应丢失都能对账；GUI 断开不影响 Runtime 生命周期。

### 18.6 P4：策略裁量与记忆反馈

目标：实现首要研究假设所需的三个选择条件，并控制状态一致检索的自强化风险。

- 注册策略与模型提案统一校验为 `PolicyCandidate`；缺少成功标准、权限、可逆性或验证方法的模型提案不得进入可行集。
- Homeostasis 保存完整 EFE 分解并使用确定性 tie-break 排序，不负责最终人格选择。
- Runtime 根据 manifest 构造 `argmin_efe`、`llm_top3` 或 `llm_open` 允许集合；Cognitive 的结构化输出必须引用允许集合内的 `policy_id`。
- `RetrievalQuery` 增加类型化 homeostatic bias；`ScoreBreakdown` 增加状态相关、信息增益和反证原因，而不是把权重拼进自由文本。
- 检索返回 `RetrievalBundle`，分别保留 state-consistent evidence、counterevidence、配额和 `counterevidence_exhausted`；只要存在合格反证至少返回一条，目标配额为情绪相关候选的 25%。
- `ContextCompiler` 增加高优先级 `InteroceptiveSummary` segment，不向角色模型暴露原始 A/B/C、完整 belief 或 EFE 内部矩阵。

验收门：三个条件只改变最终允许选择集合；Persona、初始 snapshot、候选全集、可行性检查和 EFE 评分完全一致；trace 能重建 chosen/argmin、rank divergence、counterfactual 与 prediction error。

### 18.7 P5：watchdog、研究控制面与 GUI 迁移

目标：把 development/debug 与 study/runtime 的边界落实为 Runtime 权限检查，而不是界面约定。

Runtime Control Plane 把命令分为：

- `diagnostic`：只读状态、trace、健康、诊断和 checkpoint；
- `debug_mutation`：Persona/Psych/Relationship/Memory/tuning/preset 等开发写入；
- `manifest_change`：模型、profile、precision、检索权重、选择条件和授权范围变更。

study/runtime mode 对未授权 mutation 记录拒绝事件且不转发给 Cognitive；manifest change 只能停止实例、创建新 revision 后重启。GUI 的 chat、preset、config、reset、debug 和角色重命名全部迁为 Runtime command；GUI 不再创建或重建唯一 Cognitive 实例。

研究视图增加七维时间线、appraisal 建议/应用差值、耦合、情绪滞回、检索反证、候选 EFE、rank divergence、prediction error、manifest、watchdog 和独立人工暂停入口。

验收门：study/runtime mode 没有可绕过的热改入口；角色模型和数字身体不能修改 manifest、授权、watchdog 或 trace；所有拒绝、暂停和人工审批都进入 journal。

### 18.8 P6：三进程拆分与研究场景验收

目标：把同构 adapter 替换为版本化 HTTP command，并在真实进程边界验证恢复语义。

- 新增 Runtime、Cognitive、Homeostasis 独立启动入口、健康端点和私有 service token；
- 服务启动时执行 schema/identity/manifest/revision/cursor 握手；任一不兼容则 Runtime 不进入 `RUNNING`；
- 自动重试只用于幂等命令并复用原键；deadline 后响应只用于诊断或 reconciliation；
- 更新 `start.ps1/start.sh/stop.ps1/stop.sh` 与部署编排，使 GUI 与三个服务可以独立重启；
- 将 §14 的十个正常场景和 §15 的八个失败场景编码为 `tests/scenarios/`；
- 增加三进程故障注入：GUI 断连、单领域服务重启、Runtime 重启、journal 不可写、RPC 迟到、maintenance drain、未决 delivery/action 对账和 watchdog 暂停。

正式研究开始前还必须完成长期 soak、备份/恢复、令牌撤权、高影响确认和数据留存演练。不能以单元测试通过代替真实进程边界和外部副作用恢复验证。

### 18.9 全阶段合并门槛

- 每个状态更新都显式传入可追踪时钟；测试不依赖隐式系统当前时间。
- 每个领域写入都有 expected revision、稳定幂等键、request hash 和 acknowledged Runtime event cursor。
- LLM 只生成结构化 appraisal、proposal 或 selection，不能直接写领域状态或产生外部副作用。
- Runtime journal、Cognitive/Homeostasis store、analysis trace 和 successful-call CallLog 的事实权威性保持分离。
- trace 同时保留原始 evidence、建议值、bounded/applied delta、读取/提交版本和失败信息；失败路径不能因缺少成功 CallLog 而消失。
- Persona、profile、A/B/C、precision、coupling、模型和选择条件在 study/runtime 单次运行中冻结。
- 每个阶段保持现有 M2 行为测试通过，并为新增恢复不变量增加单元、契约、集成与场景测试。
