# 常驻 Harness 编排与恢复检查点

> 后续生产状态：本文保留历史检查点；最新授权、真实验收和切换进度见 [06-production-cutover.md](06-production-cutover.md) 与 progress.json。

2026-10-25。用户确认暂不配置独立备份目的地和隔离实机身份，继续隔离实现与测试。没有启用第二个正常 Misty，没有安装 systemd 模板、调用生产模型或发送真实 QQ 消息。方案整体仍为部分实现，P5/P6 继续受门禁约束。

## 本轮代码

- NeuralCloud：`a7e287a0dddbe92ae6e4eb1b85907c19ab69cd98`。outbox 增加有界状态/故障查询与绑定事件哈希的维护重试；保留原事件和消费游标，不清空堵塞队列。
- MistyAI：`b14840cbb2f0aabad6639d57959318c5e76123c0`。新增 `compatibility/harness/resident/{composition,runtime,launch}.mjs`，以及部署配置和服务模板；实际生产 run 仍未执行。

常驻组合使用固定的 Cordis/AgentLoop/JSONL API，进程锁由 SQLite OS 锁持有；新建、恢复、后台队列、轮换和停止由同一登记层协调，不重写通用 Agent Loop。配置只接受有限 QQ 目的地、独立状态凭据、loopback 领域服务和 HTTPS 模型入口。运行前核对 lock、补丁和插件哈希、配置绑定及旧 writer 门禁。

QQ 当前原文在首次会话绑定前进入有界 ingress，即使状态服务离线也保留。尚未分配给 Agent 的输入恢复后可以续接；一旦绑定，只有持久日志中的相同 ID/正文能够确认接收，不凭进程重启自动重放。v3 partial indexes 避免对全部历史去重记录反复扫描，v2 只能通过显式维护命令升级。旧版自动空闲取消和“任何恢复失败都新建”的路径已经在生产组合中接管；非托管恢复也只对明确 NotFound 允许新建。

后台按有界批次交接和补读；outbox 五次失败后停止自动尝试，维护重试先写审计且不能与主进程并发。状态文件只报告元信息，不等于已主动通知用户。已绑定但缺少持久日志的输入仍要求人工核查，未增加自动重放或删除命令。

独立自主会话只消费一个已授权机会，等待真实显式结算；idle 不会完成 drive，也不会重复触发模型。上下文包含活动、drive、发送欠账和状态游标。正常会话达到阈值后，须无待处理输入/活动/未知副作用且记忆交接确认，才关闭并切到新会话。旧日志保留。过大、损坏、欠账或无法结算的会话进入维护；没有声称无限运行或自动冷剥离已经成立。

停止时先关闭输入，再取消当前模型调用并保留待处理 inbox，经过安全停点和持久化 barrier 后退出。不能调用会清空 pending inbox 的上游全量 dispose 代替检查点。步骤被停止/上限拒绝时，已 claim 的输入会停放，而不是无声消失。

## 模型与验证范围

只读现网配置核实主模型入口为 `https://api.deepseek.com/v1`、`deepseek-flash`。使用现成 `@deepseek-ai/dsh-llm-pi-ai@0.2.1-rc.3`，锁文件固定其传递依赖。实际适配器对本地 synthetic SSE 端点完成协议测试，启动解析模型时外部调用为零；验证了凭据没有进入 prompt。这里没有声称生产模型账号、额度、实际推理质量或 provider 最新能力已经验收。未恢复辅助评判模型。

Windows 与 Linux 各 **54 项通过**：NeuralCloud Python 15、MistyAI Python 11、NeuralCloud Node 4、Harness/QQ/launcher Node 22、真实 TCP 服务联调 2。包含进程退出后的 pending inbox 恢复、首个输入遇状态故障、重复输入不重复唤醒、会话安全轮换、UNKNOWN 保留、partial-index 迁移和实际适配器的本地协议测试。

Linux 在非 root 隔离目录运行 Python 3.12.3 和独立 Node 24.15.0，最终源码包 SHA256：`6987af26e5121875913b2f00bfba19a4c230738b6d4368a0b5e44fd28db3ea61`。Ruff 与契约漂移检查通过。MistyAI 新 wheel 的 8 个 Python 模块逐字节匹配源文件并通过编译，SHA256：`397774295c23e6749024b187e1ece80e6cd3bb3b8f7bf8277dd24949bd7f0668`。

跨仓库 CI 已改为获取明确配对的 NeuralCloud commit，再跑 Node 和 TCP 联调；这只是工作流代码，尚未推送或运行 GitHub Actions。发布顺序和启动命令见 `deploy/resident/README.md`。模板的 systemd 内存预算也尚未经过真实长期运行验收。

## 保留状态与后续

复核旧 cognitive/homeostasis/runtime/gui 与 mobile MCP/emulator 的 PID 和启动时间均未变，旧身体仍 inactive。原 D: 两工作区保留原有 58/154 条状态记录；隔离 worktree 的变更已分别提交，本轮不清理原目录。

剩余独立实现/验收包括：人格导入与旧状态重置报告、临时授权冷读的过期清理、容量/归档维护编排、已绑定不确定输入的操作员核查流程，以及对已审计浏览器/Android/MCP 能力和认证审批通知的接线。完整旧/新 Linux 启动分段对比仍未完成。

真实渠道、内容、模型和系统服务验收仍须单独执行。独立恢复副本、密钥异机恢复、停写后的跨服务恢复点和回滚增量演练尚缺；H:/I: 属于同一物理设备。没有启用生产切换或真实冷剥离，也没有留下后台自动切换任务。

后续已完成的人格、旧状态与归档维护隔离检查点见 [05-persona-state-archive.md](05-persona-state-archive.md)。生产切换和本地淘汰仍受门禁约束。
