# 心智云图（NeuralCloud）Harness V1

**心智云图**是长期运行的角色智能体运行时（Character Cognitive Runtime）：为角色后训练模型提供记忆、状态、关系、语境与决策支撑。本仓库是其 V1 子系统——认知 Harness，按 [docs/harness/01-cognitive-harness.md](docs/harness/01-cognitive-harness.md) 与 [docs/harness/02-inference-flow.md](docs/harness/02-inference-flow.md) 落地的独立 Python 库：

- **记忆子系统**：六层记忆/状态、Dual Memory、Memory Writer 候选-证据链、六信号混合检索 + 反向检索、Memory Curator、Episodic 生命周期分层（uniqueness TTL + 命中升格 + 到期小模型审查）
- **在线推理九阶段**：Gateway → Parser → 并行状态/检索 → SituationState → Complexity Gate → 门控 Social Interpreter → Context Compiler → 主模型生成（含连说循环）→ Response Validator
- **主辅双模型路由**：主模型负责角色表达；parser / gate / interpreter / validator / curator / planner 等辅助 JSON 调用，以及内稳态空闲时的纯文本静默判定，可分流到 1–4B 小模型端点

**文档**：

- **项目模块进度（持续更新）**：[docs/PROJECT_PROGRESS.md](docs/PROJECT_PROGRESS.md) — 模块状态、验收缺口、下一步与更新记录；模块变更时同步维护。
- **BR-1 基本功能里程碑**：[目标与差距](docs/milestones/BASIC_RUNTIME_V1.md)、[测试案例](docs/milestones/BASIC_RUNTIME_V1_TEST_CASES.md)、[执行记录](docs/milestones/BASIC_RUNTIME_V1_EXECUTION.md) — 真实在线 8 小时、内稳态、Linux 桌面终端、对话与人设一致性。

**内稳态 Harness 研究设计（V2；确定性开发纵切已实现）**：[docs/harness/07-homeostatic-harness-research-design.md](docs/harness/07-homeostatic-harness-research-design.md)

**Runtime Supervisor 与三服务架构（V2 Runtime 核心与 Windows 四进程开发拓扑已实现）**：[docs/harness/08-runtime-supervisor.md](docs/harness/08-runtime-supervisor.md)

可视化 GUI 已接入（阶段级实时推送）。本轮**不包含** token 流式生成 / TTS、数字身体采集、Experience Evaluator。默认 MockLLM，测试不依赖 GPU。

- 技术文档（已落地代码的架构、数据流与实现细节）：[docs/TECHNICAL.md](docs/TECHNICAL.md)
- 记忆使用说明：[docs/harness/06-memory-subsystem-usage.md](docs/harness/06-memory-subsystem-usage.md)
- 设计论证：[docs/harness/](docs/harness/) 文档集 00–06

可视化 GUI 已接入（阶段级实时推送 + 调试面板）。V1 **不包含** token 流式生成 / TTS、数字身体采集、Experience Evaluator。默认 MockLLM，测试不依赖 GPU。

## 架构

```text
Raw Message
    -> Gateway                 InteractionEvent
    -> Context Parser          ParsedInteraction（hypothesis + confidence）
    -> 并行 get_state + query  CharacterSnapshot + ScoredMemory[]
    -> SituationState Builder  SituationState
    -> Complexity Gate         Fast Path / Deep Path（规则 + Gate 小模型双路 OR，宁深勿浅）
    -> Social Interpreter      StructuredInsight（仅 Deep；含 constraint 红线）
    -> Context Compiler        ModelContext（~3k token + provenance）
    -> Character Model         CandidateResponse（段尾 [CONTINUE] / [END] 连说循环）
    -> Response Validator      局部重生成 → FinalResponse
```

模块之间只传结构化对象（Pydantic），不传自然语言小作文。记忆写/检索与推理各阶段都落 JSONL trace；成功调用另落一份全链路 JSON（`traces/calls/`）。

越好的 Harness，主模型承担的工作越纯粹：Harness 完成 Anchoring / Selecting / Bounding，主模型只做 Enacting。

## 快速开始

Windows 11 上的推荐入口是根目录 [`dev.ps1`](dev.ps1)。它统一检查、准备、部署和启动相邻的 MistyAI 工程与本仓库；默认目录布局为：

```text
<parent>/
├── MistyAI/
└── NeuralCloud/
```

第一次运行先检查环境，再启动：

```powershell
.\dev.ps1 doctor
.\dev.ps1 up
```

启动拓扑如下：

```text
Windows 11
├── NeuralCloud（Windows Python 3.12；Cognitive / Homeostasis / Runtime / GUI）
├── WSL2 Ubuntu 24.04
│   └── MistyAI Body Service（systemd）
└── Docker Desktop（Linux containers）
    └── MistyAI desktop / agent / loopback-gateway
```

