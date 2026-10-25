# 人格、旧状态与归档维护：隔离验收记录

本记录承接 [04-managed-harness.md](04-managed-harness.md)。本轮按用户指示完成这三部分的隔离实现和测试，**未切换生产、未启动第二个正式角色、未发送真实 QQ 消息、未调用生产模型**。P1/P3 的本轮隔离检查点通过；整体验收仍为 `PARTIAL_IMPLEMENTATION_VALIDATED_PRODUCTION_BLOCKED`。

## 人格与旧状态迁移

MistyAI 新增 `python -m mistyai.integration.migration`。输入为冻结的 `preset-v1` 与 `homeostasis-state-v3` JSON，分别绑定 SHA256；维护进程可读取最大 256 MiB 的旧 preset、2 MiB 的当前状态，普通启动不导入旧 GUI/harness，也不加载整个历史文件。

- 校验角色 ID、旧人格结构、带时区的创建时间；未知人格字段、错误哈希、过大提示词均拒绝。
- `persona-source.json` 保留结构化人格原文、版本与显式空值，`persona.json` 是 MistyAI 当前人格。关系、短期情绪、世界状态和对话不会混入人格提示词。
- `memory-event.json` 是待写入 NeuralCloud 的私有人格版本事件（`persona_history`、pinned）。NC 留存历史版本，MistyAI 持有当前人格。
- 新 deficit 的六项单位与旧维度不同，显式重置为 50，写入报告。旧 psych/world/homeostasis 保留在 `legacy-state.json`；旧冻结输入保留全部原始数据。
- 新状态为 stopped，自主入口默认关闭、独立 judge 调用为零；当前日预算保守耗尽、驱动冷却 24 小时。`legacy-execution-reconciliation` UNKNOWN 活动阻止直接 resume。旧命令、approval、drive、delivery **不重放**。这不是已完成旧执行账本对账的声明。
- 输出最后写入 `manifest.json`，标记 `COMPLETE_ISOLATED`，逐项绑定文件哈希。相同输入可重入；部分输出、已有内容变化、不同输入均拒绝覆盖，留待维护审查。

示例（所有路径均是隔离维护根中的副本）：

```text
python -m mistyai.integration.migration --preset frozen-preset.json --state frozen-homeostasis.json --preset-sha256 <sha256> --state-sha256 <sha256> --character misty --output new-isolated-bundle
```

状态服务配置可采用 `persona_file` 绝对路径和 `persona_file_sha256` 加载已迁移人格；它与内联 `persona` 二选一。仍需现有 operator/role/executor 三种不同凭据及发布门禁，迁移包不会安装或启动服务。

### 实际副本演练

2026-10-25 10:24:34（Asia/Shanghai）只读采集：

| 对象 | 验证结果 |
| --- | --- |
| 当前 preset | 107,626,074 bytes；读取前后内容哈希稳定 |
| 当前内稳态 | SQLite 只读事务读取 `homeostasis_state`，revision 461669、receive cursor 433010；JSON 镜像不作为权威 |
| 冻结状态 / engine state | 112,428 / 273,321 bytes；加密包解密后逐文件校验 |
| 加密包 | 13,249,828 bytes；密钥以 Windows DPAPI 保存在独立的 C: 密钥目录，未进入 Git |
| 人格输出 | revision 1，提示词 1,133 字符；原文保留、哈希加载通过 |
| 新状态 | stopped、六项 deficit=50、直接 resume 被拒绝、动作重放=0 |
| NC 独立克隆 | 从已验证的 39,811 条记录投影通过 SQLite backup API 克隆，加入人格事件后 39,812 条；重复写入返回同一 DURABLE receipt；完整性和外键检查通过 |

私有材料位于 `I:/MistyMaintenance/persona-state-20261025T022436Z`，未进入两个仓库。I: 仍只是维护暂存，不是已验证的独立恢复副本。

旧内稳态账本实际为 27,854,761,984 bytes（约 25.94 GiB）。本轮只冻结其中的当前状态、engine state 及游标，**没有复制整个账本，也没有形成跨服务停写恢复点**。完整旧账本仍保留在原处；其中 11 张表和其他旧执行/inbox/outbox/cursor 仍须在正式迁移窗口完整备份、核对后才能解除 UNKNOWN 门禁。

## 归档维护

NeuralCloud 新增 `python -m neuralcloud.maintenance`，只在显式维护入口运行。它持有现有 MemoryStore 单写者锁，因此不能与另一记忆 writer 同时使用同一个数据库。普通角色 API、启动路径没有新增冷库入口或历史扫描。

