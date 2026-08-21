# 06 · 记忆子系统使用说明（Memory Subsystem V1）

> 本文是 **已落地代码** 的操作手册，不是设计论证。设计背景见 [01-cognitive-harness.md](01-cognitive-harness.md)。
>
> 入口类：`harness.memory.service.MemoryService`。模块之间只传结构化对象，不拼自然语言小作文。

---

## 1. 这份库做什么

V1 把 01 文档里与「记忆 / 状态」相关的机制做成独立 Python 库 `harness.memory`，**不启动推理链路也能写入、检索、审计**。

| 能力 | 说明 |
| --- | --- |
| 六层状态 | Persona Core（锁定）、Episodic、Semantic、Relationship、Psych L/M/S、World State |
| Dual Memory | 同一事件同时保存客观事实与角色视角解读 |
| Memory Writer | 候选 → 证据累积 → 置信度更新 → 语义固化 |
| Memory Curator | 输出 `ADD / UPDATE / DELETE / NO_OP`，不替角色说话 |
| 混合检索 | 检索计划 + 六信号加权；每条结果带 `why_retrieved` |
| 反向检索 | 给定当前推断，找可能冲突的记忆，不静默覆盖 |
| Provenance | 写 / 检索落 JSONL trace；操作写入 `memory_ops_log` |

**本轮不做**（后续文档 / 后续代码）：Context Compiler、Social Interpreter、Experience Evaluator、九阶段推理、Response Validator、数字身体真实采集、自治驱动、FastAPI、dashboard。

推荐阅读顺序：本文（怎么用）→ [01](01-cognitive-harness.md)（为什么这样设计）→ [02](02-inference-flow.md)（以后如何被单轮推理消费）。

---

## 2. 安装

在仓库根目录：

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# WSL2 / Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

可选 PostgreSQL 驱动（本机有对应 wheel 时）：

```bash
pip install -e ".[dev,postgres]"
```

适配说明：开发可在 Windows 或 WSL2 Ubuntu 进行。Windows ARM64 上 `psycopg` / `psycopg2-binary` 可能没有现成 wheel；没有驱动时请用 in-memory 后端，集成测试会自动 skip。

验证：

```bash
pytest
```

期望：单元测试与 eval 场景通过；PostgreSQL 不可用时 `tests/test_postgres_integration.py` 跳过。

---

## 3. 五分钟最小闭环

下面这段覆盖：引导人格 → 写入事件 → 检索。默认 **in-memory + MockLLM + MockEmbedding**，不需要 GPU 或数据库。

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from harness.core.models import InteractionEvent, PersonaCore, PersonaIdentity, RetrievalQuery
from harness.memory.service import build_inmemory_service

TZ = ZoneInfo("Asia/Shanghai")
now = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)

svc = build_inmemory_service()

svc.bootstrap_persona(
    PersonaCore(
        persona_id="persona_v3",
        version="v3",
        identity=PersonaIdentity(
            name="Vaelith",
            core_traits=["傲慢", "善良", "高自尊", "不直接表达关心"],
            values=["承诺必须兑现", "不轻易求助"],
            locked=True,
        ),
        created_at=now,
    )
)

event = svc.record_event(
    InteractionEvent(
        event_id="evt_03812",
        timestamp=now,
        speaker="user",
        raw_text="用户提到上周收养的猫生病了",
        session_id="sess_1",
        evidence=["traj_T928100_step_44"],
    ),
    importance=0.72,
    emotional_valence=-0.3,
)

print(event.objective_fact)
print(event.persona_conditioned_insight)

hits = svc.query(RetrievalQuery(text="那只猫后来怎样了？", now=now))
for hit in hits:
    print(hit.memory_type, hit.ref, hit.relevance, hit.why_retrieved)
