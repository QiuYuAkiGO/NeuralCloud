# QQ 延迟改造：兼容部署、自然灰度与回退

2026-10-06 识图更新：Linux 已启用 `20261006-qq-vision-kimi-on-demand-v2`。Direct 恢复图片获取，主模型仍是 Qwen3.8-27B，由它自主选择调用 Kimi K2.6 非思考识图工具及后续联网。六个业务服务运行，QQ 已连接；服务账号公开 OCR 样本整条回复 8.381 秒，明确不用看图的对照 2.464 秒、零识图调用。部署同时修复了旧 32 位命令 ID 碰撞造成的启动恢复失败，保留原日志与不可变记录。详见 [发布与验证记录](refactor/qq-vision-kimi-rollout-20261006.md)。下方图片关闭和旧版本信息均为历史记录。

2026-10-06 更新：有界历史选择成为代码默认值，并独立于 `qq_latency_conversations` 生效；名单外群聊和私聊也采用所配置的历史选择模式。已有持久化配置中的 `shadow` 需要显式改为 `bounded`，不能只升级代码。当前请求、补充消息及已验证引用保持完整，未呈现的历史不标记为已处理。本次不调整记忆准备、正文管线、联网或调度策略。下方 2026-09-16 上线记录保留为历史背景。

当前线上为 `20260916-qq-reply-v4`（北京时间 2026-09-16 22:22 激活，22:23 核验）：整个 Direct 已关闭新图片处理，两个故障群启用 compact/direct，Cognitive 启用 SQLite 增量持久化；510 项候选回归及真实状态副本迁移验证通过。六个服务与 QQ 收发正常，线上性能验收仍待自然样本。详见 [后续超时修复记录](qq-reply-followup-20260916.md)。当前观测脚本位于 `.tmp/qq-reply-followup-20260916/`。以下 v3/v2 内容保留为历史基线，不再表示当前配置。

当前线上已于北京时间 20:47 更新为 `20260916-qq-response-v3`：从下述 v2 清单仅迁入 gate JSON 输出约束/脱敏校验诊断及失效 ambient 图片任务延后识图三项文件修复，735 项相关回归通过，六个服务及 QQ 收发状态核验正常。现有配置和灰度开关未变；详情与限制见 [两次未返回故障记录](qq-response-incident-20260916.md)。自然观测改用 `.tmp/qq-two-failures-20260916/verify.sh` 与 `export_metrics.sh`，不再使用硬编码 v2 的旧脚本。下文 v2 部署及统计保留为历史基线。

本文对应 2026-09-16 改造。兼容版本 `20260916-qq-latency-v2` 已于北京时间 19:48 上线：六个服务正常，Runtime 为 RUNNING，QQ 已连接，核验时未知送达为零。新增行为开关保持兼容设置，后续自然灰度与质量验收尚未完成。离线通过和副本微基线都不能证明线上收益。部署包由核对过的线上基线及 56 项文件差异生成，未整包发布主工作区的未提交内容。

本次核验共 6,516 项通过、22 项跳过；另有 3 项评估脚本失败，在改造前副本中复现相同错误，详见工作目录 `.tmp/qq-latency-20260916/release-validation.json`。120 个离线合同场景全部通过。最终包 SHA-256 为 `777d2fda90c99d82197624eff4c490db5e1dc7f096a26cfbe7d8bee0cecdf0ac`；一致备份位于 `/var/backups/neuralcloud/incident-20260916-before-qq-latency-v2`。

启动恢复约 389.5 秒，单独记录，不计入普通消息 SLO。首批自然计量中两条普通消息因更新上下文终止，没有首段核验样本，不能据此报告线上响应分位数或宣称质量改善。后续使用同配置下至少 100 个合格样本及 24 小时观察门槛。

19:54 的追加快照中，有 1 次完整核验送达，但它是约 62 秒送达的错误回退提示，不能计作正常正文性能达标。当前仍处于兼容链路，已有的重复辅助调用、较大输入和全量 JSON 持久化尚未切换。保留取消和超时记录，后续阶段需同时改善延迟、正常回复覆盖及质量。

已在当前任务设置每小时一次的自然观测跟进：仅读取已有状态和脱敏计量，稳定且未满足门槛时保持安静；异常或满足取样门槛时通知。该跟进不发送 QQ 测试消息、不付费预热，也不自动开启下一阶段。

