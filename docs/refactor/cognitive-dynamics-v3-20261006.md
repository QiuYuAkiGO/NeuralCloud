# 内稳态 v3 与群聊接收控制：实现与验证

本次实现将实际加工记录、被动时间推进、语义评价和群接收策略分开。七维主界面保持不变，新增变量放在内部状态与诊断面板中。默认配置仍为 `off`；提供独立的 `shadow` 配置与受条件约束的 `active` 配置生成入口。

**最新数据库热修复：** 已部署 `20261007-shared-memory-wal-v1`（`de346c8`），启用 WAL 并缩短队列写锁。详见[锁竞争修复](shared-memory-lock-hotfix-20261007.md)。连续 shadow 窗口为北京时间 **2026-10-07 21:55:35**，无新断档时最早 **2026-10-08 21:55:35** 验收；以下为历史记录。

**前次恢复路径热修复：** 已部署 `20261007-memory-validation-retry-v1`（`e2aa56d`），新增 schema 纠正反馈及可重试业务拒绝，目标跳过任务已恢复。详见[恢复路径修复](memory-validation-retry-20261007.md)。本次维护后连续窗口为北京时间 **2026-10-07 13:12:52**，无新断档时最早 **2026-10-08 13:12:52** 验收；以下均为历史记录。

**前次热修复：** 已部署 `20261007-structured-memory-hotfix-v2`（`60dc1a5`），解决已复现的人物印象缺少 `subject_refs` 的结构化校验失败，并增加安全字段诊断；同时修复旧任务恢复时的倒序印象提交，旧经历保留、过期印象增量留待审计。profile `@3`、manifest revision `4`、shadow 模式保持不变。受影响任务恢复 10/10；详见[热修复说明](structured-memory-hotfix-20261007.md)。本次维护后连续影子窗口重启于 **2026-10-07 03:55:56**，无新断档时最早验收 **2026-10-08 03:55:56**；下方旧窗口时间属于历史记录。

**2026-10-07 03:24–03:26（北京时间）检测：** 发布、profile 和 manifest 保持下述修复版本，六个服务及 QQ 连接正常，Runtime RUNNING，watchdog 为空。服务器连续影子时长为 **19,820.73 秒（约 5 小时 30 分钟）**，实际加工记录 **377 条且全部应用**，独立被动推进器无错误；尚未满足 24 小时门槛，未切换 active，也未重启或修改生产配置。[本轮检测证据](../harness/evidence/cognitive-dynamics-v3-20261006/shadow-check-20261007-0324.json)。

按阶段关联，10 次成功的 `qq_gate` 调用已有 2 条有效注意评价；总计 2/377 混入后台记忆、情绪等不同任务，不能作为正常决策失败率。46 次 `goal_decision` 尚无该类记录，需继续核查覆盖，不能将缺失评价直接视为模型调用失败。

**后续验收注意：后台记忆仍有任务被跳过。** 修复后更新的任务中，88 个 applied，10 个因 `StructuredResponseError` 达到 8 次重试上限而 skipped，另有 1 个因 `ValueError` skipped。当前 pending/preparing/retry 均为 0、worker 最近错误为空，但这不代表所有任务成功。请求日志确认前者发生在 HTTP 200 后的结构化响应校验；现有日志只保留异常类别，本轮没有证据进一步确定具体字段或拒绝原因。切换 active 前须复核跳过数量是否继续增长及其对经历/评价覆盖的影响，不能只看服务健康和时长。

**修复发布记录：2026-10-06 21:54:31（北京时间）修复版已上线，六个服务 active、Runtime RUNNING、QQ 已连接、独立被动推进器无错误。** 当前 release 为 `20261006-cognitive-dynamics-v3-shadow-v3`，代码提交 `47f9da9`，profile 为 `qq-social-drive-v1@3`，manifest revision 为 `4`，仍为 `shadow`。新数学规则与公平输入预算尚未转为 active。[修复与验证证据](../harness/evidence/cognitive-dynamics-v3-20261006/shadow-repair.json)。

本轮复核发现 20:59 又出现旧边界 watchdog 自动暂停：新增 15 条群消息事实后，旧负荷和新奇度触界，而影子候选值在暂停期间持续正常恢复。核查自动暂停后的控制事件，没有用户暂停或紧急停止；停止 writer 后再次核查，随后备份并校验 12,237 个文件、23 个 SQLite 库、953,286 条 journal 记录（10.946 GB）。新备份为 `/var/backups/neuralcloud/20261006-shadow-watchdog-repair`，原首次发布备份继续保留。

