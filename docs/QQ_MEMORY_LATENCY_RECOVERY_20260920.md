# QQ 16:25 回复超时修复（2026-09-20）

## 故障证据

用户报告时间为北京时间 16:25。服务器当前版本原为 `20260920-qq-style-reason-v1`，已含凌晨的辅助风格字段修复。本轮错误不同：回复前的同步与排队耗尽 60 秒总预算，gate 出现 `model_timeout`，决策以 `deadline_expired` 返回固定错误提示；正文调用次数为零。

| 收到消息（北京时间） | QQ 同步 | 本地同步 | 队列等待 | gate 调用 | 结果 |
| --- | ---: | ---: | ---: | ---: | --- |
| 16:25:35 | 16.345 秒 | 18.282 秒 | 0.002 秒 | 7.769 秒，超时 | 新消息到达，未交付旧决策 |
| 16:26:08 | 13.688 秒 | 0.010 秒 | 27.828 秒 | 4.966 秒，超时 | 16:27:10 交付固定故障提示 |
| 16:28:01 | 13.962 秒 | 22.860 秒 | 0.002 秒 | 3.789 秒，超时 | 16:29:02 再次交付故障提示 |

对应 trace：`qq-trace:2cd63dc87f174322a652f8a2ab88c966`、`qq-trace:72e612c0da0b451da0906ec3bb2d3964`、`qq-trace:1ea62f9707144c8893d02f000d396df7`。交付回执成功，故障发生在生成前，不是 QQ 网络发送失败。

## 修改

- `harness/qq/memory.py`：在模块初始化时创建两个 ID TypeAdapter，逐条消息复用。原严格类型、格式和私聊参与者校验不变。
- `harness/memory/shared.py`：初始化的非空检查改为 `SELECT 1 ... LIMIT 1`；增加本地历史批量写入，整批预校验并在单一事务中检查不可变来源、写入及建立索引。冲突回滚整批。
- `harness/cognitive/shared_memory.py`：本地历史同步改用批量接口，避免每个历史回合单独建连、初始化和提交。

不增加回复时限，不跳过 QQ 全量来源校验，不修改历史消息或既有回执，不重发故障轮次。

## 验证

- 共享记忆、日修订来源、共享认知、QQ 私聊共享记忆：124 passed。
- QQ 视觉记忆、协议来源、gate 合约、共享群视图、记忆导入：114 passed。
- Ruff、`git diff --check` 通过。
- 新回归覆盖批量导入幂等、冲突时全批及索引回滚、复用校验器后的非法 ID 拒绝。
- 服务器 SQLite backup 隔离副本、相同 10,009 条选中 QQ 来源：旧版同步 6.238 秒，修复版 3.133 秒；两者均成功并导入相同 2 条尚未镜像的来源。18 条既有本地来源重复导入由 0.0124 秒降到 0.0032 秒，新增均为零。此为隔离副本对比，不能等同于有后台竞争的生产端到端耗时。
- 使用当前模型配置和 QQ 数据副本进行一次只生成、不发送的决策探针：8.451 秒完成，返回合法 `natural_wait`，无错误。不是对原故障请求的精确重放。
- 补充明确提问的隔离探针：16.627 秒完成 gate 和正文两次模型调用，`action=speak`、`outcome=ready`、`error_type=null`，生成普通自然语言正文，`automatic_send=false`。两次探针均未调用 QQ 发送接口。

## 发布

以线上原版本为底，仅包含上述三个文件的修改，逐文件确认原内容与本地 HEAD 一致后制作候选。发布号 `20260920-qq-memory-latency-v1`，370 文件哈希校验通过，沿用旧版本锁定环境。

包 SHA-256：`0611fcf9ff794aa6b1054b21b97cea1f9308e3ebf2feddbe01a8d9a19dcd2267`。

先停止 QQ、GUI，再通过 Runtime 维护接口排空。停止 Cognitive 时，systemd 的反向依赖同时停止 Runtime；因此本次仍需等待完整 Runtime 历史恢复。恢复过程未跳过历史校验或修改 journal。认知与共享记忆数据库的更新前副本保存在 `/var/backups/neuralcloud/qq-memory-latency-20260920`，未恢复旧快照。

北京时间 17:29:28 完成验收：六个业务服务均 `active/running`；Runtime 为 `RUNNING`，journal、Cognitive、Homeostasis、LLM 健康标志均为 true，`degraded_reasons=[]`；QQ `running=true`、`connected=true`。370 个发布文件再次通过哈希校验。

Cognitive、Runtime、GUI、QQDirect 的实际进程工作目录均为新版本。未涉及此次修改的 Homeostasis、Board 保留原进程；部署标记记录各进程版本与 previous release。Runtime 于 17:01:59 开始启动、17:28:05 通过 systemd 就绪检查，约 26 分钟的恢复耗时仍属于旧版恢复算法的独立限制，本次未加入其他本地恢复机制改动。

最新发送仍为 `qq-direct-send:6b28e1ead3b64330bad1f248c8e838ef`；两个原故障决策各只有一条发送记录，首个被新消息替代的决策仍未发送。没有发送测试消息或重放原请求。

GUI 就绪流程正常完成；独立 `/api/status` 验收耗时 10.065 秒，仍存在状态页响应偏慢的问题。健康标志与隔离生成检查不能替代真实群消息的端到端耗时验证，本次没有人为向群内发测试消息。远端最终验收记录位于 `/var/lib/neuralcloud/release-evidence/qq-memory-latency-20260920.json`。
