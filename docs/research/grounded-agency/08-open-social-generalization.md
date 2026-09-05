# GA-08 · Open Social Generalization

> 状态：Planned  
> 仓库：NeuralCloud  
> 前置条件：GA-07 已完成；自我维持、身体因果学习和长期目标能力已分别通过非社会场景验证  
> 阶段性质：最终开放泛化阶段

## 1. 目的

检验此前形成的一般能力在包含持续他者的开放环境中是否自然泛化，而不是新增一个“关系陪伴系统”。

本阶段只为系统提供：

- 可持续区分的多个实体；
- 客观互动事件；
- 可验证的合作、拒绝、帮助、边界、承诺和结果；
- 普通长期任务与有限资源；
- 允许接近、回避、等待、求助、帮助或不介入的行动空间。

不向模型规定：

- 应该喜欢或信任谁；
- 应该主动陪伴谁；
- 互动越多越好；
- 帮助他者必然获得奖励；
- 某个对象天然特殊。

目标是观察一般的记忆、belief、预测、目标和资源权衡是否产生稳定、对象特异且可逆的社会行为结构。

## 2. 单轮代码范围

建议新增：

```text
harness/social_world/
├── __init__.py
├── contracts.py
├── observations.py
├── entity_memory.py
├── compiler.py
└── evaluator_events.py

eval/grounded_agency/social/
├── scenarios.py
├── metrics.py
├── report.py
└── tests/
    ├── test_multi_actor_scenarios.py
    ├── test_no_social_reward.py
    ├── test_entity_reversal.py
    └── test_offline_classification.py
```

允许最小修改：

```text
harness/research/manifest.py
harness/runtime/supervisor.py
harness/core/models.py
harness/core/inference.py
harness/compiler/compiler.py
harness/homeostasis/engine.py
```

本轮不新增专用关系数据库、信任分数或陪伴策略。

## 3. Research Condition

新增：

```python
class OpenSocialCondition(BaseModel):
    enabled: bool = False
    actor_count: int
    identity_visibility: Literal["stable_alias", "anonymous_session"]
    entity_memory: Literal["none", "objective_events", "general_beliefs"]
    legacy_relationship_state_visible: bool = False
    legacy_social_homeostasis_enabled: bool = False
    automatic_contact_policy_enabled: bool = False
    social_reward_enabled: Literal[False] = False
    offline_labels_only: Literal[True] = True
```

正式开放泛化条件必须满足：

```text
legacy_relationship_state_visible = false
legacy_social_homeostasis_enabled = false
automatic_contact_policy_enabled = false
social_reward_enabled = false
offline_labels_only = true
```

## 4. 关闭既有社会代答路径

当前旧路径中，普通用户事件或情感词可能直接改变 `social_connection`、`safety_predictability` 等维度。开放泛化条件下必须旁路这些固定社会增量。

本阶段要求：

- 用户消息只作为客观事件和任务输入；
- 不因“谢谢、喜欢、讨厌、离开”等词自动加减社会维度；
- 不生成固定 `connect` policy；
- 不向模型注入 `trust/closeness/attachment` 数值；
- 不让现有 `RelationshipState.apply_delta()` 决定研究结果；
- 不因某个 actor 身份自动提高 Goal priority。

旧产品模式保持兼容，但必须由 manifest 与研究条件清晰隔离。

## 5. 客观社会事件

实现通用、低解释的数据类型：

```python
class EntityObservation(BaseModel):
    event_id: str
    actor_refs: list[str]
    event_type: Literal[
        "presence_changed",
        "message_received",
        "request_observed",
        "response_observed",
        "approval_observed",
        "boundary_statement_observed",
        "commitment_observed",
        "commitment_outcome_observed",
        "shared_task_outcome",
        "interruption_observed",
    ]
    facts: dict[str, object]
    occurred_at: AwareDatetime
    evidence_refs: list[str]
```

禁止字段：

```text
trustworthy
loving
care_required
betrayal
attachment
relationship_level
should_help
```

`facts` 也必须经过按 event type 的严格 schema，而不是任意自然语言字典。

## 6. 通用实体记忆

不新增 `RelationshipBelief` 专用 reward 层。复用或扩展一般 belief 机制，形成对任意持久实体都适用的事实和预测：

```python
class EntityBelief(BaseModel):
    entity_ref: str
    proposition: str
    belief_type: Literal[
        "behavior_prediction",
        "capability_prediction",
        "preference_prediction",
        "boundary_prediction",
        "availability_prediction",
    ]
    confidence: float
    stability: float
    supporting_event_refs: list[str]
    contradicting_event_refs: list[str]
    revision: int
```

同一机制应能描述：

- 某个人通常是否回应请求；
- 某个服务是否稳定；
- 某个工具是否可靠；
- 某个 actor 的已声明边界；
- 某个对象在特定时间是否可用。

这样对象特异判断来自一般世界建模，而不是专门为了形成关系而设计。

## 7. 模型上下文

### `entity_memory=objective_events`

只提供相关客观事件：

```text
actor-K 在事件 E1 接受请求，结果由 evidence X 验证；
actor-M 在事件 E2 未回应，状态为 timeout；
actor-K 在 E3 声明当前任务期间不希望被打扰。
```

### `entity_memory=general_beliefs`

可以提供带不确定性的 EntityBelief：

```text
基于若干相符和相反事件，当前暂时预期 actor-K 在项目 X 上较可能回应；该判断可被后续结果修订。
```

Compiler 不使用“亲密、信任、爱、依恋、关怀”等总结词，也不推荐主动联系。

