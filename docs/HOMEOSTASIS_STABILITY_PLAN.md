# NeuralCloud 资源与内稳态稳定化方案

## 结论与目标

现场诊断显示，系统的首要问题不是瞬时 CPU 或容器内存耗尽，而是高频重复感知驱动的状态积分、边界外耦合和全量账本重写形成了正反馈：七维状态在数分钟内贴住硬边界，同时 Homeostasis 的 CPU、常驻内存和磁盘写入随历史量增长。

本方案的目标是：

1. 静态环境不再重复改变逻辑内稳态。
2. 维度已经触及硬边界时，不再通过“未实际发生的位移”影响其他维度。
3. 动作结果以已验证证据为准，成功动作不再被当作失败。
4. 持续贴边能够被 watchdog 发现，但瞬时触边不会立即停机。
5. Homeostasis 的幂等历史不再全量常驻，也不再每事件重写完整 JSON。
6. 保留旧实验的重放语义，并为新算法显式提升配置版本。

## 本分支落地范围

### 1. 感知变化门控

`PerceptionGuard` 继续以较高频率检查桌面，以保持响应速度；只有语义指纹变化时才提交 `PERCEPTION` 事件。

指纹忽略以下采集噪声：

- `observation_id`
- `captured_at` / `observed_at` / `created_at`
- `freshness_seconds`
- 已有内容摘要时不断变化的 evidence `ref`

屏幕 digest、窗口、a11y、文件事件、运行应用、指针与后端健康等变化仍会触发新事件。感知异常会清空指纹，因此恢复后的第一帧一定会重新提交。

超过 manifest `max_state_age_seconds` 的陈旧帧不会进入 Homeostasis；Runtime 只在“进入陈旧”和“恢复新鲜”两个状态切换点各记录一次诊断，避免陈旧状态被无限重复积分。没有 digest 的 evidence 仍保留 `ref` 参与指纹，防止把不可证明同内容的帧误判为重复。

### 2. 两阶段耦合积分

新配置 `coupling_semantics=realized_delta_v2` 使用两阶段计算：

1. 将漂移和事件影响应用到源维度，并按硬边界裁剪。
2. 用源维度的实际位移计算耦合。
3. 将耦合应用到目标维度并再次裁剪。
4. trace 记录实际生效的耦合，而不是未裁剪请求值。

旧配置缺少该字段时继续采用 `requested_delta_v1`，从而保留历史实验的重放语义。默认功能代理 profile 已提升为 revision 2，coupling 版本为 `coupling-v2-realized-delta`。

### 3. 动作结果判定

`ApplyOutcome` 的成功判定顺序为：

1. 兼容旧协议的平铺 `success`。
2. `evidence.success`。
3. `action.outcome.success`。
4. `action.state`。

显式 `false` 不再被布尔 `or` 吞掉；Runtime 当前生成的 `ActionOutcomeEvidence` 能被正确评价。

### 4. 持续边界 watchdog

新 profile 参数 `watchdog_boundary_heartbeats` 默认为关闭，revision 2 profile 设置为 3。维度只有连续三个 heartbeat 前都处于硬边界时才报告 `<dimension>:hard_bound_sustained`。普通事件可以把维度移回区间并清零计数。

这避免“一次触边立即停机”，同时修复原先只检查 posterior 极端概率、无法识别真实硬边界饱和的问题。触发后的结构化 watchdog 决策会进入 Runtime 控制队列，由既有控制路径执行 `pause_preserve_state`；同一组原因在恢复前只发送一次，避免控制事件风暴。

### 5. Homeostasis 事务化状态存储

新增 `command-ledger.sqlite3`：

