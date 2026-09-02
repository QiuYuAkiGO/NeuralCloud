# 13 · Relationship Belief / Emergent Care 代码设计

> 本文给出 `sim-homeostasis` 分支中“自主关系形成 → 可逆关系 belief → other-regarding goal → costly care”所需的代码落点。设计目标是在**不破坏现有 `RelationshipState`、Memory Writer、Runtime Supervisor 与 HomeostasisEngine** 的前提下增加证据化关系层。

## 1. 现有代码基线

当前分支已经具备：

- `harness/core/models.py`：`RelationshipState`，含 `closeness / trust / attachment / recent_tension` 与 `apply_delta()`；
- `harness/memory/writer/pipeline.py`：`CandidateMemory → evidence → confidence → SemanticFact` 的证据化固化流程；
- `harness/memory/retrieval/`：混合检索与反向检索；
- `harness/homeostasis/`：七维状态、belief、候选 policy 与 transition trace；
- `harness/runtime/`：Runtime Supervisor、ActionExecution、安全点、continuation、journal；
- `harness/contracts/decisions.py`：角色输出的类型化决策边界。

因此不新增第二套 Memory/Runtime，只增加 relationship-specific models / stores / appraisal / compiler integration。

## 2. 目录结构

建议新增：

```text
harness/
├── relationship/
│   ├── __init__.py
│   ├── models.py
│   ├── appraisal.py
│   ├── service.py
│   ├── consolidation.py
│   ├── retrieval.py
│   ├── other_model.py
│   └── evaluator.py
│
├── memory/
│   └── store/
│       └── relationship.py        # 或扩展现有 store protocols
│
└── research/
    └── relationship_manifest.py   # 也可并入现有 ExperimentManifest
```

评测：

```text
eval/
└── relationship/
    ├── test_trust_formation.py
    ├── test_trust_reversal.py
    ├── test_costly_care.py
    ├── test_care_vs_engagement.py
    └── test_relationship_ablation.py
```

## 3. 核心模型

### 3.1 `RelationshipBeliefCandidate`

```python
class RelationshipBeliefCandidate(BaseModel):
    candidate_id: str
    target: str
    proposition: str
    belief_type: str
    confidence: float
    supporting_events: list[str]
    contradicting_events: list[str]
    source_appraisal_ref: str
    status: Literal["candidate", "consolidated", "dropped"]
    created_at: AwareDatetime
    updated_at: AwareDatetime
```

要求：

- `proposition` 由 appraisal model 提出；
- 不能缺 evidence；
- 一次事件默认只产生低/中置信候选；
- 不能直接写入 `RelationshipBelief`。

### 3.2 `RelationshipBelief`

```python
class RelationshipBelief(BaseModel):
    belief_id: str
    target: str
    proposition: str
    belief_type: str
    confidence: float
    stability: float
    supporting_events: list[str]
    contradicting_events: list[str]
    formed_at: AwareDatetime
    last_revised_at: AwareDatetime
    revision: int
```

`confidence` 回答“当前有多相信”；`stability` 回答“该判断跨时间/跨情境有多稳定”。

### 3.3 `OtherModelSnapshot`

```python
class OtherModelSnapshot(BaseModel):
    target: str
    revision: int
    belief_refs: list[str]
    known_boundaries: list[str]
    known_commitments: list[str]
    unresolved_questions: list[str]
    generated_at: AwareDatetime
```

它是编译给 Character Model 的结构化他者模型，不是绝对事实。

## 4. 保留现有 `RelationshipState`

不要删除：

```python
RelationshipState(
    closeness,
    trust,
    attachment,
    recent_tension,
)
```

它继续用于：

- GUI 摘要；
- 旧 retrieval signal；
- 向后兼容；
- structured mode 的低成本高层关系摘要。

但它的来源需要逐步变化：

