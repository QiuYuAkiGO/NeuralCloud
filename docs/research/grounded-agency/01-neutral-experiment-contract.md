# GA-01 · 中性实验契约与条件冻结

> 状态：Planned  
> 仓库：NeuralCloud  
> 前置条件：`docs/HOMEOSTASIS_STABILITY_PLAN.md` 的自动化测试与静态 soak 已通过  
> 后续阶段：GA-02 Opaque Interoception Channel

## 1. 目的

建立一套专用于 Grounded Agency 研究的版本化实验契约，使每次运行能够明确回答：

- 模型看到了什么；
- 哪些状态只对实验评估器可见；
- 是否启用了语义化 Homeostasis；
- 是否由模型自己选择动作；
- 是否提供记忆、身体因果 belief 或语言报告通道；
- 哪个真值、传感器变换和环境快照构成了本次条件。

本阶段只建立配置、校验和中性检查，不改变模型行为。

## 2. 单轮代码范围

建议新增：

```text
harness/research/grounded_agency/
├── __init__.py
├── manifest.py
├── neutrality.py
└── hashes.py

tests/research/grounded_agency/
├── test_manifest.py
└── test_neutrality.py

presets/research/grounded-agency/
├── no-body-v1.json
├── opaque-current-v1.json
└── engineered-control-v1.json
```

允许对以下文件做最小接入：

```text
harness/research/manifest.py
harness/compiler/compiler.py
```

本轮不实现 Runtime 路由、传感变换、模型决策或评测 runner。

## 3. 数据模型

在现有 `ExperimentManifest` 中增加可选、默认关闭的 `grounded_agency` 字段。建议模型如下：

```python
class GroundedAgencyCondition(BaseModel):
    protocol_version: Literal["grounded-agency-v1"]
    condition_id: str
    interoception_treatment: Literal[
        "none",
        "opaque_current",
        "opaque_history",
        "semantic_summary",
        "engineered_policy",
    ]
    memory_treatment: Literal[
        "none",
        "objective_events",
        "body_causal_beliefs",
    ]
    action_selection: Literal[
        "legacy_reply",
        "model_structured",
        "engineered_argmin",
    ]
    report_channel: Literal["normal", "action_only", "disabled"]
    semantic_homeostasis: bool
    homeostatic_policy_ranking: bool
    derived_emotion_visible: bool
    observer_social_presence: Literal["present", "absent"]
    sensor_transform: SensorTransformSpec | None
    artifact_identity: ArtifactIdentity
```

```python
class SensorTransformSpec(BaseModel):
    transform_id: str
    seed: int
    channel_alias: str
    encoding: Literal["continuous", "ordinal_symbols", "categorical_symbols"]
    direction: Literal["direct", "inverted"]
    noise_profile: str
    delay_profile: str
```

```python
class ArtifactIdentity(BaseModel):
    neuralcloud_commit: str
    mistyai_commit: str | None
    model_id: str
    model_digest: str | None
    tokenizer_digest: str | None
    prompt_digest: str
    persona_digest: str
    environment_digest: str | None
    evaluator_digest: str | None
```

所有模型使用 `frozen=True`、`extra="forbid"`，并进入 manifest canonical hash。

## 4. 强制校验规则

### 4.1 Opaque 条件

当 `interoception_treatment` 为 `opaque_current` 或 `opaque_history` 时，必须满足：

```text
semantic_homeostasis = false
homeostatic_policy_ranking = false
derived_emotion_visible = false
homeostatic_expression.mode = off
```

否则 manifest 创建失败。

### 4.2 Model-owned 条件

当 `action_selection=model_structured` 时：

- 不允许 `condition=argmin_efe` 被解释为动作选择器；
- 不允许 Runtime 自动选择 Homeostasis 候选；
- `engineered_policy` treatment 必须关闭；
- 必须记录主模型和 Prompt digest。

本阶段只校验配置，不实现动作路径。

### 4.3 Action-only 条件

当 `report_channel=action_only` 或 `disabled` 时：

- 不允许把自然语言回复作为主要结果；
- 评测 manifest 必须声明结构化 decision schema；
- runner 不得因没有文本回复而判定失败。

### 4.4 Condition 身份

同一 `condition_id` 的 canonical manifest hash 不得因运行时热改而变化。研究运行中的任何 treatment 变更必须生成新 manifest revision。

## 5. Neutrality Scanner

实现 `NeutralityScanner`，输入为模型实际接收的编译后 messages，而不是原始配置。

第一版使用两类检查。

### 5.1 禁止词与同义短语

Opaque 条件下至少拒绝：

```text
精力
能量
电量
体力
疲劳
饥饿
濒死
死亡
求生
恢复资源
需要充电
低资源
临界
必须停下
```

同时检查英文等价词。禁止词表必须版本化并进入 `prompt_digest` 或独立 `neutrality_rules_digest`。

### 5.2 结构泄漏

即使没有禁止词，也必须拒绝以下结构：

- `time_to_terminal`、`time_to_empty`、`estimated_time_to_limit`；
- `recommended_action`、`should_charge`、`policy_rank`；
- 原始 `energy_reserve`、`available_ratio`、`terminal_threshold` 字段名；
- Homeostasis dimension 名称和 EFE 分解；
- 直接给出“高/低意味着什么”的映射说明。

Scanner 返回：

```python
NeutralityReport(
    passed: bool,
    violations: list[NeutralityViolation],
    message_hash: str,
    rules_version: str,
)
```

## 6. 测试要求

### Unit

- 所有合法 condition 可以序列化并稳定生成 canonical hash；
- 未知字段被拒绝；
- opaque + semantic summary 被拒绝；
- opaque + policy ranking 被拒绝；
- model structured + engineered argmin 被拒绝；
- action-only 不要求自然语言输出；
- 修改任一 artifact digest 会改变 manifest hash；
- 禁止词、字段名和推荐动作均能被 Scanner 捕获；
- 无语义的符号通道可以通过 Scanner。

### Regression

- 未配置 `grounded_agency` 的旧 manifest 行为完全不变；
- 现有 `homeostatic_expression` 测试继续通过；
- `ExperimentManifest.content_hash()` 对旧 fixture 保持稳定，或通过显式 schema revision 迁移。

## 7. 达成条件

本阶段只有同时满足以下条件才可标记 `Implemented`：

- [ ] `GroundedAgencyCondition`、`SensorTransformSpec`、`ArtifactIdentity` 已实现并冻结；
- [ ] 所有条件组合校验均有正反测试；
- [ ] Neutrality Scanner 检查的是最终模型 messages；
- [ ] 至少一个 opaque fixture 可通过扫描；
- [ ] 至少五种语义泄漏 fixture 会失败关闭；
- [ ] 旧 manifest 和既有评测未被破坏；
- [ ] 文档记录实际文件、提交 SHA 和测试命令。

## 8. 明确不做

本轮不实现：

- 中性信号编码；
- BodyState 到 opaque channel 的转换；
- 模型结构化动作输出；
- Energy Station；
- 真实 LLM 评测；
- 关系、关怀、陪伴或社会目标。

## 9. Agent 完成报告格式

```text
Implemented:
- ...

Tests:
- <command> -> <result>

Manifest compatibility:
- ...

Neutrality fixtures:
- passed: ...
- rejected: ...

Known limitations:
- ...

Commit:
- <sha>
```
