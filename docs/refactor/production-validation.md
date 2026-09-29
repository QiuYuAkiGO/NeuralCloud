# Production v1 实施与验证记录

2026-09-22。代码尚未部署，首批授权会话待用户提供。本文件中的结果是本次运行所得；不引用历史性能作为当前基线。

## 环境和保护

- NeuralCloud：`codex/grounded-agency-rebuild-plan`，基线 `0b09fad6c948ec6d040ce6a043ff2ac116be5488`。
- MistyAI：新 worktree `C:/Projects/MistyAI-embodied-production`，基线 `309c59a771528f20de431f32c14faded4bfd3eb3`。原 `C:/Projects/MistyAI` 的未提交工作保持独立。
- Windows Python 3.12 ARM64 跑核心测试与规模测试。原 venv 缺少 asyncssh，其 cryptography ARM64 安装缺 MSVC linker；另建 `.tmp/production-venv` 的 Python 3.12 x64 隔离环境跑完整测试，不替换原 venv。
- 本地 8770/8771/8772/8790 无监听。冻结本地 `data/presets/misty.json` 的一致性副本及三工作树 Git 元数据；源 SHA-256 `ae82bcb767bf24ebc260d46c804513fe866283e5c4fecd44a3865a0d3e48d70e` 前后相同。本地文件仅含人格，没有经历，**不代表线上历史迁移验收**。
- 本轮重连现有 Linux 入口成功，主机公钥固定校验，已完成只读配置与规模盘点。隔离代码、venv 和验收状态放在 `/home/qiuyu/neuralcloud-production-acceptance-20260922`，没有替换活动 release、服务配置或会话授权。此前 ConnectionRefused 是首次尝试的结果，已不再阻塞访问。Linux 的本轮结果及未通过项目见 [Linux 验收记录](production-linux-validation.md)。

## 已执行验证

- 最终冻结专项：**363 passed**，168.90 s；涵盖全部 `test_production_*`、身体合同、真实 QQ host、证据回放、冻结评估、旧研究 interoception 和先前失败的 Windows 启动测试。测试前后被测源码集合哈希一致：`965dde680e1649d8ffeb8b266fed921bc5a4a15296bc1601c56ce3427d81fc0c`。命令和 JUnit 保存在忽略目录 `.tmp/production-frozen-final-run.json`、`.tmp/production-frozen-final.xml`。
- 最终 MistyAI 生产、实际双仓 ASGI、生成合同、旧 cognition 协议回归：**12 passed**。NeuralCloud 修改涉及的 112 个 Python 文件 Ruff 通过，两个 GUI JavaScript 文件语法检查通过，双仓 `git diff --check` 通过。
- MistyAI 身体专项：147 passed，包含 explicit rest/restart/幂等/耗尽恢复、账本事务、独立结算授权、生成 schema 及真实 NeuralCloud adapter → MistyAI ASGI 测试。
- 生产用量 / Runtime / cognition / LLM 联合回归：131 passed；单独新增用量 21 项覆盖原始计量、晚到/错误响应、断连、丢回执、重启、后台补报、预算和暂停。
- 认知、QQ、共享记忆、命令生命周期首轮 48 passed；能力、留言板、日记 135 passed。后续生产 QQ gate/body、真实 host 摄取、过期记忆 fence、迁移/回放 47 passed。
- 记忆核心、迁移、GUI、来源修订/撤销和公平维护持续专项回归：QQ/记忆/维护 213 passed；启动/切换人格投影恢复 5 passed；实际 host 的来源撤销与回放 11 passed；身份、方向投影与明确保留 48 passed。上述测试集有重叠，不能相加为唯一测试数量。
- `git diff --check` 通过。Windows Git 的 LF/CRLF 转换提示不等于尾部空白错误。
- 修正两处基线已过期的测试预期：私聊 prompt 在原 HEAD 已为 v10-humanization；旧 goal 上下文在原 HEAD 已过滤原始 error 文本，测试改为验证保留 call_id 且不外泄错误正文。
- 完整 `tests/` 首轮：6556 passed、25 skipped、8 failed（1793.28 s）。其中七项为已用 Git HEAD 核实的陈旧预期：上述两项，加默认人格 v0.2.2、QQ 文案和原有句尾标点处理。另一项为冻结研究 wire 与扩展 provider DTO 边界；已修专用 fixture，并新增旧实验 truth hash 跨重启回放测试。完整首轮之后仍有代码修改，不把该结果称为最终全套通过。
- 覆盖相关路径的第二轮（230 个顶层测试文件，加 embodiment/body_belief/interoception）：4302 passed、1 skipped、4 failed（1164.21 s）。其中一项发现认知层直接导入 transport，已改为基础设施 helper；一项正确检测到测试过程中源码被修改，独立冻结评估 35 项通过；两项 Windows launcher 的一秒期限在开发机负载下先于模拟启动耗尽，独立复跑均通过。后续新增工具结算与主动联系回归另列；不能把这一轮描述为全绿。
- 主动联系收尾修复后 59 passed：实际 host 的 wait/speak、记忆撤销与 Runtime 暂停回放均覆盖。自主机会没有用户原话，不能伪造消息摄取；私聊机会只按已授权会话的稳定 peer 读取态度，群机会没有指定人物。
- 工具结算专项：web/server/core 44 passed，双仓 source/schema/实际 ASGI 9 passed；board/diary/voice 新增 25 passed，旧 board/diary 86 passed、voice 59 passed。后续源码冻结合并回归单独记录，各组有重叠。