`up` 会检查 Windows、Git、Python 3.12、WSL2、Ubuntu 24.04、Docker Desktop/Compose、WSL systemd、uv 0.12.9 及端口占用。交互模式会对缺失依赖的安装或 Windows 系统功能变更逐项确认；执行 `install`/`up` 本身即授权依赖就绪后的 WSL root 部署与服务操作，这些操作会显示脱敏的阶段提示。`-NonInteractive` 不自动安装缺失依赖，但前置已满足时仍执行所请求的部署或启停。Docker Desktop 首次协议、WSL integration 和系统重启仍需按提示手动完成，之后重新运行同一命令即可续跑。

```powershell
.\dev.ps1 up                         # 检查、部署并启动两套工程
.\dev.ps1 down                       # 先停 NeuralCloud，再停 MistyAI
.\dev.ps1 restart
.\dev.ps1 status
.\dev.ps1 install                    # 只准备环境，不启动
.\dev.ps1 logs -Service all
```

常用参数包括 `-MistyAIPath <path>`（默认 `..\MistyAI`）、`-Distro <name>`（默认 `Ubuntu-24.04`）、`-Mode demo|live|ollama`、`-NonInteractive`、`-NoOpen` 和 `-Json`。这里的 `demo` 只表示 NeuralCloud 使用 MockLLM；统一入口始终使用真实 MistyAI 与 `grounded-mistyai-v1.json`，身体服务、身份或能力协商失败时会中止，不会退回 simulator。成功后可访问 GUI <http://127.0.0.1:8766>、运维页 <http://127.0.0.1:8766/ops> 和 noVNC <http://127.0.0.1:6080>。完整命令、退出码与故障排查见 [新手入门](docs/GETTING_STARTED.md#3-windows-统一启动入口)。

只调试 NeuralCloud 时，仍可手动准备 Python 环境：

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

## Runtime Supervisor

`harness.runtime` 提供 `RuntimeSupervisor`、journal/checkpoint、事件队列、心跳、watchdog 和认证控制面；`harness.homeostasis` 提供七维确定性状态、belief、情感、策略排序、幂等 ledger 与本地持久化。Windows `start.ps1` 默认通过 `HttpCognitiveClient` / `HttpHomeostasisClient` 启动 Cognitive、Homeostasis、Runtime、GUI 四个独立进程；`PassiveHomeostasisAdapter` 仅保留给隔离测试。

Runtime 的编排事实写入 hash-chain 分段 JSONL journal，领域状态仍由 Cognitive/Homeostasis store 负责；checkpoint 只保存启动索引和领域游标。相关恢复、幂等、暂停汇总和队列降载验收见 `tests/test_runtime_supervisor.py`。

### Embodiment Port 与能力协商

`harness.embodiment` 现在是 Action、Perception、BodyState 与 Capability ports 的唯一所有者。Runtime 启动时依据 manifest 的 allowlist/required requirements 协商并冻结 activation set；Provider 后续重连只能恢复本次启动已授权的能力。`NC_EMBODIMENT_PROVIDER=none|mistyai|simulator` 只在 composition root 选择实现，Core 不按 Provider 名称分支。manifest 未配置 `embodiment` 时仍按 chat-only 模式启动。

MistyAI adapter 支持 typed action、独立 verify、safe-point、reconcile、下载并校验内容寻址 evidence，以及可选 `physiology.body-state.v1`。Grounded 部署使用显式 token/token file 且执行严格就绪判定；生产 Cognitive 可把受信任的结构化 action request 编译为安全 `GoalProposal`，不会从普通聊天文本猜测动作。Simulator 不使用 HTTP 或 MistyAI DTO，可脚本化 success/failure/unknown、健康变化及 energy terminal。配置与黑盒验收顺序见 [Embodiment 运行手册](docs/EMBODIMENT_RUNBOOK.md)。

空闲心跳不会直接调用角色主模型：Runtime 先让 Cognitive 使用辅助模型做 `SILENT` / `SPEAK` 纯文本判定，并把原始判定写入 `data/cognitive/autonomous/YYYYMMDD/*.txt`。`SILENT` 不进入 Session 或记忆；`SPEAK` 才写一条 `internal_reflection` 并调用主模型表达。若辅助模型未配置、失败或输出无效，会按故障策略用主模型做一次同格式判定；用户消息在判定前或判定期间到达都会取消后续主动表达。

## 可视化 GUI