目标是在保留当前人格、当轮情绪、来源绑定与发送核验的条件下减少调用及非模型开销。联网由发言 gate 按需规划，Runtime 执行后再生成正文；情绪仍是 appraisal、guidance 两步，不以旧情绪异步替代当前 frame。

## 1. 配置来源与会话范围

模型侧开关定义在 `harness/qq/policy.py`，由 `Settings` 继承。环境变量使用 `NC_` 加字段大写；持久化 GUI 配置的 `qq_latency` 对象在 Cognitive 启动时覆盖这些环境值。`validator_enabled` 也由持久化模型配置传入，不能仅凭 `.env` 推断开关状态。检查 Cognitive snapshot、GUI 状态及最终 turn trace 的 `effective_configuration.values`，同时核对每项 `value`、`source`、配置版本和 policy 版本。

`qq_latency_conversations` 使用账户限定的会话键：

- `null` / Python `None`：所选策略适用于全部获准会话。
- `[]`：除历史选择外，会话内优化采用兼容路径。字段缺省是 `None`，不是 `[]`。
- `["12345/67890"]`：只对账户 `12345` 的群 `67890` 启用所选策略。
- `["12345/private:12345:67890"]`：只对这个账户与这个联系人的私聊启用；私聊键内的自身账户必须一致。

名单不授予收发权限，仍须满足原 roster、private contacts、协议证据和 send guard。Cognitive/Runtime 与 Direct 的持久化配置必须使用相同名单。名单外会话使用 `legacy` projection/body/web、`shadow` scheduler，阶段预算关闭；历史选择保持配置值，默认为 `bounded`。显式选择 `shadow` 仍只计算候选统计，不能当成行为加速已生效。

SQLite 持久化是共享 owner 级设置，不受会话名单隔离。情绪输入布局、计量、前台命令优先等共享改动也不能宣称由这份名单完全隔离。

## 2. 本轮开关及保留项

模型侧 `qq_latency` 字段及代码默认值：

- `qq_context_projection="compact"`：紧凑的模型输入投影；`legacy` 保留兼容投影。完整证据仍保存在来源记录中。
- `qq_body_pipeline="direct"`：gate 已准入后直接进入普通 compiler/character/validator，跳过再次解析固定适配指令、复杂度判断和召回；`legacy` 保留原 pipeline。正文不因此新增一次情绪观察。
- `qq_history_selection="bounded"`：默认采用整条历史预算，适用于名单内外会话；`legacy` 显式保留原选择，`shadow` 显式仅计量。gate 默认软目标 `qq_gate_history_soft_characters=24000`，body 为 `qq_body_history_soft_characters=64000`。这两个数是字符数；当前请求、补充消息及已验证引用必须保留，不能靠截断必需证据满足目标。
- `qq_web_policy="legacy"`：`after_gate` 启用 PrepareQQTurn → 可选 Web → ResumeQQResponse；`off` 使用新链路但不执行自动联网。自然灰度应选 `after_gate` 以保留自动联网。沉默决定不启动 Web 或正文。
- `qq_reply_scheduler="shadow"`：`coalesced` 才启用修订请求及合并调度。Direct 的同名配置决定实际 ingress/worker 行为，不能只改模型侧。
- `qq_stage_budgets_enabled=false`：启用后 gate 上限默认 `qq_gate_timeout_seconds=15`，正文至少预留 `qq_body_reserve_seconds=10`，发送预留 `qq_delivery_reserve_seconds=3`。Web 上限 `qq_web_timeout_seconds=10`，可用时间不足 2 秒时跳过；Web 本身即使不开主模型阶段预算也使用其有限预算。所有阶段从最初 60 秒窗口扣减，不重开计时。
- `cognitive_incremental_persistence_enabled=false`：开启 SQLite 增量持久化；实际后端在 owner 启动时确定，修改后需重启 owner，不能靠 GUI 热更新声称存储已切换。
- `qq_latency_conversations=null`：范围语义见上节。