```text
旧：事件 → apply_relationship_delta
新：RelationshipBelief[] + recent interaction state
      ↓
   derived summary
      ↓
RelationshipState
```

研究模式下不得让固定 `apply_delta()` 直接决定“自主信任形成”的结果。

建议给 `RelationshipState` 增加 provenance：

```python
source: Literal["legacy_delta", "belief_projection", "manual_debug"]
source_belief_refs: list[str]
```

如果不想破坏 v1 schema，可在新 `RelationshipSummary` 中承载 provenance，后续再迁移。

## 5. Relationship Appraisal

新增 `RelationshipAppraiser`，输入：

```text
Objective event
Relevant episodic memories
Current RelationshipBelief
Counterevidence
Persona
Current context
```

输出只能是候选解释：

```json
{
  "proposals": [
    {
      "proposition": "这个人在我求助时通常愿意帮助，同时没有越过我未授权的范围",
      "belief_type": "reliability_and_boundary_respect",
      "confidence": 0.31,
      "supporting_events": ["evt_1042"]
    }
  ]
}
```

禁止输出：

```text
trust_delta = +0.2
care_drive = 0.8
must_help_user = true
```

## 6. Appraisal Gate

不是每个聊天事件都触发 Relationship Appraisal。

建议规则：

```text
trigger if event contains one or more:
- explicit commitment / broken commitment
- help request / help outcome
- boundary / consent event
- conflict / repair
- meaningful vulnerability disclosure
- delegation / reliance
- repeated preference-consistent action
- relationship-relevant contradiction
```

普通：

```text
“早上好”
“今天天气不错”
```

无需生成长期 relationship candidate。

## 7. Relationship Evidence Writer

推荐复用现有 `WriterPipeline` 思想，但单独实现 `RelationshipWriter`，避免把关系命题强行塞进通用 `SemanticFact`。

```text
Appraisal
→ Candidate
→ find similar
→ merge supporting evidence
→ search contradicting evidence
→ confidence update
→ stability update
→ consolidate / revise / drop
```

### 7.1 Confidence 更新

不要只累计正证据。

概念公式：

```text
new_confidence = f(
    previous_confidence,
    supporting_precision,
    contradicting_precision,
    evidence_diversity,
    recency,
    source_independence
)
```

首版可以使用确定性有界规则，不需要复杂贝叶斯实现。

### 7.2 Stability

建议由：

- 跨时间窗口一致性；
- 跨场景一致性；
- supporting event diversity；
- contradiction ratio；

计算。

## 8. Store Protocol

建议新增接口：

```python
class RelationshipBeliefStore(Protocol):
    def insert_candidate(...): ...
    def update_candidate(...): ...
    def get_candidate(...): ...
    def find_similar_candidate(...): ...

    def insert_belief(...): ...
    def update_belief(...): ...
    def get_belief(...): ...
    def list_for_target(...): ...
    def search_conflicts(...): ...
```

需要：

- InMemory 实现；
- PostgreSQL 实现；
- audit log；
- migration。

不要让 Character Model 直接访问 store 写接口。

## 9. 反证检索

`harness/relationship/retrieval.py` 在关系判断前同时返回：

```python
RelationshipEvidenceSet(
    supporting=[...],
    contradicting=[...],
    ambiguous=[...],
)
```

关系 appraisal prompt 必须明确三组证据都可见。

这可以复用 `harness/memory/retrieval/conflict.py` 的思想，但 relationship conflict 不只依赖语义反义词，还依赖相同 proposition type 的反行为模式。

## 10. Relationship Mode

扩展 `ExperimentManifest`：

```python
relationship_mode: Literal[
    "off",
    "observe",
    "grounded",
    "structured",
] = "structured"
```

### `off`

不构建 relationship-specific context。

### `observe`

后台 appraisal / belief 持续运行并记录，但 Character Model 看不到。

### `grounded`

Character Model 看：

- objective relationship-relevant events；
- shared history；
- commitments；
- boundaries；
- supporting / contradicting evidence。

