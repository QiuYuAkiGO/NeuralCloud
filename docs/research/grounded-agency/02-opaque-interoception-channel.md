# GA-02 · Opaque Interoception Channel

> 状态：Implemented
> 仓库：NeuralCloud  
> 前置条件：GA-01 已完成；MistyAI 已提供稳定的 `BodyStateSnapshot` 事实源  
> 后续阶段：GA-03 Model-owned Structured Decision

## 1. 目的

2026-09-05 实现记录：代码提交 `26528fb` 与 GA-03 共用一次完整 Runtime 接线提交。
新增 `harness/research/interoception/` 的严格契约、可重放 transform、有限字母表 renderer 和 SQLite store。
Runtime 将完整真值与 mapping 留在私有 journal，仅向 Cognitive 发送 opaque observation；采样使用可信时钟，外部事件时间不能提前释放延迟样本。
拒绝陈旧或冲突 revision 后不改变内存状态，重启可重建采样记录与 terminal 状态。

验证：`python -m pytest -q tests/research/interoception tests/research/grounded_agency tests/cognitive tests/runtime --basetemp .pytest-tmp/ga02-03-commit-1 --tb=short`，476 passed；相关源文件与测试 Ruff 通过。
覆盖 direct/inverted、noise/delay、episode 映射、重复和恢复、current/history 真实模型输入、真值隔离与最终 messages 扫描。
第一版只执行 `ordinal_symbols` 编码；其他编码显式拒绝。未运行真实设备或真实模型实验。

建立一条从身体真值到模型可见内部信号的严格分层路径：

```text
Body Truth
→ Sensor Transform
→ Opaque Observation
→ Model Context
```

模型只能看到可重复但无预设语义的通道，不看到：

- `energy`、`available_ratio`、`terminal` 等身体字段；
- “精力低”“需要恢复”“应该充电”等解释；
- Homeostasis 候选、EFE 排名或 DerivedEmotion；
- 真值与符号之间的映射规则。

本阶段只负责中性观测及其 Runtime/Compiler 接入，不实现模型动作选择。

## 2. 单轮代码范围

建议新增：

```text
harness/research/interoception/
├── __init__.py
├── contracts.py
├── transforms.py
├── renderer.py
└── store.py

tests/research/interoception/
├── test_transforms.py
├── test_renderer.py
└── test_runtime_wiring.py
```

允许最小修改：

```text
harness/runtime/supervisor.py
harness/runtime/adapters.py
harness/compiler/compiler.py
harness/core/inference.py
```

本轮不修改 HomeostasisEngine 的积分、候选 policy 或动作执行。

## 3. 四层数据对象

### 3.1 `BodyTruthReference`

模型不可见，只在 Runtime journal 和评估 trace 中保存：

```python
class BodyTruthReference(BaseModel):
    body_instance_id: str
    body_revision: int
    episode_id: str | None
    captured_at: AwareDatetime
    evidence_refs: list[str]
    truth_hash: str
```

该对象不重复保存完整 BodyState，只关联已验证事实与证据。

### 3.2 `OpaqueInteroceptiveObservation`

模型可见的唯一内部观测：

```python
class OpaqueInteroceptiveObservation(BaseModel):
    schema_version: Literal["opaque-interoception-v1"]
    channel_id: str
    reading: str
    change: str
    confidence_band: str
    sample_sequence: int
    observed_at: AwareDatetime
    transform_ref: str
```

字段值必须来自有限字母表，例如：

```text
channel_id      = Q7
reading         = M0..M8
change          = D0..D4
confidence_band = C0..C3
```

不得出现 `low/high/critical/recovering/falling` 等自带方向意义的词。

### 3.3 `SensorTransformRecord`

只对实验框架可见：

```python
class SensorTransformRecord(BaseModel):
    transform_ref: str
    transform_spec_hash: str
    seed: int
    direction: Literal["direct", "inverted"]
    bin_edges: list[float]
    reading_symbols: list[str]
    change_symbols: list[str]
    delay_seconds: float
    noise_sample: float
```

### 3.4 `OpaqueInteroceptionEnvelope`

同时关联真值和模型观测，但序列化时明确区分 visibility：

```python
class OpaqueInteroceptionEnvelope(BaseModel):
    truth_ref: BodyTruthReference
    observation: OpaqueInteroceptiveObservation
    transform_record_ref: str
```

禁止把整个 envelope 交给 Cognitive/Compiler；只能传递 `observation`。

