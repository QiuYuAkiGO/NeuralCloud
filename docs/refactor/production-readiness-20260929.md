# 重构工程上线准备审查 · 2026-09-29

审查基线：`codex/grounded-agency-rebuild-plan`，`ee5eb86e31c2817f3b4e8d0f4e543e31e595c9c0`。

后续首发范围已通过逐项问答确定，见 [首发范围决策记录](production-launch-scope-20260929.md)。本文保留审查时的代码与证据快照；下文“待拍板”和“按范围决定”的条目，按该决策记录落实。服务器命令、Web 目标管理、私聊记忆管理和一次自动重试现已纳入首发必做项。

实施阶段已补充[工作包与依赖顺序](production-launch-implementation-20260929.md)和[逐项验收用例](production-launch-acceptance-20260929.md)。新增核查确认纯私聊配置、可信通知、耐久长任务、文件传输和独立自主暂停也需补齐；详见实施方案，不能将本审查报告视为这些功能已经贯通。

**结论：核心机制已实现到可继续验收的阶段，尚不具备直接切换现网全部会话的条件。** 缺口集中于模型可靠性、部署接线、当前状态迁移与恢复、运维处置闭环和正式发布证据。建议先完成小范围上线条件，再试点和扩量。

本次只检查本地源码、配置、已有验收报告，并运行离线检查。没有登录服务器、调用真实模型、发送 QQ 消息或部署。报告中的 9 月 22–23 日线上状态是历史记录，不能代表 9 月 29 日实时状态。未读取凭据文件。

## 已经完成或已经决定

- 生产认知、证据 v2、来源权限、版本与撤销检查、独立提案组提交、耐久用量、身体休息与恢复协议等已有实现和专项测试，不应重新列作“完全没做”。
- Python、四服务和 SQLite 保留；MistyAI 管身体事实和结算，Runtime 管授权、调度和交付，Cognitive 管人格、认识和情绪。生产和研究路径显式分离。
- 自主模型尝试最多每滚动小时 12 次、两个前台槽、最多 4 个目标、每计划 32 步、连续 3 次无进展暂停，以及单 writer 切换，均已有明确约束。
- 七天观察安排在首批切换之后、退役旧路径之前，并非首批试点前必须先运行七天。

依据：[现行重构方案](grounded-agency-rebuild-plan.md:7)、[资源与认知边界](grounded-agency-rebuild-plan.md:14)、[自主约束](grounded-agency-rebuild-plan.md:52)、[迁移顺序](grounded-agency-rebuild-plan.md:60)。

## 上线前必须补齐

### B1 · 主模型与输出协议尚未达到完整链路验收标准

当前生产 `_ask` 仍调用 OpenAI 兼容接口的 `json_object`，要求 `finish_reason=stop` 且拒绝 tool-bearing 响应。因此原生 Messages 强制数据工具的实验成功不能通过只改模型名称接入生产。

- 9 月 22 日 QQ 完整候选复测 7/10 成功；另三次为两次 JSON 解析失败、一次引用位置错误。成功样本 p95 为 55.174 秒，且只有七个成功样本，不能推断长期尾延迟。
- 后续 v2 原生 Sonnet 4.6 诊断第一场就因提案错误被拒绝，后五场停止；并未完成新合同的真实语义验收。
- 9 月 23 日小型七字段直接 JSON 经生产解析器复核为 10/10，但合同和链路范围明显更小，不能覆盖上述失败。
- GLiNER 自然语言转换方案完整合同通过为 0/10，程序生成正确说明的控制组也为 0/10；不是可直接替换的生产路线。Qwen 0.6B 转换器位于隔离验收工具中，不是生产客户端接线。

**完成标准：** 选定模型、端点、协议和最终合同版本；使用同一候选完成结构、引用、归属、更正、反证、实际记忆写入、真实交付与延迟验证。旧失败保留，重试、主动等待、错误通知分别计数。

依据：[实际生产调用](../../harness/cognitive/production.py:212)、[QQ 复测](production-validation.md:84)、[v2 实测失败](production-evidence-v2-validation.md:36)、[小合同与 GLiNER 结果](production-nl-gliner-validation.md:23)、[隔离转换器](../../tools/production_acceptance/qwen06_converter.py:233)。

