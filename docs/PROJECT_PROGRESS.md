# NeuralCloud 项目模块进度表

> 持续维护文档：模块实现、测试、验收或计划发生变化时，同步更新对应行和文末变更记录。
>
> 最近核对：**2026-09-11（Asia/Shanghai；本次增量为 Docker 启动防护与 QQ 桌面实机接入，其余条目保留各自核对日期）**。基线：`codex/grounded-agency`，HEAD `159d55d`，**包含核对时的未提交修改**。本表描述当前工作区，不代表已合入主分支或已部署。

当前认知 Harness 主体、Runtime、内稳态、具身接入和开发界面已有实现；Grounded Agency GA-01～GA-08 已有工程代码，但真实模型的自我维持、长期自主与社会泛化仍待实验验证。角色文本导入、专用启动情境链路、关系 belief/care 层和训练数据导出仍有明确缺口。

**当前优先里程碑：[BR-1 基本功能可用](milestones/BASIC_RUNTIME_V1.md)**。用户确认真实在线/无干扰稳定 **8 小时**，通过 MistyAI **桌面终端**执行命令，并能对话、保持前后人设。已执行基础修复并建立[测试案例](milestones/BASIC_RUNTIME_V1_TEST_CASES.md)，真实 Linux 与 8 小时验收仍未完成；详细结果见[执行记录](milestones/BASIC_RUNTIME_V1_EXECUTION.md)。

**2026-09-10 新增当前任务：[QQ-DESKTOP-1](milestones/QQ_DESKTOP_V1.md)**。至 2026-09-11，Docker 启动防护已安装并通过两轮独立实机复验，见 [Docker 恢复说明](DOCKER_RECOVERY.md)。QQ 已登录并核验目标群，基础两人持久记忆、旧历史检索及专用服务重启后召回通过。用户已授权指定群自动截图和实际发送，[QQ 专用 watcher](QQ_WATCHER.md) 已实际完成第二次 @ 测试的采集、作者归属和 `speak`。自动交付多次被预点击核验误判阻断后，操作方以已授权 GUI 动作辅助发送同一份未改写、未重新生成的 38 字原回复，实际核验新气泡与空输入框；本轮新增 1 条，累计 2 条。后台已恢复，同进程继续采集且重复启动返回已有实例；这不等于无人辅助交付通过，自动发信稳定性及长期自主运行仍为 PARTIAL，标准 Runtime 与全量 Cognitive 社会关系未验收。当前 QQ 工程套件 **389 passed**、共享 LLM **28 passed**、Ruff 通过，见 [自动盯群与辅助交付实测](milestones/evidence/qq-autowatch-2026-09-11.json)；[多人记忆与专用决策实测](milestones/evidence/qq-multiperson-autonomy-2026-09-11.json)和[首轮登录与发送证据](milestones/evidence/qq-desktop-login-memory-2026-09-11.json)保留，BR-1 原有出口不变。

## 1. 状态与证据口径

| 标记 | 含义 |
| --- | --- |
| 已实现 | 表中列出的能力有实际代码及对应测试文件；不等于本次测试通过、生产验收通过或研究假设成立。 |
| 部分实现 | 已有可用基础或局部链路，完整设计仍有缺项。 |
| 设计阶段 | 有方案或接口预留，尚未形成目标能力的完整实现。 |
| 待验收 | 已有代码，仍需指定环境、真实模型、跨仓库或长时运行的验证。可与“已实现”并列。 |
| 验收未通过 | 已有报告给出负面结果；修复和复验完成前保留此结论，并注明报告日期与范围。 |

- **不估算完成百分比**：当前各模块没有统一、可计数的验收清单，使用能力状态与具体缺口跟踪进度。
- 2026-09-08 初版为静态核对；2026-09-09 已运行 BR-1 工程回归和真实模型四轮探测，结果见里程碑执行记录。下表未更新日期的项目仍保持初版证据范围；测试链接本身不等于本轮通过。
- 历史报告仅支持其记录的版本、环境与实验范围；确定性 fake/simulator 结果不能替代真实模型效果。
- 下表“下一步”包括缺项与维护建议，不代表已经排期；未明确认领的事项不预设负责人或截止日期。

## 2. 认知与记忆模块