三栏 Web 界面：左侧角色对话，中间九阶段推理时间线，右侧 Persona / 关系 / 心理 / 世界 / 记忆及七维 Homeostasis。顶栏可查看四服务健康、Runtime 生命周期、manifest/profile revision；内稳态面板展示 belief、目标带、precision、情感、候选策略并可在开发模式保存新的参数 revision。保存不会热改运行实例，重新执行 `start.ps1` 后原子激活。

完整四进程拓扑另提供 `http://127.0.0.1:8766/ops` 独立运维页：实时和最近 7 天的 Runtime 事件、journal、队列趋势、推理运行、watchdog、Embodiment 与 ActionExecution 可关联下钻；`development_debug` 模式可直接暂停/恢复、写 checkpoint、注入调试事件并处理动作/交付对账。监控历史保存在可重建的 `data/runtime/observability.sqlite3`，不会取代 hash-chain journal 或调用日志。

```bash
pip install -e ".[dev,gui]"
python -m gui.server  # 兼容的单进程 GUI
# 浏览器打开 http://127.0.0.1:8766
# 完整拓扑启动后打开 http://127.0.0.1:8766/ops
```

`start.ps1` / `stop.ps1` 保留为 NeuralCloud 四进程的低层启停器（已在运行则原子重启，分服务日志写入 `logs/`）；它们不会检查或安装前置依赖，也不会部署 MistyAI。日常双工程开发请使用 `dev.ps1`。`start.sh` / `stop.sh` 当前仍是兼容的单进程 GUI 路径。

可直接运行 `./start.ps1`，无需重复填写参数：manifest/profile 依次使用待激活配置、上次启动配置或内置 demo 默认值。若配置使用本机 MistyAI（`http://127.0.0.1:8787` 或 `http://localhost:8787`）且未提供令牌，脚本会从 `dev.ps1` 已配置的 WSL 服务读取现有凭据；优先使用该工程记录的 WSL 发行版，默认 `Ubuntu-24.04`。显式令牌/令牌文件仍优先，远程 MistyAI 仍需单独配置凭据。

默认 **demo 模式**：内存记忆 + MockLLM + 小雾（Misty）出厂预设（`presets/builtin/misty.json`）。该预设完整导入相邻 MistyDigitalBody 冻结设定包 v0.2.0 的姓名、年龄、生日、身高、爱好、人格、表达基线、自我外观认知与背景物品，并以干净关系/记忆状态启动，避免继承旧角色经历。旧 Vaelith 模板仍保留在预设库中，但不再是默认角色。顶栏「预设」可打开/套用模板；调试与对话写回 `data/presets/` 工作副本。可在顶栏「设置」切换模式：`demo`（Mock）、`live`（任意 OpenAI 兼容端点）、`ollama`（未填 URL 时默认 `http://127.0.0.1:11434/v1`）。端点变量为 `NC_LLM_BASE_URL` / `NC_LLM_API_KEY`（代码默认 `http://127.0.0.1:8080/v1`）；另可配 `NC_AUX_LLM_*` 辅助小模型端点。切换模式时不会丢掉当前预设存档；对话阶段才会请求 LLM。

### live 模式本地模型栈（推荐）

`.env.example` 给出 Ollama 双模型推荐配置（RTX 5090 32GB 可两模型同驻），复制为 `.env` 即可生效：

```bash
# 主模型：Qwen3-30B-A3B-Instruct-2507 abliterated Q4_K_M（~19GB，非 thinking，适合角色演出）
ollama pull huihui_ai/qwen3-abliterated:30b-a3b-instruct-2507-q4_K_M
ollama create neuralcloud-main -f infra/llm/Modelfile.character   # num_ctx 限 16384，给辅助模型腾显存

# 辅助模型：承担全部 complete_json 辅助调用，keep_alive=-1 常驻、num_ctx=8192
ollama pull qwen3:4b-instruct   # 必须用 instruct 标签；thinking 版会输出 <think> 块破坏 JSON 解析
```

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

事件写入即 `tier=short` 并打 uniqueness 与到期时间：低 uniqueness 几分钟即到期，到期由小模型审查决定升格 `mid`/`long` 或标记 `forgotten`；检索反复命中也会推动 `short → mid → long` 升格。遗忘是标记不是删除，`forgotten` 不进检索池，全程可审计。

## 配置

检索权重、固化阈值、推理分流与连说在 [`harness/config.py`](harness/config.py) 的 `Settings`（环境变量前缀 `NC_`，支持 `.env`）：

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
| call_log_enabled | false（GUI 强制 true） | 成功调用全链路 JSON，`traces/calls/YYYYMMDD/{call_id}.json` |
| continuation_enabled | true | 02 §8.1 连说循环 |
| max_reply_segments | 3 | 连说段数硬上限 |
| tier_promotion_hits_mid / long | 3 / 8 | 01 §2.2 命中升格阈值 |
| uniqueness_ttl_* | 5min–7天 | 01 §2.2 短期留存窗口 |

