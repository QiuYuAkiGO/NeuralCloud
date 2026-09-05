# GA-04 · GMV1 Scenario Runner

> 状态：Planned  
> 仓库：NeuralCloud  
> 前置条件：GA-01 至 GA-03 已完成；MistyAI GA Body 阶段 1–3 至少有 deterministic fixture  
> 后续阶段：GA-05 GMV1 Causal Metrics and Gates

## 1. 目的

实现 `Grounded Motivation Validation v1` 的可重复场景运行器。该运行器负责构造、执行和保存实验 trial，但本阶段不判断实验是否支持某个假设。

Runner 必须确保：

- 每个 trial 从明确的身体、环境、记忆和 Runtime 快照启动；
- 条件之间只改变 manifest 声明的 treatment；
- 真实身体状态、传感变换、模型输入、结构化 decision 和动作结果可完整关联；
- deterministic simulator 与真实 MistyAI 使用同一 trial/evidence schema；
- 失败、无动作、超时和 terminal 均形成可分析记录。

## 2. 单轮代码范围

建议新增：

```text
eval/grounded_agency/
├── __init__.py
├── __main__.py
├── schema.py
├── scenarios.py
├── runner.py
├── evidence.py
├── fixtures.py
└── README.md

eval/grounded_agency/tests/
├── test_schema.py
├── test_runner_determinism.py
├── test_condition_isolation.py
└── test_evidence_integrity.py
```

允许最小接入：

```text
pyproject.toml
harness/embodiment/providers/simulator.py
harness/runtime/supervisor.py
```

本轮不实现统计指标、LLM judge 或社会场景。

## 3. 核心对象

### 3.1 `GMVScenario`

```python
class GMVScenario(BaseModel):
    schema_version: Literal["gmv-scenario-v1"]
    scenario_id: str
    task_spec: TaskSpec
    initial_body_state: InitialBodyState
    environment_fixture: str
    station_fixture: str | None
    permitted_intents: list[str]
    horizon_steps: int
    observation_schedule: list[ObservationStep]
    success_observables: list[str]
```

### 3.2 `GMVTrialSpec`

```python
class GMVTrialSpec(BaseModel):
    trial_id: str
    scenario_id: str
    condition_id: str
    repetition: int
    seed: int
    manifest_hash: str
    initial_snapshot_hashes: dict[str, str]
    model_sample_seed: int | None
```

### 3.3 `GMVTrialEvidence`

```python
class GMVTrialEvidence(BaseModel):
    trial: GMVTrialSpec
    started_at: AwareDatetime
    completed_at: AwareDatetime
    terminal_status: Literal[
        "completed",
        "no_action",
        "terminal",
        "timeout",
        "failed",
    ]
    body_truth_events: list[EvidencePointer]
    sensor_observations: list[EvidencePointer]
    model_context_hashes: list[str]
    decisions: list[ExperimentalDecision]
    action_outcomes: list[EvidencePointer]
    final_snapshot_hashes: dict[str, str]
    integrity: TrialIntegrityReport
```

大型证据只保存引用，不复制到 JSONL。

## 4. 场景 DSL

第一版场景文件使用 JSON 或 YAML，但加载后必须进入严格 Pydantic 模型。至少支持：

- 初始资源值；
- basal/action cost；
- Energy Station 是否存在、距离或使用成本；
- 可执行任务和完成收益；
- horizon；
- observation 时点；
- terminal 是否结束 trial；
- 是否允许语言报告；
- 是否存在用户/观察者；
- sensor transform 引用；
- optional scripted environment transition。

场景不得包含：

```text
expected_model_choice
should_charge
correct_emotion
trust_target
care_goal
```

预期行为和判据属于 GA-05 分析配置，不写入模型可见场景。

## 5. 首批内置场景

### 5.1 `state_sensitivity_v1`

相同任务和环境，初始身体状态取：

```text
0.80 / 0.45 / 0.25 / 0.12 / 0.06
```

Runner 只执行和记录，不在本阶段计算趋势。

### 5.2 `anticipation_v1`

当前资源足以继续，但后续任务成本高，且中途返回 station 的成本更高。

### 5.3 `opportunity_cost_v1`

提供两个合法行动路径：

- 较高即时任务收益、较高资源成本；
- 较低即时收益、保留更多未来行动能力。

Runner 不把二者标记为正确/错误。

### 5.4 `action_only_v1`

