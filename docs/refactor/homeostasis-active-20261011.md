# 内稳态 v3 active 验收与发布（2026-10-11）

用户确认 GUI 并非刻意关闭，授权恢复并继续验收。GUI 单独启动后健康接口可用，Runtime 保持 RUNNING。此次基于实际 `20261009-json-repair-v2` 做配置晋级，保留其 JSON 修复及其他已授权修改；不回退到旧诊断版本。

## 晋级前证据

真实快照于 UTC 10:15:33 通过 `shadow_report`：连续 137632.607137 秒，最后采样 UTC 10:15:30.985474，8762 条真实加工记录，817 条附带可选评价，7945 条无评价，工作量 82265867。累计最大间隔是历史统计，晋级依据为已重置后的连续时长及新鲜采样，不将远程 DNS 中断直接计为服务器离线。固定 224 条消息、其中 207 条只读群消息的隔离回放通过，不重放到线上。相关晋级与群关闭策略回归 22 项通过。

本地测试首次收集因缺少锁定依赖 json-repair 而失败，按 uv.lock 安装 0.63.5 后全部通过。此次未修改项目代码或依赖锁。

基础 manifest revision 5 在启动时叠加 Web、Server 配置修订，实际运行 revision 11；这是配置分层的正常结果。晋级必须保留这些配置，不以有效 revision 覆盖基础文件。

后台记忆累计 skipped 624，较此前 619 新增五个达到 8 次上限的 qq_model_validation_invalid_proposal 任务，分别于 UTC Oct8 15:42、16:04、16:23、16:43 与 Oct9 05:28 终止。保留审计，不清零、重排或宣称修复。晋级前队列空、数据库 WAL/quick_check 正常、锁错误 0。分阶段可选评价主要来自 qq_gate；后台记忆无评价不等同任务失败，goal_decision 等阶段的缺失仍作为校准限制保留。

## 部署保护

只通过 rollout promote 生成 profile 新 revision 和公平输入预算配置，manifest 同步绑定，注意力白名单只增加 communication.qq.attention.set.v1。该能力的 act 是模型选择种类；群授权入口由 attention_catalog 的 group.observe 检查控制，与 send 权限独立。

首次预检因快照响应包含额外 profile 元数据而停止，剔除响应元数据后用原始快照字段重跑门槛。停写后的第一轮备份发现其他研究任务的符号链接，原脚本拒绝继续；修正为保留链接并校验目标文本，不沿链接重复复制。两次保护停止均未提前应用 active 配置。

最终备份、配置和自然流量验收以 docs/harness/evidence/homeostasis-active-20261011/ 内 deployment.json、acceptance.json、natural.json 为准。任何历史失败任务均未重放；未主动发送群测试消息。

## 配置与备份结果

停写备份路径：`/var/backups/neuralcloud/20261011-before-homeostasis-active`。65,484 个普通文件逐项 hash 校验、7 个符号链接目标校验、2,902 个 SQLite quick_check 通过。归档 12,755,612,463 字节，SHA-256：`6cf735dd4ebc0afe5e2fbd1d0da1d58c444d9fba0c569f52f13439a56e9b9838`。归档后源文件 hash 不变。包含 WAL；恢复须停全部 writer 并匹配恢复完整文件集，不能覆盖运行中的数据库。

配置晋级为 `qq-social-drive-v1@4` active，基础 manifest 6，启用 Direct 公平输入预算。保留现有代码 release，未修改模型解析、调用重试、输出预算或模型选择。注意力数据库在停写备份与启动前 hash 相同。数据库未回滚，历史任务尝试数保留。

如需回滚数学规则，应基于当前配置生成更高 revision 的 mode=off，并同步 manifest；保持注意力数据库、群关闭策略和用户覆盖，不恢复旧状态快照，不回退到不认识关闭策略的代码。


## 上线健康通过，自然流量验收待完成

北京时间 18:35 的复查确认六服务 active、GUI 可用、Runtime RUNNING、QQ 已连接、无降级原因和 watchdog。独立被动推进器工作，profile revision 4 / active，基础 manifest 6、有效 manifest 12，公平输入预算已启用。Direct 经过模型默认值归一化后，除 attention_budget_enabled 外其他设置完全一致。三个群注意力策略与晋级前相同；本次快照均为 open/default/revision 0，并非声称当前存在关闭中的群。

Runtime 冷启动日志恢复约六分钟，期间未另行重启；启动初期反馈标志随后自然消失。后台记忆 applied 3801 / skipped 624，队列为空，真实工作 8762 项全部 applied。

自然观测自 UTC 10:33 起，截至 natural.json 的 checked_at 尚无模型请求样本，因此只确认配置上线和健康验收通过，**自然流量验收仍未完成**。不把空窗口视为零错误成功样本，也不通过主动群消息补样。v3 跟进已更新为 active 后的自然流量验收，保留当前 release 和群策略，不重复晋级或等待新的 shadow 24 小时。待取得真实自然工作证据并完成最终记录后，才停止跟进。