### B2 · Linux 部署入口没有完成生产身体接线

这不只是缺少最终配置文件：

- runner 对非开发部署固定 `study_runtime`，并强制 `NC_EMBODIMENT_PROVIDER=none`。
- 生产示例 manifest 是 `development_debug`；直接替换路径会触发部署 authority 冲突。生成器继承源 manifest 的控制模式，最终部署仍需显式核对。
- bindings 不支持 `NC_MISTYAI_BASE_URL` 等 URL 配置，runner 又会清除外部 `NC_*` 环境变量，无法靠 systemd 外部环境可靠补入。
- 现有默认部署示例仍指向旧 `shared-misty-regulation-v1.json`。

**完成标准：** 部署配置显式支持 provider、身体 URL、独立身体/结算凭据以及一致的控制模式；用实际发布包验证四服务启动后连接到正确 body/episode，完成真实用量、休息和恢复回执。身体不可用时的受限对话是既定机制，但不能作为身体联调已完成的证据。

依据：[runner](../../harness/deploy/runner.py:50)、[环境清理](../../harness/deploy/runner.py:89)、[binding 白名单](../../harness/deploy/config.py:146)、[部署校验](../../harness/deploy/cli.py:54)、[生产示例](../../presets/runtime/production-embodied-v1.example.json:4)、[旧默认](../../deploy/linux.example.json:6)。

### B3 · 最新线上状态的迁移、双仓备份和实际回退尚未验收

已有迁移演练基于 9 月 15 日封存快照，证明了该副本的重复迁移、续跑和逻辑投影恢复，不能代表最新线上数据。旧 writer 不能直接接管已产生 v2 数据的新库。

普通部署布局下，Runtime 的 `production.sqlite3` 和 Cognitive 父目录中的 `production-usage.sqlite3` 会随现有状态目录备份，不能说新 SQLite 全部漏备份。但默认来源发现没有纳入独立 MistyAI 状态目录及位于 `config_root` 外的身体/结算 token。备份工具已有 `--extra-writer-unit` 可协调额外服务停启，默认模板未配置；即使增加该参数，也不会自动增加身体数据来源。跨仓恢复包和停写/一致性边界仍需补齐。

**完成标准：** 冻结最新配置、双仓版本和全部状态清单；明确哪些 writer 停止、身体账本如何取得一致副本；迁移检查来源与权限；演练在保留新经历、v2 修订/撤销、QQ UNKNOWN、spool/outbox 和身体账本的前提下回退与前滚。新增事实不能通过恢复旧 preset 被抹掉。

依据：[历史演练范围](production-linux-validation.md:29)、[新格式回退限制](production-evidence-v2-validation.md:54)、[快照 binding 清单](../../harness/deploy/snapshot.py:41)、[spool 路径](../../harness/production/metering.py:156)、[Runtime 新库](../../harness/production/runtime.py:18)。

### B4 · 发布质量门禁还没有形成可信的通过记录

本次按 CI 同样的范围运行 Ruff，得到 **21 项错误**，分布在七个测试/评估文件，主要是未使用导入/变量、lambda 赋值和同一行复合语句。这里只判断当前候选未通过，不推断均为本次重构新增。

历史报告也明确最终全套测试没有全绿结论。此次五个关键专项为 **67 passed**，验证了部分提交隔离、期限、崩溃恢复、Runtime 与证据路径，不能替代全套回归。

CI 的两处 MistyAI checkout 使用 `continue-on-error`，缺失时跨仓 schema/进程测试会条件跳过，且没有固定 MistyAI ref。因此单看 CI 绿色不足以证明将发布的双仓组合通过。CI 使用 `pip install -e`，正式部署使用 `uv sync --locked`，发布验证还需确认正式锁文件环境。

**完成标准：** 最终冻结候选通过静态检查和完整必需测试；记录 NeuralCloud/MistyAI 版本或制品哈希、依赖版本和实际执行结果；发布时跨仓门禁不可静默跳过。本次没有读取 GitHub 在线运行结果。

