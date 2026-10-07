# 共享记忆锁竞争热修复

生产检查确认共享数据库为 SQLite DELETE journal mode、约 152 MB。当写提交请求排他锁时，UI 的只读身份检查也会等待并超过既有 5 秒 timeout。日志在 UTC 12:50 之后的检查范围内包含 28 次 database is locked，堆栈到 shared.py 的 _check_preset SELECT；后台记忆队列曾报告 OperationalError，12 个 pending、最早约 13 分钟。不能仅凭这些栈确定每次竞争的写入者。

代码另确认队列 enqueue 在 BEGIN IMMEDIATE 后读取并解析全部 8405 条来源分配、计算重复与窗口，造成不必要的长写锁。修复启用 WAL，保留默认 FULL 持久性及现有 busy timeout；将历史解析、重复比较与窗口计算移到写事务之外，提交前重新核对来源 ID/哈希集合，若并发分配已变化则拒绝旧计算并由下一轮重新计算。任务插入和来源分配仍在同一事务中。

200 项回归测试通过、1 项跳过，包含写事务未提交时只读可用、读快照持有时 writer 可提交、窗口计算不持写锁以及并发分配不重复的验证；Ruff 和 diff 检查通过。已有业务与身份校验保持执行。

部署先停止 writer、生成并校验完整备份，再改为 WAL，验证 quick_check 和三张表记录数保持一致。不重排任务、不清零尝试次数；保存既有群策略与用户控制。

已部署 `20261007-shared-memory-wal-v1`（`de346c8`）。服务正常、Runtime RUNNING、QQ 已连接、watchdog 无告警，数据库 quick_check=ok。启动后核查的身份读取耗时 [0.011, 0.004, 0.003] ms；从部署完成起检查到的锁错误数为 0。后台队列状态见证据，短时检查不能证明未来无锁竞争。

本次维护后的连续 shadow 窗口为北京时间 **2026-10-07 21:55:35**，无后续断档时最早 **2026-10-08 21:55:35** 满 24 小时。

[脱敏部署证据](../harness/evidence/shared-memory-lock-hotfix-20261007/deployment.json)。

完整 UI 快照检查有一次 200（约 10.9 秒）及两次 15 秒超时；身份 SELECT 本身已快，历史全量解码仍有性能限制，不能把 WAL 修复视作 UI 延迟完全恢复。

队列在本次检查内从 33 pending + 1 retry、2442 applied，推进到 28 pending + 0 retry、2453 applied（期间新进入 5 个任务）。skipped 保持上线时基线 615，最近错误为空。仍有历史积压，尚未清空。