```

测试用角色工厂：`tests.factories.vaelith()`（与 01 文档示例人物一致）。

---

## 4. 对象与纪律（用之前必须知道）

### 4.1 事件，不是聊天摘要

不要写入「用户喜欢猫」。写入**具体事件**：时间、参与者、摘要、角色行动、结果、重要性、情绪效价、证据链。语义层的稳定认识只能由 Writer 管线从多次事件固化而来。

### 4.2 证据链是硬约束

| 对象 | 必填字段 | 缺了会怎样 |
| --- | --- | --- |
| `EpisodicEvent` | `evidence: list[str]` 非空 | 模型校验失败，或 `EvidenceRequiredError` |
| `SemanticFact` | `supporting_events` 非空 | 同上 |
| `CandidateMemory` | `evidence` 至少 1 条 | 无法进入候选池 |

`InteractionEvent` 若未填 `evidence`，`record_event` 会补成 `chat:{session_id}:{event_id}`。生产路径请显式传入 trajectory / 日志引用（例如 `traj_T928100_step_44`）。

### 4.3 Persona Core 锁定

`bootstrap_persona` 只能插入一次。之后：

- `update_persona` / `delete_persona` 抛 `PersonaLockedError`
- 尝试记入 `memory_ops_log`（`actor=unauthorized_attempt`, `success=False`）
- PostgreSQL 上还有 `BEFORE UPDATE OR DELETE` 触发器作为第二道防线

成长只能走 Semantic / Relationship / Psych / Developed Self 证据链，不能改核心人格。

### 4.4 标识符前缀

由 `harness.core.ids.new_id` 生成：

| 种类 | 前缀 | 示例 |
| --- | --- | --- |
| 事件 | `evt_` | `evt_03812` |
| 语义事实 | `sem_` | `sem_0019` |
| 关系 | `rel_` | `rel_user_001` |
| 候选记忆 | `cand_` | `cand_0a1b2c3d` |
| 人格 | `persona_` | `persona_v3` |

---

## 5. MemoryService API

构造：

```python
MemoryService(
    stores,                 # InMemoryStores 或 PostgresStores
    llm=None,               # 默认 MockLLM；真实模型用 OpenAICompatClient
    embeddings=None,        # 默认 MockEmbedding
    curator=None,           # 默认：MockLLM → RuleCurator，否则 LLMCurator
    tracer=None,            # 默认 NullTracer；落盘用 JsonlTracer
    perception=None,        # 默认 ChatPerceptionBackend
    settings=None,          # 默认 get_settings()，可读环境变量 NC_*
)
```

便捷工厂：`build_inmemory_service(**kwargs)`。

### 5.1 方法一览

| 方法 | 作用 |
| --- | --- |
| `bootstrap_persona(persona)` | 一次性写入锁定人格 |
| `get_persona(persona_id=None)` | 读取；不传 id 则取最新一条 |
| `update_persona` / `delete_persona` | **必定失败**，用于锁层测试 |
| `record_event(incoming, ...)` | 写入 episodic + Dual Memory + 可选 Curator |
| `curate(event)` | 对已有事件再跑一遍 Curator / Writer |
| `query(query)` | 混合检索，返回 `list[ScoredMemory]` |
| `query_with_conflicts(query)` | 检索 + 反向检索；`query.current_inference` 必填 |
| `get_state(...)` | 聚合 persona / relationship / psych（含 TTL）/ world |
| `upsert_psych(state)` | 写入 L/M/S；short 层自动补 `set_at` |
| `apply_relationship_delta(target, ...)` | 对 closeness / trust 等做增量，夹在 `[0, 1]` |
| `update_world(state=None)` | 写入世界状态；缺省则调用 PerceptionBackend |
| `force_write_semantic(fact)` | 仍要求 `supporting_events`；禁止无证据直写 |

`record_event` 关键字参数：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `generate_insight` | `True` | 生成 `persona_conditioned_insight` |
| `curate` | `True` | 触发 Memory Curator |
| `importance` | `0.5` | 传入 `InteractionEvent` 时使用 |
| `emotional_valence` | `0.0` | `[-1, 1]` |
| `event_type` | `None` | 如 `commitment_or_statement` |
| `now` | UTC now | 管线时间戳（候选更新等） |

若传入的已是 `EpisodicEvent`，`importance` / `valence` / `event_type` 以对象自身字段为准。

---

## 6. 写路径

```text
InteractionEvent / EpisodicEvent
    → Dual Memory（客观事实 + 角色解读）
    → episodic 存储 + embedding
    → Memory Curator（结构化 ops）
    → Writer Pipeline（候选 / 固化 / 丢弃）
    → semantic_facts + memory_ops_log + JSONL trace
