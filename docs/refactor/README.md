# Grounded Agency 全面重构调研

## 新增待办计划（2026-10-28）

- [按上下文预算自动压缩：部署记录](context-budget-compression-20261028.md)：70% 预算阈值、后台摘要、最多 3 次失败归档；58 项测试通过，生产超限群会话已自动切换。
- [自主行动经历记忆实施计划](autonomous-action-memory-plan-20261028.md)：一次自主任务提炼一份经历，关联原始日志与回执；后台处理最多调用模型 3 次，失败进入异常记忆表。状态：待实施，仅记录计划。

## 当前重构入口（2026-10-25）

- [Misty 双仓库重构实施方案 v1.0](misty-harness-refactor-20261025.md)：NeuralCloud 仅保留全局记忆和插件接口，MistyAI 承担内稳态、情绪指导与 DeepSeek Harness 集成；退役旧九阶段、Linux 身体与语音，优先现成渠道插件。在线记忆可有损，原始历史安全剥离到服务器之外的独立冷记忆仓库，正常角色不可访问。
- [执行交接与进度门禁](misty-harness-execution-handoff-20261025.md)：实施期间用户预计睡觉、几乎无人干预；必须先以实际 Linux 远端代码完成两仓库进度同步，才允许改造。包含未提交改动保全、源码同步、无人值守阻塞策略和数据剥离门禁。

**状态：仅发布方案与交接，尚未实施。** 本次未连接实际 Linux 服务器，G0 进度同步未完成。本分支的 GitHub 代码仅是文档载体，不是已核实的生产基线；后续从远端同步基线建立实施分支，不能用旧 GitHub 代码覆盖服务器。

以下条目保留历史语境；其设计、完成和上线声明不代替本轮远端核查。与本轮已确认决策冲突的产品取舍，以 v1.0 方案为准；当前实现事实始终以实际远端核实结果为准。

## 历史调研与实现记录

- [内稳态 v3 与群聊接收控制](cognitive-dynamics-v3-20261006.md)：实际加工驱动的快慢状态、独立恢复、持久接收策略、因果去重、回放与 24 小时影子切换门槛；线上切换尚未完成。
- [Misty 内稳态自主行为修复](homeostatic-autonomy-repair-20261006.md)：修复证据截断、请求故障恢复、活动生命周期、评价窗口、Web 信息反馈与 OneBot 事实回流；包含回归、历史请求重放和真实模型隔离验证。
- [Misty 内稳态自主行为调研](../research/misty-homeostatic-autonomy-assessment-20261006.md)：线上只读核实自主搜索历史、当前 33 次请求校验失败、调度频率与反馈缺口；包含脱敏证据和后续验收建议，未修改运行配置。
- [QQ 按需 Kimi 识图发布](qq-vision-kimi-rollout-20261006.md)：保留当前主模型，自主选择 Kimi K2.6 非思考识图与后续联网；公开样本、Linux 验收及历史命令 ID 碰撞修复记录。
- [QQ 记忆准备优化与隔离实测](qq-memory-prepare-optimization-20261005.md)：增量来源同步、内存人格复用和会话查询下推；67.5 ms 隔离中位数、280 项回归通过，已发布 `20261005-memory-prepare-v1`。
- [JSON 独立 DeepSeek 迁移与缓存验收](json-deepseek-migration-20261005.md)：统一结构化路由、真实协议/缓存对照、私聊隔离验证及发布记录。

## 待重构完成后的实验（2026-10-05）

- [Misty 一小时自主看门狗实验计划](../research/misty-watchdog-experiment-plan-20261005.md)：仅凭能力说明自主续期，每次续期 60 秒，连续维持一小时；超时停止 Misty 运行。已规划，未实现、未测试；等待重构完成后再开展。本次仅保存计划，不启动实验。

## CLM 接入评估（2026-10-05）

- [CLM 接入评估](clm-integration-assessment-20261005.md)：基于 `e6951ed` 和官方 CLM 源码的初始架构评估；生产接入尚未实施。
- [CLM 与 DeepSeek 实机评测](clm-deepseek-benchmark-20261005.md)：已完成 Linux CPU、RTX 5090、DeepSeek Structured Outputs 的真实功能和性能测试，包含冻结中文用例、正文检查、接口故障和原始证据。当前建议优先验证 DeepSeek none，CLM 保留局部实验。
- [OpenAI Structured Outputs 调研与部分实测](openai-structured-benchmark-20261005.md)：保留 Luna 部分结果和账户阻塞证据，按用户决定暂缓后续实现与评测。
- [DeepSeek 结构化输出接入定稿](deepseek-structured-integration-20261005.md)：将前轮选定的 Flash none + JSON Schema 接入现有客户端和类型化决策入口，包含实测、回归与回滚说明。
- [DeepSeek 输入缓存优化与实测](deepseek-cache-optimization-20261005.md)：固定内容前置、实时状态后置、去除重复 Schema；记录真实输入 token 命中率、预热开销与适用边界。

## 当前接手入口（2026-09-30）

- [首发开发交接](production-launch-handoff-20260930.md)：WP0/WP1 部分实现、实际测试结果、完整回归失败、发布阻塞及下一步。用户要求先保存推送，之后再继续开发。
- [首发范围](production-launch-scope-20260929.md)、[实施顺序](production-launch-implementation-20260929.md)、[验收清单](production-launch-acceptance-20260929.md)。

本目录保存从 `codex/grounded-agency` 分出的重构规划，不代表重构代码已经实现或部署。

- [完整调研与实施路线](grounded-agency-rebuild-plan.md)：功能取舍、源码瓶颈、目标架构、数据迁移、性能与恢复验收。
- [审计清单](audit-inventory.json)：冻结提交、仓库规模、已读取文件及其远端 blob SHA。

基线：`835d902ed5601137b504524400cff7c64aeafbab`。新分支：`codex/grounded-agency-rebuild-plan`。

首次落地从计划中的 R0（行为与状态基线）开始；本次提交仅新增调研文档。

## 实时交互与持续表现补充设计（2026-09-29）

以上文字保留首次规划的历史语境，不代表当前分支仍然只有文档。当前分支已包含显式 opt-in 的生产 Runtime、认知与证据记忆实现；是否部署和通过验收应以对应实现、配置及验证记录为准。

- [Misty 实时交互与持续表现架构](realtime-presence-architecture.md)：以本轮 `ee5eb86` 为主要审计基线，分析 NeuralCloud、MistyAI、MistyDigitalBody，包含目的、修改内容、预期呈现、协议、打断与恢复、验收和性能开销评估。
- [生产实现与验证记录](production-validation.md)：已有实验、失败、测量边界和待验收项，不等于实时表现方案已经实现。

实时表现补充方案状态为 **Proposed**；本轮仅新增架构文档和本索引，不切换部署、模型、身体权限或数据库。