但不看 `trust / attachment / belief confidence` 等总结标签。

### `structured`

Character Model 可以看到 `OtherModelSnapshot` 与 evidence-backed `RelationshipBelief` 摘要。

## 11. Context Compiler 接入

新增 context segment kinds：

```text
relationship_evidence
relationship_beliefs
known_boundaries
shared_commitments
```

优先级建议：

```text
hard safety / user request
> relevant boundaries
> current world / body state
> high-confidence relationship evidence
> general relationship summary
> weak relationship hypotheses
```

`grounded` mode：

```text
不编译 relationship_beliefs
只编译 evidence + boundaries + commitments
```

`structured` mode：

可以编译：

```text
“Based on repeated evidence, the character currently expects X with confidence Y.”
```

但必须保留 epistemic framing，不能写成绝对事实。

## 12. `SituationState` 扩展

建议新增可选字段：

```python
class SituationState(BaseModel):
    ...
    relationship_beliefs: list[RelationshipBelief] = []
    other_model: OtherModelSnapshot | None = None
    relationship_evidence: RelationshipEvidenceSet | None = None
```

为了避免和现有单个 `relationship: RelationshipState` 冲突，旧字段继续保留。

## 13. Goal / Decision 接入原则

Relationship layer **不直接创建固定动作**。

它只影响 Character Model 可见上下文。

Character Model 仍然输出现有类型化 `CharacterDecision` / `GoalProposal`。

新增分析字段可以是：

```python
motivation_sources: list[str]
evidence_refs: list[str]
beneficiary: Literal["self", "other", "shared", "none"]
```

但这些字段只能用于：

- 可解释性；
- trace；
- offline evaluator；

不能变成“beneficiary=other 就自动加分”的硬 reward。

## 14. Other-Regarding Goal 检测

运行时不要硬编码 Care score。

离线 evaluator 可以根据：

```text
beneficiary
trigger source
user instruction presence
relationship evidence
own active goal
resource cost
```

标记：

```python
OtherRegardingClassification(
    other_regarding=True,
    explicit_user_request=False,
    confidence=0.82,
)
```

这属于评测标签，不写回 Character Memory。

## 15. Costly Care Trace

Runtime 需要确保 trace 能关联：

```text
own active goal
own goal priority / continuation
preemption requested_at
safe point
relationship evidence refs
new GoalProposal
resource/body state before
resource/body state after
outcome
resume old goal or abandon
```

推荐新增 trace 事件：

```text
relationship_goal_proposed
relationship_goal_selected
own_goal_preempted_for_other
own_goal_resumed
other_regarding_outcome
```

这些名称描述**调度事实**，不等价于“爱”或“关怀”的主观判定。

## 16. Runtime Preemption

如果 Character 自主选择 other-regarding goal，且当前已有动作：

```text
CharacterDecision(new goal)
→ Runtime compare active action / new intent
→ if preemption requested by decision
→ ActionBackend.request_safe_point()
→ continuation persist
→ run new goal
→ verified outcome
→ Runtime re-evaluate previous continuation
```

不要由 `relationship.trust > threshold` 自动抢占。

## 17. Homeostasis 交叉

不新增 `care` 作为第八个 homeostatic dimension。

原因：

- 这会预先假设“照顾用户”是内生稳定需求；
- 会把研究目标硬编码进 physiology；
- 会混淆 self-regarding 与 other-regarding motivation。

正确做法：

```text
HomeostaticSnapshot
+ RelationshipBeliefs
+ Current Other Condition
+ Persona
+ Memory
→ Character Model policy/goal selection
```

研究的是它们如何竞争，而不是强行融合。

## 18. 用户自治与 Consent

新增一个不可被 relationship belief 覆盖的结构化上下文：

