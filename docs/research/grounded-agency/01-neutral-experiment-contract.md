# GA-01 · 中性实验契约与条件冻结

> 状态：Implemented
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

- [x] `GroundedAgencyCondition`、`SensorTransformSpec`、`ArtifactIdentity` 已实现并冻结；
- [x] 所有条件组合校验均有正反测试；
- [x] Neutrality Scanner 检查的是最终模型 messages；
- [x] 至少一个 opaque fixture 可通过扫描；
- [x] 至少五种语义泄漏 fixture 会失败关闭；
- [x] 旧 manifest 和既有评测未被破坏；
- [x] 文档记录实际文件、提交 SHA 和测试命令。

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

## 10. GA-01 实现说明

实际文件：

- `harness/research/grounded_agency/manifest.py`：冻结 treatment、传感变换、artifact、评测输出要求与运行条件绑定。
- `harness/research/grounded_agency/neutrality.py`、`hashes.py`：版本化扫描规则、最终 messages 检查与 canonical SHA-256。
- `harness/research/manifest.py`：默认关闭的扩展、跨字段约束、revision API 和旧哈希兼容。
- `harness/compiler/compiler.py`：显式 `validate_final_messages()` 检查边界。
- `tests/research/grounded_agency/`：manifest、扫描器及编译后 messages 集成测试。
- `presets/research/grounded-agency/`：三个完整实验 manifest 和可重新计算 artifact digest 的 `fixtures/contract-inputs-v1.json`。

新增的 `evaluation` 契约声明 `primary_result`、`decision_schema_ref` 和
`requires_text_reply`。`action_only` / `disabled` 必须以 `structured_decision` 为主要结果，
声明 schema 引用且 `requires_text_reply=false`；本阶段不实现或验证 schema 内容。
`model_structured` 同时拒绝 `runtime_auto_select_homeostasis=true`、policy ranking、
`engineered_policy` 以及外层 `condition=argmin_efe`。engineered positive control
显式使用 `engineered_policy`、`engineered_argmin`、ranking 和外层 `argmin_efe`。

运行应先用 `manifest.bind_condition()` 冻结 `(experiment_id, condition_id, revision, hash)`，
之后用 `binding.assert_matches(manifest)` 检查是否漂移。新 treatment 必须通过
`manifest.with_revision(...)` 或完整验证的新 revision 创建，已绑定运行继续拒绝该新 revision。
GA manifest 的 `model_copy(update=...)` 也会重新校验并拒绝未提升 revision 的变更。
此阶段提供校验 API；Runtime 热更新入口及 runner 接入留给后续阶段。

扫描入口接收 `ModelContext.render_messages()` 后、加入所有 continuation/revision 等附加指令后的
最终消息列表。调用方应先执行 `compiler.validate_final_messages(messages, manifest=manifest,
binding=binding)`，再把同一份、未修改的 `messages` 交给模型。测试使用记录输入的 `MockLLM`
核对扫描哈希与实际发送消息哈希一致，并覆盖附加消息泄漏在调用前失败的情况。
Scanner 不读取 provenance、原始配置或 evaluator-only 真值；`compile()` 本身不代替发送前检查。

规则版本和完整规则摘要以 `neutrality_rules_version` / `neutrality_rules_digest` 进入
manifest canonical hash，编译器检查本机规则与声明一致。非 opaque 条件保留语义输入。
未配置扩展或显式设置为 `null` 的旧 manifest 在 canonical JSON 中省略该字段，因此两个既有
runtime preset 的 SHA-256 保持不变，无需提升旧 schema revision。

预设仅用于 GA-01 离线契约验证，`model_id=mock`，artifact 来源为随附 fixture，
NeuralCloud SHA 记录分支创建时的代码基线。GA-02 sensor transform ID 和 GA-03 decision schema
引用仅为预留标识；正式实验必须生成新 revision，绑定真实模型、Prompt、Persona、环境和评估器产物。
这些预设不代表现有 Runtime 已具备 Grounded Agency 行为。

已知限制：第一版扫描为保守的词表与结构规则，不能证明所有自然语言隐含语义均被识别；
当前仅支持可验证的文本消息，不能以通过扫描来声明图像/音频的中性。
本阶段不启动真实模型、MistyAI 或静态 soak，既有稳定化方案的现场 soak 证据需独立维护。

## 11. 验证记录（2026-09-05）

- `python -m pytest -q tests/research/grounded_agency tests/test_homeostatic_expression_manifest.py tests/test_compiler.py`
  → **380 passed**（225 manifest、127 scanner、11 最终 messages 集成、17 既有回归）。
- `python -m ruff check .` → **All checks passed**。
- `git diff --check` → 通过。
- 完整离线回归：`python -m pytest -q -x -m "not integration and not soak" --basetemp .pytest-tmp/ga01-69b4db1432cc4a35942e147e738a8ae9 --junitxml .pytest-tmp/ga01-results.xml --tb=short`
  → **1053 passed, 1 skipped, 3 deselected**，261.14 秒。包含工作区已有的离线评测测试；
  需要外部 PostgreSQL/MistyAI 的 integration/soak 未运行。仅有既有 Starlette TestClient 弃用警告。

Windows 下首次全量运行在所有用例结束后的全局临时目录清理阶段遇到 `WinError 5`，
改用工作区内独立 `--basetemp` 后进程正常退出。复现时需先创建 `.pytest-tmp` 父目录，
并为每次运行选一个新的子目录。JUnit 结果保存在本地忽略目录，不作为研究运行证据发布。

通过的中性 fixture 包括 `q7: ◇`、数字/符号序列、文本 content blocks 和不携带解释的 JSON
工具载荷。失败关闭的 fixture 覆盖中英文语义词、时间估计字段、原始资源字段、推荐动作、
Homeostasis 维度、EFE 分解、高低值含义映射（含 `->` / `=>`）及不可扫描的多模态内容。

旧 runtime preset 的 canonical SHA-256 已用固定断言锁定：

- `development-demo.json`：`19e75310d9f46b270dd3e373795d21ea6b31a645be90253aead1bcee21345e9a`。
- `grounded-mistyai-v1.json`：`4b88816d549be617fd0bedb5f43f263d95880401e89e0f5428c60074d3729b07`。

本次全局 Python 环境的附加 Mypy 检查在读取 NumPy 类型桩时遇到 Python 3.12 `type` 语法
与仓库 `python_version=3.11` 设置不匹配，未完成类型检查；没有为此修改既有工具配置或依赖。

实现提交：`e00029e52023594b6b117dd041eaed27c09efb76`
（`feat(research): freeze grounded agency experiment contracts`）。
本文档状态与提交 SHA 由后续独立文档提交登记，避免提交哈希自引用。
