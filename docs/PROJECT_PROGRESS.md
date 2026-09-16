# NeuralCloud 项目模块进度表

> 持续维护文档：模块实现、测试、验收或计划发生变化时，同步更新对应行和文末变更记录。
>
> 最近核对：**2026-09-11（Asia/Shanghai；本次增量为 Docker 启动防护与 QQ 桌面实机接入，其余条目保留各自核对日期）**。基线：`codex/grounded-agency`，HEAD `159d55d`，**包含核对时的未提交修改**。本表描述当前工作区，不代表已合入主分支或已部署。

当前认知 Harness 主体、Runtime、内稳态、具身接入和开发界面已有实现；Grounded Agency GA-01～GA-08 已有工程代码，但真实模型的自我维持、长期自主与社会泛化仍待实验验证。角色文本导入、专用启动情境链路、关系 belief/care 层和训练数据导出仍有明确缺口。

**2026-09-16 群聊接话门槛调整**：按用户选择，仅提高对正在进行的群聊的参与意愿；普通分享、观点、兴趣和可可靠理解的图片有具体回应点即可优先接话。群 gate 升至 v18，保留重复/未知内容等待、原有 10/60 秒节奏和静默机会条件。相关回归 192 passed；线上同模型合成配对评测中，4 个普通场景从 1 次 speak 增至 4 次，4 个等待场景均保持 wait。验证范围与发布状态见 [执行证据](milestones/evidence/qq-participation-gate-2026-09-16.json)。

**2026-09-16 QQ 通用故障提示修复**：09:42 的图片消息正常接收，但上下文预算少计拆分包装与后加情绪提示，超出 161 字符后提前触发错误兜底。历史选择与最终检查改用同一实际编码，保留必需观测并完整预留评估消息；相关回归 213 passed。精确事件与发布验证见 [上下文预算修复记录](milestones/qq-context-budget-recovery-2026-09-16.md)。

**2026-09-16 无回复故障修复**：确认 QQ 发送 ACK 在截止时间前耗尽，缺少消息 ID 的未知交付阻止后续自动回复；增加严格协议回声与独立读取对账、发送前最低预算、桥重启后的有界历史核验，以及离线等待扫码的启动检查与日志。日报改为后台归档，监控总览减小响应并修复进程 CPU 基线。相关组合测试 287 passed、1 skipped；故障证据、备份与远端验收见 [执行记录](milestones/linux-response-recovery-2026-09-16.md)。

**2026-09-15 Qwen 情绪链路增量（实现与对照验证）**：新增独立持久化 `ObserveEmotion`、按会话核验的近期窗口、跨会话无原因情绪余额、七维内稳态动态立方占比，以及两段辅助模型评估/演绎提示。新模式覆盖本地、QQ 群聊/私聊、自主表达、留言板和直接语音脚本；主模型使用唯一情绪提示，旧心理字典、关系情感数值及重复内感语气通道被移除。提供 off/shadow/on 和回滚预设，保留 critical 与 opaque 隔离。已增加固定/动态权重的五轮生成对照，离线模型结果只验证工程接线；独立人工盲评与 QQ 在线 P95 门槛仍待验收。详见 [情绪链路说明](EMOTION_DIRECTOR.md)、[评测与证据契约](harness/17-qwen-emotion-director-evaluation.md)。

同日收尾：远端独立 Qwen3 4B 已部署并设为开机启动，正式角色配置保持原版本。最终专项152项、Ruff通过；完成四场景40条真实受控生成，发现归因、恢复表达和预算问题后保留失败证据并强化降级校验。工程候选尚未获得发布资格；见 [实施证据](milestones/evidence/qwen-emotion-implementation-2026-09-15.json)。

