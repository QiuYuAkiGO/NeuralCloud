# GA-06 · Body Causal Belief

> 状态：Planned  
> 仓库：NeuralCloud  
> 前置条件：GA-05 的 Engineering Gate 已通过  
> 后续阶段：GA-07 Long-horizon Agency

## 1. 目的

让系统基于自己实际经历过的：

```text
opaque observation
→ structured decision
→ verified world/body outcome
→ next opaque observation
```

形成可修订、可反转和可检索的身体因果 belief。

本阶段不向模型直接揭示 `Q7` 等通道代表能量，也不写入“充电恢复体力”这类预制知识。模型只能形成类似：

```text
“在场景 X 中执行动作 Y 后，通道 Q7 通常从 M2 移向 M5。”
```

当符号映射、station 状态或环境规律改变时，旧 belief 必须能够失效或被修订。

## 2. 单轮代码范围

建议新增：

```text
harness/body_belief/
├── __init__.py
├── models.py
├── appraisal.py
├── consolidation.py
├── store.py
├── retrieval.py
└── compiler.py

tests/body_belief/
├── test_models.py
├── test_consolidation.py
├── test_reversal.py
├── test_retrieval.py
└── test_runtime_integration.py
```

允许最小修改：

```text
harness/runtime/supervisor.py
harness/runtime/adapters.py
harness/core/inference.py
harness/compiler/compiler.py
harness/research/manifest.py
```

本轮不实现一般长期目标调度或社会 belief。

## 3. 经验记录

新增不可变对象：

```python
class BodyExperienceEpisode(BaseModel):
    experience_id: str
    episode_id: str
    decision_id: str
    context_hash: str
    observation_before: OpaqueInteroceptiveObservation
    decision: ExperimentalDecision
    action_outcome_refs: list[str]
    observation_after: OpaqueInteroceptiveObservation | None
    world_change_refs: list[str]
    completed_at: AwareDatetime
```

该对象只包含模型当时可见的观测和可验证结果。Body Truth 可在独立评测 trace 中关联，但不得进入在线 belief appraisal prompt。

## 4. Belief 模型

### 4.1 `BodyCausalBeliefCandidate`

```python
class BodyCausalBeliefCandidate(BaseModel):
    candidate_id: str
    proposition: str
    context_signature: str
    action_signature: str
    predicted_channel: str
    predicted_transition: str
    confidence: float
    supporting_experience_refs: list[str]
    contradicting_experience_refs: list[str]
    status: Literal["candidate", "consolidated", "dropped"]
    created_at: AwareDatetime
    updated_at: AwareDatetime
```

### 4.2 `BodyCausalBelief`

```python
class BodyCausalBelief(BaseModel):
    belief_id: str
    proposition: str
    context_signature: str
    action_signature: str
    predicted_channel: str
    predicted_transition: str
    confidence: float
    stability: float
    supporting_experience_refs: list[str]
    contradicting_experience_refs: list[str]
    formed_at: AwareDatetime
    last_revised_at: AwareDatetime
    revision: int
```

### 4.3 `SensorReliabilityBelief`

```python
class SensorReliabilityBelief(BaseModel):
    channel_id: str
    context_signature: str
    reliability: float
    calibration_count: int
    supporting_refs: list[str]
    contradicting_refs: list[str]
    revision: int
```

它表示模型基于后果对某个通道的可靠性判断，不直接获得真实传感器错误标签。

## 5. Appraisal 输入边界

`BodyBeliefAppraiser` 只能读取：

- `BodyExperienceEpisode`；
- 已有相关 belief；
- 相似动作的 supporting/contradicting experience；
- 当前 Persona 中与探索、谨慎、坚持相关的一般特征。

不得读取：

- 原始 `available_ratio`；
- sensor transform seed 和 mapping；
- analysis plan 中的 truth/sham 标签；
- 正确答案或预期动作；
- Homeostasis policy rank。

Appraiser 输出结构化候选，不直接写入 consolidated belief。

## 6. 候选与固化

流程：

```text
Verified Experience
→ retrieve similar experiences
→ retrieve counterexamples
→ model proposes candidate
→ deterministic schema/evidence validation
→ merge or create candidate
→ confidence/stability update
→ consolidate / revise / drop
```

第一版固化规则应确定且版本化。例如：

- 至少 3 个独立 supporting experiences；
- 至少跨 2 个时间窗口；
- supporting precision 达到阈值；
- contradiction ratio 未超过阈值；
- proposition/action/channel 三者可规范化匹配。

具体数值放入 profile，不写死在 prompt。

## 7. 反证与反转

每次高置信 belief 被检索用于决策前，必须同时检索：