修复仅停用影子 profile 的旧持续心理触界暂停计数，数值/存储/服务故障检查继续生效；保留旧标量计算作为对照。通过一次空评价引擎心跳重新计算健康，不重播工作或互动收益，Runtime 确认游标保持不变。维护前后的群策略库哈希一致。58 项针对性测试通过；增量汇总为 **4,419 passed、0 failed、2 skipped**。

初始 55 条工作记录经请求元数据关联，符合后台共享记忆处理路径，并不是 55 次正常决策评价失败；本版为该路径增加 `qq_memory_consolidation` 标记。覆盖率仍保留原始分母，同时按调用阶段检查，不从缺失评价推断心理收益。

21:55:52 再次核验：Runtime 仍为 RUNNING，watchdog 为空，工作记录由 55 增至 66 且全部应用；自然输入接纳计数由 20 增至 36，三群策略保持一致。连续影子时长增至 100.64 秒，独立推进器无错误，没有向群发送测试消息。后台记忆请求仍有修复前已出现的 HTTP 200 `StructuredResponseError`，属于结构化响应解析问题，继续结合重试/完成结果观察，不计作可选语义评价失败。

此次停止及启动造成真实采样断档，连续影子窗口重新开始。根据服务器持久化 `last_sample_at - continuous_seconds`，当前窗口起点为 **2026-10-06 21:54:07**，无后续断档时最早验收约为 **2026-10-07 21:54:07**。继续每小时自动复核；最终以连续时长、最近采样、真实工作及健康状态为准。旧自动暂停故障已处理，新的 24 小时验收尚未通过。

**首次发布：2026-10-06 20:30（北京时间）影子版已上线，真实 24 小时影子观测正在进行；新数学规则尚未作为生产决策的主状态启用。** 该次发布为 `20261006-cognitive-dynamics-v3-shadow-v2`，代码提交 `9882b5f`，profile 为 `qq-social-drive-v1@2`，manifest revision 为 `3`。20:34 复核时，六个服务均为 active，Runtime 为 `RUNNING`、健康检查通过，QQ 已连接，独立被动推进器无错误。详见[上线证据](../harness/evidence/cognitive-dynamics-v3-20261006/deployment.json)。

上线前确认暂停来自旧心理边界 watchdog，随后停止全部业务 writer，导出并校验 12,148 个文件、21 个 SQLite 库和 940,426 条 journal 记录（约 10.89 GB）。备份保存在服务器 `/var/backups/neuralcloud/20261006-cognitive-v3-before-shadow`。在服务身份及独占域锁下，旧引擎执行了一次空评价被动心跳，清除 watchdog，并保留 Runtime 的原始确认游标；没有重播工作或互动事件。该次旧规则积分的请求、哈希、前后值和 trace 已记录在上线证据中。

第一轮发布在配置校验时发现 manifest 将 QQ profile 的支持版本写死为 `@1`。修正为接受该谱系的后续有效 revision 后，250 项针对性测试通过；失败发生在配置写入之前，修正版使用独立发布目录重新安装。全部验证按每个用例最近一次结果汇总为 **4,411 passed、0 failed、2 skipped**，属于增量验证汇总。

第一轮影子连续采样约从 **2026-10-06 20:30:45** 开始（本次修复重启后已重新计时）。20:34 已有 77 个样本、21 次真实加工记录；可选语义评价覆盖为 0/21，作为待观测指标保留。当时门槛仅缺连续 24 小时；原定最早验收时间 **2026-10-07 20:30:45** 已因后续停机作废，以最新持久化采样时长为准。已安排当前会话每小时复核（跟进 ID `v3`），通过验收后继续已获授权的 active 切换。输入公平预算在影子期保持关闭，控制面板与持久化群策略入口已可用。

20:36 检查确认群接收控制页面资源与发布包哈希一致，GUI 接口返回 200 和 3 个授权群的策略。启动日志还存在上线前已有的 embedding HTTP 404 警告（前后各记录一次），语义检索使用既有降级路径；这不属于本次修改的配置，仍须在影子验收中与评价覆盖率一起观察。

## 实现位置

