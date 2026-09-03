# 07 / 08 主分支合并后对齐修改方案

> **方案状态：已批准并执行。** 评审意见由秋语于 2026-08-25 确认；07、08 已按本方案完成对齐修改。
>
> **代码基线：** `sim-homeostasis@e41b71e`，已合入 `origin/master@1229750`。
>
> **目标文件：**
>
> - [07-homeostatic-harness-research-design.md](07-homeostatic-harness-research-design.md)
> - [08-runtime-supervisor.md](08-runtime-supervisor.md)

本文用于先冻结修改边界、术语和跨文档契约。请在每个编号项下勾选处理意见并填写备注；完成评审后，再依据本方案修改 07、08 原文。

---

## 1. 填写方式

每个对齐项都有唯一编号，例如 `A-03`。请在对应位置填写：

- **处理意见**：勾选“接受”“调整”或“暂缓”；
- **修改意见**：填写期望调整的范围、措辞或替代设计；
- **批准人/日期**：需要形成正式变更记录时填写。

未填写的条目默认保持“待确认”，不会在后续修改中自行扩展范围。

---

## 2. 总体修改原则

### P-01 · 区分五种能力状态

07、08 统一使用以下术语，避免把设计文档中的接口称作现有代码能力：

1. **已实现（M2）**：主分支中存在代码和测试的能力；
2. **已有扩展点**：代码中已有 Protocol、构造注入点或稳定 API，但完整后端尚未实现；
3. **设计接口（尚未实现）**：只存在于设计文档中的概念对象或端口；
4. **迁移前置能力**：进入三服务架构前必须新增的持久化、幂等或 revision 能力；
5. **目标架构**：V2 完成后应满足的运行时行为。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

> 

**批准人/日期：**

> 

### P-02 · 保留兼容 API，不承诺零内部改动

`InferencePipeline.respond()` 继续作为现有单次交互兼容入口，但“保持兼容”不解释为“实现 Runtime 时不得修改内部结构”。为满足命令幂等、崩溃恢复和领域 revision，允许：

- 增加 Cognitive service adapter 和持久 command ledger；
- 将推理计算、Session 提交、episodic 写入拆成可控制步骤；
- 为现有模型和 store 增加非破坏性 revision/cursor 元数据；
- 保留旧调用方式，并通过 adapter 映射到新命令契约。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

> 

**批准人/日期：**

> 

### P-03 · 区分开发调试模式与正式研究模式

统一定义两种控制模式：

- **development/debug mode**：允许 GUI 调试端点修改 Persona、Psych、Relationship、Memory 和检索参数；所有变更继续写审计与预设工作副本；
- **study/runtime mode**：以 `ExperimentManifest` 为准，运行中冻结 Persona、profile、模型版本、precision、检索权重和选择条件；禁用或强授权所有可改变实验条件的 GUI 入口。

正式研究模式至少限制：`/api/debug/*`、`/api/config`、preset apply/restore/delete、reset 和角色重命名引发的 Persona 更新。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

> 接受

**批准人/日期：**

> 

### P-04 · 明确四类记录的事实权威性

统一按以下方式描述记录层：

- **Runtime journal**：Runtime 生命周期、全局事件顺序、服务命令、动作和恢复对账的事实来源；
- **Cognitive/Homeostasis domain store**：各自领域状态及 revision 的事实来源；
- **JSONL analysis trace**：阶段、模型、检索和状态解释数据，不用于恢复事件顺序；
- **successful-call CallLog**：一次成功顶层调用的完整调试包；异常调用不保证生成，不能作为恢复 journal。

四者通过 `event_id`、`correlation_id`、`call_id/run_id`、命令幂等键和各领域 revision 建立显式映射，不复用不同语义的 ID。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

> 接受

**批准人/日期：**

> 

---

## 3. 跨文档对齐项

### A-01 · 更新当前能力基线

**当前问题**

- 07 的状态摘要只列出 L/M/S `PsychState`、`RelationshipState`、记忆、检索和对话推理，未反映主分支已完成的九阶段 Pipeline、GUI 持久化、双模型路由、CallLog 和 tier 生命周期；
- 08 将 FastAPI GUI 描述为只管理 Web 生命周期，与当前 `HarnessChatService` 的实际所有权不符。