确定性模型、合成数据和 ASGI 只验证工程合同；不证明真实模型长期人物学习效果。

## 合成检索规模

工具：`python -m tools.benchmark_production_memory --destination <new.sqlite3> --rows <100000|1000000> --batch-size 1000 --queries 100`。各自预热 5 次，100 次计时查询，包含普通词项、主题与人物候选，候选硬上限 192。

- 100,000 条：摄取 43.695 s；SQLite 347,873,280 bytes；热查询 p50 32.339 ms，p95 51.533 ms，最大 57.051 ms；候选最多 180。
- 1,000,000 条：摄取 591.172 s；SQLite 3,586,863,104 bytes；热查询 p50 30.421 ms，p95 62.773 ms，最大 77.166 ms；候选最多 180。

加入完整来源版本、撤销链和显式身份绑定后，在同一合成库重新运行 100 次查询（不重复摄取）：10 万条 p50 37.615 ms、p95 65.364 ms、max 72.797 ms；100 万条 p50 65.713 ms、p95 136.183 ms、max 166.169 ms。候选均最多 180。开发机仍有离线验证负载，以上为实际观测，不作隔离 Linux 性能承诺。

排除网络、模型、embedding、上下文编译、冷 OS 缓存。这不是 Linux SSD 上“检索加编译 p95≤300 ms”的完整验收。百万级数据生成和测试文件仅保存在本地临时目录，不提交。

## 配置和恢复演练

生产 manifest 必须与旧 GA manifest 分离，并关闭旧 homeostatic expression / goal regulation。保留原生产 manifest，生成新文件后验证，不原地改冻结配置：

```sh
python -m tools.production_profile --source presets/runtime/shared-misty-v1.json --body-instance-id <verified-body-id> --destination <new-manifest.json>
```

生成文件不部署；保留原通道授权且默认不新增普通身体 intent。准备和请求绑定、会话有序、真实发送与领域提交用既有耐久协议；Runtime 的 job_scheduled 不清除未完成恢复项。预算、source revision、计费、休息和 QQ UNKNOWN 均应在重启/重复回执测试中保持幂等。

新事实不能随代码回退丢失。演练顺序：暂停新工作 → 保留新 journal/交付/记忆修订/spool/outbox/身体 ledger → 核验待办和 UNKNOWN → 在另一状态目录恢复兼容版本 → 对照稳定回执 → 允许单一 writer 继续。不能把旧 preset 或旧身体快照覆盖新事实。

### 未知身体期间的账单

已有身体/episode 归属的账单按原稳定回执自动补报，绝不转嫁到新 episode。首次断连而从未观察到 episode 的账单保留原用量及哈希，GUI 显示 `usage_unassigned`。它们需要操作者核验后分配当前结算目标，不能声称当前身体就是推理发生时的身体。

