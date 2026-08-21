# NeuralCloud Harness V1

按 [docs/harness/01-cognitive-harness.md](docs/harness/01-cognitive-harness.md) 与 [docs/harness/02-inference-flow.md](docs/harness/02-inference-flow.md) 落地的独立 Python 库：

- **记忆子系统**：六层记忆/状态、Dual Memory、Memory Writer 候选-证据链、混合检索 + 反向检索、Memory Curator
- **在线推理九阶段**：Gateway → Parser → 并行状态/检索 → SituationState → Complexity Gate → 门控 Social Interpreter → Context Compiler → 主模型生成 → Response Validator

**记忆使用说明**：[docs/harness/06-memory-subsystem-usage.md](docs/harness/06-memory-subsystem-usage.md)

可视化 GUI 已接入（阶段级实时推送）。本轮**不包含** token 流式生成 / TTS、数字身体采集、Experience Evaluator。默认 MockLLM，测试不依赖 GPU。

## 架构

```text
Raw Message
    -> Gateway                 InteractionEvent
    -> Context Parser          ParsedInteraction（hypothesis + confidence）
    -> 并行 get_state + query  CharacterSnapshot + ScoredMemory[]
    -> SituationState Builder  SituationState
    -> Complexity Gate         Fast Path / Deep Path
    -> Social Interpreter      StructuredInsight（仅 Deep；含 constraint 红线）
    -> Context Compiler        ModelContext（~2–4k token + provenance）
    -> Character Model         CandidateResponse
    -> Response Validator      局部重生成 → FinalResponse
```

模块之间只传结构化对象（Pydantic），不传自然语言小作文。记忆写/检索与推理各阶段都落 JSONL trace，并带 `schema_version`。

越好的 Harness，主模型承担的工作越纯粹：Harness 完成 Anchoring / Selecting / Bounding，主模型只做 Enacting。

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -e ".[dev]"
# 若本机有 PostgreSQL 驱动 wheel：pip install -e ".[dev,postgres]"

# 可选：PostgreSQL + pgvector
docker compose -f infra/db/docker-compose.yml up -d
alembic -c infra/db/alembic.ini upgrade head

pytest
```

PostgreSQL 驱动为可选 extra：`pip install -e ".[dev,postgres]"`（`postgresql+psycopg2://`）。当前 Windows ARM64 可能没有现成 wheel，集成测试在驱动或数据库不可用时自动 skip。WSL2 Ubuntu 上建议安装 `psycopg` v3 或 `psycopg2-binary`。

无 GPU、无 Docker 时，默认 in-memory store + MockLLM/MockEmbedding，单元测试与 eval 场景仍可全绿。

## 可视化 GUI

三栏 Web 界面：左侧角色对话，中间九阶段推理时间线（每阶段完整结构化输出），右侧 Persona / 关系 / 心理 / 世界 / 记忆计数。发送后通过 WebSocket **按阶段**推送结果（非 token 流式）。

```bash
pip install -e ".[dev,gui]"
python -m gui.server
# 浏览器打开 http://127.0.0.1:8765
```

默认 **demo 模式**：内存记忆 + MockLLM + Vaelith 种子（`evt_09010`–`evt_09018` 共 9 条角色设定事件，覆盖姓氏来历、招募考核、雷点暴怒、深夜莫扎特等背景故事）。可在顶栏「设置」切换到真实 OpenAI 兼容端点（`NC_LLM_BASE_URL` / `NC_LLM_API_KEY`，默认 `http://127.0.0.1:8080/v1`）。切换 live 时种子数据不会调用远端模型；对话阶段才会请求 LLM。

建议先发 `"早上好。"` 看 Fast Path，再发 `"你昨天不是说不会再管我了吗？"` 看 Deep Path（Social Interpreter + Validator 局部重生成）。

## 推理链路用法

```python
from harness.core.models import InteractionEvent
from harness.memory.service import build_inmemory_service
from harness.pipeline import InferencePipeline
from tests.factories import NOW, seed_running_example, vaelith

svc = build_inmemory_service()
svc.bootstrap_persona(vaelith())
seed_running_example(svc)

pipe = InferencePipeline(svc)
final = pipe.respond(
    InteractionEvent(
        event_id="evt_04030",
        timestamp=NOW,
        speaker="user",
        raw_text="你昨天不是说不会再管我了吗？",
        session_id="sess_0881",
    ),
    now=NOW,
)
print(final.path, final.revisions, final.text)
```