**07 计划修改**

- 重写文首“文档状态”段；
- 明确当前为 **M2 Cognitive Harness 已实现**；
- 列出与 V2 相关的现有能力：`InferencePipeline.respond()`、Memory/Relationship/Psych/World、Session window、GUI preset/config persistence、JSONL trace、CallLog、memory tier；
- 继续明确 Homeostasis、主动推断、真实心跳、自主目标、Runtime journal 和实验控制面尚未实现。

**08 计划修改**

- 重写文首“文档状态”段；
- 将当前 GUI 定义为“状态化开发外壳和 composition root”：它创建/重建 `MemoryService`、`InferencePipeline` 与 LLM/embedding client，并持久化预设和配置；
- 明确当前 GUI 仍不是独立常驻 Runtime，GUI 进程退出后没有心跳、动作调度或恢复对账。

**验收条件**

- 两份文档对 M2 已实现能力的列举一致；
- 两份文档均不把 GUI 当作目标 Runtime；
- 不声称 V2 能力已经落地。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

> 接受

**批准人/日期：**

> 

### A-02 · 校正 Perception / Action Backend 状态

**当前问题**

07 §1 和 §17 将 `PerceptionBackend.observe() -> Observation` 与 `ActionBackend.act(Goal) -> ActionResult` 都称为现有抽象端口。当前代码实际只有：

```python
PerceptionBackend.observe(*, now: datetime | None = None) -> WorldState
```

`ActionBackend`、`Goal` 和 `ActionResult` 仍是 03 文档中的设计接口，不存在于当前 Python 实现。

**07 计划修改**

- 将“现有的两个抽象端口”改为：
  - 已有扩展点：`PerceptionBackend.observe(...) -> WorldState`；
  - 设计接口：`ActionBackend.act(Goal) -> ActionResult`；
- 说明未来引入统一 `Observation` 时，需要 adapter 将其投影为 Cognitive `WorldState`，并把原始 Observation 交给 Runtime journal/Homeostasis；
- 将 §17 的“现有 Perception/Action Backend 边界不变”改为“保留现有 Perception 扩展点，并新增 Action Backend 目标边界”。

**08 计划修改**

- 在系统上下文和 Action Coordinator 章节中，把 Action Backend 明确标成目标运行时端口，而非当前实现；
- 说明首个 Runtime adapter 可以先接当前 chat-only 输入，数字身体动作端口随后实现，但幂等和验证契约不能省略。

**验收条件**

- 所有“现有接口”都能在当前代码中定位；
- `Observation` 与 `WorldState` 不再混用；
- Action Backend 被明确标为尚未实现。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

> 接受

**批准人/日期：**

> 

### A-03 · 对齐 GUI 所有权与迁移路径

**当前问题**

当前 GUI 不只是观察层：

- `HarnessChatService.rebuild()` 会更换 LLM、embedding、MemoryService 和 Pipeline；
- preset hydrate/flush 会恢复或保存 Persona、Memory、Session transcript；
- `/api/debug/*` 直接修改 Cognitive 状态；
- `/api/config` 可以在运行中切换模型和参数。

目标架构则要求 GUI 只连接 Runtime Control Plane。

**07 计划修改**

- 在研究控制面章节增加 development/debug mode 与 study/runtime mode 隔离；
- 说明现有 GUI debug 写入口不能直接作为研究控制面；
- study/runtime mode 的参数变更必须停止实例、生成新 manifest revision 后重启。

**08 计划修改**

- 在 §2.1、§13 和 §17 增加分阶段迁移：
  1. GUI 保留现有展示，聊天请求改发 Runtime；
  2. preset/config 的权威状态迁入 Cognitive/Runtime；
  3. debug 写请求改为带权限、manifest 与审计信息的 Runtime control command；
  4. GUI 不再创建、销毁或持有唯一 Cognitive 实例；
- 明确迁移完成前，现有 GUI 只用于开发演示，不用于七天正式实验。

**验收条件**

- GUI 当前所有权和目标所有权均被准确描述；
- preset、config、debug 三类迁移对象都有归属；
- 正式研究模式不会绕过 Runtime 或 manifest。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