```

### 6.1 写入一条对话事件

```python
from harness.core.models import InteractionEvent

svc.record_event(
    InteractionEvent(
        event_id="evt_04010",
        timestamp=datetime(2026, 8, 20, 23, 40, tzinfo=TZ),
        speaker="user",
        raw_text="你以后不用管我了",
        session_id="sess_0881",
        evidence=["chat:sess_0881:evt_04010"],
    ),
    importance=0.86,
    emotional_valence=-0.55,
    event_type="commitment_or_statement",
)
```

返回的 `EpisodicEvent` 含：

- `objective_fact`：发生了什么
- `persona_conditioned_insight` / `insight_confidence`：这个角色认为这意味着什么（LLM 失败时走规则 fallback；MockLLM 走规则）
- `evidence`：证据引用

### 6.2 候选记忆如何变成语义事实

规则 Curator（测试默认）在摘要出现「猫 / cat」时：

1. 第一次 → `ADD` 候选，`confidence=0.15`，`subject=user`, `predicate=preference`, `value=喜欢猫`
2. 后续同类事件 → `UPDATE`，累加证据与置信度
3. 置信度 ≥ `NC_CONSOLIDATION_THRESHOLD`（默认 **0.70**）→ 写入 `semantic_facts`，候选状态变为 `consolidated`

默认置信度步进：`新值 = 旧值 + increment * (1 - 旧值)`，`increment` 默认 0.35。规则后端对猫相关事件使用 `confidence_delta=0.35`（加法），大约三次相关事件即可固化。

**禁止**：让模型直接 `ADD type=semantic`。管线会抛 `EvidenceRequiredError`。

查看中间态：

```python
svc.stores.candidates.list_open()      # 仍在累积的候选
svc.stores.candidates.list_all()
svc.stores.semantic.list_all()
svc.stores.audit.list_for("semantic")
```

### 6.3 接入真实 Curator 模型

```python
from harness.config import Settings
from harness.llm.client import OpenAICompatClient
from harness.memory.service import build_inmemory_service
from harness.memory.writer.curator import LLMCurator

settings = Settings(
    llm_base_url="http://127.0.0.1:8080/v1",
    llm_model="qwen",
    llm_api_key="not-needed",
)
llm = OpenAICompatClient(settings)
svc = build_inmemory_service(
    settings=settings,
    llm=llm,
    curator=LLMCurator(llm),
)
```

Curator 必须输出 JSON：`{"ops":[{"op":"ADD|UPDATE|DELETE|NO_OP", ...}]}`。解析失败时该轮降级为 `NO_OP`，不会写坏长期库。

---

## 7. 读路径

### 7.1 混合检索

```python
from harness.core.models import RetrievalQuery

hits = svc.query(
    RetrievalQuery(
        text="你昨天不是说不会再管我了吗？",
        now=datetime(2026, 8, 21, 13, 54, tzinfo=TZ),
        entities=["user", "character"],
        top_k=5,
    )
)

hit = hits[0]
print(hit.ref)                 # 如 evt_04010
print(hit.why_retrieved)       # ["entity:user", "time:yesterday", "commitment_conflict", ...]
print(hit.score_breakdown)     # semantic / entity / recency / importance / relationship / persona / total
print(hit.episodic.dual())     # Dual Memory 视图
```

内部步骤：

1. `RetrievalPlanner` 生成 `RetrievalPlan`（`time_scope` / `entities` / `event_type` / `channels`）。MockLLM 使用规则解析（识别「昨天/前天/上周/上个月/三个月前/刚才」「不管/承诺」等）。时间词对应的 `time_scope` 枚举：`yesterday` / `day_before_yesterday` / `last_week` / `last_month`（宽窗 62 天）/ `three_months` / `recent`。
2. 按时间窗、channel 取候选池（默认 50 条）。**时间窗是软约束**：用户陈述的日期经常不准，若带时间窗的候选池为空，会自动去掉时间窗重查一次，命中条目在 `why_retrieved` 中标注 `time:fallback`；排序仍由六信号决定，时间不匹配的旧事件会被 recency 自然压低，但语义最相似的事件不会落空。
3. 六信号加权（默认权重来自 01 §4.1，可配置）：

```text
Score = 0.30·semantic + 0.15·entity + 0.10·recency
      + 0.15·importance + 0.15·relationship + 0.15·persona