`"早上好。"` 走 Fast Path（不触发 Social Interpreter）。贯穿案例走 Deep Path：Interpreter 给出 `avoid_explicit_psychological_explanation` 约束，Validator 拦截直接道歉/直接关心，只做局部重生成。

也接受原始字符串：`pipe.respond("早上好。", session_id="sess_1")`。

## 记忆 API 示例

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from harness.core.models import InteractionEvent, RetrievalQuery
from harness.memory.service import build_inmemory_service
from tests.factories import vaelith

TZ = ZoneInfo("Asia/Shanghai")
svc = build_inmemory_service()
svc.bootstrap_persona(vaelith())

svc.record_event(
    InteractionEvent(
        timestamp=datetime.now(TZ),
        speaker="user",
        raw_text="用户提到上周收养的猫生病了",
        session_id="sess_1",
        evidence=["traj:step_1"],
    )
)

hits = svc.query(RetrievalQuery(text="那只猫后来怎样了？", now=datetime.now(TZ)))
print(hits[0].ref, hits[0].why_retrieved)
```

Persona Core 一经写入即锁定：`update_persona` / `delete_persona` 以及数据库 UPDATE/DELETE 触发器都会拒绝，并写入 `memory_ops_log`。

长期语义记忆不能由模型输出直写，必须走候选-证据链；缺少 `evidence` / `supporting_events` 的写入会被拒绝。

## 配置

检索权重、固化阈值与推理分流在 [`harness/config.py`](harness/config.py) 的 `Settings`（环境变量前缀 `NC_`）：

| 键 | 默认 | 来源 |
| --- | --- | --- |
| retrieval_weights.semantic | 0.30 | 01 §4.1 |
| retrieval_weights.entity | 0.15 | 01 §4.1 |
| retrieval_weights.recency | 0.10 | 01 §4.1 |
| retrieval_weights.importance | 0.15 | 01 §4.1 |
| retrieval_weights.relationship | 0.15 | 01 §4.1 |
| retrieval_weights.persona | 0.15 | 01 §4.1 |
| consolidation_threshold | 0.70 | Writer 管线 |
| confidence_increment | 0.35 | 证据累积 |
| complexity_threshold | 0.5 | 02 Fast/Deep 分流 |
| context_token_budget | 3000 | Compiler 预算 |
| compiler_top_memories | 5 | Compiler 记忆条数 |
| validator_sample_rate | 0.2 | Fast Path Validator 采样 |
| max_revisions | 2 | 局部重生成上限 |
| session_window | 10 | 近期对话窗口 |
| record_interaction | true | respond 后写回记忆 |

LLM / embedding 默认走 OpenAI 兼容 HTTP 端点（`complete_json` / `complete_chat`）；测试使用 mock，不依赖真实 GPU。流式生成预留参数，本期不实现。

## 评测

```bash
pytest eval/scenarios
pytest eval/rp/test_rp_smoke.py
python -m eval.bench_retrieval --n 10000 --out eval/reports/retrieval_baseline.md
python -m eval.rp.run_eval --bench roleeval --mode both --limit 20
python -m eval.rp.run_eval --fixtures --mock --bench all
```

延迟报告对照 02 文档 §4 的 30–100ms 预算，**只出报告，不做硬性门禁**。角色扮演基准（CharacterEval / RoleEval / TimeChara）支持裸模型与 Harness 全链路对照，数据放在 `eval/external/`，用法见 [docs/harness/06-memory-subsystem-usage.md](docs/harness/06-memory-subsystem-usage.md) §13。

## 需求可追溯对应表

| 文档机制 | 实现 | 测试 |
| --- | --- | --- |
| 01 §2.1 Persona Core LOCKED | [`harness/memory/service.py`](harness/memory/service.py) + DB trigger [`harness/memory/store/schema.py`](harness/memory/store/schema.py) | `tests/test_persona_lock.py`, `eval/scenarios/test_memory_scenarios.py` |
| 01 §2.2 Episodic（事件 + evidence） | `EpisodicEvent` + `record_event` | `tests/test_models.py`, `tests/test_writer.py` |
| 01 §2.3 Semantic + 证据链 | `WriterPipeline._maybe_consolidate` | `tests/test_writer.py` cat 固化 |
| 01 §2.4 Relationship | `apply_relationship_delta` | `tests/test_state_layers.py` |
| 01 §2.5 Psych L/M/S + short TTL | `upsert_psych` / `apply_psych_ttl` | `tests/test_state_layers.py` |
| 01 §2.6 World State + PerceptionBackend 桩 | `ChatPerceptionBackend`, `update_world` | `tests/test_state_layers.py` |
| 01 §3 Dual Memory | [`harness/memory/dual.py`](harness/memory/dual.py) | `tests/test_writer.py`, eval dual 场景 |
| 01 §4.1 检索计划 + 六信号混合检索 | `retrieval/planner.py`, `signals.py`, `hybrid.py` | `tests/test_retrieval.py` 贯穿案例 |
| 01 §4.2 反向检索 | `retrieval/conflict.py` | `tests/test_retrieval.py` |
| 01 §5 Memory Writer 管线 | `writer/pipeline.py`, `writer/confidence.py` | `tests/test_writer.py` |
| 01 §6.1 Memory Curator ops | `writer/curator.py` | `tests/test_writer.py` |
| 01 §6.2 Social Interpreter | [`harness/subagents/interpreter.py`](harness/subagents/interpreter.py) | `tests/test_interpreter.py` |
| 01 §7 Context Compiler | [`harness/compiler/compiler.py`](harness/compiler/compiler.py) | `tests/test_compiler.py` |
| 02 §阶段 1 Gateway | [`harness/pipeline/gateway.py`](harness/pipeline/gateway.py) | `tests/test_pipeline.py` |
| 02 §阶段 2 Parser | [`harness/pipeline/parser.py`](harness/pipeline/parser.py) | `tests/test_parser.py` |
| 02 §阶段 3–5 并行状态 + SituationState | [`harness/pipeline/state_builder.py`](harness/pipeline/state_builder.py) | `tests/test_pipeline.py` |
| 02 §3 Fast/Deep Path | [`harness/router/gate.py`](harness/router/gate.py) | `tests/test_gate.py` |
| 02 §阶段 8 Character Model | [`harness/llm/character.py`](harness/llm/character.py) | `tests/test_llm_and_trace.py`, `tests/test_pipeline.py` |
| 02 §阶段 9 Validator | [`harness/validator/validator.py`](harness/validator/validator.py) | `tests/test_validator.py` |
| 02 九阶段编排 | [`harness/pipeline/pipeline.py`](harness/pipeline/pipeline.py) | `tests/test_pipeline.py`, `eval/scenarios/test_inference_scenarios.py` |
| 可视化 GUI（阶段观察 + FastAPI/WS） | [`gui/server.py`](gui/server.py), [`gui/observer.py`](gui/observer.py) | `tests/test_gui.py` |
| 00 原则 6/9 provenance | `infra/trace/recorder.py`, `memory_ops_log`, Compiler provenance | `tests/test_llm_and_trace.py`, `tests/test_compiler.py` |
| 05 §2 Memory 可追溯率 / contradiction / Persona 非授权修改=0 | 全链路 evidence + conflict + lock | `eval/scenarios/test_memory_scenarios.py` |
| 角色扮演基准 raw vs harness | [`eval/rp/`](eval/rp/) | `eval/rp/test_rp_smoke.py` |

## 仓库布局

```text
harness/core/            数据模型（记忆 + SituationState / ModelContext）
harness/memory/          六层记忆 / Writer / 检索
harness/pipeline/        Gateway / Parser / State Builder / InferencePipeline
harness/router/          Complexity Gate（Fast/Deep）
harness/subagents/       Social Interpreter
harness/compiler/        Context Compiler
harness/validator/       Response Validator
harness/llm/             OpenAI 兼容客户端 + CharacterModel + mock
infra/db/                docker-compose + Alembic
infra/trace/             JSONL tracer
gui/                     FastAPI + WebSocket 可视化（对话 / 九阶段 / 子系统）
eval/                    场景评测、检索延迟基线、角色扮演基准（CharacterEval / RoleEval / TimeChara）
tests/                   单元与集成测试
```