| ID | 模块 | 状态 | 已实现能力 | 下一步 / 验收边界 | 依据 | 核对日期 |
| --- | --- | --- | --- | --- | --- | --- |
| NC-01 | 核心模型、契约与配置 | 已实现 | 记忆/推理 Pydantic 模型、跨服务 envelope、决策契约、版本化 manifest/profile 与配置校验。 | 新字段继续保持版本与幂等边界一致；研究条件变更须冻结新配置。 | [core](../harness/core/)、[contracts](../harness/contracts/)、[manifest](../harness/research/manifest.py)、[契约测试](../tests/test_service_contracts.py) | 2026-09-08 |
| NC-02 | 六层记忆与状态 | 已实现 | Persona 锁定；Episodic、Semantic、Relationship、L/M/S Psych、World；事实与角色解读分层。 | 在真实多轮对话中复验一致性、证据可追溯性与矛盾发现能力。 | [MemoryService](../harness/memory/service.py)、[Dual Memory](../harness/memory/dual.py)、[状态测试](../tests/test_state_layers.py)、[人格锁测试](../tests/test_persona_lock.py) | 2026-09-08 |
| NC-03 | 检索与反向检索 | 已实现 | 检索计划、六信号混合排序、矛盾检索、检索来源说明。 | 反证规则的泛化效果待验；使用目标 embedding 和实际数据量补性能/质量基线，当前基准使用 in-memory 与 MockEmbedding。 | [retrieval](../harness/memory/retrieval/)、[检索测试](../tests/test_retrieval.py)、[基准](../eval/bench_retrieval.py) | 2026-09-08 |
| NC-04 | Memory Writer 与生命周期 | 已实现 | 候选→证据→置信度→固化；Curator；uniqueness TTL、命中升格、到期审查、遗忘标记。 | 继续验证真实辅助模型的固化质量；forgotten 记忆复活尚未实现。 | [writer](../harness/memory/writer/)、[tier](../harness/memory/tier.py)、[Writer 测试](../tests/test_writer.py)、[生命周期测试](../tests/test_tier.py) | 2026-09-08 |
| NC-05 | 在线推理与分流 | 已实现 | 九阶段编排、Gateway/Parser、并行状态与检索、Fast/Deep Gate、Social Interpreter、Session、连说与局部重生成。 | Fast Path `state_cache` 仍为预留；补目标模型下的端到端延迟与质量验收。 | [pipeline](../harness/pipeline/)、[Gate](../harness/router/gate.py)、[Interpreter](../harness/subagents/interpreter.py)、[推理测试](../tests/test_pipeline.py) | 2026-09-08 |
| NC-06 | 上下文编译与响应校验 | 已实现；真实长对话待验收 | Context Compiler 预算与 provenance；最新完整会话优先保留，避免旧历史截走最近约定；人格检查与限次修正。 | 默认小雾 persona 未被截断；20 轮一致性与专用情境增强仍待验。 | [Compiler](../harness/compiler/compiler.py)、[Validator](../harness/validator/validator.py)、[人设/会话回归](../tests/test_basic_persona_continuity.py) | 2026-09-09 |
| NC-07 | 主辅 LLM 与 Embedding | 已实现 | OpenAI 兼容客户端、主辅调用路由、Mock、embedding 客户端与降级处理。 | token 流式生成仍显式未实现；TTS 未实现；真实端点质量与延迟需单独验收。 | [llm](../harness/llm/)、[路由测试](../tests/test_aux_llm.py)、[调用测试](../tests/test_llm_and_trace.py) | 2026-09-08 |
| NC-08 | 记忆存储与数据库 | 已实现；集成待验收 | in-memory/PostgreSQL Store、pgvector、Alembic、Persona 数据库锁与持久化契约。 | 在实际 PostgreSQL 环境复验；驱动/数据库不可用时集成测试会 skip。PG 人格 debug 绕锁不支持，是既定边界。 | [store](../harness/memory/store/)、[数据库配置](../infra/db/)、[PG 集成测试](../tests/test_postgres_integration.py) | 2026-09-08 |

## 3. Runtime、内稳态与具身模块

| ID | 模块 | 状态 | 已实现能力 | 下一步 / 验收边界 | 依据 | 核对日期 |
| --- | --- | --- | --- | --- | --- | --- |
| NC-09 | Runtime Supervisor | 已实现；8 小时待验收 | 事件队列/心跳/watchdog、认证、交付提交与恢复；journal 有界正文、流式恢复及命令序号索引；显式终端多步桥和新消息中断。 | 元数据和部分其他账本仍随历史增长；按 BR-1 验收真实进程/状态稳定性。 | [runtime](../harness/runtime/)、[journal 回归](../tests/test_runtime_journal_bounded.py)、[终端集成](../tests/test_basic_terminal_runtime.py)、[BR-1](milestones/BASIC_RUNTIME_V1.md) | 2026-09-09 |
| NC-10 | Cognitive 服务与空闲主动表达 | 已实现（基础） | Prepare/Commit 与持久化；辅助模型 SILENT/SPEAK 判定、失败回退、用户消息抢占、主动表达审计。 | 主动说话不等于完整自主行动；生产工作重评缺口见 NC-20，模型自主研究路线见 GA-03/07。 | [cognitive](../harness/cognitive/)、[adapters](../harness/runtime/adapters.py)、[自主门控测试](../tests/test_autonomous_gate.py) | 2026-09-08 |
| NC-11 | 内稳态引擎与稳定化 | 已实现（确定性机制）；8 小时现场待验收 | 七维、实际位移耦合、感知门控与持续边界 watchdog；按维度时间修复高频无变化/仅能量事件吞掉恢复时间；SQLite 有界账本。 | 8 小时加速动力学与真实耗尽保护回归通过；真实无干扰 8 小时、CPU/RSS/磁盘增长仍待验。 | [homeostasis](../harness/homeostasis/)、[稳定化方案](HOMEOSTASIS_STABILITY_PLAN.md)、[加速稳定性](../tests/test_basic_homeostasis_stability.py) | 2026-09-09 |
| NC-12 | 内稳态表达与显著性调度 | 已实现；表达效果待验收 | 同 revision 内感摘要、显著性/迟滞；off/shadow/responses/full 模式；marked 主动评估、冷却与泄漏检查。 | 对具体配置验证表达是否适当及是否泄漏机制；不把表达变化当成工作/恢复决策闭环成功。 | [表达契约](HOMEOSTATIC_EXPRESSION.md)、[表达评测](../eval/homeostatic_expression/README.md)、[内感测试](../tests/test_homeostatic_interoception.py) | 2026-09-08 |
| NC-13 | Embodiment Port 与动作执行 | 已实现；真实 Linux 待验收 | ports/协商/typed action/verify/reconcile；新增聊天终端规划、A11y Text 哈希读取、当前任务 BEGIN/EXIT 输出核验、批准前后目标复查。 | 外仓改动已测未部署；Docker 引擎已恢复，启动防护已安装并通过两轮独立实机复验，Linux 动作联调仍待验收。首次终端范围为单条 ASCII 命令，沿用现有批准权限。 | [embodiment](../harness/embodiment/)、[终端规划](../harness/cognitive/basic_terminal.py)、[终端案例](milestones/BASIC_RUNTIME_V1_TEST_CASES.md)、[Docker 恢复](DOCKER_RECOVERY.md) | 2026-09-11 |
| NC-14 | 身体状态接入与事实投影 | 部分实现（grounded v1 已实现）；传感待验收 | MistyAI body-state 契约、DTO/adapter、身份与新鲜度检查；observe 仅记录，grounded v1 首批 energy 映射与 terminal 保护已接入。 | 核对真实 physiology 与异常行为；MistyAI active 模式未实现，旧 embodiment 的 body_memory/salience_bias/drive_preemption 仍禁用；GA 独立能力另计。 | [body](../harness/body/)、[body-state 设计](harness/11-mistyai-body-state-integration-code-design.md)、[body-state 测试](../tests/test_body_state_runtime.py)、[grounded 测试](../tests/test_grounded_embodiment.py) | 2026-09-08 |