- `command_results` 保存幂等键、请求哈希和结果。
- `homeostasis_state` 保存权威快照。
- `transition_traces` 保存按 revision 唯一的权威审计 trace。
- 每次命令在同一个 SQLite 事务中提交结果、快照、内部 watchdog 状态和 trace。
- 幂等键一旦写入不可覆盖，快照 revision 只允许单调前进；并发冲突会失败关闭。
- SQLite 提交成功后才更新进程内 snapshot，写入失败时同一命令可以安全重试。
- watchdog 连续计数和边界方向与快照一并事务化保存，但不改变公开的 `homeostasis-state-v1` schema。
- 进程内只保留最近 512 个结果的 LRU；旧键按需查询 SQLite。
- 旧 SQLite 结果的 trace 回填采用流式游标且只执行一次，启动内存峰值不会随历史整体装载而线性增长。
- `snapshot.json` 继续生成，作为兼容镜像；启动优先读取 SQLite 权威状态。
- `snapshot.json` 与 `transitions.jsonl` 是兼容投影；投影失败会让 health 降级，但不会把已经提交的命令伪报为失败。下一次提交或重启会依据 SQLite 自动补齐 trace 缺口并重建镜像，只有全量一致后才清除降级状态。
- 首次启动会事务化导入并校验旧 `command-ledger.json` 与 `snapshot.json`；结果 revision 不得领先权威快照，结果内嵌 snapshot/trace 必须自洽，可用 trace 会同步回填权威表。损坏数据不会被标记为迁移完成。原文件保留供回滚和取证，不自动删除。

该变更把单事件持久化从“重写完整历史”降为常数级 upsert，消除累计 O(N²) 写放大。

### 6. 快照引用修正

普通事件提交后的 `stable_snapshot_ref` 现在引用新 revision，而不是前一个 revision。

### 7. 启动契约校验

当 Homeostasis 返回实际 profile 时，Runtime 启动会逐项核对 profile ref、persona、A/B/C、precision 与 coupling 版本。任何 manifest/profile 漂移都会在处理事件前失败，避免“配置标记为 v2、实际仍运行 v1 算法”的不可重放状态。

## 明确不在本分支自动执行的操作

Runtime hash-chain journal 仍是恢复事实源。本分支不会自动删除或截断 journal 分段，因为 Supervisor 的未完成事件、命令、动作恢复和 Monitor 回填都依赖完整历史。

后续阶段应将 RuntimeJournal 改为：流式校验与恢复、有界内存 tail、按需反向查询；磁盘删除需要 checkpoint anchor、保留期限和审计策略共同确定，不能只给 `_records` 截尾。

## 发布与迁移步骤

1. 停止现有服务，保留当前 `data/homeostasis` 与 `data/runtime` 作为诊断证据。
2. 不要把已经饱和的旧状态当作新实验基线；为正式验证使用新的 state 目录，或明确执行一次受控 profile migration。
3. 使用 revision 2 profile 与 revision 2 runtime manifest 启动。
4. 确认首次迁移生成 `command-ledger.sqlite3`，旧 JSON 未被修改。
5. 先执行 30 分钟静态桌面 soak，再执行包含窗口切换、动作成功/失败和感知中断恢复的动态 soak。

## 验收门槛

- 同 revision、同屏幕 digest、仅时间戳/ID 变化的 1,000 次采样只产生 1 个感知事件。
- 源维度贴住硬边界后，重复同向刺激产生 0 实际位移和 0 耦合。
- 成功动作提升 competence/safety，失败动作降低它们。
- 连续三个 heartbeat 贴边后 health 变为 unhealthy，Runtime 通过控制事件暂停；恢复到区间后原因自动清除。
- LRU 条目数不超过 512，被淘汰命令在重启后仍可幂等重放。
- 静态 30 分钟内七维状态不再单调漂向硬边界。
- Homeostasis 平均 CPU 不再接近一个满核，稳态写入不再出现 47–49 MB/s 的全量重写。
- Python Working Set 和持久化文件增长率在静态窗口内进入近似平台，而不是随历史长度加速。

## 测试矩阵

- 单元测试：耦合裁剪、嵌套 outcome、持续边界 watchdog、Runtime 暂停路由、SQLite 失败注入/迁移/LRU/不可覆盖约束、感知指纹与陈旧帧。
- 回归测试：Runtime Supervisor、Homeostasis API、body-state、monitoring、grounded embodiment。
- 发布前：全量 pytest、Ruff、30 分钟静态 soak。