> 接受

**批准人/日期：**

> 

### A-04 · 固化研究模式下的 Persona 与参数边界

**当前问题**

07 要求 Persona、profile、precision 和实验条件运行中冻结，但当前 in-memory GUI 的 Persona debug patch 会调用 `force_update()` 绕过普通锁，检索权重也可以热修改。数据库触发器本身不足以保证默认 in-memory 研究运行的冻结条件。

**07 计划修改**

- 在 §1.2、§8.2、§11.2 和 §17 明确：
  - Persona 数据库锁是存储层防线，不等同于完整实验控制；
  - in-memory debug bypass 仅限 development/debug mode；
  - study/runtime mode 必须在 API 层拒绝热改，并记录拒绝审计；
  - 批准后的 Persona 重构创建新 `persona_id/version`，通过新 manifest 启动，不覆盖当前版本。

**08 计划修改**

- 在 Control Plane 增加命令分类：diagnostic、debug mutation、manifest-changing operation；
- study/runtime mode 只允许 diagnostic 和显式列入 manifest 的控制命令；
- Runtime 对违规 mutation 记录拒绝事件，不能转发给 Cognitive。

**验收条件**

- 数据库、in-memory store 和 API 三层边界均有说明；
- 调试能力不会被误认为正式研究能力；
- Persona 重构不会修改运行中 Persona。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

> 接受

**批准人/日期：**

> 

### A-05 · 定义 Cognitive 幂等命令与提交边界

**当前问题**

目标 `Respond` 命令要求幂等、可重放和 revision 校验，但现有 `InferencePipeline.respond()` 在一次调用中会：

1. 向 `SessionStore` 追加用户消息；
2. 完成解析、检索、生成和验证；
3. 向 Session 追加角色回复；
4. 按配置自动写入用户和角色两条 episodic；
5. 返回 `FinalResponse`，不返回高层 `Goal`。

若服务在领域写入后、返回 Runtime 前崩溃，简单网络重试可能重复 Session、审计、Curator 或其他副作用。

**推荐契约**

将 Cognitive 命令拆成两个阶段：

```text
PrepareResponse(event, interoception, expected_revision, idempotency_key)
  -> PreparedResponse(reply | goal proposal, read_version_vector, prepare_id)

CommitInteraction(prepare_id, delivery/outcome evidence, expected_revision, idempotency_key)
  -> committed cognitive revision + acknowledged event cursor
```

- `PrepareResponse` 不提交长期记忆；
- `CommitInteraction` 统一提交 Session、episodic 和可同步完成的 Cognitive 写入；
- 同一幂等键返回已提交结果，不重复执行；
- 文本回复只有 Runtime 成功交付后才形成带 outcome/delivery evidence 的正式体验记录；
- 现有 `respond()` 兼容入口可在进程内依次调用 prepare + commit。

**备选契约**

保留单个 `Respond` 命令，但必须在 Cognitive 内部以持久 command ledger 包住所有 Session/Memory 写入，并保证结果缓存与状态提交原子可证明。

**07 计划修改**

- 在 §10.2 服务边界中说明 Homeostasis 只返回 snapshot/policies，Cognitive 的人格选择和记忆提交经 Runtime 命令协调；
- 不在 07 中固定 HTTP payload，但引用 08 的 prepare/commit 或等价幂等边界。

**08 计划修改**

- 重写 §3.11 Cognitive 命令契约和 §3.12 时序；
- 将“Respond / ChoosePolicy / RecordExperience”的副作用边界写清；
- 把 §1.1 的“不修改现有 InferencePipeline”改为“保留兼容入口，不做破坏性删除”。

**验收条件**

- 明确回复生成、交付和记忆提交分别何时发生；
- 能解释服务崩溃在每个窗口中的重试结果；
- 同一命令不会重复推进 Session 或记忆；
- 当前 `respond()` 仍有兼容路径。

**处理意见：**

- [ ] 接受推荐的 prepare + commit
- [ ] 采用单命令 + 持久 command ledger
- [ ] 需要其他方案
- [ ] 暂缓

**修改意见：**

> 接受

**批准人/日期：**

