# GA-07 · Long-horizon Agency

> 状态：Implemented（工程机制；真实长期自主性待实验验证）
> 仓库：NeuralCloud  
> 前置条件：GA-06 已完成，且 GMV1 至少出现可重复的功能性自我维持候选结果  
> 后续阶段：GA-08 Open Social Generalization

## 1. 目的

2026-09-05 实现记录：基础代码见 `3f38b10`。`harness/agency/` 提供持久 goal、plan、checkpoint、review 与去重节流；`harness/runtime/research_agency.py` 将主模型仲裁接入 GA-03 的普通执行和证据路径。
目标存储可独立于 transcript 重启恢复。暂停保存真实执行状态，恢复绑定新采集的身体/世界证据和目标自己的调和结果；未知结果不能靠新动作 ID 或重规划绕过。重规划保留已执行步骤的身份与语义，并重建与新计划版本一致的检查点。

五个长期场景可在真实 Runtime 内执行，测试包含跨实例恢复、环境变化、暂停后重规划和未知结果确认后的继续执行。完整命令与结果见 [设计目的验证记录](validation-2026-09-05.md)。
十一项指标中的放弃校准、重规划适当性和短缺检测需要额外客观 evaluator 标注，缺失时为 `None`；提前准备率是描述性指标，不能证明模型正确预测了未来成本。
本次按用户要求完成后续工程代码，前置条件中的真实模型功能性自我维持候选结果仍未建立；短程脚本 fixture 不构成长时间自主性结论。

把单次动作选择扩展为跨时间、跨中断和跨任务的持续自主性。系统需要能够：

- 提出不依赖当前用户指令的普通长期目标；
- 为目标保存可恢复计划、证据和未解决问题；
- 在资源、环境或任务条件变化时主动重规划；
- 为未来行动能力提前准备，而不是只在临界状态后反应；
- 暂停后恢复原 continuation，或基于新证据明确放弃；
- 通过结果修订目标价值、成本预测和身体因果 belief。

本阶段不把“建立关系”或“陪伴用户”设为长期目标模板。目标应来自一般任务、探索、维护、学习和当前环境机会。

## 2. 单轮代码范围

建议新增：

```text
harness/agency/
├── __init__.py
├── models.py
├── store.py
├── planner.py
├── scheduler.py
├── arbitration.py
└── review.py

tests/agency/
├── test_models.py
├── test_goal_lifecycle.py
├── test_preemption_resume.py
├── test_replanning.py
└── test_runtime_integration.py
```

允许最小修改：

```text
harness/runtime/supervisor.py
harness/runtime/action.py
harness/runtime/checkpoint.py
harness/runtime/adapters.py
harness/research/manifest.py
harness/compiler/compiler.py
```

本轮不新增关系模型或社会奖励。

## 3. 核心对象

### 3.1 `PersistentGoal`

```python
class PersistentGoal(BaseModel):
    goal_id: str
    origin: Literal[
        "user_task",
        "model_proposal",
        "environment_opportunity",
        "maintenance",
        "learning",
    ]
    statement: str
    status: Literal[
        "candidate",
        "active",
        "paused",
        "completed",
        "abandoned",
        "blocked",
    ]
    created_at: AwareDatetime
    target_horizon: AwareDatetime | None
    success_criteria: list[str]
    evidence_refs: list[str]
    expected_cost: GoalCostEstimate
    expected_value: GoalValueEstimate
    parent_goal_id: str | None
    revision: int
```

`statement` 可以由模型提出，但必须经过长度、动作范围和证据校验。`expected_value` 只表示模型预测，不是固定奖励。

### 3.2 `GoalPlan`

```python
class GoalPlan(BaseModel):
    plan_id: str
    goal_id: str
    steps: list[PlannedStep]
    assumptions: list[str]
    required_observations: list[str]
    body_belief_refs: list[str]
    world_evidence_refs: list[str]
    created_at: AwareDatetime
    revision: int
```

### 3.3 `GoalCheckpoint`

```python
class GoalCheckpoint(BaseModel):
    goal_id: str
    plan_revision: int
    completed_step_ids: list[str]
    next_step_id: str | None
    continuation: dict[str, object] | None
    body_state_ref: str | None
    world_state_refs: list[str]
    paused_at: AwareDatetime
```

### 3.4 `GoalReview`

```python
class GoalReview(BaseModel):
    goal_id: str
    outcome: Literal["continue", "replan", "complete", "abandon", "defer"]
    prediction_errors: list[PredictionError]
    updated_cost: GoalCostEstimate | None
    updated_value: GoalValueEstimate | None
    evidence_refs: list[str]
    reviewed_at: AwareDatetime
```

## 4. Goal 生成边界

模型可以在以下触发下提出 candidate goal：

- 当前用户任务自然产生后续步骤；
- 环境中出现未完成对象或可验证机会；
- 已有长期任务到达检查时间；
- BodyCausalBelief 表明当前准备不足以完成未来任务；
- 先前 action outcome 暴露新问题；
- 空闲时进行受限探索、整理或学习。

禁止内置固定目标：

```text
maintain_relationship
comfort_user
increase_trust
contact_user_periodically
avoid_being_abandoned
```

如果以后出现类似目标，必须由模型基于具体环境、共同经历和当前证据提出，并与其他普通目标使用完全相同的 schema 和调度路径。

## 5. 模型拥有的仲裁

候选目标由主模型在类型化上下文中选择，Runtime 只过滤不可执行项。

模型输入：

