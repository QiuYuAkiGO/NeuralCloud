# BR-1 执行记录

> 持续更新。日期：2026-09-09；工作区 `codex/grounded-agency@3f38b10` **加未提交修改**。外仓 MistyAI `26d4f0a` 加本轮修改。此前已有 GA runner/agency/social 等修改被保留，本记录不把它们全部归为本轮新增。

## 本轮判定

**BR-1 尚未通过。** 工程回归、实际模型、真实 Linux 和真实 8 小时分别记账。

| 范围 | 当前证据 | 判定 |
| --- | --- | --- |
| 内稳态机制 | 480 次 60 秒心跳加速覆盖、扰动与重启、高频无变化/仅能量事件、耗尽保护。 | 工程通过；实际 8 小时未运行。 |
| journal / 命令查找 | 有界尾部、分段旧游标、重启校验、损坏/写失败、幂等命令索引。 | 工程通过；长期 RSS/磁盘增长待实测。 |
| 终端规划与 Runtime | 聊天到 GUI 多步模拟链路、实际证据限定、重复批准/中断/等待/目标变化。 | 工程通过；真实 Linux 待部署联调。 |
| 人设与上下文 | 默认小雾完整上下文、多轮提交去重、重启恢复；真实模型现场记录另存。 | 确定性通过；20 轮人工验收未完成。 |
| 真实模型首轮现场 | 4 轮生产 CognitiveServiceHost；第 2 轮 commit 64.59 秒暴露事件循环阻塞与 30 秒时限问题。 | 时限未通过；修复复验单独记录。 |
| MistyAI 外仓 | 默认 API/BodyState/X11 共 55 项；后续 vendor MIME/grounder/Text 契约补测 10 项。 | 工程通过；本轮未部署。 |
| 真实 8 小时 | 已实现并测试只读采样器，尚未启动正式运行。 | NOT_RUN。 |

## 环境阻塞

Windows ARM64、Python 3.12；WSL Ubuntu 24.04 可用。`dev.ps1 doctor -Mode live -NonInteractive -NoOpen -Json` 返回 action_required/10：本机依赖与既有 `/opt/mistyai` 托管目录可识别，但 Docker Linux engine 不可用。

启动 Docker Desktop 后，后端因 `sailor-ingest.sock` 重命名失败而退出，日志返回 “The file cannot be accessed by the system”。已停止该失败实例。清理该运行时套接字被自动审核拒绝，返回 “blocked by policy”；未通过其他方式重试删除，未重置 Docker 数据。恢复 Docker 环境后才有条件部署和实际验收 Linux 桌面。

外仓本轮测试临时目录清理也被自动审核拒绝，目录保留；不影响已记录的测试结果。

## 工程测试记录

以下阶段测试相互重叠，**不得相加当作不同用例总数**。

| 阶段 | 实际结果 | 范围 |
| --- | --- | --- |
| 改动前基线 | 121 passed，2 warnings | Runtime/perception/service/action-planner/GUI/presets/homeostasis。 |
| 首轮集成 | 299 passed | Runtime、journal、内稳态加速、人设及研究 manifest 兼容。 |
| 命令索引复验 | 77 passed | index/journal/supervisor/goal-defer/service-contract。 |
| 终端与批准集成 | 107 passed | basic-terminal actions/runtime/approval + 原 supervisor。 |
| 隔离启动与终端 | 38 passed | PowerShell 启动脚本、StateRoot 隔离、终端链路与批准。 |
| 最终回归 | 待补本轮最终结果 | 以最终源码状态与 JUnit 为准。 |

## 复验记录模板

```markdown
### YYYY-MM-DD · 本次运行名称
- 配置：两仓 SHA/工作区摘要、manifest/profile 哈希、模型和实际 embedding（不含密钥）。
- 环境：OS、服务版本、MistyAI 能力、physiology 开关和初始能量。
- 命令及报告：可复现命令、独立样本/summary/JUnit 路径。
- 实际时长与样本：真实单调时间、样本数、四进程身份、缺口。
- 结果：逐项 BR-ONLINE/HOMEO/TERMINAL/CHAT/PERSONA，失败和跳过均保留。
- 剩余项：责任方、具体阻塞或下次动作；不能用加速/模拟证据替代现场。
```