> 

### A-06 · 补齐 Session、领域 revision 与 event cursor

**当前问题**

当前 `SessionStore` 是进程内窗口；`CharacterSnapshot`、`PsychState`、`RelationshipState` 和 `WorldState` 没有统一领域 revision；`MemoryService` 没有 acknowledged event cursor 或命令幂等接口。08 不能将这些描述为“沿用现有 revision 规则”。

**07 计划修改**

- 在 §10.2 说明 Homeostasis revision 是新能力；
- 明确 Cognitive 提供的 interoception/retrieval 输入必须标注读取到的 cognitive 与 homeostasis revision；
- 将当前模型对象与未来服务 envelope 分开：现有 Pydantic 对象可保持领域 payload，revision 放在服务响应 envelope。

**08 计划修改**

- 将以下内容列为 Cognitive service 迁移前置项：
  - 持久化 Session 或建立可从领域事件重建的 Session 投影；
  - 单调 cognitive revision；
  - acknowledged Runtime event cursor；
  - 幂等命令结果索引；
  - snapshot 响应携带 payload revision 与 schema version；
- 修正 §10.1 中“沿用其 evidence 与 revision 规则”为“沿用 evidence 规则，新增服务级 revision/cursor 规则”。

**验收条件**

- current state 与 target envelope 不混淆；
- VersionVector 中每个 revision 都有明确所有者和生成规则；
- Session 可在 Cognitive 重启后恢复或确定性重建。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

> 接受

**批准人/日期：**

> 

### A-07 · 纳管 memory tier 后台任务

**当前问题**

M2 的 `MemoryService` 使用后台线程处理 tier 分类、命中升格和到期审查。这些任务可能在顶层调用返回后继续写 Cognitive 状态。现有 08 的 pause、checkpoint、graceful shutdown 和 recovery 没有覆盖它们。

**07 计划修改**

- 在 §9 说明 memory tier 是 Cognitive 既有机制，不属于 Homeostasis belief；
- 内稳态只能通过状态偏置和 evidence 请求影响检索，不能直接改 tier；
- tier 变更进入 Cognitive revision 和 analysis trace。

**08 计划修改**

- 将 Cognitive background maintenance 纳入受监督生命周期：
  - 每个任务关联触发 event/correlation ID；
  - 实际发生 tier 变更时推进 cognitive revision；
  - `PAUSING` 停止接受新维护任务并有界 drain；
  - `PAUSED` 不运行会改变角色状态的 tier 任务；
  - `STOPPING` 等待、取消或记录未完成任务；
  - 重启时根据 command/maintenance ledger 对账，不依赖线程内 future；
- 明确 Runtime 不直接操作 tier，只监督 Cognitive 是否完成或暂停。

**验收条件**

- 后台写入不会绕过 pause 或 revision；
- shutdown 后不存在未记录的 Cognitive 写任务；
- Homeostasis 与 memory-tier 所有权不混淆。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

> 接受 

**批准人/日期：**

> 

### A-08 · 对齐 trace、CallLog 与 Runtime journal

**当前问题**

当前：

- `JsonlTracer` 逐条追加阶段/操作记录；
- `CallLog` 只在顶层调用成功时写完整 JSON；
- GUI 使用 `run_id` 作为 `call_id`；
- 两者都没有 Runtime `receive_seq`、命令 intent/result 或恢复 checkpoint 语义。

**07 计划修改**

- 在 §11 增加“与 M2 trace 的映射”：
  - 继续复用 `schema_version/model_version/prompt_version` 和阶段 provenance；
  - Homeostasis 新增 A/B/C/precision/coupling/manifest revision；
  - `HomeostaticTransitionTrace` 属于分析轨迹，不替代 Runtime journal；
  - 失败事件必须在 JSONL trace 或 Runtime journal 留痕，不能依赖 success-only CallLog。

**08 计划修改**

- 在 §3.7、§10.1、§13.4 明确四类记录的权威性；
- 建立 ID 映射：

```text
RuntimeEvent.event_id
RuntimeEvent.correlation_id
DomainCommand.idempotency_key
Cognitive/Homeostasis revision
Inference call_id / GUI run_id
Runtime journal receive_seq / process_seq
```