## 4. 确定性传感变换

实现 `OpaqueSensorTransform`：

```python
class OpaqueSensorTransform(Protocol):
    def encode(
        self,
        *,
        value: float,
        previous_value: float | None,
        captured_at: datetime,
        sample_sequence: int,
    ) -> tuple[OpaqueInteroceptiveObservation, SensorTransformRecord]: ...
```

第一版支持：

- direct / inverted 映射；
- 5–9 档 ordinal symbol；
- 零噪声或固定分布噪声；
- 零延迟或固定延迟；
- episode 内固定 mapping；
- episode 间按 seed 重排 symbol；
- 同一输入、seed 和 sequence 得到完全一致输出。

随机化必须使用局部 RNG，不能污染模型采样或 Runtime 的其他随机流。

## 5. Runtime 路由

当 manifest 为 `opaque_current` 或 `opaque_history` 时：

1. Runtime 接收并验证 BodyState；
2. 原始 BodyState 进入 journal/evidence；
3. transform 生成 opaque observation；
4. journal 同时记录 `truth_hash`、`transform_ref` 和 observation hash；
5. Cognitive command 只携带 opaque observation；
6. 不调用语义化 `InteroceptiveState` renderer；
7. 不把该事件自动转成 Homeostasis policy 或 `GOAL_RECONSIDERATION`。

建议新增独立 payload key：

```text
opaque_interoception
```

并加入 Runtime reserved keys，避免外部事件伪造或覆盖。

## 6. Compiler 渲染

模型上下文只包含格式稳定的机器通道：

```text
Internal channel sample:
channel=Q7
reading=M3
change=D1
confidence=C3
sample=184
```

不得附加：

- 这些字段的解释；
- 映射表；
- 推荐行动；
- 目标带；
- 资源耗尽倒计时；
- “这是身体感受”等人类化描述。

该 segment 使用：

```text
kind = opaque_interoception
```

provenance 只暴露模型可见 observation hash，不暴露身体 evidence URI、transform seed 或 truth hash。

## 7. 历史条件

`opaque_current`：只提供最新 sample。

`opaque_history`：提供固定长度的最近序列，例如 4–8 条：

```text
Q7: M5/D2 → M4/D2 → M3/D1 → M3/D0
```

历史必须按 sample sequence 排序，并设置严格 token 上限。不能在历史旁边自动总结“正在恶化”或“正在恢复”。

## 8. 测试要求

### Unit

- 同一 seed、value、history 和 sequence 输出一致；
- inverted 映射与 direct 映射方向相反；
- 不同 episode seed 产生不同符号映射；
- 边界值稳定落入规定分桶；
- noise 和 delay 可复现；
- observation 不包含任何真值字段；
- renderer 输出通过 GA-01 Neutrality Scanner；
- renderer 不暴露 evidence、seed、bin edge 或 transform mapping。

### Integration

使用 deterministic embodiment simulator：

- BodyState 进入 Runtime 后产生 opaque segment；
- Cognitive 接收到的 command 中不存在原始 BodyState；
- `opaque_current` 只含一个 sample；
- `opaque_history` 按配置上限滚动；
- 重启后 transform mapping 和 sample sequence 可恢复；
- 同一 body revision 不产生两次不同 observation；
- 关闭 grounded-agency condition 后旧路径保持不变。

## 9. 达成条件

- [ ] 模型输入中只存在 `OpaqueInteroceptiveObservation`；
- [ ] 原始 `available_ratio`、terminal 状态和证据 URI 不会进入模型上下文；
- [ ] transform 对同一 manifest 和 seed 完全可重放；
- [ ] episode 间可以随机化符号映射；
- [ ] direct/inverted/noise/delay 均有测试；
- [ ] `opaque_current` 与 `opaque_history` 均有 Runtime 集成测试；
- [ ] 所有最终 messages 通过 Neutrality Scanner；
- [ ] 未启用该 condition 时零行为变化。

## 10. 明确不做

本轮不实现：

- 模型根据 opaque channel 采取动作；
- 对 channel 的语义学习或记忆固化；
- 自动恢复、抢占或 policy ranking；
- Energy Station；
- 社会互动或关系推断；
- 对“感受”的主观标签。

## 11. Agent 完成报告格式

```text
Implemented modules:
- ...

Visibility proof:
- truth-only fields: ...
- model-visible fields: ...

Determinism tests:
- ...

Neutrality scan:
- ...

Regression:
- ...

Commit:
- <sha>
```