- `GET /v1/production/accounting/unassigned?limit=100&offset=0` 返回有界待办、`usage_hash` 和刚核验的当前目标。
- `POST /v1/production/model-usage/{attempt_id}/assignment` 接受 `assignment_id`、`expected_usage_hash`、`body_instance_id`、`episode_id`、`reason`。重复分配幂等，冲突/原用量已绑定 episode 均拒绝。
- 两接口仅接受独立 `NC_CONTROL_TOKEN`；服务令牌不能分配。控制令牌与服务令牌相同则关闭此操作。分配记录与原账单分开存储，不改原用量、哈希或 inference ID，后续心跳仍由 MistyAI 唯一结算。

### 模型、工具和协议交付分别结算

模型每次实际调用独立保留用量，NC 执行的工具按稳定操作 ID 记录，QQ 按真实交付回执结算。工具操作不占 12 次模型调用预算，但工具内部生成文本的模型调用照常计量。MistyAI 自己执行并结算的动作以及 QQ 协议交付不能再次记为 NC 工具成本。

工具调用先冻结身体归属，再在实际执行边界记录 dispatch；校验失败且未 dispatch 不扣费。重启时未决 dispatch 只形成 unknown 和待结算事实，不声称工具成功；后续证据可补充，但不能更改原成本 ID 或重复扣费。

覆盖 web/server 实际请求、board 显式读取与写入、diary 写入及 voice.render。诊断和能力目录读取不算工具执行。示例生产配置仍关闭 voice；计费接线不会新增任何通道授权。

### 人物认识和跨会话态度

`IdentityStore.bind` 只接受显式操作者授权及保留的证据记录，绑定事件与解绑保留历史。检索只扩展稳定主体 ID，不放宽受众。原作者、来源 ID 不变，不通过昵称或模型猜测自动关联。

`OrientationStore` 将态度与人物事实分开：模型可选择 approach / avoidance / neutrality / repair，没有固定收益或必须增加的分数。跨受众仅传主体、方向和不透明版本，理由、私聊原文和来源 ID 不进入群聊提示；撤销依据、认识更新或身份解绑会使旧投影失效。QQ appraisal 同时读取当前受众合法的既有认识，支持当场更正和反证。

明确的中英文“请记住”请求在真实消息摄取时直接保留原话并 pin，断连受限对话也生效；其他表达仍可形成带理由和证据的重要标记。检索命中、上下文暴露、模型明确采用分别记录，重放不会提高可信度。休息、暂停、断连、耗尽、预算不足暂缓维护而不消耗任务故障重试次数，已生成提案在恢复后继续使用。

## QQ 修复与 10 次复测（2026-09-22）

本轮按“先修复与复测”执行，未切换生产版本。新结果保存在 [QQ 10 次复测报告](production-qq-latency-10-retest-results.json)，首轮报告和原始失败响应保留。

- 主体字段：将运行时已有的“`subject_id` / `subject_mention` 必须恰有一个非空值”发布到模型 JSON Schema，并明确提示模型填写。主体、引文、权限和可信度校验保持严格，不从作者或引文推测缺失主体。
- 时间预算：生产 `PrepareQQTurn` / `ResumeQQResponse` 使用原始持久化请求的绝对期限与冻结预留。默认 60 秒窗口对应准备阶段第 47 秒截止、正文第 57 秒截止；最终普通快照校验保留 3 秒交付时间。真实 HTTP RPC 按剩余期限执行，重试不续期；通用命令仍使用原超时。已生成且绑定的固定错误通知保留既有受限收尾宽限，不增加模型计算时间。阶段耗尽仅终止当前轮。
- 诊断：保留原拒绝行为，增加固定白名单失败分类和错误码，区分模型无效、证据拒绝及阶段超时；不把失败或错误通知计作成功回复。
- Linux 冻结候选回归 **838 passed、0 failed、0 skipped**，pytest 94.091 秒（测试脚本总计 96.141 秒）。覆盖生产机制、主体 schema、QQ 准备与交付、真实 RPC 合同、身体双仓合同及研究回放。995 个打包文件测试前后、实测后哈希一致。目录为 `/home/qiuyu/neuralcloud-qq-fix-retest-20260922-v1`。

沿用同一主模型 `claude-sonnet-4-5-20250929`、同样 10 条串行消息和模拟 OneBot 回执。**7/10 成功，0 次超时**；第 2、8 次 JSON 解析失败，第 3 次证据提案拒绝。成功 7 次的 p50 **41.826 秒**、p95 **55.174 秒**；全部 10 次尝试的 p50 **34.584 秒**、p95 **55.174 秒**，后者包含提前失败，不能代替成功回复性能。采用最近秩；10 次的 p95 为最大值，样本不足以推断长期尾延迟。