- 明确 CallLog 可作为诊断包附件，但不得参与崩溃恢复判断。

**验收条件**

- 任一推理可从 Runtime event 追到领域命令、状态 revision 和分析调用；
- 任一失败命令不会因为缺少成功 CallLog 而从恢复历史消失；
- 不同 ID 的语义没有混用。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

>  确认

**批准人/日期：**

> 

### A-09 · 区分文本回复与高层 Goal

**当前问题**

08 的 Cognitive `Respond` 可以返回“reply or Goal”，但当前 `InferencePipeline.respond()` 只返回 `FinalResponse`。若不区分类型，Runtime 无法判断结果应直接交付文本，还是进入权限、动作锁和结果验证流程。

**07 计划修改**

- 继续把策略层输出定义为 `PolicyDecision`；
- 主角色模型输出改为类型化 `CharacterDecision`：
  - `ReplyDecision`：候选回复，由 Runtime 交付；
  - `GoalProposal`：高层目标，必须进入 Runtime Action Coordinator；
  - `NoAction/Defer`：拒绝、等待或继续观察；
- `InteroceptiveSummary` 只提供状态和候选，不直接触发动作。

**08 计划修改**

- 在 Cognitive 命令响应中加入判别字段；
- 文本回复也由 Runtime 统一交付，但可以使用低风险快捷路径；
- `GoalProposal` 必须通过 revision、权限、审批、动作锁和验证；
- 当前 `FinalResponse` 通过 adapter 映射为 `ReplyDecision`，不冒充 Goal。

**验收条件**

- 所有 Cognitive 输出都有明确类型；
- 只有 Runtime 能把 Goal 变成外部副作用；
- 当前聊天 API 有无歧义的兼容映射。

**处理意见：**

- [ ] 接受
- [ ] 调整
- [ ] 暂缓

**修改意见：**

> 接受

**批准人/日期：**

> 

---

## 4. 按文件的预计修改位置

### 4.1 `07-homeostatic-harness-research-design.md`

| 位置 | 修改动作 | 对齐项 |
| --- | --- | --- |
| 文首状态说明 | 更新 M2 当前能力和 V2 未实现边界 | A-01 |
| §1 研究定位与边界 | 校正 Perception 签名；Action 标为设计接口 | A-02 |
| §1.2 明确不做的事 | 增加 debug/study mode 边界 | A-03、A-04 |
| §3 总体架构与闭环 | 将 reply/Goal 改为类型化角色决策 | A-09 |
| §7 候选策略与模型裁量 | 明确 PolicyDecision 与 CharacterDecision 的关系 | A-09 |
| §8 自我重构 | 补充 in-memory debug bypass 不适用于 study mode | A-04 |
| §9 记忆、关系与注意反馈 | 纳入现有 memory tier 所有权与 revision 规则 | A-07 |
| §10 概念数据契约 | 增加服务 envelope、revision 和 Cognitive 提交边界引用 | A-05、A-06 |
| §11 Trace 与实验控制 | 映射 JSONL trace、CallLog、domain store、Runtime journal | A-03、A-08 |
| §17 与现有 Harness 的关系 | 按 M2 实际实现重写扩展点和迁移说明 | A-01 至 A-09 |

**对 07 整体修改的意见：**

> 可行

### 4.2 `08-runtime-supervisor.md`