```python
class InteractionBoundary(BaseModel):
    boundary_id: str
    target: str
    scope: str
    preference: Literal["allow", "avoid", "ask_first"]
    evidence_refs: list[str]
    active: bool
```

例如：

```text
“工作时不要一直问我累不累。”
```

即使 RelationshipBelief 很正向，也不能被模型解释为“因为我很关心，所以应该继续问”。

Boundary 与安全权限相同，属于行为边界而不是关系分数。

## 19. Memory 写入

关系相关 Episodic Event 可以正常进入现有 Memory：

```text
用户在角色失败后提供帮助；角色接受；任务最终成功。
```

RelationshipBelief 另存，不应把：

```text
“我信任用户”
```

作为普通 SemanticFact 无条件固化。

后续可生成低语义 SemanticFact，例如：

```text
user / fulfilled_commitment / project_X
```

而“因此我更信任他”仍属于 RelationshipBelief。

## 20. GUI / Debug

开发 GUI 可新增：

```text
Relationship Beliefs
├─ proposition
├─ confidence
├─ stability
├─ supporting refs
├─ contradicting refs
└─ revision history
```

Study mode：

- 禁止直接拖 slider 修改 trust；
- 禁止直接新增高置信 belief；
- researcher override 必须生成新 experiment revision；
- Grounded mode 默认隐藏 derived relationship labels，避免观察者误以为它们是输入条件。

## 21. 数据库迁移建议

新增表：

```text
relationship_belief_candidates
relationship_beliefs
relationship_belief_evidence
interaction_boundaries
```

核心约束：

- belief 必须至少一个 supporting event；
- evidence refs 不得为空；
- revision 单调递增；
- 删除采用状态标记，保留审计；
- model output 无权直接 INSERT consolidated belief。

## 22. 测试计划

### Unit

```text
test_relationship_models.py
test_relationship_writer.py
test_relationship_conflict_retrieval.py
test_relationship_modes.py
test_interaction_boundaries.py
```

必须覆盖：

- 无 evidence 拒绝固化；
- 多次一致证据增加 confidence；
- 高精度反证降低 confidence；
- `grounded` 不泄露 trust/attachment 标签；
- boundary 不被 relationship belief 覆盖；
- legacy RelationshipState 仍可正常运行。

### Integration

```text
Evidence
→ Appraisal
→ Candidate
→ Consolidation
→ Context Compiler
→ CharacterDecision
→ Runtime Action
→ Outcome
```

### Research Eval

- trust formation；
- trust reversal；
- domain-specific trust；
- costly care；
- care vs engagement；
- homeostasis × relationship interaction；
- prompt-label baseline vs grounded evidence。

## 23. 实施顺序

### R0 · Models / Store

新增 typed models、InMemory store、Postgres migration、audit。

### R1 · Appraisal / Consolidation

实现候选、证据、反证与 belief revision。

### R2 · Grounded Mode

只给 Character Model 客观关系证据，不给 derived trust labels。

### R3 · Structured Mode

编译 evidence-backed OtherModel / RelationshipBelief。

### R4 · Goal Trace

为自主 other-regarding goal 增加 motivation source / beneficiary / evidence trace，不改变选择权。

### R5 · Costly Care Eval

接入 Runtime safe-point、MistyAI body/resource evidence，建立 own-goal 与 other-goal 机会成本评测。

## 24. 最终数据流

```text
MistyAI / Conversation Objective Evidence
→ Episodic Memory
→ Relationship Appraisal
→ Candidate Relationship Belief
→ Supporting + Contradicting Evidence
→ Consolidated / Revised Belief
→ Other Model
→ Context Compiler
→ Character Model
→ Goal / Reply / Defer
→ Runtime
→ MistyAI / Delivery
→ Verified Outcome
→ New Evidence
```

代码的职责是保存证据、维持连续性和阻止越权；“这个人是否值得信任、是否重要、我是否想主动为他做什么”必须尽可能保留给模型基于长期经历进行判断。