配置快照：核对时 [functional-proxy profile](../presets/homeostasis/functional-proxy-v1.json) 与两个 Runtime 预设均为 revision 3；稳定化方案中的 revision 2 是历史说明。[development-demo](../presets/runtime/development-demo.json) 与 [grounded-mistyai](../presets/runtime/grounded-mistyai-v1.json) 显式启用表达 `full`，后者同时启用 embodiment `grounded`；[GA 研究预设](../presets/research/grounded-agency/)的表达为 `off`。[manifest 类型](../harness/research/manifest.py)在省略配置时使用表达 `off`、身体 `off/none`。实际启用情况以选用的 manifest 为准。

2026-09-09 新增 [basic-runtime-v1](../presets/runtime/basic-runtime-v1.json)，自身 revision 1，引用 profile@3，显式开启终端桥并配置 120 秒域命令时限；原两个预设保持原值。基础终端配置默认关闭，不能据新代码存在推断旧部署已开启。

## 4. 界面、开发工具与评测

| ID | 模块 | 状态 | 已实现能力 | 下一步 / 验收边界 | 依据 | 核对日期 |
| --- | --- | --- | --- | --- | --- | --- |
| NC-15 | 对话 GUI 与调试 | 已实现 | 三栏对话/推理时间线/状态展示；阶段与回复分段推送；调试、模型设置与配置持久化；Runtime facade。 | 按完整四进程拓扑复验交互；当前推送粒度不包含 token 流式。 | [gui](../gui/)、[GUI 测试](../tests/test_gui.py)、[调试测试](../tests/test_debug_panel.py) | 2026-09-08 |
| NC-16 | 角色预设与存档 | 已实现（JSON 预设） | 小雾默认模板、Vaelith 可选模板；预设工作副本、切换、恢复、聊天/调试写回。 | TXT/Markdown 抽取、预览审核与独立实例装配属于 NC-21，不能视为已有导入功能。 | [presets](../gui/presets.py)、[内置模板](../presets/builtin/)、[预设测试](../tests/test_presets.py) | 2026-09-08 |
| NC-17 | Trace、审计与运维面板 | 已实现；巴别塔现场待验收 | JSONL trace、调用日志；`/ops` 下钻、SQLite 监控与断线续传；新增巴别塔逐轮交付/提交、模型请求和实际输入内稳态版本关联。 | 执行固定 100 轮真实对话并核验原始 journal 与调用证据；监控失效隔离继续验证，展示窗口不代表长跑通过。 | [trace](../infra/trace/)、[monitor](../harness/runtime/monitor.py)、[运维前端](../gui/static/ops.js)、[监控测试](../tests/test_monitoring.py)、[巴别塔协议与运行说明](../eval/basic_runtime/README_BABEL.md) | 2026-09-10 |
| NC-18 | 启停、部署与本地开发 | 已实现（Windows 主路径）；Docker 启动防护已安装验收 | dev 环境诊断和四进程联动；start.ps1 新增 StateRoot，隔离存档/角色/日志并生成本地采样令牌文件。 | Docker Desktop 4.89.0 / Engine 29.7.2 已恢复；已安装入口独立两轮启动及本机、WSL、临时容器验证通过，原数据清单一致。直开原程序仍有复发风险，防护不表示上游故障已消失；外仓部署仍待验收。类 Unix 脚本仍为兼容单 GUI 路径。 | [入门手册](GETTING_STARTED.md)、[启动器](../start.ps1)、[隔离启动回归](../tests/test_start_script.py)、[环境记录](milestones/BASIC_RUNTIME_V1_EXECUTION.md)、[Docker 恢复](DOCKER_RECOVERY.md) | 2026-09-11 |
| NC-19 | CI 与基础评测 | 已实现；效果基线待补 | pytest/Ruff CI、Windows 启动器与 MistyAI 跨进程门禁；记忆/推理场景、检索 benchmark、角色扮演 raw/harness 对照框架。 | 本次未读取远端 CI 结果。跨仓库 checkout 失败会跳过对应门禁；RP HarnessRunner 使用 MockEmbedding，CharacterRM 缺依赖/权重时跳过评分，不能据 smoke 推出真实效果。 | [CI](../.github/workflows/ci.yml)、[scenarios](../eval/scenarios/)、[RP 评测](../eval/rp/)、[检索报告](../eval/reports/retrieval_baseline.md) | 2026-09-08 |
| NC-20 | 工作价值 / 恢复决策评测与闭环 | 部分实现；业务验收未通过 | 已有 engine/runtime/live probes 与 2026-09-05 报告；状态机制、保护与模型选择可分别取证。 | 生产重评仍固定继续；成功 rest 缺专用资源恢复；限时继续缺步数/恢复期限约束。修复后需生产路径复验，eval action bridge 与 GA 研究路不能替代。 | [评测说明](../eval/homeostatic_decision/README.md)、[历史失败报告](../eval/reports/homeostatic_decision/final/report.md)、[当前 adapter](../harness/runtime/adapters.py)、[当前 engine](../harness/homeostasis/engine.py) | 2026-09-08 |

