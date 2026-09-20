# Runtime journal 增量恢复

实现日期：2026-09-20。默认保持 `strict`；本文不表示已经升级或重启生产 Misty。

## 行为与配置

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `NC_RUNTIME_RECOVERY_MODE` | `strict` | `strict` 从头校验并归约；`fast` 验证快照锚点后只归约尾部，无有效缓存则回退 strict |
| `NC_JOURNAL_AUDIT_BYTES_PER_SECOND` | `16777216` | 后台只读审计的读取速率上限，字节/秒 |
| `NC_JOURNAL_AUDIT_DUTY_CYCLE` | `0.1` | 审计扫描批次的近似忙碌比例，范围 `(0, 1]` |

审计结果只进入 `journal-audit.json`、服务日志及 `RuntimeStatus.recovery.audit`。发现损坏、I/O 错误或诊断文件写入失败，均不暂停本体、不改变健康检查或执行权限、不禁用快照、不触发重启，也不设置阻止下一次启动的标记。临时 I/O 错误按 30 秒、2 分钟、10 分钟退避重试。fast 启动完成后开始审计；strict 已完成前台全量验证，首轮后台审计延后 24 小时。后续轮次间隔 24 小时，不重叠执行。

启动和前台历史查询仍校验它们实际读取的记录；这些操作直接遇到 journal 损坏时，按正常错误规则处理。审计报告不会调用该控制路径。

## 存储与恢复

`journal/runtime-*.jsonl` 保持原格式和事实源地位，不删除或重写历史。新增 `recovery.sqlite3` 是派生缓存，使用 WAL 和 `synchronous=FULL`。同一事务发布恢复状态、历史定位索引、待处理工作及其游标；提交前先 flush journal，emergency buffer 中的记录不能进入恢复锚点。

恢复投影在运行期间按顺序追平，达到 1,000 条新记录或 30 秒时提交，正常关闭时再提交。前台使用同一个串行化数据库连接更新派生索引，周期提交的 fsync 在后台执行。快照归约 journal 的固定前缀，不序列化正在变化的 Supervisor 对象。

快照绑定角色、manifest 内容 hash、恢复格式版本、journal 首条记录身份，以及锚点记录的序号、hash、文件和字节位置。小型状态快照另有 checksum。历史幂等键、命令结果、action 和稀疏定位元数据按需查询，不整体装入内存；journal payload 尾部仍有界。

完整恢复和增量恢复共用 `RecoveryProjector`。action 与事件恢复使用索引，不逐 action 搜索整个 journal，也不逐事件遍历全部完成区间。UNKNOWN action 仍进入核对流程；已完成 action 可以按需查询而不抬高运行版本号。新 action 日志携带 `_runtime_action_revision`，用于恢复版本下限。

v1 `checkpoint.json` 保留兼容用途。完整回放仅在经过已核对 hash 的对应游标时应用其状态，然后继续回放后续事实；恢复末尾不再以旧快照覆盖新的 body/agency 状态。

Goal contributions、opaque sensor、body belief 使用各自数据库中的消费游标，并核对对应 journal hash。模块数据库丢失或回退时重建对应投影，不错误沿用另一份数据库的高水位。研究决定和 agency binding 从 journal 引用延迟读取。外部服务仍以自己的持久化状态为准，不构造跨服务原子快照。

恢复缓存缺失、版本不兼容或锚点不符会回退完整恢复。无法打开的损坏缓存保留为 `.corrupt-*` 后重建；派生写入失败时可以退回内存投影，状态明确报告 `persistent=false` 和落后量，不把内存进度冒充持久化快照。原 journal 不因此删除。

监控数据库独立保存 journal 身份、序号和 hash。监控事件写入与消费游标同事务提交，正常重启只回填增量，首次历史及 trace 回填在后台运行，不阻塞业务 ready。

`RuntimeStatus.recovery` 提供模式、fallback 原因、快照游标和年龄、缓存落后量、待处理数量、回放条数、启动阶段耗时、审计进度及监控 lag。这些诊断字段不改变健康判断。