**2026-09-15 Linux 部署增量（已迁移上线）**：通用 systemd 部署 CLI、不可覆盖 release、停写快照/离线校验/恢复、Windows 路径清单、独立 NapCat 及认证入口已落地。正式版本 `20260915-linux-2` 运行四核心、Board 与 QQDirect；原 Windows/WSL 写入者已停止。15 个业务数据库与 94,846 条源 journal 校验通过，源/目标表计数一致。真实模型、网页对话、公网认证/WebSocket、QQ 登录/名单/历史及指定私聊单条发送回读均通过；Mock 回滚恢复和新端一致性备份恢复通过。组合回归 149 passed、12 skipped。整机重启、长期运行和异机备份尚未验收，定时备份未启用。见 [实例交接](LINUX_INSTANCE_20260915.md)、[执行证据](milestones/evidence/linux-migration-2026-09-15.json) 和 [通用部署手册](LINUX_DEPLOYMENT.md)。

**2026-09-14 QQ 协议接入增量**：新增 [NapCat / OneBot 与图片理解](QQ_DIRECT.md)，保留同一 Runtime、Cognitive、人格、创造者事实和共享记忆；按名单接收两个群，PHANTOM-C 可回复、超级豪人大宅门只观察。原图存证、OCR、动图抽帧、协议身份与回执已接通；两个群及静态图、100 帧动图的原图读取和模型描述通过。服务已实际切换，用户带图 @ 的两段自动回复与完整协议回执通过，首次耗时 58.810 秒；594 项组合回归通过，后续 Windows 启动排序修复另有 9 项回归通过（测试范围有重叠）。详见 [部署证据](milestones/evidence/qq-direct-deployment-2026-09-14.json) 和 [接入验证](milestones/evidence/qq-direct-implementation-2026-09-14.json)。模拟与真实收发分开记录，长期稳定性仍待验。

**同日连续消息延迟修复**：保留后续 1 次自然等待和 1 次过期结果，定位 QQ ingress 每条新消息重扫完整日志造成的 13–14 秒额外入队延迟，改为既有启动/增量扫描建立的序号索引。78 项相关回归通过；仅重载 Runtime，保留热身后的 Cognitive。详见 [原因与验证边界](milestones/evidence/qq-direct-ingress-index-2026-09-14.json)。

**同日群回复授权更新**：用户已授权 PHANTOM-C（1044946257）及超级豪人大宅门（1060979317）均可回复。当前协议名单的两项 `observe`、`send` 均已开启，并重载 QQ 协议进程；群回复继续由 Misty 自主决定。