依据：[CI 静态与测试步骤](../../.github/workflows/ci.yml:55)、[可跳过跨仓检查](../../.github/workflows/ci.yml:39)、[第二处条件检查](../../.github/workflows/ci.yml:77)、[历史全套边界](production-validation.md:23)。

### B5 · 上线自检会把没有实际产出的结果判为通过

本次纯 Mock 复现：

- `processing_completed` 的 `decision_kind=wait`，`chat.reply` 仍为 `pass`；代码计算了回复判断 `ok`，返回状态却只看是否存在完成事件。
- 日记 `today_count` 前后均为 0，且 `committed=False`，`diary.write` 仍为 `pass`，因为使用 `after >= before`。

主动等待本身可以是合法行为，但不能计为成功回复；日记不写也不能计为成功持久化。这会污染发布验收口径。

**完成标准：** 自检区分“流程正常结束”“主动等待/拒绝”“实际回复”“实际持久化”“真实交付”，用当前操作的稳定 ID 和回执判断。

依据：[回复自检](../../tools/selfcheck/probes_production.py:81)、[日记自检](../../tools/selfcheck/probes_production.py:152)。

### B6 · 证据失败后的学习处置和运维可见性没有闭环

v2 已经耐久保存拒绝提案，`resolve_task` 支持明确关闭或引用已接受替代组；但没有自动重试 worker，生产 API/GUI 也没有接入处置入口。GUI 只展示 `local:main` 最多 32 项，QQ 受众的失败待办不在该列表。

同时，任一提案组被拒绝会保守阻止可能依赖它的回复交付。原话不丢并不代表认识已更新；持续失败也可能同时损害回复率。

**完成标准：** 首发至少有按受众查询待办、积压/年龄可见、明确负责人以及可审计处置方法。自动修复可以后续做，但不能把长期无人处理的 pending 当作“学习完成”。是否允许无争议的回复在坏提案之外继续，需要另行设计依赖边界，不能直接删掉拒绝保护。

依据：[拒绝与交付边界](../../harness/cognitive/production.py:252)、[待办操作](../../harness/memory/evidence_v2.py:527)、[GUI 数据范围](../../harness/memory/production.py:856)、[明确未实现项](production-evidence-v2-validation.md:48)。

### B7 · 最新真实拓扑与持续负载证据仍缺失

已有 Linux 性能样本验证了限定条件下的心跳、暂停与检索准备；QQ 复测使用模拟 OneBot 回执和进程内调用；身体独立安装验收不等于四服务已经连接它。v2 后尚未复测十万/百万规模、冷缓存和持续维护积压。

9 月 23 日文档曾记录 Runtime 重启、GUI 停止；这只能作为现场复核线索，不能断言现在仍故障。

**完成标准：** 在最终制品和真实授权试点上覆盖四服务 RPC、QQ 网络、真实身体、记忆回流、后台学习、重启/断连/暂停；记录成功回复延迟及失败分类，并证明持续前台流量下维护任务能推进。先现场确认现网健康和实际 release/config/hash。

依据：[QQ 测量边界](production-validation.md:90)、[Linux 性能边界](production-linux-validation.md:85)、[v2 未覆盖项](production-evidence-v2-validation.md:54)、[历史服务异常](production-nl-gliner-validation.md:66)。

## 审查时待拍板的事项

1. **首发模型和协议路线。** 继续兼容 JSON 并修复可靠性，还是实现原生结构化数据适配，或进一步验证自然正文/受控元数据分离。建议只选一条路线作为首发候选；GLiNER 与 Qwen 转换实验不作为已通过方案。
2. **首批会话和能力范围。** 明确群/私聊清单、主动联系、网页读取/搜索，以及 board/diary/voice/server/普通身体动作是否开启。建议先少量可观察会话，仅开放实际完成验收的能力；已有会话列表不能自动充当这次新机制的试点决定。
3. **成功率、延迟和中止扩量标准。** 当前没有已确认的 QQ 可接受延迟上限。分别规定成功回复、主动 wait/defer、错误通知、超时、拒绝提案的口径；设定试点观察和停止条件。历史 10 条样本的最大值不能直接定义长期 SLO。
4. **失败提案如何处置。** 人工、有限重试还是人工加自动；谁处理，何时告警，是否需要用户重述，怎样避免重复用量/交付。自动重试不是首发唯一选项，但明确可执行的处置路径必须有。
5. **运维控制和首发 UI 承诺。** 确定生产控制模式及有权操作的人；是否必须逐目标取消/暂停、撤销记忆、分配未知身体期间账单。若本期只提供全局暂停和运维脚本，应在入口和文档中明确。
6. **切换窗口与恢复责任。** 明确停写窗口、备份保存位置、单 writer 切换步骤、回退候选，以及观察期的负责人。七天观察结束后再决定旧路径退役。