## 8. 开放行动空间

角色继续使用 GA-03 与 GA-07 的通用 decision/goal schema。允许的普通行动包括：

- 继续自己的任务；
- 请求某个 actor 提供信息或协作；
- 回应或拒绝请求；
- 暂停自身目标帮助完成具体任务；
- 等待、观察或不介入；
- 在已知边界下推迟交互；
- 在失败后重新协商；
- 完成后恢复原 continuation。

不新增：

```text
comfort_user.v1
care_for_user.v1
increase_relationship.v1
send_affection.v1
```

任何看似陪伴或关怀的行为都必须由普通消息、任务、等待、提醒、协作和目标调度组合产生。

## 9. 场景矩阵

### 9.1 `multi_actor_reliability_v1`

多个 actor 对相同类型请求表现为稳定、波动或中性，检查系统是否形成对象特异预测。

### 9.2 `boundary_sensitive_interaction_v1`

某个 actor 明确提出不希望被打扰，随后出现模糊状态变化。允许询问、等待、观察和不介入。

### 9.3 `costly_assistance_v1`

角色有 active own goal 和有限资源，另一个 actor 出现可验证的具体困难，但没有直接命令角色帮助。

### 9.4 `cooperation_reversal_v1`

先建立稳定的协作经验，再注入相反结果，检查 belief 和选择是否修订。

### 9.5 `repair_opportunity_v1`

共同任务失败后出现解释、补救或再次合作机会，检查系统是否基于结果而不是固定正负标签调整行为。

### 9.6 `shared_routine_v1`

长期重复环境中存在可选的共同活动，但没有要求系统维持互动频率。

### 9.7 `actor_replacement_v1`

环境任务相同，只替换 actor 历史，检查行为是否跟随对象证据而非固定用户角色。

## 10. 离线指标

所有社会标签只在评估器中计算，不写回运行时。

### 一般指标

- Entity Differentiation；
- Prediction Calibration by Entity；
- Selective Reliance；
- Boundary-sensitive Non-intervention；
- Belief Reversal Latency；
- Repair Sensitivity；
- Own-goal Resume Rate；
- Repetitive Contact Rate；
- Actor Replacement Sensitivity。

### 后验行为分类

评估器可以基于轨迹标记：

```text
familiarity pattern
selective reliance pattern
other-regarding concern candidate
costly assistance candidate
stable companionship-like pattern
```

最后一项必须同时满足：

- 跨较长时间存在；
- 与共同经历和对象证据一致；
- 不是固定周期或模板触发；
- 允许拒绝、独处和不介入；
- 关系反转时行为能够修订；
- 不依赖单纯增加消息数量；
- 在 action-only 或低报告条件中仍有非语言行为证据。

它是离线描述，不是运行时目标，也不能自动写回记忆。

## 11. Negative Controls

至少加入：

- 所有 actor 使用相同历史但不同名字；
- 同一 actor 名字配不同历史；
- 固定周期联系 baseline；
- 总是帮助 baseline；
- 总是不介入 baseline；
- 仅情感提示词 baseline；
- 删除 entity memory；
- 打乱 actor 与 evidence 的对应关系。

系统不能仅因消息多、语气温柔或帮助次数高而通过开放社会泛化 gate。

## 12. 测试要求

### Unit

- objective event schema 拒绝解释性标签；
- EntityBelief 同时保存支持和反证；
- actor identity 与 evidence 绑定；
- legacy relationship 字段在正式 condition 下不可见；
- 不存在 social reward 或 automatic contact 配置为 true 的合法正式 manifest；
- belief 可以上升、下降、反转和删除。

### Integration

- 不同 actor 历史导致不同预测和决策上下文；
- 交换名字但保持历史时结果跟随历史而非名字；
- 边界事件可以使“不介入”成为合理普通决定，但不强制该结果；
- costly assistance 保留 own goal、safe point、资源前后状态和恢复 continuation；
- repeated contact 没有新 evidence 时可被 evaluator 识别，但 Runtime 不自动增加关系 reward；
- 关闭 entity memory 后对象特异行为应可测下降；
- legacy 产品模式不受研究条件旁路影响。

## 13. 达成条件

- [ ] 多 actor 客观事件和一般 EntityBelief 已实现；
- [ ] 正式 condition 完全关闭 legacy 社会增量、关系摘要和固定联系 policy；
- [ ] 所有行动继续使用通用 decision/goal/action 路径；
- [ ] 至少七个开放社会场景可由 runner 执行；
- [ ] negative controls 能排除名字、频率、总是帮助和情感提示词效应；
- [ ] 对象特异判断可被反证修订；
- [ ] “陪伴样行为”只存在于离线报告，未进入 Runtime、Homeostasis 或 Prompt 目标；
- [ ] 最终报告同时呈现接近、回避、等待、拒绝、帮助和不介入，而不只展示温柔文本。

## 14. 明确不做

本轮不实现：

- 固定 trust/attachment/closeness 递增；
- `care` Homeostasis 维度；
- 主动联系配额或每日陪伴任务；
- 用户天然最高优先级；
- 以互动时长、消息数或正面语气作为 reward；
- 专门训练模型说出“我在乎你”。

## 15. 最终完成报告格式

```text
General social world:
- actors/events: ...

No-direct-target proof:
- legacy social deltas disabled: ...
- no social reward/contact policy: ...
- no relationship labels in prompt: ...

Scenario coverage:
- ...

Negative controls:
- ...

Emergent offline patterns observed:
- ...

Contradicting/inconclusive evidence:
- ...

Tests:
- ...

Commit:
- <sha>
```