**同日秋语私聊增量（已启用并通过文字收发验收）**：已部署 QQ `2285027817`（秋语 / 秋语随风）的好友白名单通道，独立会话键为 `private:2015188508:2285027817`。私信沿用同一人格、认知与记忆 owner，自主判断回复，保留原图、OCR 和 GIF 抽帧；回执通过绑定对端的好友历史核验，未知发送不重发。群回复权限保持，私聊记忆不进入群上下文或内部目标评审，通用身体目录仍只有两个群。真实私信触发两段回复并取得完整回执，耗时 22.645 秒；已修复实时事件 `target_id` 会话对方语义误判，见 [私聊通道说明](QQ_DIRECT.md#秋语私聊通道)。

**2026-09-12 持续目标增量**：已接通生产目标存储、共享 `PrepareGoalDecision`、现有能力执行器、证据复查、受限 `ApplyAppraisal` 与 `/goals` 控制页。用户目标优先，常驻心理目标可暂停恢复；未知副作用先对账，准备失败亦受三次无进展限制。心理自评不能操作电力、精度与目标区间，重复证据不重复加分。已完成专项与相关回归测试、真实引擎的成对模拟评估；默认中性自评样例没有产生额外恢复收益。生产配置已上线，8 小时真实记录进行中，尚不能宣布持续运行或调节效果验收通过。详见 [使用与验收说明](GOAL_REGULATION.md) 及 [实现证据](milestones/evidence/goal-regulation-implementation-2026-09-12.json)。

**2026-09-12 联网身体增量**：新增可独立协商的 `web.search.v1` 与 `web.read.v1`、Tavily 适配器、持久请求额度及作用域证据、共享对话和自主目录接入，以及重启生效的 GUI 配置入口。关闭时保持旧行为；模拟 HTTP 已验证搜索、正文读取与单次回复提交。用户 Key 已保存，真实 Tavily 搜索与正文提取各一次请求通过；共享服务已按请求重启，联网配置版本 1 生效，对话与自主授权开启。真实主模型对话与自主在线验收待完成，见 [联网搜索身体](WEB_BODY.md)。

**2026-09-12 语音增量**：MistyAI D008 已注册为 NeuralCloud 的独立 `expression.voice.*.v1` 身体功能，提供档案、生成、任务与资产接口。工程接入和跨仓库协议已验证，在线 VoiceProfile 绑定、真实 GPU 合成与听评仍待完成；详见 [语音身体功能](VOICE_BODY.md)。不改变其他模块的历史验收结论。

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
| NC-07 | 主辅 LLM 与 Embedding | 已实现 | OpenAI 兼容客户端、主辅调用路由、Mock、embedding 客户端与降级处理。 | token 流式生成仍显式未实现；独立语音资产接入见 [VOICE_BODY](VOICE_BODY.md)，自动播放未实现；真实端点质量与延迟需单独验收。 | [llm](../harness/llm/)、[路由测试](../tests/test_aux_llm.py)、[调用测试](../tests/test_llm_and_trace.py) | 2026-09-12 |
| NC-08 | 记忆存储与数据库 | 已实现；集成待验收 | in-memory/PostgreSQL Store、pgvector、Alembic、Persona 数据库锁与持久化契约。 | 在实际 PostgreSQL 环境复验；驱动/数据库不可用时集成测试会 skip。PG 人格 debug 绕锁不支持，是既定边界。 | [store](../harness/memory/store/)、[数据库配置](../infra/db/)、[PG 集成测试](../tests/test_postgres_integration.py) | 2026-09-08 |

## 3. Runtime、内稳态与具身模块

| ID | 模块 | 状态 | 已实现能力 | 下一步 / 验收边界 | 依据 | 核对日期 |
| --- | --- | --- | --- | --- | --- | --- |
| NC-09 | Runtime Supervisor | 已实现；8 小时待验收 | 事件队列/心跳/watchdog、认证、交付提交与恢复；journal 有界正文、流式恢复及命令序号索引；显式终端多步桥和新消息中断。 | 元数据和部分其他账本仍随历史增长；按 BR-1 验收真实进程/状态稳定性。 | [runtime](../harness/runtime/)、[journal 回归](../tests/test_runtime_journal_bounded.py)、[终端集成](../tests/test_basic_terminal_runtime.py)、[BR-1](milestones/BASIC_RUNTIME_V1.md) | 2026-09-09 |
| NC-10 | Cognitive 服务与空闲主动表达 | 已实现（基础） | Prepare/Commit 与持久化；辅助模型 SILENT/SPEAK 判定、失败回退、用户消息抢占、主动表达审计。 | 主动说话不等于完整自主行动；生产工作重评缺口见 NC-20，模型自主研究路线见 GA-03/07。 | [cognitive](../harness/cognitive/)、[adapters](../harness/runtime/adapters.py)、[自主门控测试](../tests/test_autonomous_gate.py) | 2026-09-08 |
| NC-11 | 内稳态引擎与稳定化 | 已实现（确定性机制）；8 小时现场待验收 | 七维、实际位移耦合、感知门控与持续边界 watchdog；按维度时间修复高频无变化/仅能量事件吞掉恢复时间；SQLite 有界账本。 | 8 小时加速动力学与真实耗尽保护回归通过；真实无干扰 8 小时、CPU/RSS/磁盘增长仍待验。 | [homeostasis](../harness/homeostasis/)、[稳定化方案](HOMEOSTASIS_STABILITY_PLAN.md)、[加速稳定性](../tests/test_basic_homeostasis_stability.py) | 2026-09-09 |
| NC-12 | 内稳态与 Qwen 情绪表达 | 已实现；表达效果待验收 | 保留显著性/迟滞与 critical 调度；新增持久化情绪观察、分会话窗口、共享余波、动态占比、两段辅助模型及统一主模型提示；覆盖 QQ/本地/自主/留言板/语音。 | 新链路 off/shadow/on 独立发布；已有离线三变体及五轮固定/动态生成对照，真实盲评与 QQ P95≤30秒尚未达到发布验收口径。 | [情绪链路](EMOTION_DIRECTOR.md)、[评测契约](harness/17-qwen-emotion-director-evaluation.md)、[状态测试](../tests/test_emotion_service.py)、[Runtime 测试](../tests/test_emotion_runtime.py) | 2026-09-15 |
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
| NC-18 | 启停、部署与本地开发 | 已实现；Windows 一键四服务实机验收通过 | start.cmd / stop.cmd 读取 launch.json，默认 shared 沿用现有历史、模型和 QQ 配置；真实四服务就绪且 Runtime 为 RUNNING。local 的 cmd 冷启动 21.31 秒；shared/local 重复启动 PID 不变，local 停止通过。 | 本次未验收实机 QQ 登录/收发及完整 MistyAI 部署。Docker 启动防护的历史验收与直开原程序风险保留；类 Unix 脚本仍为兼容单 GUI 路径。 | [一键启动](ONE_CLICK_START.md)、[统一入口](../launch.ps1)、[启动配置](../launch.json)、[入口回归](../tests/test_oneclick_launcher.py)、[隔离启动回归](../tests/test_start_script.py)、[Docker 恢复](DOCKER_RECOVERY.md) | 2026-09-15 |
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

### 2026-09-15 · Qwen 64K 试验与窗口回调

- 在保留 `Qwen3-4B-Instruct-2507` 的前提下，曾将应用与辅助 Ollama 的上下文从 8,192 调为 65,536；正式配置入口补齐 `NC_AUX_LLM_NUM_CTX`，相关部署测试 45 passed、1 skipped（含 13 项新增）。模型实际加载 64K，短输入 JSON 验证成功；约 48,000-token 的合成冷输入在 240 秒后超时，长输入响应耗时验收未通过。
- 按用户要求已回调：应用与实际加载窗口均恢复 8,192，辅助服务内存上限恢复 10G，线上版本恢复 `20260915-qq-reply-repair-v1`。六服务 active、Runtime `RUNNING`、队列 0，QQ 已连接；保留新的业务数据及先前回复故障修复。见 [试验与回调证据](milestones/evidence/qwen-context-64k-2026-09-15.json)。
- 使用失败调用原始 pipeline 快照重建校验请求并按官方 Qwen 分词器复算，两次总数精确匹配服务端 51,794 / 47,679。最近请求的 91.48% 是引用、来源 ID、证据属性与 JSON 结构，历史及记忆正文仅 6.26%；528 次引用中有 184 次重复（166 次在同一条目内）。这是实际序列化输入开销，不是字符比例或合成测试推算，见 [输入 token 来源占比](milestones/evidence/qq-validator-input-attribution-2026-09-15.json)。

### 2026-09-15 · QQ 校验容量与单轮到期修复

- 原始 Linux 模型请求与 Ollama 日志确认：正文已由 DeepSeek 生成，完整校验证据超过 Qwen 8,192-token 窗口，造成 `validate` HTTP 400。大校验输入改走原主模型，短输入继续辅助模型，保留全部已准入证据及 QQ 截止时间。
- 单轮 QQ 到期从整体故障处理中隔离；过期准备记录终态，恢复时不重放。真实迟到失败、早期超时、非 QQ 故障以及 Cognitive 命令锁语义保持原有约束。
- 新增 41 项回归通过，Runtime 相关最终测试 146 项通过；校验路由、记忆证据与 QQ 场景相关测试通过，独立代码复核通过。实现和操作语义见 [QQ 协议说明](QQ_DIRECT.md#2026-09-15-校验容量与单轮到期修复)。
- Linux 已切换 `20260915-qq-reply-repair-v1`，316 个发布文件哈希校验通过；停写备份包含 1,568 个文件、15 个 SQLite 数据库。六服务 active、Runtime `RUNNING`、队列 0；135 个恢复发现的过期 QQ 轮次已记录 `ignored / qq_turn_expired`，原过期准备未重放，发送总数仍为 77。
- 真实模型校验验证：Qwen 短输入 1,062 tokens 成功；DeepSeek 大输入 39,377 tokens 成功，耗时约 2.4 秒。上线后自然新消息正常接收并由模型选择 `wait`；未发送测试群消息，本版本完整群回复交付仍待自然消息验证。见 [修复部署证据](milestones/evidence/qq-reply-repair-2026-09-15.json)。

### 2026-09-15 · Linux 通用部署与状态迁移工具（实机验收待补）

- 关联 NC-18；基于 `codex/grounded-agency@6c7a473` 与现有未提交业务修改，新增 `harness/deploy/`、`deploy/linux.example.json`、`tools/deploy/` 及部署回归。打包实际工作区的允许源码并记录文件哈希，Git HEAD 不代替本次工作区发布包标识。
- 实现 `package/install/activate/start/stop/status/preflight/verify/backup/restore/rollback`；代码 release 与私有配置/状态分离，systemd 使用非 root 账户、环回绑定、依赖健康等待和受控停止。代码回滚保留现态，快照恢复仅写入全新目录。
- shared 清单覆盖仓库 Cognitive/preset、私有 Runtime/Homeostasis/共享记忆、QQ 配置与数据库、证据及令牌引用；WSL Board 作为明确 extra 源加入。SQLite backup 包含已提交 WAL，离线验证不修复 journal。仅结构化改配置路径；Direct/desktop 旧运行 status 不复制，新端须生成确认停用的 desktop 状态。
- 已运行快照专项：**29 passed，1 skipped**，覆盖停写确认、完整/非空 WAL、SQLite 只读连接产生空 sidecar 的误报修复、源变化诊断、配置路径、watcher 真实 trace 及 observation/gaps 存证、只读哈希链校验与拒绝覆盖恢复。此前四组部署组合回归 **84 passed，8 skipped**；跳过项为当前 Windows 的符号链接权限、Linux 专属安装/权限场景和本机未安装 Caddy。Ruff、文档本地链接和补丁空白检查通过；这些结果不替代目标 Linux 实机验收。
- 增加可选一致备份作业与每日 timer 模板：记录原运行范围、停写/导出/校验/恢复，失败不自动恢复流量，支持第二存储目录及限定保留策略。模板不由 install 自动启用，是否已启用及实机作业结果另行记录。
- 远端 bootstrap 安装成功已进入演示启动验证；正式数据切换、Linux 上 QQ 扫码与授权收发、公众域名认证/WebSocket、备份恢复演练和长期运行尚待执行方记录。**本条不标记 Linux 业务部署验收通过**。
- 使用说明见 [Linux 迁移与部署手册](LINUX_DEPLOYMENT.md)，测试见 [状态快照](../tests/test_deploy_snapshot.py)、[Linux 部署](../tests/test_linux_deploy.py)、[入口认证](../tests/test_deploy_ingress.py)。后续需补充最终 release ID、snapshot 摘要、切换时刻、验证结果和未解决项；凭据及私有原文不写入该文档。

### 2026-09-15 · 一键启动入口与共享实例实机验收

- 关联 NC-18；基于 `codex/grounded-agency@6c7a473e0387eadce878eeba8d74bba44a60b054` 及本次未提交的启动器、测试和文档修改。
- 新增 `start.cmd` / `stop.cmd` 与 `launch.ps1`，启动参数集中在 `launch.json`。默认 `shared` 沿用现有历史、模型设置与 QQ 配置；另提供独立 `local` 演示配置。修复配置校验、残留 PID 识别、重复启动与状态隔离，并保留恢复暂停状态。
- Windows 实机默认共享配置已启动 Cognitive、Homeostasis、Runtime、GUI 四服务，Runtime 为 `RUNNING`。`local` 通过 cmd 入口冷启动耗时 **21.31 秒**；`shared` / `local` 重复启动均保留原 PID，`local` 停止已验证。
- 启动器相关回归 **301 passed**，补充测试 **32 passed**；两组存在重叠，不累计为独立测试总数。核心四服务就绪不代表 QQ 在线；实机 QQ 登录/收发与完整 MistyAI 部署不在本次验收范围。
- 现行用法与默认参数见 [一键启动说明](ONE_CLICK_START.md)，代码及回归见 [统一入口](../launch.ps1)、[配置文件](../launch.json)、[一键入口测试](../tests/test_oneclick_launcher.py)。[改造前验证报告](milestones/startup-verification-2026-09-15.md)保留原始失败证据并注明历史范围。

### 2026-09-14 · 情绪表达与参数语义调研计划

- 基于 `codex/grounded-agency@c3c7e523a1f287f4130ce0f094293e793f470e54` 编写[调研计划](harness/16-emotional-expression-diagnostic-research-plan.md)，针对用户反馈的云端 DeepSeek V4.1 Flash 表达不足，区分输入接线、参数定义、强度投影、人格约束、校验与生成配置等假设。
- 将 QQ 专用表达是否接入内稳态列为优先审计项；源码调用点未显式传入摘要，不代表已核验用户当前线上路径。将参数语义补充与新增信息分开对照，避免混淆。
- 明确真实请求证据、分阶段采样、盲评、统计口径、费用上限配置、原有发布门禁复用与后训练决策；opaque 核心研究条件继续隔离。
- 本次仅新增计划并更新 README/NC-12 文档入口；没有修改运行代码或部署，没有执行付费模型调用、后训练、QQ 发送或产品测试。文档相对链接与表格结构已检查；既有模块验收日期及研究结论不变。

### 2026-09-14 · 秋语 QQ 私聊通道实现

- 新增 `private_contacts` 与 `allowed_private_peer_ids` 双层授权，当前目标为秋语 / 秋语随风（2285027817）；仅好友私聊进入独立会话分区，不接受群临时会话。PHANTOM-C 与超级豪人大宅门原有回复权限保持。
- 复用同一 Runtime、Cognitive、人格、记忆 owner 和视觉/OCR/动图链路；收到私信后自主决定回复。实际发送使用对端 QQ 号，按对端绑定的好友历史核对回执，未知发送不自动重试。
- 私聊 audience 与群记忆隔离；真实本地 `chat` 可检索授权私聊，内部能力选择和目标评审排除私聊正文。专用操作者确认事实仅补充给秋语；全局身体目录不暴露私聊读取，避免正文进入通用目标结果。
- 补充 [私聊 actor](../tests/test_qq_private_actor.py)、[契约](../tests/test_qq_private_contracts.py)、[共享记忆](../tests/test_qq_private_shared_memory.py)和[能力隔离](../tests/test_qq_private_capabilities.py)测试；收发与 Runtime/Web 套件 **395 passed**、独立共享记忆套件 **115 passed**。首次维护重载前后原活动任务和 23 条结果保留；后续 WS 字段修复重载前后原活动任务和 24 条结果保留。字段修复回归 **111 passed**（与前述套件重叠），真实私聊消息 `1738656136` 完成两段回复，消息 ID 为 `1083295069`、`2110992886`，完整回执 `qq-delivery:6f700910d74247bea0d26f1898ff64e8`；此次为文字样本，尚无真实私聊图片样本。

### 2026-09-14 · QQ 跨群返回后的发送恢复

- 关联 NC-26；12:17（04:17 UTC）历史轮次首段有真实分段回执，第二段在剩余约 1.72 秒时于桌面输入和点击之前取消。聚合 `deliveries=0` 不代表未发出任何消息；新增持久账本累计的 `verified_segments_sent`、`partial_deliveries`，保留 QQ 固定通信错误码，避免把能力检查失败统一记成模型错误或普通预检失败。
- 该轮次 vision 调用为 0；归属核验 8.890 秒、共享认知 RPC 37.016 秒、首段 GUI 6.141 秒。三次 persona 请求是正常解析、正文生成和响应验证，均 HTTP 200、无重试，验证 pass、修订 0；未把它们误记为网络重试或续写。首段已发、第二段未发的原始状态与过期余稿继续保留，详见 [失败与耗时诊断](milestones/evidence/qq-crossgroup-send-failure-2026-09-14.json)。
- 发送在原始 60 秒期限内等待传感器恢复，期间仍检查新消息、暂停和作用域；旧缺口闭合并重新核验数字身份后，不再重复使新证明失效，新缺口仍会失效。独立修复当前 QQ 标题完整父节点 Text 可读而成员数子节点 Text 缺失的 AX 读取退化；该问题不是上述零 vision 历史轮次的已证实原因。
- 实机已完成主群 → 观察群 `1060979317` → 主群的数字群号核验、本人核验及发送前证明检查，随后主动中止，未点击发送且自有草稿已清空。保留首次群资料卡首帧未核验失败及下一次 AX 通过，不能据单次通过宣称绝对稳定或新的实际发送成功。
- 最终相关回归 **428 passed / 30.51s**，Ruff 通过；全 QQ 初轮出现的 25 项最小适配器初始化错误已修复并复测通过，各分组不累计。2026-09-14 12:47（北京时间）新 watcher PID 44304 源码哈希匹配，自动读完观察群并返回主群，读取/发送就绪、当前 gap=0、无未知交付。状态正确恢复为累计已发送分段 37、部分发送 1，最近回执 12:17:41。见 [修复验收证据](milestones/evidence/qq-crossgroup-send-repair-2026-09-14.json) 与 [QQ watcher](QQ_WATCHER.md)。旧超期段不重放；修复后完整回复实际交付及连续运行仍需自然消息验收。

### 2026-09-14 · QQ 群观察名单

- 新增 `group_roster` 与独立逐群读取能力；保留单个 watcher、GUI 执行线程和主群发送账本。新增群只开放观察，群名用于导航提示，数字群号通过真实资料卡核验。
- 空闲时按间隔轮询名单，读取结果分别保存；跨群访问全程阻挡主群传感器，结束后返回主群。共享认知接收带来源和时刻的有界群观察资料，不将显示昵称提升为已核验的作者身份。
- 工程验证与实机部署结果见 [群名单接入证据](milestones/evidence/qq-group-roster-2026-09-14.json)。

### 2026-09-14 · QQ 最后一次输入框复核移除

- 按用户要求移除最终截图的重复草稿 AX、输入框锚点及像素检查；输入完成后的正文/上下文验证、发送目标校验和发送回执保留。
- 相关七文件共 **181 passed**，Ruff 通过。QQ watcher 已重启，运行版本摘要与源码一致，恢复 `watching`、观察和发送就绪；本次未执行真实消息发送测试。
- 只读核对为一个常驻心理目标及其一个活跃子任务；子任务累计 3 次 wait、5 次 error，`no_progress=8`。计数和调度规则未改动。详见 [变更与上线证据](milestones/evidence/qq-final-input-check-removal-2026-09-14.json)。

### 2026-09-12 · 注册 MistyAI 语音身体功能

- 新增版本化语音契约、VoiceAssetPort 和独立 HTTP Provider，复用 `/voice/v1`，固定角色档案版本、保留异步幂等任务与取消语义、校验资产摘要。
- Runtime 提供认证语音入口、能力状态与可选共享认知选择；任务关联持久化，未知提交不重新生成，播放/发送/内稳态奖励不由生成成功推断。
- `NC_VOICE_CONFIG` 支持正常启动持久绑定并仅传给 Runtime；同机语音端口建议 8794，避免与 Runtime 的 8770 冲突。
- 新增适配器、Runtime、配置和启动测试，并使用相邻 MistyAI 的真实 ASGI API 与临时数据库验证跨仓库协议。测试使用合成器替身，不代表真实 GPU 或听感验收。小雾 A2 最终试听资产尚无 D008 VoiceProfile，本次没有伪造或替换角色档案。

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