Direct 配置另外提供 `qq_media_cache_enabled=false`、`qq_media_parallel_enabled=false`。这两个开关同样按 Direct 的 `qq_latency_conversations` 生效，名单外关闭；实际事件冻结的 scheduler/cache/parallel 值会记入 trace。媒体并行由有界队列控制，当前 acquisition 并发 2、vision 并发 1；已经开始的调用不被抢占。成功图片分析缓存按图片摘要、模型身份/版本、提示和处理版本绑定。未知或可变模型版本采用进程 epoch，不保证跨进程命中；失败结果不会冒充成功分析。

保留原 `emotion_mode`，appraisal 8 秒、guidance 4 秒，两步各自只处理当前绑定 frame。`aux_llm_keep_alive=-1`、`aux_llm_num_ctx=8192` 是既有配置，需查实际 provider 驻留情况，不能把默认值当作新收益。`parser_llm_enabled`、`planner_llm_enabled` 是全局设置，本轮 QQ direct body 的跳过行为不要求关闭其他场景的解析。后验 `validator_enabled` 可按既有持久化配置关闭；协议、身份、证据、修订、TTL、分段格式和送达硬校验必须继续执行。

## 3. 部署与启用顺序

1. 保存当前程序版本及一致的运行数据备份。正常停止或使用 SQLite backup API 获取数据库，不能在 WAL 活跃时仅复制主 `.sqlite3` 文件。保留原模型配置、Runtime journal、Cognitive 状态、QQ memory、Direct inbox/outbox 和媒体证据。
2. 先部署完整且相互匹配的候选代码，明确配置 `qq_latency_conversations=[]`，存储与媒体开关保持关闭，Direct scheduler 保持 `shadow`。先验证启动、已存在 journal 的读取、原发送链路及内容安全计量。不要只复制调用端而遗漏 DTO、路由、policy 和迁移实现。
3. 确认 Cognitive snapshot 广告 `PrepareQQTurn`、`ResumeQQResponse`，Runtime 客户端/路由也支持相同接口；确认两端配置来源和名单一致。Runtime 会在启用新 Web 策略时检查服务能力。
4. 单独切换 owner 级 SQLite 存储并重启，先验收新结果重放、保留 reservation、重启恢复及关闭开关导出路径。该阶段收益看持久化分段，不能混入模型耗时。
5. 选少量已授权会话灰度 `compact` + `direct` + `after_gate`，保持情绪两步和主人格不变。gate 只输出联网意图，Runtime 对每一步重新确认能力、范围、来源与预算；不确定的外部请求结果不重发。
6. 历史选择默认采用 `bounded`；旧部署若仍显式保存 `shadow`，需单独迁移并回放验证。阶段预算、Direct `coalesced` scheduler、媒体缓存/并行仍逐项开启。每次固定配置版本重新累计样本，避免多个开关同时变化后无法归因。合并请求必须继续校验修订号和所有 source IDs，新增补充产生新的当前情绪输入，不重复刺激已处理 anchor。
7. 满足下节证据和人工质量条件后再扩大会话名单。变更期间保持送达核验与结构化终止原因完整；不要因为延迟下降就跳过遗漏/错误回复审查。

兼容模式控制的是新请求。已经 journaled 的新命令使用冻结 policy；切换名单或开关不能改写其原 TTL，也不能把未完成外部动作当作安全重试。变更前应排空或按现有取消/过期协议终止在途请求。

## 4. 指标与离线验收

在项目根目录运行以下离线命令。路径为示例，应替换为实际采集文件；这些工具不调用模型、不发送消息：

```powershell
python -m tools.qq_offline_acceptance --output qq-offline-acceptance.json
python -m tools.qq_latency_report --trace direct.trace.jsonl --requests model_requests.jsonl --output qq-latency-report.json
```

`--trace` 与 `--requests` 可各自重复指定；`--high-queue-ms` 默认 5000。Direct trace 路径为其 `state_db.with_suffix(".trace.jsonl")`；物理模型请求日志位于对应服务的 `settings.trace_dir/model_requests.jsonl`。模型调用跨服务时需收齐相应日志；按明确 trace/correlation/decision/event 身份关联，不能按时间重叠猜测。

灰度分析使用实际 trace 上的 `conversation_key` 和 `effective_configuration.version`：`--conversation-key`、`--configuration-version` 均可重复指定。前者格式为 `qq-scope:` 加 64 位十六进制 SHA-256，由 Direct 对 `账户 + NUL + group_id` 绑定生成；工具只精确匹配已记录键，不从正文、event ID 或缺失字段猜会话。筛选后只纳入与被选 trace 明确关联的物理 span，排除数量保留在 `filters`。例如把实际键与版本放进变量后执行：