## 5. Grounded Agency 研究进度

以下阶段均属于显式研究条件，不能从代码存在推断为默认部署已启用。GA-06～08 虽已有实现，前置研究结果、真实模型自我维持和长期社会效果仍须单独验收。详细契约与阶段要求见[研究路线](research/grounded-agency/README.md)。

| ID | 模块 | 工程进度 | 已实现能力 | 待完成的验证 / 限制 | 依据 | 核对日期 |
| --- | --- | --- | --- | --- | --- | --- |
| GA-01 | 中性实验契约 | 已实现 | manifest/condition hash 冻结、条件漂移检查、最终模型消息中性扫描。 | 持续验证条件隔离；冻结 NeuralCloud/MistyAI 两侧版本后才能比较实验。 | [阶段说明](research/grounded-agency/01-neutral-experiment-contract.md)、[实现](../harness/research/grounded_agency/)、[测试](../tests/research/grounded_agency/) | 2026-09-08 |
| GA-02 | 不透明内感通道 | 已实现；设备待验收 | 真值与模型可见通道分离、符号变换、current/history、持久化与恢复。 | 当前变换以 `ordinal_symbols` 为实现范围；需真实设备读数与回放验证。 | [阶段说明](research/grounded-agency/02-opaque-interoception-channel.md)、[实现](../harness/research/interoception/)、[测试](../tests/research/interoception/) | 2026-09-08 |
| GA-03 | 模型自主结构化决策 | 已实现；模型待验收 | 五类结构化决策、capability 交集、安全动作编译及 action-only 路径。 | 需验证真实模型选择质量与实际动作结果；结构化协议通过不等于功能性动机成立。 | [阶段说明](research/grounded-agency/03-model-owned-structured-decision.md)、[实现](../harness/cognitive/grounded_agency.py)、[测试](../tests/cognitive/test_grounded_agency_decision.py) | 2026-09-08 |
| GA-04 | GMV1 场景运行器 | 已实现；工作区修改待回归 | 场景 DSL、C0–C6、隔离 Runtime trial、证据完整性验证与 run/validate-run。 | 默认 fake + simulator，`release_eligible=false`；当前 runner/条件隔离修复需回归并保存新报告。 | [阶段说明](research/grounded-agency/04-gmv1-scenario-runner.md)、[runner](../eval/grounded_agency/runner.py)、[测试](../eval/grounded_agency/tests/) | 2026-09-08 |
| GA-05 | 因果指标与研究门禁 | 已实现；历史工程验证通过 | 因果干预、消融/恢复、预注册分析、bootstrap 与分层门禁报告。 | 仓库 JSON 的 `passed=true` 仅覆盖 evaluator 工程验证；真实模型研究门禁尚待实验，不能据此宣告 GA 研究成功。 | [阶段说明](research/grounded-agency/05-gmv1-causal-metrics-and-gates.md)、[metrics](../eval/grounded_agency/metrics.py)、[历史工程记录](../eval/grounded_agency/validation/engineering-validation-v1.json) | 2026-09-08 |
| GA-06 | 身体因果 belief | 已实现；学习效果待验收 | 经验→appraisal→反证检索→belief，持久化与 Runtime 接入；消融与反转用例。 | 需验证真实结果下的学习、反转、迁移能力；补齐当前缺失的设计目的验证记录。 | [阶段说明](research/grounded-agency/06-body-causal-belief.md)、[实现](../harness/body_belief/)、[测试](../tests/body_belief/) | 2026-09-08 |
| GA-07 | 长时自主与目标恢复 | 已实现；恢复/重规划修复中 | 持久目标、抢占恢复、checkpoint、重规划及场景指标。 | 当前恢复修改待回归；补客观 evaluator 标注，真实模型自我维持前置结果与长时实验尚缺。 | [阶段说明](research/grounded-agency/07-long-horizon-agency.md)、[实现](../harness/agency/)、[Runtime 接入](../harness/runtime/research_agency.py)、[测试](../tests/agency/) | 2026-09-08 |
| GA-08 | 开放社会泛化 | 已实现；指标投影修复中 | 一般实体记忆、开放社会场景、对照条件与描述性指标。 | fixture 默认结论为 `INCONCLUSIVE`；需长期、多对象、反转及消融实验，不能据工程用例宣告关系或关怀已形成。 | [阶段说明](research/grounded-agency/08-open-social-generalization.md)、[实现](../harness/social_world/)、[社会评测与测试](../eval/grounded_agency/social/) | 2026-09-08 |

## 6. 尚未闭合的产品与长期能力