| 责任 | 代码 |
| --- | --- |
| 连续时间负荷、疲劳、心理基线/反应、习惯化、无聊 | `harness/homeostasis/dynamics.py` |
| v3 快照、状态事务、迁移、评价去重、主动模式切换 | `harness/homeostasis/{models,engine,ledger,appraisal}.py` |
| 不含对话正文的实际加工记录 | `harness/homeostasis/work.py`、`infra/trace/request_spans.py` |
| 独立 5 秒状态推进器 | `harness/runtime/background.py::PassiveStateWorker`、`supervisor.py` |
| 持久化接收策略与历史读取检查 | `harness/qq/attention.py` |
| 接收、图片、缓存、发送、事实反馈检查 | `harness/qq/{direct,direct_source,direct_capabilities,vision_tool}.py`、Runtime QQ 入口 |
| 后台记忆和跨会话历史过滤 | `harness/cognitive/{qq_memory,shared_memory,daily_revision}.py`、`harness/memory/{shared,production,revision/recall}.py` |
| 独立复查与预算 | `harness/runtime/attention.py`、`goals.py` |
| 控制面板 | `gui/static/attention.js`、`app.js`、Runtime/GUI attention API |
| 回放、影子门槛、生成启用配置 | `harness/homeostasis/rollout.py` |

## 状态计算

`cognitive_load` 只接收已发生的加工量：主动阅读、实际生成、图片加工、上下文切换。过去 60 秒统一计量，容量初值 4096，图片权重 256，切换权重 128。需求按 `r / (1 - 0.4 × fatigue)` 调整，负荷目标为 `0.10 + 0.85 × d`，上升时间常数 30 秒，下降 180 秒。

疲劳使用 `r × (1-F) / 2700 - (1-r) × F / 1200`。没有输入时采用解析解；有输入时按固定 UTC 秒网格分段积分。离线期间的旧工作样本按原始时间过期，随后按实际经过时间恢复。网络等待时间没有单独的疲劳收费项。

四个心理维度保存基线与短期反应。安全、自主、能力、联结的短期时间常数分别为 1800、1800、3600、7200 秒。模型置信度乘以 0.35，每次短期变化限于 ±0.05，每维滚动一小时累计绝对变化最多 0.15；基线只允许主机绑定的真实结果训练，滚动一天累计绝对变化最多 0.02。账本按不可变因果引用及内容身份去重，不能通过换评价 ID、跨群或切换正负方向刷额度。

迁移没有足够信息推断旧值中哪些是短期反应。因此将当时心理显示值保存为低置信度初始基线，短期反应初始化为零，不把人格偏好与当前值之间的差额解释成待恢复的关系收益。迁移保持七维显示连续；已记录的旧评价继续参与去重，不重放旧收益。

`energy_reserve` 继续只接受已有可信身体观测。观测过期只降低置信度，聊天、模型自评和休息计划完成不能给精力充值。

每个话题的习惯化按 `H += 0.25 × (1-H)` 增长，6 小时自然衰减。同一注意片段的多次模型调用只更新一次习惯化；相同文本的新消息仍有阅读成本。新奇度按 600 秒平滑；缺少新评价时回到 0.5。习惯化不缩减实际工作量，也不阻止新威胁或新事实的心理评价。

无聊与负荷分开。模型明确愿意参与时，意义不足或需求不匹配可提高无聊；主动休息的目标为零。参与意愿由已有评价保留，避免一次评价刚过一分钟就被当作自动放弃参与。只有无聊、低负荷、低疲劳和参与意愿同时满足，才允许提高探索建议。

### 计量边界

- 生成量优先使用供应商返回的 usage；保留计量来源。
- 自动系统前缀和自动携带的旧内容不重复收费；明确重读以及新事件中的相同文本仍收费。
- 当前缺少主模型对应的精确局部 tokenizer，因此新增文本使用确定性的 `text_units` 估计（CJK 字符计一单位，其余 UTF-8 字节约每四字节一单位）。记录明确标注 `input_count_basis=text_units_estimate`，不能当作精确 token 数或人的生理测量。
- 图片和上下文切换采用设计中的工程权重。
- 可选语义评价校验失败时，仍保留工作记录并正常回复；覆盖率中表现为未评价。
- 正常 QQ 决策、能力选择及已有活动复评复用当前模型调用。影子计算本身不额外调用模型。

## 接收策略与并发