以上是当前未闭合的发布决策，不是要求推翻已经冻结的身体参数、自主预算、权限或证据规则。

## 首发范围决定后可延后，但要明确边界

- **旧路径与 preset 整体 DTO 兼容桥彻底退役。** 已知仍有后台全量 DTO 保存，应先量测真实积压/性能；完成试点观察后再清理，不为形式上“全重构”贸然移除兼容路径。
- **GUI 完整迁移。** `/goals` 仍接旧 `GoalController`，新生产目标保存在独立 production store；生产主卡片未正确取嵌套 `goal.description`，可能显示 ID。若首发承诺逐目标管理则转为必修；否则至少隐藏/说明旧入口。
- **记忆撤销和跨平台身份绑定入口。** 底层规则存在，但自然对话/API/GUI 的完整用户入口尚未打通。若首发承诺“用户一句话即可忘记”，必须先实现并验收；不能将底层方法存在当作该功能已可用。
- **自动重试 worker、语义去重与长期学习效果评估。** 可阶段性交付，准确的 pending 状态和人工处置不能延后。
- **GLiNER、Qwen 转换、研究对照、语音/桌面/服务器等扩展。** 不应在基本链路未验收时同时增加生产变量。尤其服务器 SSH 命令能力启用前，须修复命令分类对双引号内 shell 替换等语法的识别缺口；未启用该能力的首发不因此被无条件阻断。
- **文档和工作区收尾。** `docs/refactor/README.md` 仍称“仅规划”，根 README 主要讲旧链路；更新单一上线入口和 release 清单。当前有大量未跟踪测试目录及 `data/`，发布应使用明确制品清单，不整目录同步或一键全部加入版本库。

依据：[旧兼容桥](grounded-agency-rebuild-plan.md:69)、[旧目标入口](../../harness/runtime/goals.py:652)、[新目标存储](../../harness/production/runtime.py:254)、[GUI 展示](../../gui/static/app.js:500)、[撤销底层](../../harness/memory/production.py:657)、[命令分类](../../harness/runtime/server_policy.py:146)、[陈旧目录说明](README.md:3)。

## 推荐执行顺序

1. 冻结首发范围：模型/协议、会话、能力、验收口径和失败处置。
2. 修复部署接线、自检误报和现有 CI 静态错误；补足首发所需运维入口。
3. 固定双仓制品、依赖与最终配置，完成必需回归和跨仓测试。
4. 只读确认现网 → 停写取得最新一致副本 → 迁移、恢复与回退演练。
5. 同一候选完成真实拓扑验收 → 单 writer 小范围切换 → 至少七天观察 → 扩量/退役旧路径。

## 本轮验证记录

- 命令：`.tmp/production-venv/Scripts/python.exe -m ruff check harness tests gui eval infra --output-format concise`；结果：21 errors，退出码 1。
- 专项：`test_production_commit_fence.py`、`test_qq_production_deadline.py`、`test_evidence_v2_crash_recovery.py`、`test_production_runtime.py`、`test_production_evidence_acceptance.py`；结果：67 passed，26.69 秒，2 条依赖弃用警告。
- 纯离线复现：回复/日记自检假阳性；部署 provider/控制模式/URL binding 冲突；默认快照来源未包含身体与结算外置凭据。
- 未运行完整仓库测试；未复测真实模型、Linux 性能、真实 QQ 或线上健康；没有修改业务源码。