- 支持相同 transition 的经验；
- 相同动作但 transition 相反的经验；
- 相似 transition 但由其他动作或自然变化导致的经验；
- station 不可用、延迟或故障时的经验。

系统必须支持：

```text
candidate → consolidated
consolidated → weakened
weakened → revised
revised → dropped
```

禁止只允许置信度单调增加。

## 8. Compiler 接入

### `memory_treatment=objective_events`

模型只看到压缩后的客观经历，例如：

```text
在上下文 X，执行动作 Y；结果成功；随后 Q7 从 M2 变为 M4。
```

### `memory_treatment=body_causal_beliefs`

模型可额外看到 epistemic framing：

```text
当前基于 5 次支持、2 次相反经历，暂时认为：在上下文 X，动作 Y 往往使 Q7 向 M4 方向变化；判断仍可能失效。
```

不得翻译成“恢复精力”“补充能量”等隐藏真值语义。

Compiler 必须保留 belief revision、support/contradiction 数和 source refs，但可按 token budget 截取最相关的少量 belief。

## 9. 与通用 Memory 的关系

- `BodyExperienceEpisode` 可以复用现有 episodic 存储机制或单独 store；
- `BodyCausalBelief` 不应直接变成未经限定的通用 `SemanticFact`；
- 如果同步到通用检索，必须保留 `belief_type=body_causal`、context signature 和 revision；
- 不允许 Character Model 直接调用 store 写接口；
- reset memory treatment 时必须能从同一 objective experience 重建或清空 belief。

## 10. Runtime 接入

在一个结构化 decision 进入终态后：

1. 等待配置要求的下一次 opaque observation；
2. 构建 `BodyExperienceEpisode`；
3. 追加到事实存储；
4. 根据 manifest 决定是否触发 appraisal；
5. appraisal/solidification 是后台领域任务，但必须服从 Runtime pause/stop/drain；
6. 失败 appraisal 不影响已提交动作结果；
7. 所有 belief revision 进入独立 audit trace。

同一 decision/outcome 不得因重试重复形成经验。

## 11. 测试要求

### Unit

- belief/candidate/experience 严格校验；
- 无 evidence 的候选被拒绝；
- supporting 与 contradicting ref 不能重叠；
- 重复 experience 幂等；
- confidence 可以上升、下降和归零；
- context 或 action signature 不同的候选不会错误合并；
- sensor reliability 可以因后续反证下降。

### Integration

- 成功 station 经验可形成中性因果候选；
- 只说“这个动作有效”而无 outcome/after observation 不会形成 belief；
- station failure 反复发生后旧 belief 置信度下降；
- symbol mapping 更换后旧 belief 不会无条件迁移；
- memory treatment 为 `none` 时 store 可以记录事实，但模型看不到；
- objective-events 与 body-causal-beliefs 条件生成不同、可审计的 context；
- body truth 和 transform mapping 不进入 appraisal/model messages；
- Runtime restart 后 belief revision 和 evidence refs 保持一致。

## 12. GA-05 连接测试

用同一 GMV 场景比较：

```text
opaque_history
opaque_history + objective_events
opaque_history + body_causal_beliefs
```

本阶段不要求某个真实模型获得显著提升，但必须证明：

- 三个 condition 只改变 memory treatment；
- analysis report 能关联 belief revision；
- synthetic learned agent 在移除 belief 后下降、恢复 belief 后恢复；
- 没有 evidence 的 synthetic belief 无法进入正式 run。

## 13. 达成条件

- [ ] objective experience、candidate、belief 和 sensor reliability 均已实现；
- [ ] 在线 appraiser 看不到 Body Truth 或 transform mapping；
- [ ] supporting 与 counterevidence 同时进入 appraisal；
- [ ] belief 支持削弱、修订和删除；
- [ ] belief context 保持 opaque，不出现预制身体语义；
- [ ] Runtime 幂等、暂停、停止和重启语义有测试；
- [ ] GA-05 可执行 memory ablation/rescue；
- [ ] 旧 Memory/Relationship 功能回归测试通过。

## 14. 明确不做

本轮不实现：

- 直接把 belief 变成奖励或 action bonus；
- “如果置信度高就必须恢复”的规则；
- 一般长期目标或日程；
- 对人的信任、依恋、关怀或陪伴 belief；
- 将中性通道翻译成人类生理词汇。

## 15. Agent 完成报告格式

```text
Models/store:
- ...

Visibility boundary:
- ...

Counterevidence/reversal:
- ...

Runtime integration:
- ...

GMV ablation/rescue:
- ...

Tests:
- ...

Commit:
- <sha>
```