## 本机验证与基准

验证覆盖 journal 分段、截断尾部、hash/缓存损坏、不同快照切点、真实子进程退出、未落盘 emergency buffer、旧 checkpoint 后的 terminal 状态、UNKNOWN action、action revision、监控游标事务及审计无控制副作用。

本次验证结果：默认模式定向回归 **487 passed**，额外以 `NC_RUNTIME_RECOVERY_MODE=fast` 运行 Supervisor、身体状态、研究恢复及 agency 回归 **82 passed**；加入离线验证工具后的恢复与监控测试 **25 passed**。这些集合存在重叠，不累加为独立用例总数。修改文件的 Ruff 检查通过。

本地现有 journal 的隔离对比：235 条记录，快照游标 135，增量 100 条；归约状态、KV、定位、事件、区间、action 和命令投影全部相等。此结果不代表生产实例的数据规模。

在 Windows / Python 3.12 的本机合成数据上，每 20 条包含事件接收、处理开始、action 提议、action 完成、事件完成及普通记录。快照后的增量固定为 1,000 条，未完成工作为 0。每档 fast 重复五次；OS 文件缓存未清空，测试期间存在其他本地回归负载。

| 历史条数 | journal 字节数，含增量 | strict 一次，秒 | fast 五次中位数，秒 | fast 回放条数 |
| --- | ---: | ---: | ---: | ---: |
| 10,000 | 7,385,210 | 0.298 | 0.01939 | 1,000 |
| 100,000 | 69,598,711 | 2.491 | 0.02001 | 1,000 |
| 1,000,000 | 693,524,712 | 24.765 | 0.02007 | 1,000 |

计时范围是 journal 打开、恢复投影及待处理索引查询，**不包含服务 RPC、外部 action 核对或完整应用启动**。没有清理操作系统缓存，不能据此声称磁盘冷启动耗时。10 万至 100 万条历史的 fast 中位数比约 1.003，满足本机该负载下不超过两倍的目标；常驻 journal payload 不超过 512 条。

原始结果见 [基准 JSON](evidence/journal-recovery-benchmark-2026-09-20.json)。复现命令：

```text
python -m tools.benchmark_runtime_recovery --output .tmp/recovery-benchmark.json
```

## 生产启用与回退

1. 在维护前取得一致的生产 journal 副本及匹配 manifest。不要把活动 SQLite 数据库只复制主文件而遗漏 WAL；需要备份其他模块数据库时，使用 SQLite backup 或正常停止后复制。
2. 对副本执行以下离线验证工具。工具再次复制到临时目录，只比较派生恢复结果，不构造服务客户端，不执行消息发送、推理或身体动作；即使输入有半条尾记录，也只修复临时副本。

   ```text
   python -m tools.verify_runtime_recovery --journal <journal-copy> --manifest <matching-manifest.json> --tail 1000
   ```

3. 在隔离环境使用真实模块数据库副本和替代外部执行后端检查完整启动，记录服务核对耗时及未完成工作的恢复结果。离线投影相等不能替代这一步。
4. 验证通过后，在既有部署环境文件中设置 `NC_RUNTIME_RECOVERY_MODE=fast`，在维护窗口正常重启。首次没有缓存仍会完整恢复，后续重启使用已提交锚点。检查 `recovery.mode`、`fallback_reason`、各阶段耗时和后台审计结果。
5. 回退只需设置 `NC_RUNTIME_RECOVERY_MODE=strict` 后正常重启；保留 journal 和模块数据库，恢复缓存可重建。后台审计结果供人工判断，不会自动触发回退。

本次代码交付没有修改生产环境文件或重启服务。生产副本验收、磁盘冷缓存测量和维护窗口启用仍需在目标环境执行。grounded research 等模块自身数据库的既有构造/完整性检查未被跳过，因此 journal 的增量复杂度不等于所有服务都具有恒定启动时间。