第 2、8 次响应完整返回且标记 `finish_reason=stop`，但字符串内部包含未转义的 ASCII 引号（分别 2 处、6 处）；请求已带 JSON 模式。第 3 次的三个主体字段均完整，但引文的 start/end 与原文不符，触发 `evidence_anchor_span_invalid`。离线定位没有修改实际响应、修补证据或补发请求。模型没有被更换，不能把本轮工程修复称为模型可靠性验收通过。

实际 24 次模型 HTTP 请求准备事件与 24 条耐久用量记录一一核对，结束时 `in_flight=0`。轮间冷却和结果后的任务收尾单列，不包含在回复延迟内。测试仍采用进程内 Runtime/Cognitive 调用，未经过真实 QQ 网络、四进程 HTTP RPC、后台维护或自主循环；真实 RPC 超时合同由确定性测试覆盖。

线上四服务保持 active，四份配置哈希不变，活动 release 仍为 `20260920-qq-memory-latency-v1`。成功率从首轮 4/10 提高至本轮 7/10，但仍有 3 次失败，且成功样本构成发生变化，不能据此声称回复变快。生产切换继续暂停；下一步需解决非法 JSON 与引用位置生成的稳定性，并保留严格来源校验和失败记录。

## GLiNER2.5 隔离接入测试（2026-09-22）

已复用 Linux 下载的多语言 GLiNER2.5，接入经历摄取、来源清单和原文位置核验。新增桥接器不直接写人物认识、不推断稳定身份，线上配置保持不变。[完整记录](production-gliner-scenario-validation.md)与[原始结果](production-gliner-scenario-results.json)保留全部边界和失败。

Linux 桥接与来源清单回归 **89 项通过**。16 个中文场景的抽取加校验 p50 **581.116 ms**、p95 **640.696 ms**；含额外 QQ 合成重放，47 个返回片段均精确匹配原文。但必要命题只覆盖 11/28，主模式标签符合 4/16，关系分类仅 1/6；不能将引用位置正确误称为人物归属或反证判断通过。主模型非法 JSON 与完整 QQ 可靠性门禁仍未解决。

## 尚未验收

2026-09-22 的 QQ 条件切换检查未通过，因此继续运行原线上 release。首轮候选的 [10 次串行实测](production-qq-latency-10-results.json) 使用真实主模型和模拟 QQ 回执，仅 4 次成功；另有 2 次解释阶段校验拒绝、4 次命令超时。成功回复 p50 为 32.433 秒、p95 为 36.003 秒；全部尝试 p50 为 30.082 秒，包含失败，不能当作成功回复性能。采用最近秩分位数。本次没有实际 QQ 网络或四进程 RPC，不能据此确认真实授权会话已验收。

原始响应离线复核确认：第 1、2 次认识提案同时缺少 `subject_id` 和 `subject_mention`，触发 `claim_requires_subject_or_unbound_mention`，并非外层 appraisal JSON 解析失败。第 3、5、8、10 次的发言决策 HTTP 请求超时；经历解释和发言决策共用 `PrepareQQTurn` 默认 30 秒命令期限，发言决策实际仅剩约 8–11 秒。不能把这四次归因为完整 QQ 60 秒窗口已经耗尽，也不能通过删除主体校验或将错误算作主动等待来提高通过率。

条件切换检查再次只读核对线上四份配置与原基线哈希一致，四服务 active，当前仍为 `20260920-qq-memory-latency-v1`。QQ 可接受延迟上限和首批新机制授权会话待明确；失败问题、当前一致性副本迁移、四服务与真实身体联调及保留新增事实的发布回退必须先完成。至少七天观察安排在首批切换之后，再决定旧生产路径退役，不作为首次小范围切换前必须已经完成的观察期。

当前线上全部 writer 一致性快照与发布回退；持续维护积压与真实历史负载性能；真实授权会话切换及至少七天观察。已执行的 Linux 隔离测试、历史副本迁移、真实模型固定场景和性能测量单列于 Linux 验收记录，不能用这些结果代替仍待完成的上线验收。

后台 legacy preset 兼容桥仍有整体 DTO 保存，旧生产路径尚未彻底退役。现在不能声称全部六阶段已完成或可以直接切换全部线上会话。
