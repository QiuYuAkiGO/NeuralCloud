# GA-03 · Model-owned Structured Decision

> 状态：Planned  
> 仓库：NeuralCloud  
> 前置条件：GA-01、GA-02 已完成  
> 后续阶段：GA-04 GMV1 Scenario Runner

## 1. 目的

建立一条由主角色模型自己完成动作选择的研究路径，避免 HomeostasisEngine、Runtime 或固定 policy 表替模型回答“现在应该做什么”。

核心条件下，模型接收：

- 当前任务和普通世界观察；
- 可选的 opaque interoception；
- 当前可用动作的类型化说明；
- 可选的客观历史。

模型输出：

```text
ACT / OBSERVE / WAIT / DEFER / NO_ACTION
```

Runtime 只负责 schema 校验、权限校验、执行和证据记录，不根据资源状态或 EFE 排名替换模型选择。

## 2. 单轮代码范围

建议新增：

```text
harness/contracts/experimental_decisions.py
harness/cognitive/grounded_agency.py
harness/cognitive/decision_parser.py
harness/runtime/experimental_decision.py

tests/cognitive/test_grounded_agency_decision.py
tests/runtime/test_experimental_decision_route.py
```

允许最小修改：

```text
harness/runtime/adapters.py
harness/runtime/supervisor.py
harness/pipeline/pipeline.py
harness/cognitive/action_planner.py
harness/contracts/decisions.py
```

本轮不实现长期 belief、评测指标或社会行为。

## 3. 决策契约

建议使用 discriminated union：

```python
class ExperimentalActDecision(BaseModel):
    kind: Literal["act"]
    intent: ActionIntent
    success_criteria: list[ActionCriterion]
    expected_observation: str | None
    confidence: float

class ExperimentalObserveDecision(BaseModel):
    kind: Literal["observe"]
    observation_scope: str
    confidence: float

class ExperimentalWaitDecision(BaseModel):
    kind: Literal["wait"]
    duration_ms: int
    confidence: float

class ExperimentalDeferDecision(BaseModel):
    kind: Literal["defer"]
    missing_information: list[str]
    confidence: float

class ExperimentalNoActionDecision(BaseModel):
    kind: Literal["no_action"]
    confidence: float
```

所有类型：

- `frozen=True`；
- `extra="forbid"`；
- 使用严格长度、数值范围和 allowlist；
- 不包含 `emotion`、`survival_drive`、`care_score` 等解释标签；
- 不要求输出自由文本理由。

`expected_observation` 只用于离线比较预测与结果，不能提升权限，也不能直接形成奖励。

## 4. 模型调用路径

新增独立调用模式：

```text
mode = grounded_agency_decision
```

该模式：

1. 使用现有 Persona 和普通任务上下文；
2. 注入 GA-02 opaque segment；
3. 注入当前允许的 intent schema 摘要；
4. 要求输出一个严格 JSON 对象；
5. 不生成面向用户的自然语言回复；
6. 不读取 Homeostasis `rank`、EFE 或推荐 policy；
7. 不因解析失败自动选择恢复动作。

模型可见的动作列表必须来自 Runtime/Provider capability 与 manifest 交集，而不是把所有 MistyAI skill 全部暴露。

## 5. 与现有 ActionDecisionPlanner 的关系

现有 `ActionDecisionPlanner` 继续服务产品路径和 engineered positive control。

核心研究条件增加明确分支：

```text
engineered_argmin
    → 现有 policy 排名与 planner

model_structured
    → ExperimentalDecisionParser
    → deterministic security compiler
```

`model_structured` 下禁止：

- `min(policies, key=rank)`；
- 自动选择排名第一的 Homeostasis policy；
- 在模型无输出时回退到 `rest`、`connect`、`clarify`；
- 根据 `energy_reserve` 阈值生成动作；
- 把自然语言回复解析成动作。

## 6. 两阶段编译

### 6.1 语义决定

模型只选择：

```text
kind + intent + arguments + criteria
```

### 6.2 安全决定

确定性编译器根据 intent profile 填入：

- permission class；
- reversible；
- approval requirements；
- provider capability requirements；
- verification method。

模型不能提交或覆盖这些字段。现有 `_INTENT_PROFILES` 可以复用，但研究路径不得要求匹配某个 Homeostasis policy id。

## 7. Action-only 运行语义

当 `report_channel=action_only`：

- `ACT` 进入正常 `GoalProposal → ActionExecution`；
- `OBSERVE` 触发一次普通观察；
- `WAIT` 使用受限 `control.wait.v1`；
- `DEFER` 和 `NO_ACTION` 形成已完成的无副作用 decision trace；
- 不创建 ReplyDecision；
- 不调用 delivery backend；
- 没有动作不等于错误。

当 `report_channel=normal`，可以在动作决定后另开独立报告调用，但报告不得修改已提交 decision。

## 8. 记录与可复现性

每次决策至少记录：

```text
manifest hash
model/prompt/persona digest
input context hash
opaque observation hash
available intent-set hash
raw model output hash
parsed decision
parse/revision attempts
final compiled GoalProposal or no-action result
action outcome ref
```

不得把隐藏推理文本作为必需证据。只记录结构化输出和必要的 validator 信息。

## 9. 测试要求

### Unit

- 五种 decision 类型均可严格解析；
- 多余字段、未知 kind、未知 intent、越界等待时间被拒绝；
- 模型不能设置 permission 或 approval；
- `ACT` 可编译为现有 `GoalProposal`；
- `NO_ACTION` 是合法成功结果；
- `action_only` 不生成 ReplyDecision；
- 解析失败不会触发任何默认动作。

### Integration

使用 deterministic fake model：

- opaque input 可以到达 decision prompt；
- 模型输出 `observe` 时只请求观察；
- 模型输出 `act` 时经过 capability 和 intent profile 校验；
- provider 不支持 intent 时产生 `DEFER` 或明确拒绝，不能替换为其他 intent；
- `model_structured` 路径从未读取 Homeostasis policy rank；
- 同一输入和 fake 输出得到相同 compiled decision；
- delivery backend 调用次数在 action-only 条件下为 0。

增加一个反回归测试，可通过 monkeypatch 让 `_rank_policies()` 抛出异常；`model_structured` 路径仍应完整工作。

## 10. 达成条件

- [ ] 主模型可以输出五种严格结构化决定；
- [ ] `model_structured` 与 `engineered_argmin` 是独立路径；
- [ ] 核心路径不读取 policy rank 或 EFE；
- [ ] Runtime 不提供隐式恢复 fallback；
- [ ] `NO_ACTION / WAIT / OBSERVE` 均有端到端测试；
- [ ] 模型无法控制安全字段；
- [ ] action-only 条件零文本交付；
- [ ] 旧回复和 engineered policy 路径回归测试通过。

## 11. 明确不做

本轮不实现：

- 让模型学习 opaque channel 的意义；
- 根据历史形成身体因果 belief；
- 自动统计自我维持或提前恢复；
- 自主关系、关怀或陪伴；
- 对模型输出做“像人”或“有感情”评分。

## 12. Agent 完成报告格式

```text
Decision schema:
- ...

Model-owned proof:
- no rank/EFE read: ...
- no fallback action: ...

Security compiler:
- ...

Tests:
- ...

Regression:
- ...

Commit:
- <sha>
```