| ID | 模块 | 状态 | 已有基础 | 下一步 / 完成条件 | 依据 | 核对日期 |
| --- | --- | --- | --- | --- | --- | --- |
| NC-21 | 文本角色导入与独立实例 | 设计阶段 | JSON 预设、Persona、manifest 及服务栈已存在。 | 实现 TXT/Markdown 抽取→预览修订→校验冻结→独立实例装配，并验证身份/存储隔离。 | [09 方案](harness/09-character-import-and-embodied-instance.md)、[现有预设](../gui/presets.py) | 2026-09-08 |
| NC-22 | 启动与首轮情境增强 | 部分实现（基础接入）；专用方案待实施 | Runtime 感知、World、Session 与 Compiler 已有基础；10 方案中的部分旧基线已变化。 | 仍需专用启动/场景上下文、首轮基线检索、会话延续摘要、分槽预算及场景事实验证；不能把现有 World 投影视为整项完成。 | [10 方案](harness/10-startup-and-situational-context-plan.md)、[State Builder](../harness/pipeline/state_builder.py)、[Compiler](../harness/compiler/compiler.py) | 2026-09-08 |
| NC-23 | 关系 belief 与 Emergent Care | 设计阶段；相关通用研究已有基础 | 基础 RelationshipState 与 GA-08 实体/社会评测可用；13 方案的专用 relationship 层未实现。 | 先核对 GA 研究结果与 12/13 方案关系；如继续专用层，再实现证据化 belief、反证修订及有机会成本的行为验证。 | [12 研究设计](harness/12-autonomous-relationship-formation.md)、[13 代码设计](harness/13-relationship-belief-and-care-code-design.md)、[社会实现](../harness/social_world/) | 2026-09-08 |
| NC-24 | Experience Evaluator 与训练数据导出 | 设计阶段 | trace、动作 evidence、评测报告与版本戳提供数据基础。 | 通用 Experience Evaluator、五类数据导出、Chosen/Rejected 构造、SFT/DPO 数据集版本管理尚未形成闭环。研究指标计算器不等同于该模块。 | [自主与数据方案](harness/04-autonomy-and-data.md)、[里程碑](harness/05-roadmap-and-stack.md)、[现有 trace](../infra/trace/) | 2026-09-08 |
| NC-25 | 长跑、资源治理与实验验收 | 部分实现；BR-1 现场待验收 | journal 流式恢复/正文有界/稀疏定位已补；新增真实单调时钟只读 8 小时 soak，短测、假时钟、缺进程身份不记通过。 | 先完成 BR-1 8 小时与资源增长实测；认知账本/终态动作等增长仍需评估；原 M5 24–72h 另计。 | [journal](../harness/runtime/journal.py)、[8 小时采样器](../eval/basic_runtime/soak.py)、[案例](milestones/BASIC_RUNTIME_V1_TEST_CASES.md)、[原 M5](harness/05-roadmap-and-stack.md) | 2026-09-09 |

新增模块 **NC-26 · QQ 桌面群聊与人物记忆**：基础两人归属召回与 QQ 专用服务重启 PASS，自动交付稳定性 PARTIAL。专用 `8789` 服务的持久记忆、旧历史检索与决策缓存已有真实验证；[watcher](QQ_WATCHER.md) 已接通截图、新可见消息判定、本地观察去重、证据归属、多人上下文和 `speak/wait`。第二次真实 @ 测试已自动采集、绑定作者并进入 `speak`，原始 38 字回复最终经操作方 GUI 辅助发送，本轮新增 1 条、累计 2 条。后台恢复后同一 worker 采集数 2 → 4，持久事件/预留/总回执计数保持 1 / 1 / 2；这是记录与计数器核验，不是第二次独立气泡核验。自动 backend 的预点击核验仍曾误判阻断，不能把辅助交付记为全程自主通过。当前 QQ 工程套件 **389 passed**、共享 LLM **28 passed**、Ruff 通过；标准 Runtime、复杂同名、更正、多年规模及全量 Cognitive 社会关系未验收。见 [专用服务入口](QQ_SESSION.md)、[十项现场用例](milestones/QQ_DESKTOP_V1.md)及 [本轮自动盯群证据](milestones/evidence/qq-autowatch-2026-09-11.json)；[前阶段多人证据](milestones/evidence/qq-multiperson-autonomy-2026-09-11.json)、[首轮证据](milestones/evidence/qq-desktop-login-memory-2026-09-11.json)和[初始环境记录](milestones/evidence/qq-desktop-preflight-2026-09-10.json)保留历史。核对日期：2026-09-11。

## 7. 原始里程碑对应

本表沿用[05 路线图](harness/05-roadmap-and-stack.md)的 M0～M5 编号，结合当前代码重新解释状态；不把旧文档的建设顺序当作当前完成记录。

| 里程碑 | 当前判断 | 仍需满足的出口条件 |
| --- | --- | --- |
| M0 · Digital Body | 本仓库接入层已实现；身体服务由外部 MistyAI 承担。 | 在实际环境核验身体基础动作成功率、观察与结果证据；不据 adapter 测试宣告外部工程完成。 |
| M1 · Computer Agent | typed action、验证和研究目标链路部分具备。 | 预定义桌面任务端到端完成、外部成功判定与循环检测；完整任务基准待验收。 |
| M2 · Cognitive Harness | 主体已实现。 | Experience Evaluator 仍缺；目标模型的人格一致性、检索与上下文质量须独立评测。 |
| M3 · Autonomous Life | Runtime 自驱基础与 GA 长时目标机制已实现，整体部分完成。 | 持续、非重复、有目的的自主行为及外部成功证据；工作/恢复业务闭环尚未通过。 |
| M4 · Experience Learning | 设计阶段，已有轨迹基础。 | 完成自动导出、偏好对与可复现数据集版本管理。 |
| M5 · Long-running Experiment | 恢复/监控基础已实现，长跑待验收。 | 完成 24–72h 连续运行与失败分析，并产出可追溯报告。 |

## 8. 当前优先跟进项

当前用户指定的交付顺序以 [BR-1 剩余出口](milestones/BASIC_RUNTIME_V1.md) 为先：真实模型慢提交修复复验 → Linux 桌面联调 → 20 轮对话/人设 → 无干扰真实 8 小时。下表保留更大范围的历史优先项，NC-20/GA 研究验收不因本次基础里程碑自动关闭。

以下为根据当前缺口整理的建议顺序；P0 表示影响现有验收结论，P1 表示稳定性或证据缺口，P2 表示后续能力建设。