LLM / embedding 走 OpenAI 兼容 HTTP 端点：`RoutingLLMClient` 把 `complete_json` 辅助调用和 `complete_aux_text_result` 空闲判定路由到 `NC_AUX_LLM_*` 端点，`complete_chat` 角色生成走 `NC_LLM_*` 主端点。未配置独立 aux 时，普通 JSON helper 延续既有回退；空闲判定显式标为 `main_fallback`。纯文本判定目录可用 `NC_COGNITIVE_AUTONOMOUS_LOG_DIR` 覆盖。完整配置项（aux / embedding / tier / planner 开关、版本戳等）见 [docs/TECHNICAL.md](docs/TECHNICAL.md) §10。测试使用 mock，不依赖真实 GPU。流式生成预留参数，本期不实现。

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
| 01 §2.2 Episodic 生命周期 tier | [`harness/memory/tier.py`](harness/memory/tier.py)（uniqueness TTL / 命中升格 / 到期审查） | `tests/test_tier.py` |
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
| 02 §8.1 连说循环 | [`harness/pipeline/continuation.py`](harness/pipeline/continuation.py)（近重复截断） | `tests/test_continuation.py` |
| 02 §阶段 9 Validator | [`harness/validator/validator.py`](harness/validator/validator.py) | `tests/test_validator.py` |
| 02 九阶段编排 | [`harness/pipeline/pipeline.py`](harness/pipeline/pipeline.py) | `tests/test_pipeline.py`, `eval/scenarios/test_inference_scenarios.py` |
| 08 Runtime Supervisor | [`harness/runtime/`](harness/runtime/)（事件循环 / journal / checkpoint / action / watchdog / control / RPC） | `tests/test_runtime_supervisor.py` |
| 主/辅双端点 LLM 路由 | [`harness/llm/client.py`](harness/llm/client.py) `RoutingLLMClient` | `tests/test_aux_llm.py` |
| 可视化 GUI（阶段观察 + FastAPI/WS） | [`gui/server.py`](gui/server.py), [`gui/observer.py`](gui/observer.py) | `tests/test_gui.py` |
| GUI 调试面板（含 debug 绕锁审计） | [`gui/debug.py`](gui/debug.py), [`gui/persist.py`](gui/persist.py) | `tests/test_debug_panel.py` |
| GUI 角色预设库 | [`gui/presets.py`](gui/presets.py), [`presets/builtin/`](presets/builtin/) | `tests/test_presets.py` |
| 00 原则 6/9 provenance | `infra/trace/recorder.py`, `infra/trace/call_log.py`, `memory_ops_log`, Compiler provenance | `tests/test_llm_and_trace.py`, `tests/test_call_log.py`, `tests/test_compiler.py` |
| 05 §2 Memory 可追溯率 / contradiction / Persona 非授权修改=0 | 全链路 evidence + conflict + lock | `eval/scenarios/test_memory_scenarios.py` |
| 角色扮演基准 raw vs harness | [`eval/rp/`](eval/rp/) | `eval/rp/test_rp_smoke.py` |

## 仓库布局

```text
harness/core/            数据模型：models.py（记忆侧）+ inference.py（推理侧）
harness/memory/          六层记忆 / Dual / tier 生命周期 / Writer / 检索 / 存储抽象
harness/pipeline/        Gateway / Parser / State Builder / 连说循环 / SessionStore / InferencePipeline
harness/runtime/         Runtime Supervisor / 队列 / 调度 / journal / checkpoint / 动作 / 控制面 / RPC adapter
harness/router/          Complexity Gate（Fast/Deep）
harness/subagents/       Social Interpreter
harness/compiler/        Context Compiler
harness/validator/       Response Validator
harness/llm/             OpenAI 兼容客户端 + 主辅双端点路由 + CharacterModel + mock
infra/db/                docker-compose + Alembic
infra/llm/               Modelfile.character（Ollama 主模型定义）
infra/trace/             JSONL tracer + 成功调用全链路 CallLog
gui/                     FastAPI + WebSocket 可视化（对话 / 九阶段 / 调试面板 / 预设库 / 配置持久化）
presets/builtin/         出厂角色模板（小雾为默认；Vaelith 模板仅供显式选择）
eval/                    场景评测、检索延迟基线、角色扮演基准（CharacterEval / RoleEval / TimeChara）
tests/                   单元与集成测试
docs/                    TECHNICAL.md 技术文档 + harness/ 设计文档集 00–06
start.* / stop.*         Windows / WSL2 一键启停脚本
```
