# QQ 有界历史默认启用 · 2026-10-06

## 上线结果

- 版本：`20261006-bounded-history-v1`；前一版：`20261005-memory-prepare-v1`。
- 北京时间 2026-10-06 00:22:18 切换，2026-10-06 00:25:00 完成复核；六个服务 active/running，Runtime RUNNING，QQ connected。
- 唯一配置变化：`/etc/neuralcloud/gui-settings.json` 中 `qq_latency.qq_history_selection` 从 `shadow` 改为 `bounded`。
- Cognitive 运行快照已广告 bounded，712342021 群经过名单外策略派生后仍为 bounded。
- 线上记忆准备保持现状；`harness/cognitive/shared_memory.py`、`harness/memory/shared.py`、`harness/cognitive/service.py` 哈希与上一版一致。发布清单之外全部文件沿用上一版。

## 行为

`QQLatencyOptions.qq_history_selection` 默认 bounded。`for_conversation` 不再把名单外会话强制改成 shadow，其他优化仍受原名单控制。显式 legacy/shadow 可用于兼容诊断；未设置的新配置使用 bounded，已有显式配置需单独迁移。

判断历史软目标仍为 24,000 字符，正文为 64,000 字符，总输入预算未变。当前触发、补充及已验证引用保持完整，省略的历史不计为已呈现。

默认启用回归同时修复两处选择器问题：软目标按去除私有证明附件后的实际模型历史计算，避免附件过大挤掉对话；正文到判断的二次选择保留已省略回执/旧决策元数据，并继续计入硬预算。

## 验证

本地与 Linux 候选版各 259 项通过，2 条既有告警；Ruff 和差异空白检查通过。覆盖群聊/私聊名单外策略、旧持久化配置、显式回退策略、111 条未处理历史对照、当前消息/引用保留、硬预算、回执证明、冻结与恢复、共享认知等。

111 条积压历史的合成回归在旧 shadow 下复现超预算且未进入 gate；默认 bounded 下在相同 120,000 字符预算内进入 gate，仅把实际呈现的历史计入 seen/new。该测试使用模拟模型，没有向群内发送测试消息。

本次没有修改固定故障提示的产品规则，也没有修改其他异常路径的 seen 语义。必需内容本身超过硬预算时仍可能失败，bounded 不保证任意大小输入都可处理。线上自然消息数据见部署核验 JSON；没有样本时不声明真实回复质量已验证。

## 发布与回退

以当前线上完整清单为基线，只更新 10 个明确文件（4 个运行代码、5 个测试、1 个文档），未整体部署工作区其他未提交修改。421 个运行文件、1,418 个源文件清单校验通过。标准排空/停止/启动流程更新各服务工作目录。

原配置与部署标记备份：`/var/backups/neuralcloud/bounded-history-20261006`。回退应排空服务，恢复该目录中的 GUI 配置，切回 `20261005-memory-prepare-v1` 后核验服务及 QQ 连接。无需回退业务数据库；本次没有存储迁移。

证据：[部署 JSON](../../eval/reports/qq-group-712342021-20261006/bounded-history-deployment.json)、[异常诊断报告](../../eval/reports/qq-group-712342021-20261006/report.html)。