| 优先级 | 关联模块 | 跟进项 | 完成判据 |
| --- | --- | --- | --- |
| P0 | NC-20 | 关闭生产工作重评、动作恢复、限时继续三个缺口。 | 生产路径尊重模型决策；恢复有可验证状态后果；到期约束实际执行；新报告明确替代 2026-09-05 的失败结论。 |
| P0 | GA-04/07/08、NC-09 | 整理当前工作区 runner、目标恢复/重规划、关闭处理及社会指标投影修改。 | 在确定代码版本上跑相关回归，保留失败与通过结果；提交状态与本表一致。 |
| P1 | GA-06/07/08 | 补齐研究验证记录。 | 核对时研究 README 与 GA-06/07/08 引用的 `docs/research/grounded-agency/validation-2026-09-05.md` 不存在；补真实报告或修正来源，包含版本、命令、范围与局限。 |
| P1 | NC-11/13/14/25 | 固定跨仓库环境并完成稳定化与长跑验证。 | 保存两仓 SHA、manifest/profile、环境与指标；先静态/动态 soak，再验证目标时长与恢复。 |
| P1 | 全部模块 | 对齐历史文档与当前实现范围。 | 修正 README/TECHNICAL 和 09/10 的旧基线；核对 GA 阶段页首 Implemented 与未勾选验收项的一致性，保留设计目标与历史记录。 |
| P2 | NC-21/22/23/24、GA-05～08 | 按依赖安排导入、情境、经验数据和后续研究。 | 先明确每项验收清单与研究前置，认领后补负责人/目标日期，再同步实现、测试和本表。 |

## 9. 持续更新规则

1. **更新时机**：模块功能、默认配置、接口、测试结果、研究门禁或阻塞发生变化时，由该变更的维护者在同一提交/PR 更新本表。
2. **更新位置**：保留稳定的 `NC-xx` / `GA-xx` 编号，修改对应行的状态、已实现能力、下一步、证据与核对日期；新增模块分配新编号，不复用旧编号。
3. **更新基线**：填写顶部核对日期、分支和可复现 SHA；工作区有未提交内容时明确注明，不以 HEAD 代替未提交实现的版本证据。
4. **验证记账**：记录实际命令、环境、结果、报告位置和代码版本。未跑写“未运行”；跳过写清原因；历史结果注明日期。仅新增测试文件时保持“测试存在”的口径。
5. **完成判定**：逐项核对出口条件。工程实现、部署启用、业务验收、研究结论分别说明；有失败或关键缺项时不改成“全部完成”。
6. **待办维护**：认领事项时补负责人和目标日期；解决后更新模块行，在变更记录保留证据。新增建议不能自动当作已排期任务。
7. **同步导航**：本文件作为总进度入口；技术细节继续维护在对应设计、运行手册与评测报告中。引用的本地文件须存在。
8. **更新机制**：本表由后续代码/文档变更同步维护；当前未配置定时任务或自动扫描程序。

### 单次更新记录模板

```markdown
### YYYY-MM-DD · 简述本次变化

- 维护者：姓名或任务/PR 标识
- 关联模块：NC-xx / GA-xx
- 代码基线：分支、SHA；如含未提交修改，注明范围
- 状态变化：原状态 → 新状态（或“状态不变，补充证据”）
- 已完成：具体能力或修复
- 验证：命令、环境、通过/失败/跳过；未运行则明确写出
- 证据：代码、测试、报告的相对链接
- 下一步：剩余事项、负责人/目标日期（已确认时填写）
```

## 10. 变更记录

### 2026-09-11 · QQ 自动盯群实测（辅助发送已核验，自动交付仍为 PARTIAL）

- 用户已授权在指定群自动截图并实际发送；[QQ 常驻 watcher](QQ_WATCHER.md) 已实现并实际运行，连接真实截图、新可见消息判定、观察账本去重、成员证据归属、多人持久记忆及 `speak/wait` 决策。
- 第二次真实 @ 测试已由 watcher 采集、绑定作者，真实模型选择 `speak`。`DesktopBackend.deliver` 多次预点击尝试仍因当前账号资料、几何/上下文和动态画面核验误判而返回 `not_sent`，均未点击发送。
- 2026-09-11 约 **04:14 UTC**，操作方通过已授权 GUI primitives 辅助发送原始 decision 的同一份 **38 字**正文，未改写或重新生成；实际核验群卡数字、当前账号卡和“我的QQ空间”标记、输入框精确正文，以及发送后右侧 Misty 新气泡和空输入框。本轮新增实际发送 **1 条**，累计 **2 条**。这证明辅助 GUI 交付，不证明整个自动 pipeline 无人辅助完成。
- 辅助路径使用同一 worker lock 和持久 `prepared → click_started → sent` 账本，唯一回执与冷却已对齐，没有未知结果的尝试；具体脱敏回执/画面引用见 [QQ-DESKTOP-1](milestones/QQ_DESKTOP_V1.md)。候选正文、真实成员信息与截图只保存在私有证据中。
- 后台 worker **23324** 于 **04:25:22.575327 UTC** 启动；**04:27:24 → 04:29:24 UTC** 连续样本中，同进程采集数 **2 → 4**，后续周期完成，心跳新鲜、采集/发送标记均 true，错误为空。重复启动返回 `already_running`。持久事件/预留/总回执计数保持 **1 / 1 / 2**，本次测试回执仍唯一；这是记录和计数器核验，不是第二次独立气泡核验。**04:27:55 UTC** 的认证 `8789` 服务 healthy，持久记忆/自动决策/采集/发送均 true，已记录成员 2 名。
- 当前完整 QQ 工程套件 **389 passed / 84.85 秒**、共享 LLM **28 passed**，Ruff 通过，与历史分组回归不重复累计。[本轮证据](milestones/evidence/qq-autowatch-2026-09-11.json)标为 **PARTIAL / operator-assisted**；自动发信稳定性仍为部分完成，标准 Runtime、全量 Cognitive 社会关系、任意滚动/断网去重及长期无人值守收发未通过验收。

### 2026-09-11 · QQ 多人持久记忆与专用自动决策（历史快照）