| 位置 | 修改动作 | 对齐项 |
| --- | --- | --- |
| 文首状态说明 | 更正当前 GUI、Pipeline 和持久化所有权 | A-01、A-03 |
| §1.1 首版边界 | “不修改现有接口”改为“保持兼容、不做破坏性删除” | A-05、A-06 |
| §2 系统上下文 | 标注 current topology 与 target topology；Action 为目标接口 | A-02、A-03 |
| §3.5 State Managers | 增加 Cognitive revision/cursor 与后台任务 | A-06、A-07 |
| §3.11 服务命令契约 | 固化 prepare/commit 或批准的替代契约 | A-05、A-09 |
| §3.12 三服务时序 | 加入回复交付、Goal 执行和最终提交时点 | A-05、A-09 |
| §7 并发与最终一致 | 增加 Session、tier 和 command ledger 的提交顺序 | A-05 至 A-07 |
| §9 暂停与恢复 | 冻结并 drain Cognitive 后台写任务 | A-07 |
| §10 Journal/checkpoint | 修正 Cognitive 当前无 revision 的描述；增加四层记录映射 | A-06、A-08 |
| §11 崩溃恢复 | 增加 Cognitive command/maintenance 对账 | A-05 至 A-08 |
| §13 控制面 | 增加 debug/study mode 命令分类和权限 | A-03、A-04 |
| §14 部署 | 明确 GUI 迁移、preset/config 状态迁移和 Cognitive worker 生命周期 | A-03、A-07 |
| §17 与现有 Harness 的关系 | 用分阶段 adapter 迁移计划替代“一层简单包装” | A-01 至 A-09 |

**对 08 整体修改的意见：**

>  可行

---

## 5. 建议的执行顺序

### 阶段 1 · 修正事实与术语

- [x] 执行 A-01：当前能力基线；
- [x] 执行 A-02：Perception/Action 状态；
- [x] 执行 P-01：五类能力标签。

### 阶段 2 · 固化服务与状态契约

- [x] 执行 A-05：Cognitive 幂等提交边界；
- [x] 执行 A-06：Session/revision/cursor；
- [x] 执行 A-09：Reply/Goal 类型。

### 阶段 3 · 纳管现有 M2 副作用

- [x] 执行 A-07：memory tier 后台任务；
- [x] 执行 A-08：trace/CallLog/journal 映射。

### 阶段 4 · 收紧研究与 GUI 边界

- [x] 执行 A-03：GUI 状态所有权迁移；
- [x] 执行 A-04：Persona/参数冻结；
- [x] 执行 P-03：debug/study mode。

### 阶段 5 · 双文档交叉复核

- [x] 07 与 08 对 current/target 状态使用相同术语；
- [x] 07 的所有 Runtime 语义都能回指 08；
- [x] 08 的所有 Homeostasis 语义都能回指 07；
- [x] 所有“现有代码”声明均能定位实现；
- [x] 所有目标接口均标注“尚未实现”或“迁移前置”；
- [x] 不修改 00–06、09 的既有定义；若发现冲突，另列后续任务，不在本次顺带扩写。

**对执行顺序的意见：**

> 可执行

---

## 6. 本次修改范围外

以下事项不随 07、08 文档对齐自动实施：

- 不新增 Runtime、Homeostasis 或 Action Backend 代码；
- 不修改数据库 migration、Persona trigger 或 store schema；
- 不关闭当前 GUI debug API；
- 不实现 command ledger、revision、cursor 或 study mode；
- 不改动 00–06、09 文档；
- 不把本方案视为 V2 实现已获批准。

如需把任一项纳入本轮，请在下方明确填写。

**范围调整意见：**

> 无意见

---

## 7. 最终批准

### 7.1 未决问题

请列出仍需讨论、实验或原型验证的问题：

1. 
2. 
3. 

### 7.2 批准状态

- [ ] 按本方案修改 07、08
- [ ] 按已填写意见修改后再执行
- [ ] 暂不修改

**最终意见：**

> 可执行

**批准人/日期：**

> 秋语/2026-8-25

---

## 8. 执行记录

**执行日期：** 2026-08-25

**已修改：**

- `07-homeostatic-harness-research-design.md`：完成 A-01 至 A-09 中属于 Homeostatic Harness 的当前能力、接口状态、研究模式、角色决策、memory tier、服务 envelope 和记录层对齐；
- `08-runtime-supervisor.md`：完成 GUI 所有权、Cognitive prepare/commit、revision/cursor、后台 maintenance、控制模式、记录层、部署迁移及恢复验收对齐。

**范围保持：**

- 未修改 00–06、09；
- 未修改代码、数据库 migration、Persona trigger、GUI API 或部署脚本；
- 本次只更新设计与迁移契约，不表示 V2 能力已经实现。

**验证：**

- Markdown code fence 数量成对；
- 两份文档的本地相对链接均可解析；
- 旧的 GUI/接口/revision 失实表述已清除；
- `git diff --check` 通过。