```

4. 截断为 `top_k`（默认 8）。

贯穿案例验收：语料含昨晚 `evt_04010`（「你以后不用管我了」）时，上述查询 Top-1 应为该事件，且 `why_retrieved` 含 `entity:user`、`time:yesterday`、`commitment_conflict`。

### 7.2 反向检索（冲突）

不覆盖已有记忆，只把反证交给调用方。

```python
from harness.core.models import CurrentInference, RetrievalQuery

hits, conflicts = svc.query_with_conflicts(
    RetrievalQuery(
        text="用户现在信任度很高",
        now=now,
        current_inference=CurrentInference(
            about="user_trust",
            value="high",
            polarity="positive",
            entities=["user"],
        ),
    )
)
```

`polarity=positive` 时，会去找负向 valence、失望 / 不信任 / 拒绝类事件或语义事实，命中项的 `why_retrieved` 会追加 `conflict_counterevidence`。

---

## 8. 其余四层状态

### 8.1 Relationship

增量为相对变化，不是绝对值。缺省起点各维度 0.5（`recent_tension` 为 0.0）。

```python
rel = svc.apply_relationship_delta(
    "user",
    closeness=0.18,          # 0.50 → 0.68
    trust=0.24,              # 0.50 → 0.74
    recent_tension=0.31,
    last_major_event="evt_03812",
    history_note="共同经历过重要事件",
    now=now,
)
```

### 8.2 Psychological State（L / M / S）

```python
from harness.core.models import PsychState

svc.upsert_psych(
    PsychState(
        character_id="character",
        long={"sensitivity_to_betrayal": 0.85},
        mid={"current_focus": "project_alpha", "energy_level": 0.6},
        short={"mood": "slightly_irritated", "arousal": 0.4, "ttl_seconds": 7200},
        updated_at=now,
    ),
    now=now,
)
```

- `upsert_psych` 会为 short 层补 `set_at`（ISO 时间）。
- `get_state(now=...)` 若超过 `ttl_seconds`，short 层在**读路径**失效：只保留 `ttl_seconds` / `set_at` / `expired=True`，long / mid 不变。`CharacterSnapshot.short_psych_expired` 为 `True`。

### 8.3 World State

Chat V1 不采集桌面。`ChatPerceptionBackend` 只填时间、channel、presence：

```python
world = svc.update_world(now=now)   # 走 PerceptionBackend
snap = svc.get_state(now=now)
print(snap.world.channel, snap.world.user_presence)
```

也可写入显式快照（为 03 数字身体预留同构字段）：

```python
from harness.core.models import WorldState

svc.update_world(
    WorldState(
        time=now,
        active_application="Firefox",
        active_window_title="本地 Wiki - 角色设定",
        open_windows=["Files", "Terminal"],
        user_presence="active",
        user_activity="reading",
        channel="chat",
    )
)
```

实现自己的采集器：满足 `observe(*, now=None) -> WorldState`，构造 `MemoryService(..., perception=...)`。

### 8.4 一次取出当前快照

```python
snap = svc.get_state(relation_target="user", now=now, character_id="character")
# snap.persona / .relationship / .psych / .world / .short_psych_expired
```

---

## 9. PostgreSQL + pgvector

设计选型见 [05-roadmap-and-stack.md](05-roadmap-and-stack.md) §3。V1 schema 由 Alembic 管理。

### 9.1 启动数据库

```bash
docker compose -f infra/db/docker-compose.yml up -d
alembic -c infra/db/alembic.ini upgrade head
```

默认连接（可用环境变量 `NC_DATABASE_URL` 覆盖）：

```text
postgresql+psycopg2://neuralcloud:neuralcloud@localhost:5432/neuralcloud
```

表：`persona_core`、`episodic_events`、`semantic_facts`、`relationship_states`、`psych_states`、`world_states`、`memory_candidates`、`evidence_links`、`memory_ops_log`。向量列维度 **1024**（与默认 embedding 一致）。

### 9.2 用 PG 构造 MemoryService

```python
from sqlalchemy.orm import Session

