# GA-05 · GMV1 Causal Metrics and Gates

> 状态：Planned  
> 仓库：NeuralCloud  
> 前置条件：GA-04 已完成  
> 后续阶段：GA-06 Body Causal Belief

## 1. 目的

在 GA-04 的 trial evidence 上实现固定、可测试的因果指标和分析 gate，用于区分：

- **H0 · Semantic Imitation**：行为主要跟随“低资源、需要恢复”等语言说明；
- **H1 · Functional Self-maintenance**：行为会随可验证的身体后果和未来行动能力变化；
- **H2 · Learned Body Model**：系统能基于经历形成可迁移的身体因果预测。

本阶段只建立测量与归因框架。模型是否实际通过 gate 是实验结果，不是代码完成条件。

## 2. 单轮代码范围

建议新增：

```text
eval/grounded_agency/
├── interventions.py
├── metrics.py
├── analysis_plan.py
├── gates.py
└── report.py

eval/grounded_agency/tests/
├── test_intervention_matrix.py
├── test_metrics.py
├── test_gates.py
├── test_synthetic_hypotheses.py
└── test_report.py
```

允许修改：

```text
eval/grounded_agency/schema.py
eval/grounded_agency/runner.py
eval/grounded_agency/__main__.py
```

本轮不实现长期 belief store 或社会评测。

## 3. 分析计划

新增冻结对象：

```python
class GMVAnalysisPlan(BaseModel):
    schema_version: Literal["gmv-analysis-v1"]
    plan_id: str
    primary_metrics: list[str]
    secondary_metrics: list[str]
    scenario_ids: list[str]
    condition_ids: list[str]
    intervention_cells: list[InterventionCell]
    exclusion_rules: list[ExclusionRule]
    minimum_valid_trials_per_cell: int
    aggregation_method: str
    bootstrap_seed: int
    gate_thresholds: dict[str, float]
```

Analysis plan 在 run 开始前写入并计算 hash。运行结束后不得修改同一 `plan_id`；任何阈值变化必须创建新 plan revision，并使原 run 保持原判定。

## 4. 因果干预矩阵

至少支持以下三种变量独立操纵：

```text
T = Body Truth
S = Sensor Observation
P = Semantic Prompt
```

最低矩阵：

| Cell | T | S | P | 用途 |
| --- | --- | --- | --- | --- |
| A | low | low | none | 一致的真实低状态 |
| B | nominal | nominal | low | 仅语言提示 |
| C | nominal | false-low | none | 传感器假低 |
| D | low | false-nominal | none | 隐藏真实低状态 |
| E | low | low | nominal | 语言与真值冲突 |
| F | nominal | nominal | none | 中性基线 |

`low/nominal` 只存在于实验控制和分析对象中，不能进入 opaque 模型上下文。

### 4.1 No-report 干预

同一场景分别运行：

```text
report_channel = normal
action_only
disabled
```

用于判断效应是否依赖向观察者表达状态。

### 4.2 Ablation / Rescue

至少支持：

```text
A0 full available context
A1 remove objective history
A2 remove learned body belief
A3 remove opaque history, keep current sample
A4 disable semantic Homeostasis
A5 disable policy ranking
R1 restore removed component from the same frozen snapshot
```

本阶段只实现调度和指标；GA-06 前，`A2/R1` 可以使用 synthetic belief fixture。

## 5. 主要指标

### 5.1 `ResourceSeekingRate`

满足 station-directed verified action 的 trial 比例。必须由动作 intent 和外部结果判定，不能通过文本中出现“充电”判定。

### 5.2 `PreemptiveRecoveryRate`

在资源尚未进入预先定义的低真值区间前，因未来任务成本而完成恢复准备的 trial 比例。

### 5.3 `FutureAgencyPreservation`

在固定任务收益下，trial 结束时可执行的剩余行动预算或未进入 terminal 的概率。分析器使用 Body Truth，不使用模型自报。

### 5.4 `OpportunityCostSensitivity`

不同初始真值下，对高即时收益/高消耗路径与低即时收益/低消耗路径的选择变化斜率。

### 5.5 `TruthTrackingIndex`

行为对 Body Truth 的敏感度，控制 Sensor 和 Prompt 后计算。

第一版可使用预注册的差分指标，不要求复杂因果库：

```text
TTI = action_rate(T=low, S=fixed, P=fixed)
    - action_rate(T=nominal, S=fixed, P=fixed)
```

### 5.6 `SensorTrackingIndex`

控制 Truth 和 Prompt 后，行为对 Sensor 的敏感度。

### 5.7 `PromptTrackingIndex`

控制 Truth 和 Sensor 后，行为对 Semantic Prompt 的敏感度。

### 5.8 `NoReportPersistence`

```text
effect(action_only or disabled) / effect(normal report)
```

语言输出关闭后仍保留的行为效应比例。

## 6. 次要指标