能力 ID 为 `communication.qq.attention.set.v1`。参数为 `group_id`、`mode`、`reason` 和可选的 `review_after_seconds`（900–14400）。Runtime 绑定身份、幂等键、能力目录及策略版本。授权来自 `observe`，不要求 `send`，所以只读群也可以自主关闭。

策略保存在独立的 `attention.sqlite3` 中；默认位于 Direct 状态库同目录，也可通过 `attention_db` 明确指定。关闭没有到期自动开放机制。默认复查间隔为 15、30、60、120、240 分钟，之后保持 240 分钟。

原始协议消息先留档，再检查策略。关闭后的消息不生成认知观察、图片分析、记忆、摘要或内稳态事实；@ 不绕过关闭。历史查询、缓存读取、跨群回忆和后台整理也使用同一策略。关闭立即取消等待中的共享回复，已开始的任务在下一次模型读取、提交或发送前再次验证。先前已经完成的工作仍计入成本。

人工操作不依赖群消息队列。模型使用版本条件写入，用户操作更新版本，因此旧模型结果不能覆盖较新的人工恢复。重新开放时旧队列和缓存不复活；关闭期间的原始记录只能通过主动、有界的历史读取返回，不能自动补入记忆或重播互动奖励。

开放群统一限制每分钟 4096 加权输入单位。配置中的多个群各有 `4096 / 群数` 的上限，另为被直接寻址的输入保留部分容量；预算暂缓的内容留在原始记录中。这个初版采用保守配额，空闲群的份额不会无条件借给繁忙群。

独立复查每小时最多 4 次，与既有目标决策合计最多 12 次。普通目标调用预留复查额度；更低的已配置总预算同样生效。所有到期群合并为一次复查，输入只有当前自身状态、关闭理由和既有策略，没有关闭期间的群内容。模型仍可保持关闭。用户手动暂停时只推进被动状态，不触发复查模型或恢复对外活动。

控制 API 为认证后的 `GET/POST /v1/qq/attention`，GUI 对应 `/api/qq/attention`。面板显示策略理由、来源、版本、复查时间，并区分自主关闭、预算暂缓、模型选择不回复和运行故障。

## watchdog 与恢复

主动模式的心理边界写入 `regulation_pressure`，不再使 Runtime 全局暂停。watchdog 保留对非有限数值、状态损坏及必要服务异常的处理。低负荷不再被视为必须寻找事情的偏差。

`PassiveStateWorker` 使用独立队列，每 5 秒推进状态。普通认知队列拥堵或 Runtime 暂停时仍执行；每个新状态事件也先推进真实经过时间。暂停期间不运行自主模型调用。进程离线补算不截成 90 秒，不生成虚构的工作、社交或探索结果。

影子模式保留旧版七维计算、证据时钟和决策输入，同时维护独立候选状态。首次上线仍保留了旧边界 watchdog，但在 20:59 再次因 15 条新增消息事实触发全局暂停，阻断了有代表性的流量。修复后的 `stage-shadow` 将 `watchdog_boundary_heartbeats` 设为 `null`，停用持续心理触界的暂停计数；非有限数值、存储和必要服务故障检查继续生效。独立被动推进器不擅自清除已有暂停；部署时必须先核实暂停来自自动边界检查、没有新的用户暂停，再在停写备份后通过引擎命令恢复健康。新数学规则仍需满足连续 24 小时门槛，停机造成的断档重新计时。

## 验收与证据

回放固定于[224 条原始消息时间线](../../tests/fixtures/homeostasis-224-message-timeline.json)，包括只读群 `1060979317` 的 207 条、群 `1044946257` 的 10 条、群 `712342021` 的 7 条及 3,826 次心跳。使用诊断保留的初始七维值，最终报告见 [replay.json](../harness/evidence/cognitive-dynamics-v3-20261006/replay.json)。

历史资料没有真实加工记录，因此这次回放只检验原始入站不能直接制造负荷或心理奖励，不伪造旧 token 消耗或旧模型评价。负荷/疲劳公式、有符号心理变化、习惯化、工作计量和并发关闭另由专项测试检验。测试汇总见 [validation.json](../harness/evidence/cognitive-dynamics-v3-20261006/validation.json)。