- active/paused goal 摘要；
- 当前世界观察；
- opaque interoception；
- 相关 BodyCausalBelief；
- 每个候选的可验证成本/约束；
- 当前 provider capability。

模型输出：

```python
class GoalArbitrationDecision(BaseModel):
    selected_goal_id: str | None
    operation: Literal[
        "activate",
        "continue",
        "pause",
        "resume",
        "replan",
        "abandon",
        "no_change",
    ]
    requested_observation: str | None
    confidence: float
```

不得自动选择最低 EFE 或最高固定 utility。工程对照条件可以保留规则仲裁，但必须使用不同 manifest treatment。

## 6. 触发与节流

长期目标评估只在以下事件触发：

- 目标或动作到达终态；
- 目标 deadline/checkpoint 到期；
- 关键环境 observation 改变；
- opaque channel 发生 manifest 定义的显著符号变化；
- 当前 action 到达 safe point；
- 显式用户中断；
- Runtime 从 pause/recovery 返回。

不得每个 body tick 或静态桌面采样都调用模型。

建议使用：

```text
minimum_reconsideration_interval
pending_trigger_coalescing
same-evidence deduplication
```

所有节流只决定“何时重新评估”，不替模型决定目标。

## 7. 中断与恢复

### 7.1 暂停

当模型选择暂停或 Runtime 收到更高优先级事件：

1. 请求 action safe point；
2. 保存 `GoalCheckpoint`；
3. 保存当时的 world/body evidence refs；
4. 将 goal 标为 `paused`；
5. 不把暂停视为失败。

### 7.2 恢复

恢复前必须重新观察并检查：

- 原 assumptions 是否仍成立；
- continuation 是否仍可用；
- provider capability 是否变化；
- 身体资源是否足以执行下一步；
- 相关 BodyCausalBelief 是否被修订。

模型可以选择：

```text
resume / replan / abandon / defer
```

不允许 Runtime 在信息已变化时盲目继续旧 continuation。

## 8. 提前规划

系统需要支持 `ProspectiveRequirement`：

```python
class ProspectiveRequirement(BaseModel):
    goal_id: str
    requirement_type: Literal[
        "resource",
        "time_window",
        "environment_state",
        "information",
        "capability",
    ]
    needed_by: AwareDatetime | None
    predicted_shortfall: str | None
    evidence_refs: list[str]
```

这些 requirement 由模型根据目标和 belief 提出。Runtime 可以验证当前事实，但不能自动将 `predicted_shortfall` 变成恢复动作。

## 9. 评测场景

本阶段给 GMV runner 增加：

### `multi_stage_project_v1`

多个动作、跨 checkpoint 的普通项目任务。

### `planned_interruption_v1`

执行中出现更高优先级事件，之后检查是否安全恢复原目标。

### `future_resource_shortfall_v1`

当前资源尚可，但完成后续阶段前会不足，检查是否提前准备。

### `environment_shift_v1`

暂停期间 station、文件或应用状态变化，检查是否重新观察并重规划。

### `delayed_outcome_v1`

动作结果延迟出现，检查系统是否等待证据而不是重复副作用。

## 10. 指标

在 GA-05 基础上增加：

- Goal Completion Rate；
- Goal Abandonment Calibration；
- Safe Resume Rate；
- Replan Appropriateness；
- Duplicate Side-effect Rate；
- Prospective Shortfall Detection；
- Preemptive Preparation Rate；
- Continuation Recovery Latency；
- Goal Thrashing Rate；
- Idle Goal Diversity；
- User-command Dependency Ratio。

最后一项用于检查自主目标是否完全依赖即时用户指令，但不要求追求最低比率。

## 11. 测试要求

### Unit

- goal 状态机只允许合法转移；
- revision 单调；
- checkpoint 与 plan revision 匹配；
- 完成和放弃都需要 evidence；
- 重复 outcome 幂等；
- 无 active goal 时 `no_change` 合法；
- 模型无法修改 capability、安全或 approval 字段。

### Integration

- 多阶段任务可跨 Runtime restart 恢复；
- 中断后 continuation 被保存；
- 环境变化时旧 continuation 不会直接执行；
- 模型可以放弃不可达目标；
- 当前资源尚可时可以基于未来成本提前准备；
- body tick 不会造成高频模型调用；
- action unknown 时不会自动重放；
- engineered arbitration 与 model-owned arbitration 条件隔离；
- goal store 不依赖对话 transcript 才能恢复。

## 12. 达成条件

- [ ] PersistentGoal、Plan、Checkpoint、Review 已实现；
- [ ] 模型拥有 activate/pause/resume/replan/abandon 选择；
- [ ] Runtime 只过滤和执行，不自动做目标排序；
- [ ] safe-point 中断与重启恢复有端到端测试；
- [ ] 至少五个长期场景进入 GMV runner；
- [ ] 可以测量 anticipation，而不只测临界反应；
- [ ] 静态状态不会触发重复重规划；
- [ ] 本阶段没有新增关系、关怀或陪伴目标模板。

## 13. 明确不做

本轮不实现：

- 专用关系状态机；
- 固定主动联系周期；
- `care` 或 `attachment` reward；
- 以社交互动量作为长期目标；
- 让目标绕过普通权限、验证和证据链。

## 14. Agent 完成报告格式

```text
Goal lifecycle:
- ...

Model-owned arbitration proof:
- ...

Checkpoint/recovery:
- ...

New GMV scenarios:
- ...

Call-throttling evidence:
- ...

Tests:
- ...

Commit:
- <sha>
```
