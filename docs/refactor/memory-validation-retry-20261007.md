# 记忆提案校验恢复热修复（2026-10-07）

## 诊断

两次 `subject_refs` enum 拒绝发生于 UTC 04:48:05、04:51:41，均已按完整冻结来源集合关联到任务；两项任务均在 attempt 2 成功 applied。它们不属于字段缺失，身份枚举约束工作正常。

另一个 8 来源任务于 UTC 04:11:20 以 `ValueError`、attempt 1 跳过，未保存 proposal。用同一冻结来源与当前上下文受限复现，第一次提案通过 schema，但因 `impression needs new non-self, non-duplicate, non-quoted evidence` 被业务校验拒绝；模型收到反馈后第二次提案通过。原失败响应没有保留，因此这是同一任务的可复现风险，不证明历史失败恰好由同一条件触发。

源码证实两个恢复问题：schema 拒绝发生在业务反馈循环之前；业务校验连续失败时透传 ValueError，而队列将 ValueError 视为不可恢复，直接跳过。前者缺少纠正反馈，后者混淆可重试模型输出与不可恢复数据损坏。

## 修复边界

- 仅对 `deepseek_structured_schema_invalid` 在现有两次生成循环内给予一次安全字段诊断反馈；不扩大身份或来源枚举，不接纳被拒绝结果。
- 连续业务提案拒绝改为专用可重试异常，仍受原任务 8 次尝试、每次最多两次生成及共同 deadline 约束。已识别的独立证据、身份归属、来源分区错误记录固定分类，其他原因只记录通用安全代码。
- 原始记录、存储损坏及其他系统错误处理不变。仅恢复本次已复现、当前 observe 授权且接收开放的目标任务，保留其已有尝试次数与冻结来源。
- 114 项针对性及回归测试通过，Ruff 和 diff 检查通过。模型探针不提交生成记忆、不发送群测试消息，实际阅读正常记账。

## 部署

已部署 `20261007-memory-validation-retry-v1`（`e2aa56d`）。六个服务 active，Runtime RUNNING，QQ 已连接，watchdog 无告警。目标任务以保留原尝试次数的 attempt 2 成功 applied，存在提交 receipt，待处理/重试队列清空。上线后截至本次核查有 2 次后台记忆生成成功，无新结构化错误。

停写备份完整校验通过，配置和群策略保持一致。profile @3、manifest revision 4、shadow 保持不变。服务器采样推得新连续窗口开始于北京时间 **2026-10-07 13:12:52**，无新断档时最早 **2026-10-08 13:12:52** 满 24 小时。

[完整脱敏证据](../harness/evidence/memory-validation-retry-20261007/deployment.json)。