```powershell
python -m tools.qq_latency_report --trace direct.trace.jsonl --requests model_requests.jsonl --conversation-key $CanaryKey --configuration-version $CognitiveConfigVersion --output qq-canary-report.json
```

即使文件包含多个版本，`configuration_groups` 仍分别给出每个有效配置下的 totals/cohorts。分组键同时包含 Cognitive 配置版本、Direct 实际 `scheduler_mode_*`、`media_cache_enabled`、`media_parallel_enabled`；不能把不同 Direct 策略因 Cognitive 版本相同而混算。顶层混合总览保持 pending，但每个配置组可以独立累积 100 样本与 24 小时。缺 Direct 标记的组仍为 unknown/pending；缺会话键的旧数据不会混入指定会话灰度。

报告先按 trace ID 取最新 snapshot，再按逻辑 event ID 去重覆盖分母；请求日志按 span ID 去重，finished 优先于 started。普通文字、图片、Web、高排队四个 cohort 独立统计，图片/Web/高排队可重叠，不能把它们相加当总数。报告给出：

- received → 首段已验证、received → 全部分段已验证的 P50/P95；只有 `sent` 状态不能推算核验完成时刻。
- 回复覆盖、完整送达、部分送达、沉默、错误、fallback、late、未知送达及未知结果；不能只选成功回复计算覆盖率。
- 实际请求数、输入/输出 token、缓存已知覆盖、已知 hit/miss、每个模型阶段和非模型阶段耗时。缓存 unknown 不算 miss，输入 API usage 不完整时覆盖率保持空值。
- appraisal/guidance 的 `model_calls`、新提交 `results`、`fallbacks`、`reused_results` 和已知/未知事件数。fallback rate 分母仅为新提交结果；复用旧 fallback 不增加模型调用或新 fallback。没有计量字段保持 unknown。
- 每个 cohort 的配置版本及 pending 原因；嵌套 stage 时长不能相加当总耗时。latest event 的情绪工作计数不是跨日志去重物理 span 的替代品。

物理 span 计量与内容 call log 独立。只读识图可以产生 `traces/model_requests.jsonl`，其中只允许时间、模型/脱敏端点、usage、状态及稳定前缀摘要等运维字段；不能出现图片 bytes/base64、prompt、OCR、观察正文、密钥或异常原文，也不能因此写入观察、session 或 memory 文件。对应 vision 回归测试对文件路径及字段白名单作明确约束。

自然样本要求每个目标 cohort 在同一有效配置下至少跨越 24 小时，至少 100 个终态样本、100 个首段已验证样本、100 个完整已验证样本。未达到的 cohort 保持 pending；24 小时指观察到的 first-seen 时间跨度，不证明采集连续无缺口。配置混杂或缺失不能合并为 ready。报告的 `ready_for_review` 仅代表计量样本齐备。

固定 fixture `tools/fixtures/qq_latency_contract_cases.json` 包含至少 120 个不同 case ID，覆盖文字、多成员身份、情绪、图片、Web、修订、取消和重启。runner 使用固定输入、既有合同和替身，检查结构归属与行为约束；它不能判断真实模型的人格表现、自然群聊取舍、语气质量或真实线上 P95。人工复核应从自然样本中同时抽取成功、沉默、fallback、多人补充、图片和 Web 回复，评估身份/事实归属、是否漏答、角色延续与情绪是否贴合。结果仍须记录为质量待验，不能用 120/120 代替。

性能对照需要保留变更前相同会话与可比输入类型的 trace。检验延迟、回复覆盖、错误/回退、模型调用和 token/缓存指标是否同时改善或保持可接受；不能用降低回复量换出的低 P95 宣称成功。观察到身份/证据错用、修订后发送旧正文、重复刺激/重复发送、送达未知增长或明显人格退化时停止扩量，使用新程序的兼容配置回退。

本轮只读副本微基线记录：状态文件 26,572,982 bytes（约 25.34 MiB）；JSON serialize 三次 451/433/439 ms，write + fsync 为 49/38/42 ms；read 约 8.8 ms，parse 约 133 ms。这是隔离副本操作，未包含正常 owner 锁等待，**不是线上逐阶段 P95，也不是 SQLite 改造收益实测**。aux 驻留检查亦只是设备状态证据，不替代逐请求 timing。

