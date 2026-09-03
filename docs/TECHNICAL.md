# NeuralCloud Harness 技术文档

> **项目**：NeuralCloud Harness V1（`neuralcloud-harness` v0.1.0）
> **定位**：长期运行的角色智能体运行时（Character Cognitive Runtime）——为角色后训练模型提供记忆、状态、关系、语境与决策支撑的认知 Harness。
> **读者**：需要理解、使用或扩展本代码库的工程师。设计论证见 `docs/harness/` 文档集（00–06），本文聚焦**已落地代码**的架构、数据流与实现细节。

---

## 目录

1. [项目概述](#1-项目概述)
2. [总体架构](#2-总体架构)
3. [核心设计原则在代码中的体现](#3-核心设计原则在代码中的体现)
4. [核心数据结构](#4-核心数据结构)
5. [在线推理流水线（九阶段）](#5-在线推理流水线九阶段)
6. [记忆子系统](#6-记忆子系统)
7. [存储层](#7-存储层)
8. [LLM 与 Embedding 层](#8-llm-与-embedding-层)
9. [可视化 GUI](#9-可视化-gui)
10. [配置体系](#10-配置体系)
11. [Trace 与审计](#11-trace-与审计)
12. [评测体系](#12-评测体系)
13. [测试体系](#13-测试体系)
14. [部署与运行](#14-部署与运行)
15. [扩展点与接口约定](#15-扩展点与接口约定)
16. [已知边界与路线图](#16-已知边界与路线图)

---

## 1. 项目概述

NeuralCloud 要构建的不是 chatbot，而是 **Character Runtime**：一个能在隔离数字环境中长期存在、积累经验、保持人格一致性的角色智能体。整个项目规划由两个解耦子系统构成：

| 子系统 | 职责 | V1 状态 |
| --- | --- | --- |
| **A. Character Cognitive Harness**（认知 Harness） | 角色"基于什么现实进行思考"：记忆、状态、检索、语境编译、推理编排 | **已实现，即本仓库主体** |
| **B. Linux Desktop Digital Body**（数字身体） | 角色的"第一副身体"：桌面观察与行动 | 仅预留 `PerceptionBackend` 接口，未实现 |

**核心命题**：Harness 的重要性不低于模型本身。Memory-Driven Role-Playing 研究将角色行为拆为 Anchoring（锚定身份）→ Selecting（选择记忆）→ Bounding（界定边界）→ Enacting（生成行为）四阶段；Harness 替模型完成前三项，让主模型只做最有价值的"演出"。结构化记忆管理可以让 8B 级模型逼近大模型的长期角色表现。

**V1 已交付能力**：

- 六层记忆/状态体系（Persona Core 锁定层 / Episodic / Semantic / Relationship / Psych L·M·S / World State）
- Dual Memory（客观事实 + 角色视角解读分离）
- Episodic 生命周期分层（short / mid / long / forgotten，uniqueness TTL + 命中升格 + 到期小模型审查）
- Memory Writer 候选-证据链管线（候选 → 证据累积 → 置信度更新 → 语义固化）
- 六信号加权混合检索 + 反向检索（反证发现）
- 在线推理九阶段链路 + Fast/Deep Path 双路分流
- 门控 Social Interpreter（结构化判断，绝不替角色说话）
- Context Compiler（token 预算管理 + provenance 追踪）
- Response Validator（人格红线 + 局部重生成 + 空记忆防捏造）
- 连说循环（continuation，`[CONTINUE]` / `[END]` 提议 + 近重复截断）
- FastAPI + WebSocket 三栏可视化 GUI（阶段级实时推送 + 调试面板）
- JSONL trace + 成功调用全链路 JSON（`traces/calls/`）+ `memory_ops_log` 审计全链路 provenance
- PostgreSQL + pgvector 可选持久化（Alembic 迁移、人格锁 DB 触发器）

**V1 明确不做**：token 流式生成 / TTS、数字身体真实采集、Experience Evaluator、多 Agent 自由讨论、公网访问、AI 自主修改 Persona Core、边运行边训练。

---

## 2. 总体架构

### 2.1 代码仓库布局

```text
harness/
  core/           # 数据模型（Pydantic）：models.py（记忆侧）+ inference.py（推理侧）+ ids/exceptions
  config.py       # Settings（pydantic-settings，环境变量前缀 NC_）
  memory/         # 记忆子系统
    service.py    #   MemoryService 门面：record_event / query / get_state / curate
    dual.py       #   DualMemoryWriter：客观事实 + 角色视角解读
    tier.py       #   Episodic 生命周期：uniqueness TTL、命中升格、到期审查
    perception.py #   PerceptionBackend 协议 + ChatPerceptionBackend 桩
    writer/       #   Writer 管线：pipeline.py（候选固化）+ curator.py + confidence.py
    retrieval/    #   检索：planner.py（检索计划）+ signals.py（六信号）+ hybrid.py + conflict.py（反证）
    store/        #   存储抽象：protocols.py（Store Protocol）+ inmemory.py + postgres.py + schema.py
  pipeline/       # 推理链路：gateway → parser → state_builder → (continuation) → pipeline 编排
  router/gate.py  # Complexity Gate：Fast/Deep 双路判定（regex + Gate 小模型，OR 合并）
  subagents/      # Social Interpreter（门控子代理）
  compiler/       # Context Compiler：预算化高密度 ModelContext
  validator/      # Response Validator：人格红线校验 + 局部重生成指令
  llm/            # LLM 抽象：client.py（OpenAI 兼容 + Mock + 双端点路由）+ character.py + embeddings.py
infra/
  db/             # docker-compose（PostgreSQL+pgvector）+ Alembic 迁移
  llm/            # Modelfile.character（Ollama 主模型：num_ctx=16384）
  trace/          # JsonlTracer / NullTracer / 成功调用 CallLog
gui/              # FastAPI + WebSocket 可视化：server / service / observer / debug / persist / presets / demo_seed + static/
  runtime_server.py # Runtime 控制面的无状态 GUI facade
harness/homeostasis/ # 七维 belief、积分、情感、策略、profile revision 与文件持久化
harness/cognitive/   # Cognitive 独立服务 composition root
presets/builtin/  # 出厂角色模板（Vaelith 日常版为默认，原设定版保留）
eval/             # 场景评测 + 检索延迟基线 + 角色扮演基准（CharacterEval / RoleEval / TimeChara）
tests/            # 单元与集成测试（pytest，全量不依赖 GPU/DB）
docs/             # TECHNICAL.md（实现手册）+ harness/ 设计文档集 00–06
start.ps1 / stop.ps1 # Windows 四进程编排；start.sh / stop.sh 保留单进程兼容路径
```

### 2.2 模块协作总图

```text
                        ┌─────────────────────────────────────────────┐
                        │              InferencePipeline               │
                        │  (harness/pipeline/pipeline.py, 九阶段编排)  │
                        └─────────────────────────────────────────────┘
  Raw text / InteractionEvent
        │
        ▼
  Gateway ──► Context Parser ──► SituationStateBuilder ──► Complexity Gate
  (事件化)     (结构化假设)        ├─ MemoryService.get_state   (regex + Gate LLM
                                  └─ MemoryService.query        双路 OR 判定)
        │                                                              │
        │                                            fast ▼            ▼ deep
        │                                            (直通)     Social Interpreter
        │                                                              │
        ▼                                                              ▼
  Context Compiler ◄─────────────── StructuredInsight (约束红线)
        │  ModelContext（预算 3000 token，分段 + provenance）
        ▼
  Character Model ──► CandidateResponse（段尾 [CONTINUE]/[END]）
        │
        ▼
  Response Validator（Deep 全量 / Fast 采样 0.2；违规 → 局部重生成 ≤ max_revisions）
        │
        ▼
  FinalResponse ──► MemoryService.record_event 写回（用户轮 + 角色轮各一条 episodic）
```

**关键纪律**：模块之间只传结构化 Pydantic 对象，不传自然语言小作文。`SituationState` 是 Harness 内部 canonical state（机器状态，不是 prompt）；`ModelContext` 才是编译后的模型输入。

### 2.3 依赖注入与可替换性

`InferencePipeline` 与 `MemoryService` 的所有协作者（gateway / parser / gate / interpreter / compiler / character / validator / llm / embeddings / curator / tracer / perception / stores）都通过构造函数注入，默认从 `Settings` 与 `MemoryService` 派生。测试可逐个替换为规则实现或 Mock；存储层通过 `MemoryStores` Protocol 抽象，in-memory 与 PostgreSQL 可互换且业务代码零改动。

---

## 3. 核心设计原则在代码中的体现

设计文档 00 提出十条原则，其在代码中的落点：

| # | 原则 | 代码体现 |
| --- | --- | --- |
| 1 | 模型负责判断，程序负责现实 | 时间窗、TTL、recency 衰减、uniqueness 打分、证据链校验全部是确定性代码；LLM 只做语义推断 |
| 2 | 不让主模型自己记世界 | 六层状态由 Harness 维护并编译进 `ModelContext`，主模型只看到编译结果 |
| 3 | 信息密度优先于长度 | `ContextCompiler` 按 3000 token 预算分段裁剪，检索 50 条候选只给模型 5 条 |
| 4 | Fast/Deep 分离 | `ComplexityGate` 双路判定（regex 打分 + Gate 小模型，OR 合并，宁深勿浅）；Fast Path 不触发 subagent，Validator 采样 0.2 |
| 5 | Persona Core 锁定 | 服务层 `update_persona`/`delete_persona` 抛 `PersonaLockedError` 并写审计；PG 侧还有 `BEFORE UPDATE OR DELETE` 触发器双保险 |
| 6 | 记忆是事件且带证据链 | `EpisodicEvent.evidence`、`SemanticFact.supporting_events`、`CandidateMemory.evidence` 均为 Pydantic 强制非空；缺失抛 `EvidenceRequiredError` |
| 7 | Subagent 门控触发 | 仅 Deep Path 调用 Social Interpreter；输出 `StructuredInsight`（含 `constraint` 红线），绝不生成角色台词 |
| 8 | 可积累经验，不可自定真理 | 长期语义只能走 `候选 → 证据累积 → 置信度 → 固化`；Curator 直接 `ADD semantic` 会被管线拒绝 |
| 9 | 一切可溯源 | 每个阶段落 JSONL trace（带 `schema_version`/`model_version`/`prompt_version`）；成功调用另落一份全链路 JSON；所有写操作入 `memory_ops_log`；`ModelContext.provenance` 记录每段来源 |
| 10 | Harness 可以生长 | Store Protocol、PerceptionBackend、CuratorBackend、Tracer 等接口预留替换点；schema 版本号随 trace 落盘 |

---

## 4. 核心数据结构

全部数据模型位于 `harness/core/models.py`（记忆侧）与 `harness/core/inference.py`（推理侧），基于 Pydantic v2，数值字段带范围校验。

### 4.1 记忆侧模型（`core/models.py`）

| 模型 | 说明 | 关键字段 |
| --- | --- | --- |
| `InteractionEvent` | 进入系统的原始事件（"输入不是字符串，是事件"） | `event_id` / `timestamp` / `channel` / `speaker` / `raw_text` / `session_id` / `participants`（自动推导）/ `evidence` / `uniqueness` |
| `PersonaCore` / `PersonaIdentity` | 角色本体，LOCKED | `name` / `core_traits` / `values` / `speech_style` / `knowledge_boundary` / `locked=True` |
| `EpisodicEvent` | 情景记忆（事件，非摘要） | `summary` / `objective_fact` / `persona_conditioned_insight`（Dual）/ `importance` / `emotional_valence` / `evidence`（强制非空）/ `tier` / `uniqueness` / `expire_at` / `hit_count` / `embedding` |
| `DualMemoryRecord` | Dual Memory 视图 | `objective_fact` + `persona_conditioned_insight` + `insight_confidence` + `supporting_evidence` |
| `SemanticFact` | 语义记忆（已固化的认识） | `subject` / `predicate` / `value` / `confidence` / `supporting_events`（强制非空）|
| `RelationshipState` | 关系状态（独立层） | `closeness` / `trust` / `attachment` / `recent_tension`（均 0–1）+ `apply_delta` 增量并 clamp |
| `PsychState` | 心理状态三层 | `long` / `mid` / `short` 三个自由 dict；short 带 `ttl_seconds` / `set_at` |
| `WorldState` | 世界状态 | `time` / `active_application` / `open_windows` / `user_presence` / `user_activity` |
| `CandidateMemory` | 候选记忆 | `claim` / `confidence` / `status`(candidate/consolidated/dropped) / `evidence`（≥1）/ `subject`/`predicate`/`value` 三元组 |
| `RetrievalQuery` / `RetrievalPlan` | 检索输入与计划 | `text` / `entities` / `current_inference`；计划含 `time_scope` / `event_type` / `channels` |
| `ScoredMemory` / `ScoreBreakdown` | 检索结果 | `relevance` / `why_retrieved`（可解释标签）/ 六信号分数分解 |
| `CuratorOp` / `CuratorOutput` | Curator 结构化操作 | `ADD` / `UPDATE` / `DELETE` / `NO_OP` |
| `CharacterSnapshot` | 状态聚合快照 | persona + relationship + psych + world + `short_psych_expired` |
| `ProvenanceRecord` | 溯源记录 | model/prompt/schema 版本 + 时间戳 |

### 4.2 推理侧模型（`core/inference.py`）

| 模型 | 说明 |
| --- | --- |
| `ParsedInteraction` | Parser 输出：`speech_act` / `references` / `possible_social_intent`（hypothesis+confidence）/ `emotion_cues` / `gate_vote`（parser 的 LLM 调用兼作 Gate 第二裁判，省一次调用） |
| `SituationState` | **全 Harness 最核心的中间对象**：trigger / utterance / parsed / actor / relationship / world / `relevant_memories`（最多陈列 5 条） / `hypotheses` / `complexity` / `snapshot` |
| `ComplexitySignals` / `ComplexityAssessment` | 复杂度信号（时间范围、承诺词、记忆探针、价值冲突、人名、未知名词、情绪强度等）与评估结论 |
| `InferenceRequest` | Gate 决策：`path`(fast/deep) / `score` / `reasons` / `trigger_subagents` |
| `StructuredInsight` | Social Interpreter 输出：`possible_intents` / `recommended_stance` / `constraint`（红线） |
| `ModelContext` / `ContextSegment` / `ContextBudget` / `ProvenanceEntry` | 编译后模型输入：分段（persona/constraint/user/psych/relationship/world/insight/memory/session）+ 预算 + 溯源；`render_messages()` 渲染为 system+user 两条消息 |
| `CandidateResponse` / `ValidationResult` / `FinalResponse` | 生成与校验结果；`FinalResponse` 带 `path` / `revisions` / `segments` / `stopped_by` / `usage` |
| `TokenUsage` | token 计量（api / estimate / mixed 三源），支持跨调用累加 |
| `SessionTurn` | 会话窗条目（`SessionStore` 维护最近 N 轮） |

---

## 5. 在线推理流水线（九阶段）

入口：`InferencePipeline.respond(source, ...)`（`harness/pipeline/pipeline.py`）。每个阶段通过 `_traced()` 包装：计时 → 执行 → 落 trace → 通知 observer（GUI 阶段推送的数据源）。

### 阶段 1 · Gateway（`pipeline/gateway.py`）

把原始输入（字符串或 `InteractionEvent`）标准化为 `InteractionEvent`：补 `event_id`、UTC 时间戳、channel/speaker/modality、`session_id`。从第一步起系统处理的就是事件而非字符串。用户消息同时追加进 `SessionStore`（近期对话窗口，默认 10 轮）。

### 阶段 2 · Context Parser（`pipeline/parser.py`）

输出 `ParsedInteraction`：**只给假设与置信度，不裁决语义**（"不宣布用户在撒娇，只输出 `seeking_reassurance: 0.58`"）。

- **规则优先**：`rule_parse` 用 regex 识别问候/疑问/时间词/承诺词/情绪词，零成本产出 baseline。
- **LLM 按需**：`_llm_needed` 判定（有引用/意图/情绪线索，或非问候且长度 >12 字符）才调用 `complete_json`；失败回退 baseline。可用 `NC_PARSER_LLM_ENABLED=false` 强制纯规则。
- **一票两用**：parser 的 LLM prompt 同时要求输出 `gate.path`，该 `gate_vote` 被 Complexity Gate 直接复用为第二裁判，省去一次 LLM 调用。

### 阶段 3–5 · 并行状态查询 + 检索 + SituationState（`pipeline/state_builder.py`）

`SituationStateBuilder` 用常驻 `ThreadPoolExecutor(2)` **并行**执行两路（响应速度优化的关键）：

- `MemoryService.get_state()` → `CharacterSnapshot`（persona / relationship / psych（含 short TTL 读路径失效判定）/ world）
- `MemoryService.query()` → `list[ScoredMemory]`（见 §6.5 混合检索）

汇合后构建 `SituationState`——canonical state，是机器内部状态，**还不是 prompt**。`relevant_memories` 最多陈列 5 条（检索可返回更多，入态时截断）。

### 阶段 6 · Complexity Gate（`router/gate.py`）

决定整个请求的延迟与成本结构。采用**双路独立判定、OR 合并（宁深勿浅）**：

1. **规则路**：`collect_signals` 提取 11 种信号 → `score_complexity` 加权打分（价值冲突 +0.82、人名/未知名词 +0.30、远时间 +0.30、承诺 +0.22、记忆探针 +0.18……）；`_force_deep` 硬性规则命中（人名、未知名词、价值冲突、`three_months`/`last_month` 远程时间、承诺+时间/探针组合）直接判 deep。`score >= complexity_threshold`（默认 0.5）亦判 deep。
2. **模型路**：仅当规则路判 fast 且不是纯问候时，咨询 Gate 小模型（复用 parser 的 `gate_vote`，否则独立 `complete_json`）。其 deep 裁决一定生效（`llm_decided:deep`）；fast 裁决需与规则路一致才成立。MockLLM 或调用异常时回退规则单路。
3. 纯问候（greeting 且无任何其他信号）完全不走 LLM。

输出 `InferenceRequest(path=fast|deep)`。Deep Path 自动 `trigger_subagents=["social_interpreter"]`。

### 阶段 6.5 · Social Interpreter（仅 Deep，`subagents/interpreter.py`）

门控子代理：输入完整 `SituationState`（含最多 5 条检索记忆的完整事实+角色视角），输出 `StructuredInsight`：意图假设列表、预期关系变化、建议姿态，以及**红线约束 `avoid_explicit_psychological_explanation`**——subagent 可以知道"用户在寻求安全感"，但角色绝不能说出"我检测到你在寻求关系确认"，否则角色扮演直接死亡。LLM 失败回退 `rule_interpret`（规则表覆盖贯穿案例、价值困境、记忆核查三类）。

### 阶段 7 · Context Compiler（`compiler/compiler.py`）

把几百条记忆、Persona Core、各层状态、subagent 推理**按预算编译**成高密度 `ModelContext`（默认 3000 token）。分段按固定优先级入栈，每段有 preferred 上限（二分裁剪）：

| 顺序 | 段 | preferred | 说明 |
| --- | --- | --- | --- |
| 1 | `persona` | 400 | 人格锚点（特质/价值观/表达风格 + "只表演这个人，不要解释心理机制"） |
| 2 | `constraint` | 80 | 红线约束（subagent constraint + 空记忆时的 `no_fabricated_memory` 防捏造红线） |
| 3 | `user` | 200 | 触发话语 |
| 4 | `psych` | 150 | 当前心理 short 层 |
| 5 | `relationship` | 150 | 关系状态数值 |
| 6 | `world` | 80 | 世界状态 |
| 7 | `insight` ×n | 200 | 社交判断（意图+姿态+约束） |
| 8 | `memory` ×≤5 | 220 | 最相关记忆（事实 + 角色视角解读 + relevance） |
| 9 | `session` | 400 | 近期对话窗口 |

token 估算用 CJK 友好启发式 `(len+1)//2`。每段记录 `source_ids` 与 `why`，汇总为 `provenance`——事后可回答"哪条记忆把角色带偏了"。

### 阶段 8 · Character Model + 连说循环（`llm/character.py` + `pipeline/continuation.py`）

主模型唯一负责"演出"：persona-conditioned decision + generation。`ModelContext.render_messages()` 把所有非 user 段拼为 system，user 段拼为 user，再视情况追加连说协议指令、已说段落（`assistant` 历史）、续说 nudge / 修正指令。

**连说循环**（`continuation_enabled`，默认开启）：模型每段末尾标 `[CONTINUE]`（提议还有话说）或 `[END]`（说完，无标记默认收尾防死循环）。CONTINUE 只是提议。Harness 负责循环、硬性兜底与放行：新段与已提交段近重复则丢弃并停（`stopped_by=duplicate`）；`max_reply_segments`（默认 3）强制收尾；正文为空立即停止。续写轮把已提交正文作为 `assistant` 消息，nudge 不再引用上一段全文。每段独立过 Validator；多段在会话窗各成一条气泡；写入长期记忆时角色侧仍拼为**一条** episodic 避免碎片化。这符合"模型负责判断（是否继续），程序负责现实（是否新话、循环与上限）"。

### 阶段 9 · Response Validator（`validator/validator.py`）

生成后不立即发送，对照人格红线校验：

- **规则层**：按 Persona 特质动态启用检测——"不直接表达关心" → 检测直球关心词；"高自尊/不轻易道歉" → 检测道歉词；存在 `avoid_explicit_psychological_explanation` 约束 → 检测心理分析泄漏；`memory_empty`（检索为空）→ 激活 `fabricated_memory` 检测（"你上次说过/我们以前"等捏造共同过去）。
- **LLM 层**（可选）：规则判 pass 后再由 critic 模型复核，判 revise 则合并违规项。
- **运行策略**：Deep Path 全量运行；Fast Path 按 `validator_sample_rate`（默认 0.2）采样运行，避免延迟翻倍。
- **修正策略**：判 `revise` 则附 `instruction`（如"保持嘴硬的表达外壳，关心只能通过行动或迂回措辞体现"）做**局部重生成**（不重跑整个 pipeline），上限 `max_revisions`（默认 2）。

### 写回（`pipeline._record`）

`record_interaction=true` 时：用户消息与角色回复各写一条 episodic。用户轮走完整管线（Dual Memory 解读 + Curator + tier 打标）；角色轮不生成 insight、不走 Curator，证据链为 `chat:{session}:{response_id}`。

### Token 计量

每次生成调用记录 `TokenUsage`（API 返回优先，缺失用估算），跨续说段与重生成累加，`FinalResponse.usage` 含总 prompt/completion/calls/revision_tokens，trace 与 GUI 阶段推送均可见。

---

## 6. 记忆子系统

入口门面：`harness.memory.service.MemoryService`（`build_inmemory_service()` 为便捷工厂）。可脱离推理链路独立使用。

### 6.1 六层体系与写路径

```text
InteractionEvent / EpisodicEvent
  → DualMemoryWriter.enrich        （客观事实 + 角色视角解读，LLM 失败回退规则）
  → stamp_retention                （计算 uniqueness 与 expire_at，tier=short）
  → evidence 非空校验              （缺省补 chat:{session}:{event_id}；显式传入为空则拒绝）
  → embed（summary + objective_fact）→ episodic 存储
  → 审计 + trace
  → 异步提交到期审查任务
  → curate=True 时跑 Memory Curator → WriterPipeline
```

`record_event` 关键参数：`generate_insight`（Dual 解读）、`curate`（跑 Curator）、`importance` / `emotional_valence` / `event_type` / `uniqueness` / `now`。

### 6.2 Dual Memory（`memory/dual.py`）

同一事件保存两份认识：`objective_fact`（发生了什么）+ `persona_conditioned_insight`（这个角色认为这意味着什么，带置信度）。事实一样，不同人格的解读完全不同——"不同人物对同一现实拥有不同认识"。LLM 生成失败或不确定时回退规则解读（`不用管`→气话解读、`猫`→偏好解读），最差也只丢失解读层、保留事实层。

### 6.3 Memory Writer 管线（`memory/writer/`）

**长期语义记忆禁止模型直写**，必须走候选-证据链：

```text
Raw Event → Curator 提出 ops → WriterPipeline 执行
  ADD candidate     → 去重（find_similar）→ 入候选池（confidence=0.15）
  UPDATE candidate  → 累积 evidence；置信度按 value + step·(1−value) 递推（step=0.35）
                      或直接 confidence_delta 加法
  置信度 ≥ consolidation_threshold（0.70）→ _maybe_consolidate 写入 SemanticFact
                      （同三元组已存在则合并证据、取最大置信度），候选标 consolidated
  DELETE candidate  → 标 dropped
  ADD semantic      → 直接拒绝（EvidenceRequiredError）
```

- **Curator 后端**：`RuleCurator`（确定性，测试默认：识别"猫"→"用户喜欢猫"候选、"打扰"→"不喜欢被突然打扰"候选）与 `LLMCurator`（真实小模型，输出 `ADD/UPDATE/DELETE/NO_OP` JSON）。`sanitize_curator_output` 负责修复/降级非法 op（缺 ref 的 UPDATE/DELETE 尝试匹配开放候选，否则降 NO_OP）；解析失败整轮降 `NO_OP`，**不会写坏长期库**。
- 所有操作（含失败）入 `memory_ops_log` 审计。

### 6.4 Episodic 生命周期 tier（`memory/tier.py`）

事件写入即 `tier=short`，附 uniqueness（0–1）与 `expire_at`：

- **uniqueness 打分**（确定性启发式）：问候/超短句 ≈0.05 起；importance、情绪强度、承诺词、记忆探针、额外参与者、长度逐项加分。
- **TTL 分段线性**：uniqueness 0→5 分钟，0.4（low_cutoff）→30 分钟，1.0→7 天。
- **到期审查**（后台线程池）：`expire_at` 到达后由 `TierClassifier` 小模型判定升格 `mid`/`long` 或标记 `forgotten`；到期仍判 short 视为"未赢得留存"改判 forgotten。LLM 失败回退 `rule_verdict`（承诺/高 importance/强情绪 → mid 或 long，到期默认遗忘）。
- **命中升格**：`query()` 中 relevance ≥ 0.35 的 episodic 命中累计 `hit_count`，≥3 升 mid、≥8 升 long；short 命中同时刷新 `expire_at`。只升不降；`forgotten` 不进检索池、V1 不复活。`query_with_conflicts` 的反证命中不计数。
- **检索侧联动**：`forgotten` 从候选池排除；`long` 的 recency 下限 0.4；`mid` 的半衰期 ×7。

遗忘是**标记**而不是删除——数据不移动，`tier` 字段决定可见性，全程可审计（`tier_reasons` 累积每次变更原因）。

### 6.5 混合检索（`memory/retrieval/`）

`query()` 内部步骤：

1. **检索计划**：`RetrievalPlanner` 从查询文本产出 `RetrievalPlan`（`time_scope` / `entities` / `event_type` / `channels`）。规则解析识别"昨天/前天/上周/上个月（宽窗 62 天）/三个月前/刚才"与承诺/偏好/共同经历类事件；LLM 计划默认关闭（`NC_PLANNER_LLM_ENABLED`，计划只是提示，hybrid 自带时间窗回退）。计划与 query embedding **并发**执行（线程池），重叠两个网络往返。
2. **候选池**：按时间窗 + channel 从 episodic 取 50 条候选 + semantic 取 max(8, pool/4) 条。**时间窗是软约束**：带窗为空时自动去窗重查，命中标注 `time:fallback`——用户陈述的日期经常不准，语义最相似的事件不能因此落空。
3. **六信号加权打分**（`signals.py`）：

   ```text
   Score = 0.30·semantic(余弦) + 0.15·entity(参与者重合) + 0.10·recency(指数衰减,tier联动)
         + 0.15·importance + 0.15·relationship(closeness与tension各半) + 0.15·persona(特质/价值观命中)
   ```

   每条结果带 `why_retrieved` 可解释标签（`entity:user` / `time:yesterday` / `commitment_conflict` / `tier:short`……）与完整 `ScoreBreakdown`。
4. 按总分截断 `top_k`（默认 8）。

**反向检索**（`conflict.py`）：`query_with_conflicts` 要求传入 `CurrentInference`（当前推断+极性）。正向推断会生成反证探针文本（"与 X 冲突的失望、不信任、拒绝或负面事件"）做二次检索，命中负向 valence 或否定类条目追加 `conflict_counterevidence` 标签——只把反证交给调用方仲裁，**不静默覆盖**已有记忆，防止角色越来越相信自己错误的总结。

### 6.6 状态层读写

- **Relationship**：`apply_relationship_delta(target, closeness=…, trust=…, …)` 做增量并 clamp 到 [0,1]，可附 `history_note` 与 `last_major_event`；全部变更写审计。
- **Psych L/M/S**：`upsert_psych` 自动为 short 层补 `set_at`；`get_state` 在读路径判定 `ttl_seconds` 到期——到期后 short 只保留 `ttl_seconds/set_at/expired=True`，`CharacterSnapshot.short_psych_expired=True`，long/mid 不受影响。
- **World**：`update_world()` 缺省调用 `PerceptionBackend.observe()`（V1 为 `ChatPerceptionBackend` 桩，只填时间/channel/presence；数字身体时代替换为桌面采集实现），也可写入显式快照。

---

## 7. 存储层

### 7.1 Store Protocol 抽象（`store/protocols.py`）

8 个 Protocol 定义全部存储能力：`PersonaStore` / `EpisodicStore` / `SemanticStore` / `RelationshipStore` / `PsychStore` / `WorldStore` / `CandidateStore` / `AuditStore`，聚合为 `MemoryStores`。两个实现：

- **`InMemoryStores`**：进程内字典，测试与 GUI demo 模式默认。
- **`PostgresStores`**（`store/postgres.py` + `store/schema.py`）：SQLAlchemy 2.0 + pgvector。

### 7.2 PostgreSQL schema（`store/schema.py`）

9 张表：`persona_core`、`episodic_events`、`semantic_facts`、`relationship_states`、`psych_states`、`world_states`、`memory_candidates`、`evidence_links`（候选↔事件证据关联）、`memory_ops_log`（审计）。向量列 `Vector(1024)`（须与 `NC_EMBEDDING_DIM` 一致）。

**Persona 锁双保险**：应用层 `update_persona`/`delete_persona` 抛 `PersonaLockedError` 并记 `actor=unauthorized_attempt` 审计；数据库层 `prevent_persona_mutation()` 触发器在 `BEFORE UPDATE OR DELETE` 时直接 RAISE EXCEPTION——即使绕过应用层直连 DB 也无法篡改核心人格。

向量检索用 `embedding.cosine_distance(...)` 排序，取 `limit*4` 后在 Python 侧做 tier 过滤与精确余弦重排。迁移由 Alembic 管理（`infra/db/`），开发期可用 `create_all(engine)` 跳过迁移（自动装 vector 扩展与锁触发器）。

---

## 8. LLM 与 Embedding 层

### 8.1 LLMClient 协议与实现（`llm/client.py`）

协议方法：`complete_json`（结构化 JSON 调用）/ `complete_chat` / `complete_chat_result`（带 token usage）。实现：

| 类 | 用途 |
| --- | --- |
| `OpenAICompatClient` | OpenAI 兼容 `/chat/completions`（llama.cpp / vLLM / Ollama），长连接 httpx.Client；`complete_json` 用 temperature=0.1 + JSON 提取（剥 markdown 代码块、正则兜底） |
| `RoutingLLMClient` | **双端点路由**：`complete_json` → 辅助小模型（aux），`complete_chat*` → 主角色模型。让 1–4B 小模型承担 parser/gate/interpreter/validator/curator/planner/dual 等辅助 JSON 调用，主模型只做生成 |
| `MockLLM` | 关键词路由的确定性 mock，测试零 GPU 依赖；内置贯穿案例剧本（违规关心回复→修正后回复） |

**辅助端点（aux）**：`NC_AUX_LLM_BASE_URL/MODEL/API_KEY` 显式配置或与主端点不同时激活；针对 Ollama 有 `keep_alive=-1` 常驻 + `num_ctx=8192` 限制 KV cache（防小模型默认 256K 上下文挤爆显存）+ 启动后异步 1-token 预热加载。端点探活用 `/models` 0.8s 超时探测。

### 8.2 CharacterModel（`llm/character.py`）

主模型包装：渲染 `ModelContext` → 追加连说协议 / 已说段落（assistant）/ 续说 nudge / 修正指令 → 调用 → 封装 `CandidateResponse`（含 usage.for_call 标记是否重生成调用）。流式生成签名预留（`stream` 参数），本期抛 `NotImplementedError`。

### 8.3 Embedding（`llm/embeddings.py`）

`OpenAICompatEmbedding`（`/v1/embeddings`，长连接，Authorization 复用当前端点的 api_key）与 `MockEmbedding`（字符 1/2/3-gram + MD5 哈希到 1024 维带符号累加再归一化——进程内确定性，相似字符串共享维度，足以支撑测试中的语义检索断言）。`GuardedEmbedding` 包一层：HTTP 失败返回 `None` 向量而不是抛穿回合（`cosine` 对空向量打 0）。`build_embedding_client()` 按序探测 embedding URL → 本机 aux / 本机主端点，第一可达者启用；全不可达则回退 Mock。云端 Chat API（如 DeepSeek）不会被当作 embedding 回退。GUI live/ollama 走这条路径；demo 强制 Mock。

### 8.4 辅助 LLM 调用的共性纪律

所有 helper（parser / gate / interpreter / validator / curator / planner / dual / tier）都遵循同一模式：**LLM 优先 + 规则兜底**。JSON 解析失败、校验失败、网络异常一律回退确定性规则实现——真实模型不可用时系统行为依然正确（只是精度降级），这是测试无需 GPU 与线上鲁棒性的共同基础。

---

## 9. 可视化 GUI

### 9.1 架构（`gui/`）

- **`runtime_server.py`**：Windows 四进程拓扑使用的 GUI facade。它只持有 Runtime control token，把现有 `/api/*` 路由代理为 Runtime 控制命令，并把 Runtime SSE 转成浏览器 WebSocket；浏览器不直接访问 Cognitive/Homeostasis。
- **`harness/runtime/monitor.py`**：`/ops` 的非权威可观测投影。它异步收集 Runtime/Cognitive 事件、journal 与成功调用日志，写入 WAL SQLite，保留 7 天并提供 cursor 查询、断线续传、运行聚合和单次诊断包；投影失败只影响面板，不改变 Runtime 生命周期。
- **`static/ops.html|ops.js|ops.css`**：独立 Runtime 运维页，包含概览、事件/Journal、九阶段运行和动作/控制四个视图。完整控制只通过 GUI facade 转发，浏览器不持有 Runtime bearer token。

- **`server.py`**：FastAPI 应用。REST：`POST /api/chat`（发消息，`asyncio.to_thread` 跑同步 pipeline，`run_lock` 串行化）、`GET /api/status`、预设库（`GET /api/presets`、`POST /api/presets/{open,apply,new,restore}`、`DELETE /api/presets/{id}`）、`POST /api/config`（`demo` / `live` / `ollama` 切换与端点配置）、`GET /api/models`（探活端点模型列表）、`POST /api/reset`（当前预设恢复出厂）、`POST /api/speakers`、`GET /api/debug/state` + 7 个调试写入端点。WebSocket `/ws`：订阅者队列广播，25s 心跳。静态托管 `gui/static/`（三栏原生 JS 界面）。
- **`service.py`**：`HarnessChatService` 包装 pipeline。`rebuild()` 重建 Settings → LLM 客户端（`demo`=MockLLM；`live`/`ollama`=OpenAI 兼容 + aux 路由 + 探活 + 异步预热；`ollama` 未填 URL 时默认 `:11434`）→ embedding 客户端（demo=MockEmbedding；live/ollama=`build_embedding_client`：可达则 `GuardedEmbedding(OpenAICompatEmbedding)`，否则 Mock）→ 按当前预设 hydrate 记忆（种子缺向量时批量补 embedding；模式切换会先 flush 存档，不再丢调试改动）。GUI 强制 `call_log_enabled=true`、`validator_sample_rate=1.0`（Fast Path 也全量跑 Validator，便于观察）。状态快照 `snapshot_state()` 聚合 persona/relationship/psych/world/各 store 计数/完整聊天记录/打开的预设选项卡/端点可达性（含 embedding backend）。
- **`observer.py`**：`QueueObserver` 把 pipeline 每个阶段的结构化结果序列化后 `call_soon_threadsafe` 广播到所有 WebSocket 订阅者——实现**阶段级实时推送**（非 token 流式）。`RecordingObserver` 供测试。
- **`debug.py`**：调试面板写入逻辑（关系滑条、心理三层编辑、人格编辑（debug 绕锁并记 `bypass=persona_lock` 审计）、事件编辑/新建/重生成解读/重跑 Curator、语义/候选编辑、检索权重等调参）。写入后自动写回当前预设工作副本。
- **`persist.py`**：GUI 配置（模式/端点/称呼/打开的预设/调参）持久化到本地 JSON（默认 `data/gui_settings.json`，可用 `NC_GUI_CONFIG` 覆盖），重启恢复。模式为 `demo` | `live` | `ollama`。
- **`presets.py`**：角色预设库。一份预设 = 人格 + 记忆快照 + 完整聊天记录（`schema_version=preset-v1`）。出厂模板在 `presets/builtin/`（git 跟踪），工作副本在 `data/presets/`（gitignore，可用 `NC_GUI_PRESETS` 覆盖）。hydrate 时若种子缺向量会批量补 embedding。`builtin_id == preset_id` 的出厂 id 受保护不可删除。
- **`demo_seed.py`**：从出厂 JSON 注入 MemoryService 的薄封装（测试与兼容入口）。

### 9.2 使用

```bash
pip install -e ".[dev,gui]"
python -m gui.server   # http://127.0.0.1:8765
```

三栏界面：左角色对话（预设选项卡）、中九阶段推理时间线（每阶段完整结构化输出+延迟+token）、右 Persona/关系/心理/世界/记忆计数。三种模式：`demo`（内存 + MockLLM + MockEmbedding）、`live`（任意 OpenAI 兼容端点）、`ollama`（未填 URL 时默认 `http://127.0.0.1:11434/v1`，并从 `/models` 或 Ollama `/api/tags` 拉模型列表）。顶栏**预设**打开模板库，**设置**切模式。发"早上好。"看 Fast Path；发"你昨天不是说不会再管我了吗？"看 Deep Path（Interpreter + Validator 局部重生成）。调试与对话改动自动写回当前预设工作副本；切换模式不会丢存档；**重置**把当前预设恢复为出厂模板。成功对话落盘 `traces/calls/YYYYMMDD/{run_id}.json`（gitignore）。

使用 `start.ps1` 时右栏额外展示七维 Homeostasis value、目标带、posterior、precision、情感、候选策略和 watchdog；顶栏**内稳态**可校验并保存新的 immutable profile/manifest revision，下一次 `start.ps1` 激活。

同一拓扑下 `/ops` 展示 `1h / 6h / 24h / 7d` 运行历史。Runtime 私有控制面提供 `/v1/monitor/*`，GUI 映射为 `/api/ops/*` 并通过 `/ops/ws` 转发可续传实时流。`study_runtime` 在服务端拒绝调试事件注入和人工结果改写；普通生命周期控制、checkpoint 与正式动作审批仍可使用。兼容单进程 `gui.server` 只显示“需要完整 Runtime 拓扑”，不生成伪监控数据。

---

## 10. 配置体系

`harness/config.py` 的 `Settings`（pydantic-settings，环境变量前缀 `NC_`，支持 `.env`）。分组如下：

| 组 | 关键项（默认） |
| --- | --- |
| 存储 | `NC_DATABASE_URL`（postgresql+psycopg2://…localhost:5432/neuralcloud） |
| 主 LLM | `NC_LLM_BASE_URL`（http://127.0.0.1:8080/v1）/ `NC_LLM_MODEL`（qwen）/ `NC_LLM_API_KEY` / `NC_LLM_TIMEOUT` / `NC_LLM_JSON_MAX_TOKENS`（512，防小模型 JSON 前啰嗦） |
| 辅助 LLM | `NC_AUX_LLM_*`（base_url/model/api_key/preload/keep_alive=-1/num_ctx=8192） |
| Embedding | `NC_EMBEDDING_BASE_URL` / `NC_EMBEDDING_MODEL`（bge-m3）/ `NC_EMBEDDING_DIM`（1024，须与 PG vector 列一致） |
| 记忆/检索 | 固化阈值 0.70、候选初始置信度 0.15、置信度步进 0.35、检索池 50、top_k 8、recency 半衰期 24h、六信号权重、uniqueness TTL 四参数（5min/30min/0.4/10080min）、tier 升格命中数（3/8）与最低 relevance 0.35、long recency 下限 0.4、mid 半衰期倍数 7 |
| 推理 | `NC_COMPLEXITY_THRESHOLD`（0.5）、`NC_CONTEXT_TOKEN_BUDGET`（3000）、`NC_COMPILER_TOP_MEMORIES`（5）、`NC_VALIDATOR_SAMPLE_RATE`（0.2）、`NC_MAX_REVISIONS`（2）、`NC_SESSION_WINDOW`（10）、`NC_RECORD_INTERACTION`（true）、`NC_CONTINUATION_ENABLED`（true）/ `NC_MAX_REPLY_SEGMENTS`（3）、`NC_CALL_LOG_ENABLED`（false；GUI 强制 true）、`NC_PARSER_LLM_ENABLED`（true）、`NC_PLANNER_LLM_ENABLED`（false，计划只是 hint） |
| GUI | `NC_GUI_CONFIG`（默认 `data/gui_settings.json`）、`NC_GUI_PRESETS`（默认 `data/presets/`） |
| Embodiment | `NC_EMBODIMENT_PROVIDER`（`none`/`mistyai`/`simulator`）；MistyAI 的 `NC_MISTYAI_BASE_URL` / `NC_MISTYAI_TOKEN` / timeout / poll；Simulator 的 seed / offline / observation-only / physiology |

版本戳：`NC_SCHEMA_VERSION`（state-v1）/ `NC_PROMPT_VERSION`（memory-v1）/ `NC_MODEL_VERSION`，随每条 trace 落盘。

**注意**：`get_settings()` 有进程内 `lru_cache`；测试或多次改环境变量时应显式构造 `Settings(...)` 传入 Service，不要依赖缓存。

Embodiment 的研究条件不从环境变量读取：mode、required/optional capability、body instance、BodyState freshness 与 active-mode debounce 均冻结在 `ExperimentManifest.embodiment`。环境变量只负责在 Runtime composition root 选择并连接 Provider。Canonical JSON Schema 位于 `contracts/embodiment/`。

---

## 11. Trace 与审计

**原则 9 落地**：没有 provenance 就无法事后回答"哪条记忆把角色带偏了"。四条独立但互补的溯源通道：

1. **JSONL trace**（`infra/trace/recorder.py` 的 `JsonlTracer`，线程安全追加）：每行含 `ts` / `kind` / 三个版本号 + payload。推理侧 kind：`ingest/parse/state_build/gate/interpret/compile/generate/validate/final`（各带 latency_ms、token、verdict 等）；记忆侧 kind：`persona_bootstrap/persona_lock_violation/episodic_write/curator_ops/retrieval/psych_upsert/relationship_delta/world_update/tier_hits/tier_expiry_review/tier_error`。
2. **成功调用全链路 JSON**（`infra/trace/call_log.py`）：`NC_CALL_LOG_ENABLED` 开启时，每次**成功返回**的顶层调用写 `{trace_dir}/calls/YYYYMMDD/{call_id}.json`，收录 input、各阶段完整结构化 JSON（去掉 embedding）、嵌套的记忆写/检索，以及 `result`。GUI 默认开启，并用前端 `run_id` 作为 `call_id`。异常路径不落盘。后台 tier 升格/到期审查若真正改档，另写独立文件。
3. **`memory_ops_log` 审计表**：所有记忆写操作（含失败的越权尝试）记录 op/target/actor/success/detail。`unauthorized_persona_attempts()` 直接给出"Persona 非授权修改尝试"计数（验收标准要求 =0 成功，尝试可追溯）。
4. **Compiler provenance**：`ModelContext.provenance` 记录每个上下文段的 segment_kind + source_ids + why，与 trace 中的检索分数分解互相印证。

---

## 12. 评测体系

`eval/` 三层（全部接 `pytest`，无 GPU/外部数据可跑冒烟）：

| 层 | 入口 | 说明 |
| --- | --- | --- |
| 场景评测 | `pytest eval/scenarios` | 验收场景：Persona 锁攻击、冲突可发现不覆盖、证据链完整、short psych TTL、Dual 分离、猫候选固化、02 贯穿案例检索等 |
| 检索延迟基线 | `python -m eval.bench_retrieval --n 10000` | 对照 02 §4 的 30–100ms Harness 预算**出报告不做硬门禁**；近期参考：in-memory 1 万条 episodic P95 ≈ 8ms |
| 角色扮演基准 | `python -m eval.rp.run_eval --bench roleeval --mode both --limit 20` | CharacterEval / RoleEval / TimeChara 三基准，**raw（裸模型）vs harness（全链路）对照**；数据集与 CharacterRM 权重放 `eval/external/`（gitignore，一次性下载后离线）；每条样本 `trace_ref` 回指 JSONL；报告写 `eval/reports/rp_<bench>_<date>.md` |

CharacterEval 的 13 维评分需 `pip install -e ".[eval]"` + BaichuanCharRM 权重；judge 缺失时报告标注不可用而不硬失败。`--fixtures --mock` 提供无数据时的内置迷你题。

---

## 13. 测试体系

- `tests/`：20+ 个测试文件，逐模块覆盖（compiler/continuation/debug_panel/gate/gui/interpreter/llm_and_trace/models/parser/pipeline/persona_lock/retrieval/state_layers/tier/validator/writer/aux_llm/call_log/postgres_integration 等）。`tests/factories.py` 提供 `vaelith()` 人格工厂与贯穿案例种子。
- **零外部依赖设计**：默认 MockLLM + MockEmbedding + InMemoryStores + NullTracer，全量测试不需 GPU/数据库/网络。PostgreSQL 集成测试标 `@pytest.mark.integration`，驱动或 DB 不可用时自动 skip。
- `conftest.py` 配置 pytest 路径与 marker；`ruff`（E/F，行宽 120）+ `mypy`（check_untyped_defs）做静态检查。
- README 附有**需求可追溯对应表**：设计文档条款 → 实现文件 → 测试文件，三方互锁。

---

## 14. 部署与运行

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows；WSL2: source .venv/bin/activate
pip install -e ".[dev]"                          # 核心 + 测试 + GUI 依赖
pip install -e ".[dev,postgres]"                 # 可选：PG 驱动（Windows ARM64 可能无 wheel）
pip install -e ".[dev,gui]"                      # GUI 已含在 dev；单独安装时用 gui extra

# 可选 PostgreSQL + pgvector
docker compose -f infra/db/docker-compose.yml up -d
alembic -c infra/db/alembic.ini upgrade head

pytest                                           # 全量测试
python -m gui.server                             # 可视化 GUI
```

环境基线（适配说明）：开发主机为 **Windows + WSL2**。推荐本机 live 栈为 **Ollama 双模型**（主 `neuralcloud-main` + 辅 `qwen3:4b-instruct`），见 README 与 `.env.example`；llama.cpp `:8080` 仍可用。Harness / GUI 经 OpenAI 兼容 HTTP 调用模型；PostgreSQL 可选，跑在 Docker（WSL2 backend）。仓库提供 `start.ps1` / `stop.ps1`（Windows）与 `start.sh` / `stop.sh`（WSL2/Linux）一键脚本。无 GPU、无 Docker 时默认 in-memory + Mock 全功能可用。

---

## 15. 扩展点与接口约定

| 扩展点 | 接口 | 说明 |
| --- | --- | --- |
| 存储后端 | `harness.memory.store.protocols.MemoryStores`（8 个 Protocol） | 实现 Protocol 即可替换，业务代码零改动 |
| 感知后端 | `memory/perception.py: PerceptionBackend.observe(*, now) -> WorldState` | 数字身体（03 文档）接入点；V1 为 chat 桩 |
| 主/辅 LLM | `llm/client.py: LLMClient` Protocol | 任何 OpenAI 兼容端点；aux 分流辅助调用 |
| Embedding | `llm/embeddings.py: EmbeddingClient` Protocol | 维度须与 Settings 及 PG vector 列一致 |
| Curator | `memory/writer/curator.py: CuratorBackend.propose(...) -> CuratorOutput` | 接 1–3B 小模型当 memory daemon |
| Trace | `infra/trace/recorder.py: Tracer.record(kind, payload)`；成功调用 `infra/trace/call_log.py` | 换落盘格式/接 OTel 均在 Tracer 层；排查单次调用看 `traces/calls/` |
| 阶段观察 | `pipeline/pipeline.py: StageObserver.on_stage(kind, result, payload)` | GUI 之外可挂新观察者（录制、监控） |
| Pipeline 各阶段 | `InferencePipeline(..., gateway=/parser=/gate=/interpreter=/compiler=/character=/validator=...)` | 逐阶段替换实现做 A/B |

**接口纪律**：新模块只收发 `core/models.py` 与 `core/inference.py` 中的结构化对象；写长期记忆必须带证据链；改 Persona Core 只有 `bootstrap_persona` 一条路。

---

## 16. 已知边界与路线图

**V1 边界**（设计文档 00 §5 + README 明确排除）：token 流式生成/TTS（接口预留）、数字身体真实采集（仅 PerceptionBackend 桩）、Experience Evaluator（第三个门控 subagent 未实现）、Fast Path 状态缓存（`state_cache` 槽位预留未启用）、forgotten 记忆复活、PostgreSQL 上的人格 debug 绕锁（触发器阻止）。

**里程碑对应**（05 文档）：当前代码完成 **M2 Cognitive Harness** 的核心部分。后续 M0/M1（数字身体与 GUI agent）反向依赖本 Harness 的 `PerceptionBackend` 接口；M3 自主驱动（Drive/Goal/IDLE 循环）、M4 经验学习数据管线（五类数据集导出）、M5 长跑实验（24–72h + dashboard）均不在本仓库当前范围，但 trace 的 `Perception → State → Decision → Action → Outcome` 记录纪律与版本戳已为其预留数据基础。

---

*本文档基于 V1 代码（git worktree 当前状态）撰写；设计论证与演进方向请对照 `docs/harness/00–06` 文档集阅读。*
