# Grounded Agency 全面重构调研

本目录保存从 `codex/grounded-agency` 分出的重构规划，不代表重构代码已经实现或部署。

- [完整调研与实施路线](grounded-agency-rebuild-plan.md)：功能取舍、源码瓶颈、目标架构、数据迁移、性能与恢复验收。
- [审计清单](audit-inventory.json)：冻结提交、仓库规模、已读取文件及其远端 blob SHA。

基线：`835d902ed5601137b504524400cff7c64aeafbab`。新分支：`codex/grounded-agency-rebuild-plan`。

首次落地从计划中的 R0（行为与状态基线）开始；本次提交仅新增调研文档。

## 实时交互与持续表现补充设计（2026-09-29）

以上文字保留首次规划的历史语境，不代表当前分支仍然只有文档。当前分支已包含显式 opt-in 的生产 Runtime、认知与证据记忆实现；是否部署和通过验收应以对应实现、配置及验证记录为准。

- [Misty 实时交互与持续表现架构](realtime-presence-architecture.md)：以本轮 `ee5eb86` 为主要审计基线，分析 NeuralCloud、MistyAI、MistyDigitalBody，包含目的、修改内容、预期呈现、协议、打断与恢复、验收和性能开销评估。
- [生产实现与验证记录](production-validation.md)：已有实验、失败、测量边界和待验收项，不等于实时表现方案已经实现。

实时表现补充方案状态为 **Proposed**；本轮仅新增架构文档和本索引，不切换部署、模型、身体权限或数据库。
