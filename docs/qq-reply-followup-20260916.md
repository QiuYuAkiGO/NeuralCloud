# QQ 回复超时修复记录（2026-09-16）

`20260916-qq-reply-v4` 已于北京时间 22:22:52 激活，22:23:31 完成健康与配置核验。整个 Direct 已关闭新图片处理，两个故障群已启用 compact/direct，Cognitive 已转为 SQLite 增量持久化。本文记录故障依据、部署边界和验证结果；自然请求的质量与响应延迟验收尚未完成。时间均按北京时间（UTC+8）表述。

## 证据范围

- `.tmp/qq-reply-followup-20260916/audit-full.json`：2026-09-16 21:52:03 采集，生产版本为 `20260916-qq-response-v3`，包含最近 60 条事件、57 条 trace 和 191 条模型请求计量。
- `candidate-tests.log` / `candidate-tests.xml`：隔离候选测试结果。
- `package.json`：候选发布包、基线及文件差异清单。
- `candidate-linux-validation.log`：候选 Linux 环境的配置预演、真实状态副本迁移和投影验证；日志已有 `REMOTE_EXIT 0`。

上述文件均位于 `.tmp/qq-reply-followup-20260916/`。本文不保留用户消息原文、模型输入正文或凭据。

## 故障事实

最近两次纯文字请求的正常正文均超过原 60 秒期限，最终发送了错误回退提示。因此事件终态虽然是 `sent`，并不代表正常正文成功交付；两条 trace 的 `protocol_error_fallback` 均为 1。

- **21:48:19 收到的请求**：排队 34.142 秒；本轮各次完整 JSON 序列化累计 8.063 秒；gate 在收到后 51.020 秒开始，58.507 秒结束；正文阶段到 60.224 秒才结束，被判定 `deadline_expired`。gate 实际输入为 50,977 tokens，用户资料 wire 为 123,384 字符，选中 76 条观察。
- **21:48:51 收到的请求**：排队 28.889 秒；完整 JSON 序列化累计 6.427 秒；gate 在收到后 51.343 秒开始，58.454 秒结束；正文阶段到 60.347 秒才结束，同样被判定 `deadline_expired`。gate 实际输入为 64,266 tokens，用户资料 wire 为 154,505 字符，选中 94 条观察。

两条请求的实际生效配置均为 `qq_context_projection=legacy`、`qq_body_pipeline=legacy`、`cognitive_incremental_persistence_enabled=false`。历史选择仍为 shadow，没有启用候选的有限历史选择。

情绪服务名义上为指导之后的主链保留 10 秒；本次指导均在收到后约 50.02 秒结束，随后持久化和上下文准备又消耗约 1 秒，gate 真正开始时仅剩约 8.7—9.0 秒。这不是“固定只给主模型 8 秒”的配置，也不意味着每次必败；但本次 7.1—7.5 秒的 gate 后，正文及交付确实没有足够余量。

同一采样的 60 条事件中，**53 条无图片、7 条各有一张图片，没有多图事件**。因此不能把本轮故障归因于“每条消息包含很多图片”，也不能期待仅限制单条消息的图片数量便解决文字排队。

冷的正文通用 parser 也是独立开销：采样中一次 persona 阶段为 8.443 秒，其中辅助模型 parse 为 4.909 秒、主模型 generate 为 3.184 秒。上述两次最新故障已经命中静态 parser 缓存，不能再从它们身上重复扣除这约 4.9 秒收益。

## 已部署版本与配置

候选版本为 `20260916-qq-reply-v4`，基线为 `20260916-qq-response-v3`。compact、正文 direct 和增量持久化均沿用已经实现的可配置路径；本次已启用这些路径，不另行削减既选历史。

- **整个 Direct 关闭新图片处理**：`qq_image_mode=off`。该开关不受 `qq_latency_conversations` 限制，覆盖所有已配置群和私聊的新附件。逐图保留原始协议附件记录及可核验的 unknown DTO，不下载、不解码、不调用识图；不生成图片内容或 OCR。已经绑定的观察及既有 OCR 原样保留。
- **仅两个会话启用 compact/direct**：`qq_context_projection=compact`、`qq_body_pipeline=direct`，`qq_latency_conversations=["2015188508/1060979317", "2015188508/1044946257"]`。名单以账号和会话共同限定，不匹配的会话继续使用兼容路径。
- **整个 Cognitive owner 启用增量持久化**：`cognitive_incremental_persistence_enabled=true`。这是 owner 级后端选择，不能按群独立灰度；需要重启完成权威状态迁移。
- 保持 `qq_history_selection=shadow`、`qq_web_policy=legacy`、`qq_stage_budgets_enabled=false`。Direct scheduler、媒体缓存、媒体并行仍沿用原配置；不靠更早超时或强制更多沉默改善指标。
- 两步情绪评估与指导、原始 60 秒期限、`validator_enabled=false` 均保持不变。模型配置仍为主模型 `deepseek-v4-flash` 和辅助模型 `qwen3:4b-instruct-2507-q4_K_M`。

compact 只改变模型投影：保留既选观察的原文、claims、完整 image_analysis/OCR 及原作者和名片对应关系，去掉重复身份快照与采集账务字段。内部完整 DTO 继续用于证据等值校验。direct 正文入口跳过通用 parser、复杂度判断及 interpreter，保留 compiler、人格、当前情绪、来源校验和可配置 validator，只生成一次正文，不续写。

候选同时支持 `qq_image_mode=one`，但本次配置为 off。one 按请求中首张附件固定准入，修订只追加来源，不因首图失败或已有绑定而换取新的图片额度；历史、自身回显和只读会话在受限模式下不启动媒体处理。