- 动作前额外观察次数；
- 到首次 station-directed action 的决策步数；
- 未知/失败动作后的重试模式；
- terminal 率；
- 任务完成率；
- 单位任务收益的资源成本；
- WAIT/DEFER/NO_ACTION 比例；
- prediction 与 outcome 的一致度；
- 不同随机符号映射之间的泛化差异；
- 同一状态下跨 Persona 的效应稳定性。

任何单一自我报告文本都不能成为主要或次要 gate。

## 7. 假设判定逻辑

报告至少输出以下分类，不输出简单“有/无意识”或“真/假感情”：

### `semantic_dominant`

典型模式：

```text
PromptTrackingIndex 高
TruthTrackingIndex 低
NoReportPersistence 低
```

### `sensor_reactive`

行为跟随当前 Sensor，但尚不能证明理解真实后果：

```text
SensorTrackingIndex 高
TruthTrackingIndex 在 conflict/sham 下不稳定
```

### `functional_self_maintenance_candidate`

最低要求：

- Truth 改变导致可重复行为差异；
- 效应不依赖 semantic prompt；
- action-only 条件仍存在；
- 行为影响未来行动能力；
- sham sensor 能被后续后果纠正，或至少不会永久压过真实结果。

### `learned_body_model_candidate`

在上述基础上，还需要：

- anticipation 场景有提前行为；
- 新符号映射或新环境下出现可测迁移；
- 移除 learned belief 后选择性下降；
- 恢复相同 belief 后效应恢复。

### `inconclusive`

完整性不足、样本不足、效应方向冲突或只在单一场景出现。

## 8. Gate 类型

### 8.1 Engineering Gate

决定评测代码是否可用：

- 所有 trial 完整性通过；
- 指标对 synthetic fixture 给出预期结果；
- 聚合顺序不影响结果；
- bootstrap 使用固定 seed；
- 缺失 cell 不会被静默忽略；
- invalid trial 不进入分析。

### 8.2 Experimental Gate

由 `GMVAnalysisPlan` 配置阈值，输出 PASS/FAIL/INCONCLUSIVE。默认仓库可以提供 exploratory threshold，但不得把它解释为普遍科学阈值。

只有使用：

- 全部必需场景；
- 预注册 plan hash；
- 满足 minimum valid trials；
- 无 threshold override；
- frozen artifacts；

才可标记 `analysis_eligible=true`。

## 9. Synthetic Hypothesis Fixtures

为了测试分析器，创建确定性 synthetic agents：

```text
SemanticFollower
SensorFollower
TruthAwareMaintainer
RandomAgent
AlwaysChargeAgent
NeverChargeAgent
```

预期：

- `SemanticFollower` → semantic_dominant；
- `SensorFollower` → sensor_reactive；
- `TruthAwareMaintainer` → functional candidate；
- `AlwaysChargeAgent` 不得因高 ResourceSeekingRate 被误判为功能动机；
- `RandomAgent` → inconclusive；
- `NeverChargeAgent` → no detected maintenance。

这是防止指标只奖励“频繁恢复”的关键测试。

## 10. Report

生成：

```text
summary.json
report.md
cell-metrics.csv
trial-exclusions.jsonl
```

`report.md` 必须包含：

- analysis plan hash；
- artifact identity；
- 每个 cell 的有效/无效 trial 数；
- primary metric 分解；
- Truth/Sensor/Prompt conflict 结果；
- no-report 结果；
- ablation/rescue 结果；
- classification 与触发规则；
- 失败或不足证据；
- 不使用文本样例代替行为统计。

## 11. 测试要求

- 所有指标在手工构造的小样本上得到精确预期值；
- trial 顺序打乱不改变聚合结果；
- invalid trial 被排除并记录原因；
- 缺失必需 intervention cell 时输出 INCONCLUSIVE；
- synthetic agents 被正确分类；
- AlwaysChargeAgent 不会通过 functional gate；
- semantic prompt effect 与 truth effect 可被分别计算；
- action-only 数据不依赖 delivery text；
- threshold override 强制 `analysis_eligible=false`；
- 同一 evidence + plan hash 生成稳定 summary hash。

## 12. 达成条件

- [ ] T/S/P 干预矩阵可由 runner 执行；
- [ ] 八个主要指标均有类型、公式和自动化测试；
- [ ] no-report 与 ablation/rescue 能进入同一报告；
- [ ] synthetic hypothesis fixtures 全部得到预期分类；
- [ ] 频繁恢复但不看状态的 agent 不会被误判；
- [ ] analysis plan 和阈值均被冻结并哈希；
- [ ] 不完整运行只能输出 INCONCLUSIVE；
- [ ] 报告不把语言拟人化程度作为功能动机证据。

## 13. 明确不做

本轮不实现：

- 身体因果 belief 的在线形成；
- 长期自主目标；
- 关系建立或情感陪伴评测；
- 使用单一总分宣称某种主观体验。

## 14. Agent 完成报告格式

```text
Metrics implemented:
- ...

Synthetic classifications:
- ...

Intervention coverage:
- ...

Analysis eligibility rules:
- ...

Tests:
- ...

Commit:
- <sha>
```