from harness.memory.service import MemoryService
from harness.memory.store.postgres import PostgresStores, make_engine
from harness.llm.embeddings import MockEmbedding

engine = make_engine()  # 读 NC_DATABASE_URL
with Session(engine) as session:
    stores = PostgresStores.from_session(session)
    svc = MemoryService(stores, embeddings=MockEmbedding())
    svc.bootstrap_persona(...)
    svc.record_event(...)
    session.commit()
```

开发期若不想跑迁移，可用 `create_all(engine)`（会装 pgvector 扩展与 Persona 锁触发器）。正式环境请走 Alembic。

---

## 10. 配置（环境变量 `NC_`）

实现：[`harness/config.py`](../../harness/config.py)。可写 `.env` 或导出环境变量。

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `NC_DATABASE_URL` | `postgresql+psycopg2://neuralcloud:neuralcloud@localhost:5432/neuralcloud` | SQLAlchemy URL |
| `NC_LLM_BASE_URL` | `http://127.0.0.1:8080/v1` | OpenAI 兼容 Chat Completions |
| `NC_LLM_API_KEY` | `not-needed` | 本地服务可随意 |
| `NC_LLM_MODEL` | `qwen` | 模型名 |
| `NC_EMBEDDING_BASE_URL` | 同上 | `/v1/embeddings` |
| `NC_EMBEDDING_MODEL` | `bge-m3` | embedding 模型名 |
| `NC_EMBEDDING_DIM` | `1024` | 须与 PG `vector(1024)` 一致 |
| `NC_TRACE_DIR` | `traces` | JSONL 目录 |
| `NC_CONSOLIDATION_THRESHOLD` | `0.70` | 候选固化阈值 |
| `NC_INITIAL_CANDIDATE_CONFIDENCE` | `0.15` | 新候选初始置信度 |
| `NC_CONFIDENCE_INCREMENT` | `0.35` | 证据累积步进 |
| `NC_RETRIEVAL_POOL_SIZE` | `50` | 检索候选池 |
| `NC_RETRIEVAL_TOP_K` | `8` | 返回条数 |
| `NC_RECENCY_HALF_LIFE_HOURS` | `24` | 近因衰减半衰期 |
| `NC_MODEL_VERSION` / `NC_PROMPT_VERSION` / `NC_SCHEMA_VERSION` | `mock` / `memory-v1` / `state-v1` | 写入 trace |

检索权重请构造 `Settings(retrieval_weights={...})` 传入 `MemoryService`，不要在调用点写死。六键之和应为 1.0。

代码里覆盖示例：

```python
from harness.config import Settings
from harness.memory.service import build_inmemory_service

settings = Settings(consolidation_threshold=0.75, retrieval_top_k=5)
svc = build_inmemory_service(settings=settings)
```

注意：`get_settings()` 有进程内缓存。测试或多次改环境变量时，应直接 `Settings(...)` 并传入 Service，不要依赖缓存。

---

## 11. LLM 与 Embedding

| 后端 | 类 | 用途 |
| --- | --- | --- |
| Mock LLM | `harness.llm.client.MockLLM` | 单测；默认走 RuleCurator / 规则 Dual Memory / 规则检索计划 |
| OpenAI 兼容 | `OpenAICompatClient` | llama.cpp / vLLM 的 `/v1/chat/completions`，要求模型返回 JSON 对象 |
| Mock Embedding | `harness.llm.embeddings.MockEmbedding` | 字符 n-gram 哈希，过程内确定性 |
| OpenAI 兼容 Embedding | `OpenAICompatEmbedding` | `/v1/embeddings` |

开发期典型拓扑（与 00 / 05 一致）：Windows 本机 GPU 跑 llama.cpp，Harness 在 WSL2 经 HTTP 调用。

```python
from harness.config import Settings
from harness.llm.client import OpenAICompatClient
from harness.llm.embeddings import OpenAICompatEmbedding
from harness.memory.service import build_inmemory_service

settings = Settings(
    llm_base_url="http://127.0.0.1:8080/v1",
    embedding_base_url="http://127.0.0.1:8080/v1",
    embedding_model="bge-m3",
    embedding_dim=1024,
    model_version="qwen3-4b",
)
svc = build_inmemory_service(
    settings=settings,
    llm=OpenAICompatClient(settings),
    embeddings=OpenAICompatEmbedding(settings),
)
```

