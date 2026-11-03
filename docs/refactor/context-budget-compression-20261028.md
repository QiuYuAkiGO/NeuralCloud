# 按上下文预算自动压缩：部署记录

日期：2026-10-28（北京时间）。已部署到远端 Linux 的 `resident-20261028-context-budget-v1`。

## 行为

- 默认阈值为 `(context_window - max_output_tokens) × 70%`。当前窗口 131072、输出保留 4096，阈值 88883 token。
- 使用适配器返回的 `totalTokens` 统计已用上下文，包含缓存输入和已生成内容；不把缓存命中误当成无需上下文预算。对于尚无 usage 的新增文本按 UTF-8 字节保守估算，图片按每张 16384 token 估算。这是运行保护阈值，不是精确 tokenizer。
- 在定时调度及新消息交付前检查预算；上次模型以 `max-tokens` 结束时也触发恢复。原有 512 事件上限继续有效。
- 等当前轮执行完、收集持久化日志并确认活动和回执已结算后，冻结旧会话并后台生成摘要。轮内正在执行的模型请求不会被该机制自动截断；单个超大输入与高流量长轮次仍需单独的逐请求预算控制。
- 摘要期间该会话的新输入持久保留；其他会话继续运行。后台摘要并发上限为 1。
- 每个冻结会话最多尝试摘要 3 次，包含重启前已经记录的尝试。失败停止自动调用并进入 runtime 数据库的 `exception_memories` 表，状态可见于 `status.json` 的 `compression` 字段。
- 成功后以摘要和结构化历史事实建立新会话，保留旧日志；不会自动重放已经接受的历史群消息或发送测试消息。

## 验证结果

部署门禁通过，Harness 全部 58 项测试通过。新增测试覆盖缓存输入计数、输出预留、低于事件数上限时按预算压缩、后台调度、重启继承及 3 次失败后归档。

生产群会话于 09:51:43 因约 262863 token 触发压缩，09:51:56 生成摘要，09:51:58 完成切换。之后主状态为 READY，压缩中任务、等待切换会话和异常数均为 0。尚未通过新群消息实测新的发送回执，不将会话切换成功等同于真实回复已送达。

## 证据与源码

- [部署回执](../harness/evidence/context-budget-20261028/deployment.json)
- [相对于实际远端基线的源码补丁](../harness/evidence/context-budget-20261028/source.patch)

补丁路径以两仓库共同父目录为根，目标为 MistyAI 下的 Harness；本地 MistyAI 主工作区存在无关改动且缺少该部署目录，因此未覆盖它。实际部署从远端运行基线复制为新发布目录，再替换补丁文件，未原地修改旧发布代码。NeuralCloud 记忆插件和配置继续使用此前已校验版本。

远端完整测试日志：`/tmp/misty-context-budget-tests-20261028.log`。数据库备份：`/var/lib/misty/backups/context-budget-20261028/`。

## 回滚

停止 `misty-harness.service` 后，将 `/etc/systemd/system/misty-harness.service.d/50-context-budget.conf` 移到备份目录，再执行 `systemctl daemon-reload` 并启动服务，恢复此前 `40-memory-workflow.conf` 指定的发布。新数据库表和审计记录可保留，旧代码忽略它们；会话 handoff 格式保持兼容，不应把备份数据库覆盖回去而丢失上线后收到的消息。回滚前检查旧代码是否会再次接近上下文预算。