## 5. SQLite 状态兼容与回退

`harness/runtime/cognitive_store.py` 的 sidecar 路径是在 Cognitive JSON 状态路径后追加 `.sqlite3`，例如 `state.json.sqlite3`。它使用 SQLite WAL、`synchronous=FULL`，在一个完整事务中保存 command reservations、ledger、prepared、emotion 与 session 的变更行。每次写入只序列化 dirty rows，不先序列化全量历史再比较；事务提交后才确认 dirty version 并发布 owner snapshot。

首次开启从旧 JSON 一次导入；之后 metadata 的 `authority=sqlite` 表明 SQLite 才是最新真相。每 128 次提交及正常关闭尝试生成 JSON checkpoint。checkpoint 失败不会撤销已提交事务，所以磁盘上存在 JSON 并不说明它最新。

关闭步骤必须使用**认识 SQLite 状态的新程序**：

1. 排空前台命令，正常停止 owner；保留 sidecar、WAL 及原 JSON。
2. 在实际生效的持久化 `qq_latency` 配置中设置 `cognitive_incremental_persistence_enabled=false`。
3. 用同一兼容新版重启。加载器即使 flag 关闭也先检查 sidecar；若 authority 是 SQLite，读取最新 DB，再原子写入并 fsync 最新 JSON，成功后才将 authority 改为 JSON。
4. 确认启动成功、实际后端、最近 durable command 的结果与 cursor/revision 一致，再恢复入口。若导出失败，保留 SQLite 权威，不删除 sidecar 或强行读取旧 JSON。

再次开启时，若 authority 已为 JSON，则将最新 JSON 重新导入 SQLite。后端在启动时冻结，运行中改设置不执行这一转换。

**不能直接回滚到不认识新状态的旧程序。** 强制关闭后的旧 JSON 可能过期；即使最新 JSON 已导出，旧程序仍可能不认识新 QQ 命令、checkpoint、reply request 修订及 inbox/outbox 状态。安全回退先使用新版 `[]` / legacy / 关闭新增开关，保持新状态读取能力。只有另行完成状态兼容审计、在途请求处理及一致备份验证，才可考虑更老二进制；本候选没有提供通用旧状态降级器。

## 6. 文件分批审阅建议

以下是审阅与打包归类，不是允许拆开发布有依赖的文件：

- 配置和计量：`harness/qq/policy.py`、`harness/config.py`、`gui/persist.py`、`gui/service.py`、QQ telemetry、request spans 与报告工具。先确保 effective configuration、终止原因、首段/完整核验标记可用。
- 增量持久化与共享调度：`harness/runtime/cognitive_store.py`、adapters 持久化局部、`pipeline/session.py`、emotion 持久化接口、`cognitive/qq_memory.py`、`daily_revision.py`。同时包含迁移、崩溃恢复、dirty tracking 和取消等待测试。
- gate/body 与证据：`pipeline/pipeline.py`、`qq/decision.py`、`context_budget.py`、`context_window.py`、`history_selection.py`、`prepared_turn.py`、`prepared_evidence.py`、`turn_checkpoints.py`、`cognitive/qq_turn.py`。必须连同绑定和 source/DTO 校验部署。
- Runtime 与自动联网：supervisor、`qq_pipeline.py`、`qq_web.py`、`qq_sources.py`、`qq/web_plan.py`、对应 Cognitive/Runtime 路由与客户端。保留原 web guard、固定执行 ID 和持久化不确定结果语义。
- Direct 调度与媒体：`direct.py`、`direct_store.py`、`direct_source.py`、`direct_config.py`、`reply_requests.py`、`shared.py`、`emotion_window.py`、`direct_images.py`、`direct_media.py` 及相关证据 DTO。协议保护、修订/outbox 和媒体缓存应成组审阅。
- 验收资产：相关新/更新测试、120 场景 fixture、offline runner、latency/cache 报告工具与本说明。验收记录必须绑定候选文件摘要及配置版本。

最终部署清单以隔离候选与已知线上基线的精确差异为准；上面的归类不是穷尽清单。没有通过依赖检查和完整候选回归之前，不从主工作区手工挑文件替换生产。