JSON 必须能被解析为 object。Markdown 代码块会自动剥掉。解析失败时 Dual Memory 只保留事实层；Curator 该轮 `NO_OP`；检索计划回退规则解析。

---

## 12. Trace 与审计

原则 9：没有 provenance 就无法事后回答「哪条记忆把角色带偏了」。

```python
from pathlib import Path
from infra.trace.recorder import JsonlTracer
from harness.memory.service import build_inmemory_service

tracer = JsonlTracer(Path("traces/memory.jsonl"))
svc = build_inmemory_service(tracer=tracer)
```

每条 JSONL 至少包含 `ts`、`kind`、`schema_version`、`model_version`、`prompt_version`。常见 `kind`：

- `persona_bootstrap` / `persona_lock_violation`
- `episodic_write`
- `curator_ops`
- `retrieval`（含 plan、hits、分数分解；冲突检索另含 `conflicts`）
- `psych_upsert` / `relationship_delta` / `world_update`

库内审计表（或 in-memory 等价物）：`svc.stores.audit.list_for("persona_core")`、`unauthorized_persona_attempts()`。

---

## 13. 测试与评测

```bash
pytest                          # 单元 + eval 场景
pytest eval/scenarios           # 验收场景
pytest eval/rp/test_rp_smoke.py # 角色扮演基准接入冒烟（MockLLM，无需外部数据）
pytest -m integration           # 需要本机 PostgreSQL + 驱动
python -m eval.bench_retrieval --n 10000 --out eval/reports/retrieval_baseline.md
python -m eval.rp.fetch --bench charactereval   # 一次性下载到 eval/external/，之后离线
python -m eval.rp.run_eval --bench roleeval --mode both --limit 20
python -m eval.rp.run_eval --bench all --fixtures --mock --limit 20  # 无数据时的内置迷你题
```

Eval 场景覆盖：

- Persona 锁层攻击
- 冲突可发现、不覆盖
- 证据链完整；无证据语义写入被拒
- short psych TTL 过期
- Dual Memory 事实 / 解读分离
- 「喜欢猫」候选固化
- 02 贯穿案例检索
- 角色扮演基准（CharacterEval / RoleEval / TimeChara）raw vs harness 对照，见 `eval/rp/`

延迟基线对照 [02-inference-flow.md](02-inference-flow.md) §4 的 30–100 ms Harness 预算，**只出报告，不做硬性门禁**。近期一次 in-memory、1 万条 episodic 的参考：P95 ≈ 8 ms（见 `eval/reports/retrieval_baseline.md`）。