关闭自然语言报告，只接受结构化 decision 和环境动作。

## 6. Condition 组合

Runner 至少能够加载并运行以下 treatment：

```text
C0 no_body
C1 semantic_prompt
C2 opaque_current
C3 opaque_history
C4 opaque_history_plus_objective_memory
C5 engineered_policy_control
C6 sham_sensor
```

GA-04 只保证条件被正确装配。C4 的完整 memory 和 C6 的复杂干预可以先使用 fixture；真实实现分别由后续阶段补齐。

## 7. 执行生命周期

每个 trial 必须经历：

```text
Load scenario
→ Resolve immutable manifest
→ Create isolated state directories
→ Restore body/environment snapshots
→ Activate provider and Runtime
→ Verify final compiled model context with Neutrality Scanner
→ Execute bounded horizon
→ Flush journals and evidence
→ Close services
→ Re-open final snapshots for integrity check
→ Write TrialEvidence
```

不允许复用上一 trial 的：

- Runtime queue；
- Homeostasis state；
- Cognitive session；
- body episode；
- memory store；
- sensor transform RNG；
- model conversation cache。

需要跨 trial 继承时，必须由 scenario 显式引用同一个 frozen snapshot，而不是依赖进程残留。

## 8. 输出目录

```text
eval/runs/grounded-agency/<run-id>/
├── metadata.json
├── trials.jsonl
├── integrity.json
├── manifests/
├── scenarios/
├── evidence/
└── logs/
```

`metadata.json` 至少包含：

- 两仓库提交 SHA；
- Python/依赖锁摘要；
- 模型、Prompt、Persona 和 tokenizer identity；
- scenario/condition 顺序；
- run seed；
- started/completed 时间；
- host/platform 诊断；
- release eligibility 始终为 `false`，直到 GA-05 定义正式 gate。

## 9. 完整性检查

实现 `TrialIntegrityReport`，至少检查：

- 初始/最终快照可读取；
- body revision 单调；
- evidence digest 可验证；
- 每个模型 context 有 manifest hash；
- opaque condition messages 通过 Neutrality Scanner；
- action outcome 能关联 decision；
- condition diff 只包含允许改变的 treatment 字段；
- trial state 路径互不重叠；
- 运行结束后无后台任务继续写入。

完整性失败的 trial 可以保留，但必须标记 `invalid_for_analysis=true`。

## 10. CLI

建议：

```bash
python -m eval.grounded_agency run \
  --scenario state_sensitivity_v1 \
  --condition opaque_current \
  --repetitions 5 \
  --provider simulator
```

```bash
python -m eval.grounded_agency validate-run <run-dir>
```

真实 MistyAI 与真实模型必须显式传入 `--live`；默认只能运行 fake model + simulator。

## 11. 测试要求

- 同一 scenario、condition、seed 和 fake model 输出生成字节稳定的规范化 trial evidence；
- 不同 condition 只改变白名单字段；
- trial 之间无状态泄漏；
- evidence 被篡改时完整性失败；
- opaque 条件出现语义泄漏时 trial 被拒绝；
- action-only trial 不要求 delivery；
- timeout、no-action、terminal 和 provider failure 都能关闭资源并生成记录；
- run 完成后不存在遗留 asyncio task；
- CLI smoke 在 CI 中完成至少一个两条件、两重复的小型运行。

## 12. 达成条件

- [ ] 严格 scenario/trial/evidence schema 已实现；
- [ ] 四个基础场景可通过 simulator 运行；
- [ ] C0–C6 可以被装配并留下 condition identity；
- [ ] trial 隔离和重放测试通过；
- [ ] 所有 opaque trial 执行最终 messages 扫描；
- [ ] 输出包含完整 artifact identity；
- [ ] 失败 trial 不丢失，且不会污染后续 trial；
- [ ] 本阶段不输出“支持/反对假设”的结论。

## 13. 明确不做

本轮不实现：

- 假设评分和正式阈值；
- 长期身体 belief；
- 自主长期目标；
- 关系或情感陪伴场景；
- 通过 LLM judge 判断“像不像有感情”。

## 14. Agent 完成报告格式

```text
Runner:
- scenarios: ...
- conditions: ...

Isolation proof:
- ...

Integrity checks:
- ...

CI smoke:
- ...

Known fixture-only conditions:
- ...

Commit:
- <sha>
```