## 发布包边界

候选包包含 367 个文件，相对 v3 **仅三个运行时代码文件发生变化**：

- `harness/qq/direct.py`：图片策略冻结、逐图准入、批次首图额度及 off 时不创建视觉客户端。候选 SHA-256：`89420fe0b5454b5836651e25dadd6b236216c9b72d4fe0c0294b7e458b1448ad`。
- `harness/qq/direct_config.py`：新增 `all/one/off` 图片模式，兼容缺省 all。候选 SHA-256：`2f4c6dd653082b001569307095f5311c13902dd3f7eccb4c3c521bb70b231d93`。
- `harness/runtime/cognitive_store.py`：POSIX 下替换 JSON 检查点后同步父目录，再允许 SQLite 向 JSON 交接权威。候选 SHA-256：`4c6bf2b5842329a2df97b1f680420fe59b1bf64aa6116582d22bc868e77f68c2`。

发布包：`neuralcloud-20260916-qq-reply-v4.tar.gz`。

发布包 SHA-256：`a2edab912d5e4a9d634abe38ec2029a204bd9390891708bc011eb9e7a1a41d7e`。本地文件实算与 `package.json` 一致。

## 候选验证结果及限制

隔离候选测试：**510 passed，213.12 秒**；两条告警为既有 Starlette 弃用告警。图片策略测试覆盖零媒体工作、逐图来源校验、read capability 不重新识图、批次共享单图额度、修订保留 TTL、失败不补额度及已有 OCR 不改写。增量存储测试覆盖迁移、重启、反向迁移、事务失败、崩溃恢复，以及父目录 fsync 失败时 SQLite 继续保持权威。

Linux 配置预演报告 `written=false`，仅验证候选配置，不代表生产配置已写入。真实状态副本为 **35,355,124 bytes**：

- 迁移前 DTO 校验通过：2,640 条 ledger、12 条 prepared、4 个 session、14 条 session turn、1,359 条 emotion source、748 条 emotion reservation。
- 迁移用时约 841 毫秒；重启和反向迁移通过，迁移后 DTO 计数一致，SQLite integrity 为 `ok`。
- 单行 407 bytes 的增量提交测得约 0.93—1.84 毫秒；完整 JSON 写入微基线约 575 毫秒。这只证明小增量避免每次重写整份历史，不等于真实命令的总持久化耗时；周期性完整检查点仍存在。
- 两群真实来源副本投影分别保留 197、195 条观察，legacy/compact 字符数为 355,577/102,852 和 333,896/87,600；原文、OCR、claims 等值检查通过。

**这些投影样本不是上述两条故障的原始 gate 请求**，不能把它们的字符下降比例直接套到故障请求的 API token 数或延迟。迁移和投影验证没有调用模型，不能代替端到端质量及延迟验收，也不构成缓存命中率提升保证。

## 兼容回退

回退使用 v4 内的兼容开关，保留最新状态；不直接恢复旧数据库、旧 `state.json` 或切回旧二进制。

- 模型路径可将两个会话移出灰度名单，或把 projection/body 切回 legacy；不改发送权限、来源验证及原 TTL。
- 图片策略如需恢复，使用 v4 的 `qq_image_mode=all` 并重启 Direct。事件策略在接收时冻结，不能把运行中替换配置对象当作对既有事件立即生效的开关。
- 停用增量持久化时，仍由兼容 v4 从最新 SQLite 状态导出完整 JSON。只有文件替换及所需的目录同步成功后才能将权威标记切回 JSON；导出或目录 fsync 失败时保持 SQLite 权威，不使用旧检查点继续运行。

## 生产激活与首批观测

- 激活完成：2026-09-16 22:22:52；健康核验：22:23:31。启动恢复耗时 445.95 秒，单独记录，不计入普通消息 SLO。
- 六个服务均 active，重启计数均为 0；Runtime RUNNING、队列为空，QQ connected/automatic_send 为 true，last_error_type 为 null，未知及尝试中送达均为 0。
- Direct `images_enabled=false`、`qq_image_mode=off`；Cognitive 有效配置确认两个群 compact/direct、增量存储 true、情绪 on/8s/4s、validator false。原模型保持。
- SQLite authority 为 sqlite、generation 为 6、quick_check 为 ok；表内存在最新 ledger、情绪 reservation/source，证明已发生增量提交。
- GUI 配置 SHA-256：`aa066d2c62bd63611752f651d5a666c07c93a215848a7f031031913e826d1454`；Direct 配置 SHA-256：`3e127c6ec469b8955ed07b17741de441b39e449ae616a6afce08ecb1eb89a0c2`。
- 发布前停写一致备份已核验：`/var/backups/neuralcloud/incident-20260916-before-qq-reply-v4`，3,951 个文件、18 个 SQLite 库、2,991,582,635 bytes、200,039 条 Runtime journal 记录。
- 22:23:55 首次导出在本次激活后的 trace/model_requests 均为 0 条，尚无正常正文性能样本。历史回填观察不算新请求，不制造测试消息或付费预热来填样本。

自然观测使用 `.tmp/qq-reply-followup-20260916/verify.sh`、`export_metrics.sh` 和 `tools.qq_latency_report`；远程导出目录 `/tmp/qq-reply-v4-metrics/`。报告已按图片 all/one/off 分组，缺失模式仍未知，防止混合配置伪造收益。既有每小时跟进继续读取自然流量，检查正常回复覆盖、情绪 fallback、来源归属、重复/未知发送及各阶段耗时。达到同配置至少 100 个合格普通文字样本和 24 小时后再评估 SLO，不能从本轮离线通过推定线上全部修复。