| 计划中的验收 | 主要检查 |
| --- | --- |
| 224 条消息不触发全局暂停 | 固定时间线回放、raw fact 回归 |
| 重复话题适应、新话题响应、数量不产生联结收益 | `test_cognitive_dynamics.py` |
| 关闭后 @、图片、历史、后台均受限制 | `test_qq_attention.py`、QQ Direct/记忆/来源回归 |
| 心跳频率一致、暂停及长离线恢复 | 动态积分测试、真实 Runtime 暂停测试 |
| 换 ID、反复开关、重复证据不能刷收益 | 持久因果账本、绝对预算及正/零/负评价测试 |
| 并发、重启、人工覆盖一致 | SQLite 版本条件写入、在途发送与缓存取消测试 |
| 评价缺失仍能工作 | 可选 DTO 校验、加工记录保留、覆盖率与影子门槛测试 |

## 影子启用与后续切换

1. 使用经过测试的发布代码；先解决旧实例的健康/暂停原因。保持现有用户暂停和群权限的含义。
2. 从当前配置生成影子 revision，保持原有 profile ID；同步更新 manifest 对 profile revision 的引用。预置示例为 `presets/homeostasis/qq-cognitive-dynamics-shadow-v3.json`。不要直接将旧 revision 改写。
3. 所有模型工作进程和 Runtime 共用 `NC_COGNITIVE_WORK_DB`。Linux 部署默认 `/var/lib/neuralcloud/cognitive-work.sqlite3`；自定义启动同样需要配置这一绝对路径。启用 `shadow` 后，确认 5 秒推进器、实际 work receipts 和评价覆盖统计持续增长。
4. 至少连续观测 24 小时。超过 30 秒的采样断档会重新计连续时长；停机时间不能冒充影子观测。没有最近实际工作记录，即使放置满 24 小时也不能晋级。
5. 用真实快照检查，再生成一份新的 active profile 和一份启用公平预算的 Direct 配置。生成配置不会改写 live 文件或自动部署。应用时应作为同一发布更新 manifest/profile/Direct 绑定。

```powershell
.venv\Scripts\python.exe -m harness.homeostasis.rollout replay --timeline tests/fixtures/homeostasis-224-message-timeline.json --output docs/harness/evidence/cognitive-dynamics-v3-20261006/replay.json
.venv\Scripts\python.exe -m harness.homeostasis.rollout stage-shadow --profile presets/homeostasis/qq-social-drive-v1.json --output .tmp/homeostasis-shadow.json
.venv\Scripts\python.exe -m harness.homeostasis.rollout check-shadow --snapshot .tmp/shadow-snapshot.json --output .tmp/shadow-gate.json
```

在目标 Linux 主机读取其 Direct 配置时，使用该主机的 Python：

```sh
python -m harness.homeostasis.rollout promote \
  --snapshot /path/to/real-shadow-snapshot.json \
  --profile /path/to/shadow-profile.json \
  --replay-report /path/to/replay.json \
  --direct-config /etc/neuralcloud/direct.json \
  --direct-output /path/to/direct-active.json \
  --output /path/to/homeostasis-active.json
```

`check-shadow` 的 `ready=false` 和 `reasons` 是明确未通过；`promote` 会拒绝未通过报告、无加工样本、无效状态、错误 profile 绑定或不完整的固定回放。缺少可选语义评价本身不使回复与恢复停摆，覆盖率必须保留供校准。

回滚计算规则时，在支持 v3 的这版程序中生成更高 revision 的 `cognitive_dynamics.mode=off` 配置。保留 `attention.sqlite3` 及注意力检查代码，不能通过换回不识别关闭策略的旧程序来回滚数学规则。新增工作库已纳入部署快照收集。

## 论文机制与工程参数

本实现采用用户设计中限定的机制范围：[Rankin 等的习惯化](https://pmc.ncbi.nlm.nih.gov/articles/PMC2754195/)、[Shenhav 等的控制价值](https://pmc.ncbi.nlm.nih.gov/articles/PMC3767969/)、[Kurzban 等的机会成本](https://pubmed.ncbi.nlm.nih.gov/24304775/)、[Westgate 与 Wilson 的 MAC 无聊模型](https://pubmed.ncbi.nlm.nih.gov/29963873/)、[Matthews 与 Tye 的社交内稳态](https://pmc.ncbi.nlm.nih.gov/articles/PMC7593988/)、[Ten 等的学习进展与探索](https://www.nature.com/articles/s41467-021-26196-w)。论文不是本系统系数的测量依据；时间常数、额度、工作权重、配额和阈值仍需根据真实影子数据校准。