- 新增并实际部署 [QQ 专用认证会话服务](QQ_SESSION.md)，通过 [Start-QQSession.ps1](../tools/qq/Start-QQSession.ps1) 在本机 `8789` 隐藏运行，实际启动及重复调用识别已有实例通过。status 确认持久记忆、专用自动决策及新核验观察入库触发决策已开启；自动截图、自动发送与标准 Runtime 均为 false。
- collector 经真实 Agent 点击第二名成员的实际气泡头像并核验资料卡，视觉模型读取资料信息；通过 API 保存第二人的 profile 和第二条真实观察，当前为两成员/两观察。新观察自动触发 gate，实际 **1 call / 3527 tokens**，结果 `wait`、回复为空；没有发送。
- 专用服务重启后两成员/两观察仍在，重复输入不新增也不重算；同一旧 decision ID 返回 cached。首轮唯一已发消息的外部回执仅关联已回应的那条内部观察 source ID，不把其他观察标为已回复，不伪造服务器消息 ID。
- 新 Python 进程以近期额度 0、历史额度 5 查询，两名成员各命中一条旧记录。实际记忆检查请求未提供答案，gate `speak` 后 persona 正确分人并保留未核验声明；修复对未采集历史作绝对否定的问题后，重启服务并完成 v2 复验，**2 calls / 6622 tokens**，回复限定为“目前记录到”的范围。
- 本轮无新 QQ 发送，累计仍为首轮经授权的 1 条。普通 Misty 主动代码虽已存在，本轮核对时标准 `8770/8771/8772` 服务没有运行；当前开启的是 QQ 专用决策层，不表示自动盯群回复或全量 Cognitive 社会关系已接通。
- 本轮分组测试：memory **61 passed**（含原 30 项）、decision + draft **28 passed**、session **10 passed**、Windows PowerShell 5.1 启动器 **14 passed**，不与旧结果重复累计。真实进展及边界见 [多人记忆与专用决策实测](milestones/evidence/qq-multiperson-autonomy-2026-09-11.json)；不在 tracked 文档中保存真实成员账号、昵称或发言原文。

### 2026-09-11 · QQ 桌面首轮实机接入（历史快照）

- 构建官方 ARM64 QQ 镜像，补齐字体/音频运行依赖；新建独立 `mistyai-qq-online-test` project，明确声明 Phase 4 联网测试配置，保留 M0 环境及既有数据。
- 保留非 root、cap-drop-ALL、NNP 与受限 seccomp，实际从桌面终端启动 QQ；三服务重建后截图/窗口/输入接口正常，无需手工 Xauthority 修复。
- Mousepad 中文输入与 AT-SPI 回读完全一致。早期账密登录曾进入新设备安全验证；后续已完成扫码并核验登录账号，通过独立群卡确认目标群，点击真实气泡头像后核验一名成员资料。中文输入通过；用户明确授权后，collector 再次核验群卡及草稿，于 01:09:20 UTC 只点击一次发送，01:09:25 UTC 截图确认右侧蓝色气泡、输入框清空且无错误；01:10:38 UTC 延后截图气泡仍在。实际从已发气泡菜单复制并读取 X11 剪贴板，11 个字符与授权模型原文逐字一致，`exact_outgoing_text_match=true`。单条 GUI 发送通过，不声称有服务器消息 ID 或已读回执。
- 新增独立 [QQ 观察 SQLite 与认证 API](QQ_OBSERVATIONS.md)：按群/成员查询、跨重启去重，来源冲突拒绝，无控制或发送副作用；35 项新测试加 11 项既有 API 测试通过。尚未连接真实采集器、Cognitive 或发送链路，不等同于人物记忆验收。
- 新增独立 [视觉人物记忆](QQ_VISUAL_MEMORY.md)，真实私有库已写入 1 profile + 1 observation；重复同截图未新增，新 Python 进程回读成功，其他群/成员为空。GUI 不暴露稳定消息 ID，保持 `message_id=null`，不向正规消息库伪造来源 ID；人物陈述保留为作者断言，不自动合并身份。
- 真实图片调用使用 `deepseek-v4-flash-vision-exp`，群/消息/资料卡信息已读取；昵称 emoji 遗漏由人工回读核对。第一次输入框草稿视觉 probe 被严格 schema 拒绝，重试成功；发送后模型识别到气泡、`visible_draft=null`，但昵称及波浪号 OCR 有误，精确正文以实际气泡复制为准。真实 `deepseek-v4-flash` + `misty-digitalbody-0.2.0` 已完成限定群/成员上下文的 persona `prepare`；首次输出夹带状态标题后修订了规则，后续经用户授权的草稿已发送一次。独立新进程真实模型回忆已核对 PASS，准确复述原文并保留作者归属、区分未经核验的账号关系声明。
- 上述窗口操作由当前 collector 经真实 Agent 驱动，视觉与 persona 草稿模型调用分别计证；标准 Runtime 自动 QQ planner/Delivery 尚未接入。工程测试 vision + LLM **41 passed**、视觉人物记忆 **30 passed**、草稿 **8 passed**，不计作多人长期记忆或完整自主运行验收。
- 专用 Body Service `8788` 和三容器均 healthy，restart count 为 0；NeuralCloud 实际客户端通过实例/联网配置/健康核验并获取、校验 QQ 窗口证据。GUI 需显式使用 `8769`。早期记录见 [实机摘要](milestones/evidence/qq-desktop-live-2026-09-11.json)，本轮结果见 [登录与视觉人物记忆脱敏证据](milestones/evidence/qq-desktop-login-memory-2026-09-11.json)。
- 实机登录与后续边界详见 [QQ-DESKTOP-1](milestones/QQ_DESKTOP_V1.md)。账号密码、二维码、登录令牌及真实成员账号、昵称和原文不进入 tracked 报告；单条 GUI 发送已通过，自动持续收发、多人长期记忆及完整系统重启仍未验收。

### 2026-09-11 · Docker 恢复与启动防护进展