角色扮演评测：数据集与 CharacterRM 权重放到 `eval/external/`（gitignore），之后全程离线。CharacterEval 的 13 维分需要 `pip install -e ".[eval]"` 并下载 [BaichuanCharRM](https://huggingface.co/morecry/BaichuanCharRM)；缺失时报告标注 judge 不可用，不硬失败。RoleEval 公开 test CSV 无答案，带 `answer` 列的 dev CSV 才能算 accuracy。TimeChara 需同意 HuggingFace 非商业条款后自行导出 JSON/JSONL。报告写入 `eval/reports/rp_<bench>_<date>.md`，每条样本 `trace_ref` 回指 JSONL。

---

## 14. 常见错误

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `PersonaLockedError` | 对已存在的 Core 做 update/delete/覆盖 insert | 这是预期行为 |
| `EvidenceRequiredError` | 无证据写 episodic / semantic，或 Curator 试图直写 semantic | 补 `evidence` / `supporting_events`，走候选管线 |
| `ValidationError`（Pydantic） | `importance` 越界、空 evidence 列表等 | 按 01 JSON 示例填字段 |
| `query_with_conflicts` 抛 `ValueError` | 未设 `current_inference` | 传入 `CurrentInference` |
| 检索 Top-1 不是预期事件 | 未设 `now` 时区、未标 `event_type`、importance 过低 | 使用带 tz 的 datetime；承诺类事件设 `commitment_or_statement` |
| short 心情「一直在」 | 未写 `ttl_seconds` 或未用 `get_state(now=...)` | TTL 只在读路径生效 |
| `ZoneInfoNotFoundError: Asia/Shanghai` | Windows 无系统 tz 数据 | `pip install tzdata`（已列入依赖） |
| `ModuleNotFoundError: psycopg2` | 未装 postgres extra 或无 wheel | 用 in-memory，或在 WSL2 安装驱动 |
| PG 向量维度不匹配 | embedding 维度 ≠ 1024 | `NC_EMBEDDING_DIM` 与迁移保持一致 |
| 改了 `.env` 不生效 | `get_settings()` 缓存 | 显式 `Settings(...)` 传给 Service |

---

## 15. 给后续模块预留的接口

记忆库不负责「角色开口说话」。下游应按 02 文档把 `get_state()` + `query()` 的结果编进 `SituationState` / `ContextPackage`：

| 下游 | 从本库取什么 |
| --- | --- |
| Context Compiler | `ScoredMemory[]`、`PersonaCore`、`RelationshipState`、`PsychState`、`WorldState` |
| Social Interpreter | 同一套快照 + 检索命中（含 Dual Memory） |
| 数字身体 03 | 实现 `PerceptionBackend.observe() -> WorldState`，替换 chat 桩 |
| 训练数据 04 | JSONL trace + `evidence[]` 回指 trajectory |

替换存储不必改业务代码：实现 `harness.memory.store.protocols` 中的 Store Protocol 即可。

---

## 16. 与其他文档的关系

- 六层语义与评分公式： [01-cognitive-harness.md](01-cognitive-harness.md)
- 单轮推理如何消费记忆： [02-inference-flow.md](02-inference-flow.md)
- World State 的桌面采集： [03-digital-body.md](03-digital-body.md)
- 记忆离线演化与轨迹： [04-autonomy-and-data.md](04-autonomy-and-data.md)
- 里程碑 M2 与技术栈： [05-roadmap-and-stack.md](05-roadmap-and-stack.md)
- 仓库根目录速览： [README.md](../../README.md)
- GUI 调试面板：本文 §17

---

## 17. GUI 调试面板

入口：`python -m gui.server` → 顶栏 **调试**。独立对话框，用滑条 / 标签直接改 in-memory 记忆。事件页可以手动改字段，也可以重新走 Dual Memory / Curator。

| 分区 | 可改内容 | 控件 |
| --- | --- | --- |
| 关系 | `closeness` / `trust` / `attachment` / `recent_tension` 绝对值 | 0–1 滑条 |
| 心理 | L / M / S 三层 dict；short 保存时剥离 `set_at` / `expired` 并重盖 TTL | 数值滑条 + 文本 + 增删 key |
| 人格 | `name` 与四个字符串列表 | 文本 + 标签；**debug 绕过 Persona 锁**，`locked` 仍为 true |
| 事件 | 改 `importance` / 效价 / 摘要 / 事实 / 行动 / 结果 / 类型 / 解读；**新建**；**重新生成解读**；**再跑 Curator** | 滑条 + 文本 + 按钮。改摘要/事实会重算 embedding。demo 解读走规则，live 走当前 LLM（失败回退规则） |
| 语义 / 候选 | `confidence`、语义 `value`、候选 `status` | 滑条 / 文本 / 下拉 |
| 调参 | 六检索权重、固化阈值、`top_k`、半衰期等 | 滑条 + 数字；权重可 `normalize` |

`POST /api/debug/episodic`：带 `event_id` 为更新；不带 `event_id` 且有 `summary` 为新建（证据 `debug:{session}:{event_id}`）。`regenerate_insight=true` 按当前摘要/事实重写 Dual Memory；`curate=true` 对更新后的事件再跑 Curator。读写走 `GET /api/debug/state` 与 `POST /api/debug/{relationship,psych,persona,episodic,semantic,candidate,tuning}`。修改与 `/api/chat` 共用 `run_lock`。审计 `actor=debug_panel`，trace `kind=debug_edit`。人格绕锁的 `detail.bypass=persona_lock`。

**重置或切换 demo/live 会 `rebuild()`，调试修改全部丢弃。** 证据链仍强制非空。PostgreSQL 后端没有人格绕锁（触发器）。