维护策略必须提供 `min_free_bytes`、`max_staging_bytes`，可配置等待队列、重试次数和间隔。候选记录由维护者明确提供，不擅自选择保留天数或假设尚未采集的访问频率。每批至多 500 条、明文至多 8 MiB；每次 tick 默认一个任务、最多四个。等待任务至多 32 个，总审计记录至多 256 个；到限停止并要求维护，不滚动删除审计或旧历史。

- enqueue 固定记录版本并检查 pin、活动保护与双向依赖闭包；稳定 UUID 可恢复“批次已建、任务回执未写”的中断。
- export 写入加密文件后提交回执；“文件已落盘、任务回执未写”的中断可重入验证。部分文件或不同密文保留原样并阻断，不覆盖。
- I/O 重试有限额和冷却；阻断后的 retry 需要当前任务哈希及人工核对依据，保留至多八次审计。
- capacity 只读取页计数、数据库/WAL 大小、可复用页、磁盘余量及至多 258 个自有暂存目录项。容量不足暂停任务并返回 `optional_collection_should_pause`；外部调度接入仍属后续部署配置。
- checkpoint 仅显式执行 PASSIVE checkpoint；不自动 VACUUM、不在角色启动时整理全库。
- worker **只导出**。两处独立物理故障域的签名解密恢复证明仍由 `accept_verification` 验证；detach 仍需该门禁；`evict` 仍无条件拒绝。

```text
python -m neuralcloud.maintenance enqueue --database isolated-memory.sqlite3 --character misty --root isolated-archive-staging --policy reviewed-policy.json --request reviewed-records.json
python -m neuralcloud.maintenance tick --database isolated-memory.sqlite3 --character misty --root isolated-archive-staging --policy reviewed-policy.json --key-file protected-archive.key
python -m neuralcloud.maintenance status --database isolated-memory.sqlite3 --character misty --root isolated-archive-staging --policy reviewed-policy.json
```

`reviewed-records.json` 包含 `record_ids` 和稳定 `job_id` UUID。同一维护根绑定数据库 UUID；迁移维护根、策略变更、额外未登记文件都需要显式审查。生产保留策略、调度器和告警未安装，Harness 已轮转的旧会话也没有被删除。

## 临时授权读取

`RestoreGrant.mode` 增加 `temporary_view`；永久恢复明确只接受 `permanent_online`。签名绑定数据库 UUID、角色、目标环境、批次与密文哈希、精确记录集、目的及不超过一小时的时限。生成契约和 TypeScript 声明已同步。

管理代码使用 `TemporaryViews(ArchiveManager(store), protected_root)`，通过 `open` 传入签名授权、归档密钥和 operator 公钥，再按 view ID 与 record ID 分页 `read`；结束必须 `close`。此对象没有注册为普通角色工具或 HTTP 路由。

- 最多八个活动视图，只把授权记录的当前版本写入加密临时副本。临时密钥仅驻留本进程，不持久化，不建立全文索引或明文磁盘缓存。
- 每次 read 重查当前 correction/retraction、来源版本与在线权威，始终保持原记录 `ARCHIVED_OFFLINE`。来源改写后不能继续展示旧解释。
- 默认每五秒清理过期副本；read 本身即时检查到期，不依赖清理周期。单调时钟与持久化时间高水位防止常见时钟回退延长访问。
- 到期、关闭或进程重启先撤销内存密钥与读取权限，再清理生成的文件；文件锁等清理失败记录 `CLEANUP_PENDING`，不会延长读取。启动恢复不重新授权；已消费 grant 不能重用。
- 只清理当前维护根内经 UUID、路径和密文哈希核验的生成副本。归档原包与本地原始 revisions 始终保留。已交付给授权调用者的文本无法撤回；Python 内存/SSD 的物理安全擦除不在此实现的保证范围内。

## 验证及未满足条件

Windows/Python 3.12.13 与独立 Linux/Python 3.12.3、Node 24.15.0 各通过 **75 项**：NC pytest 28、MistyAI pytest 19、NC Node 4、Harness/QQ Node 22、真实本地 TCP 联调 2。新测试包括错误/未知迁移输入、部分输出、停止恢复门禁、授权签名/目标/模式/范围、过期与重启、来源撤回/更正、清理失败、空闲清理、时钟回退、容量暂停、断点恢复和有限重试。Ruff、生成契约检查、构建检查记录见 [validation-20261025.json](validation-20261025.json)。

测试中的归档故障域、QQ sender、模型和内容服务为 synthetic；独立 Linux 验证只上传代码与合成 fixture，私有人格和状态没有写入其测试目录。测试未替代真实独立备份、账号/群、内容能力、完整旧账本对账或生产上线验收。

用户已说明独立恢复目标及隔离真实 QQ 验收配置暂未提供。本轮保留 P5/P6、生产切换和本地淘汰门禁；后续工作应继续在隔离环境完善兼容性、性能、会话副本清单和运维流程。