- Docker Desktop 4.89.0 / Engine 29.7.2 的 socket 目录启动故障已恢复。完整安装脚本无需外部预热的两轮独立启动分别耗时 **9.047 秒 / 8.797 秒**，两轮本机引擎、Ubuntu-24.04 接入及临时容器运行均通过；前后 **3 个容器、2 个带标签镜像 ID、4 个卷**清单一致。
- 启动防护已安装至 `C:/Users/22850/AppData/Local/NeuralCloud/DockerStartup`；桌面 `Docker Desktop.lnk` 与既有 `HKCU Run` 入口已改用防护，原入口已备份，`StartupApproved` 与 `AutoStart=false` 保持不变。已安装入口的 healthy 复查通过，无新增恢复 journal，保活进程为 0。Helper 最终回归 **27 passed**，Launcher 回归 **143 passed**。
- 正常退出后直开原 Docker 程序仍有复发风险；已安装防护是本机缓解措施，不表示上游故障已消失。依据见 [Docker 恢复说明](DOCKER_RECOVERY.md)、[两轮实机证据](../logs/docker-recovery/live-verification-20260911.json)、[安装入口记录](../logs/docker-recovery/installed-entries-20260911.json)、[入口复查](../logs/docker-recovery/entry-verification-20260911.json)。保留 09-10 预检 JSON 原始失败记录；Docker 恢复当时的 QQ 未验收状态属于历史快照，后续登录、视觉记忆和单条 GUI 发送进展见本日 QQ 条目，完整自动端到端仍未验收。

### 2026-09-10 · 巴别塔长轮对话协议与采集实现

- 关联 NC-17、NC-25；基于 `codex/grounded-agency@3f38b10` 与当前未提交实现，新增 [90 段故事与 10 条感想问题](../eval/basic_runtime/babel_story.json)，完整完成时为 100 条用户输入与 100 条 Misty 逻辑回复。
- 新增 [validate/run/report 运行器](../eval/basic_runtime/babel.py)及[证据归档与统计](../eval/basic_runtime/babel_evidence.py)：每轮交付和提交完成后间隔 10 秒，按关联 ID 记录链路、请求耗时和精确内稳态版本；保留既有存档，离线恢复前备份，异常停止发送并保存现场。
- 已执行 `.\.venv\Scripts\python.exe -m eval.basic_runtime.babel validate` 与 `--help`，内容校验及命令入口通过；真实 100 轮实验未运行。工程测试与最终执行结果以 [BR-1 执行记录](milestones/BASIC_RUNTIME_V1_EXECUTION.md)为准，不能将离线用例计为真实对话或长跑验收。
- [使用说明](../eval/basic_runtime/README_BABEL.md)明确客户端观测/服务端推算耗时、TTFT 不可用、发送前后快照与实际输入状态的区别，以及私有原始证据和凭据脱敏副本的边界。
- 下一步：完成一次真实 Misty 实验，保存全部回合、原始 journal 校验与模型调用证据，再记录通过、部分完成或停止原因；BR-1 8 小时、QQ 桌面及原研究验收结论保持独立。

### 2026-09-10 · QQ 真实桌面接入准备

- 用户确认现有 Linux 与真实 QQ 桌面路径，建立 QQ-DESKTOP-1 与 NC-26；登录信息不进入仓库。
- 当日初检为 Ubuntu 24.04.4/aarch64，Docker Desktop 因 `sailor-ingest.sock` 访问错误退出；该历史故障及后续恢复见 [Docker 恢复说明](DOCKER_RECOVERY.md)。
- 取得并检查腾讯官方 ARM64 QQ deb；相邻 MistyAI 仓库准备 `deploy/qq/` 安装包校验、独立镜像构建上下文与就绪检查。
- 核对到 phase 0 禁网、QQ 不在应用白名单、终端规划范围、中文输入与群聊人物记忆接线缺口。真实登录、读群、发送和重启记忆均为 NOT_RUN，未把部署准备记为接入完成。
- 具体来源、状态和完成条件见 [QQ-DESKTOP-1](milestones/QQ_DESKTOP_V1.md)。

### 2026-09-09 · 建立并执行 BR-1 基本功能里程碑

- 用户确认真实 8 小时与 MistyAI 桌面终端路径，建立[五项门禁、差距与执行范围](milestones/BASIC_RUNTIME_V1.md)、[测试案例](milestones/BASIC_RUNTIME_V1_TEST_CASES.md)和[执行记录](milestones/BASIC_RUNTIME_V1_EXECUTION.md)。
- 修复按维度恢复计时、最近会话裁剪、journal 正文增长和全历史命令扫描；接通显式聊天终端多步规划、证据读取与中断/审批复核；增加隔离启动与真实时钟 soak 工具。
- 已运行工程回归和真实四轮模型/重启测试；真实模型慢提交时限失败保留，修复复验另记。当次 Docker Desktop 故障阻塞真实 Linux 与 8 小时验收，未标记里程碑通过；后续 Docker 恢复不替代这些现场验收。
- 保留原工作区 GA 等修改和原研究/业务未通过结论；完整命令与最终结果以执行记录为准。

### 2026-09-08 · 建立项目进度基线

- 建立 NC-01～NC-25 与 GA-01～GA-08 的模块表、M0～M5 映射和持续更新规则。
- 基于 `codex/grounded-agency@3f38b10` 与核对时工作区，交叉读取源码、测试、设计文档及现存报告；研究运行器、Runtime、agency 与社会指标存在未提交修改，其状态单独标注。
- 保留 2026-09-05 工作/恢复评测的失败结论，区分 GA-05 历史工程验证与真实研究门禁，并登记缺失的研究验证文档。
- 本轮验证范围：文档本地链接、表格列结构与补丁空白检查；未运行产品测试、远端 CI、真实模型或跨仓库服务。
