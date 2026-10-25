# Misty resident 运维入口

## 固定边界

代码根 `/opt/misty/current` 指向固定发布；数据根 `/var/lib/misty`。四个正式单元为 `neuralcloud-memory.service`、`misty-state.service`、`misty-harness.service`、`misty-operator-notifier.service`。前三个服务分别持有记忆、状态、会话写锁；通知进程只发送审批通知，不运行第二个 Agent Loop。

手机 emulator / MCP 是复用的能力后端，独立于退役的旧认知与 homeostasis 管线。正式角色、迁移和管理凭据分属不同 OS 用户。目录只开放启动门禁需要的 traverse/stat；数据库仍为各自用户 0600。不要以 root 运行正常角色来掩盖权限错误。

当前新增 QQ 765681548 的 OneBot 通道；账号/群范围、@ 触发条件、真实回执和独立恢复副本见 [OneBot 生产接入记录](08-onebot-production.md)。`nc_765681548.service` 负责 QQ 登录与协议连接，推理和发送授权仍由同一个 `misty-harness.service` 承担。

## 审批与只读诊断

本机受限目录 `D:\MistyOperator` 提供 `Start-MistyOperator.ps1`；启动隐藏 SSH 转发后访问 `http://127.0.0.1:18766/operator`。`operator.token` 只在该受限目录保存，页面输入框仅驻留内存。QQ 通知是提醒，QQ 回复不是批准；不把 operator token 粘贴到 Misty 对话。

查看四个 systemd 单元的状态、重启计数与内存；查看 `/var/lib/misty/harness/status.json` 的新鲜度、pending/blocked/input_pending，以及 operator notices 的 UNKNOWN。通过受认证 operator CLI 查询 retention、autonomy 和待审批请求。状态中的 READY 表示调度与持久队列就绪；自主活动另看 `autonomy.reason` 与 `generation` 中的冷却截止时间、需求/阈值、预算和 timer_status。角色只读接口为 `/v1/autonomy/status`，管理只读接口为 `/operator/v1/autonomy`，查询不修改状态或触发活动。

自主活动不再要求官方 QQ 与 OneBot 同时在线；实际发送只检查目标通道。官方连接 `READY` / `RESUMED` 都会恢复就绪标记，普通 SDK error 不等于掉线。systemd journal 中 `QQ_CONNECTION` 只记录固定事件名、时间和数字断开码；`AUTONOMY_STATUS` 记录自主阻塞原因变化。已知离线的发送在 SDK 调用前结算为 NOT_SENT，发送后不确定仍为 UNKNOWN；既有发送预算及不重试规则保留。真实发送必须有外部回执。

`RECONCILIATION_REQUIRED`、UNKNOWN、outbox blocked 不自动清库或重放。使用部署中的 `ingress-status` / `outbox-status` 离线检查；人工核对后用带 expected hash / operator / evidence 的维护命令。原始输入和退役会话仍保全。无对应日志的 PREPARED 输入只能隔离处理，不能当作从未执行。

72 小时变化正文的维护由状态服务每 30 秒 tick 有界执行，`retention` 显示最近维护游标。运行回执和操作审计不随生理变化过期。对后台任务不声称有未部署的 QQ 运维报警；当前持久状态和 systemd journal 是诊断入口，审批通知是实际配置的主动管理通道。

## 停止、保全与恢复

先经 operator stop 收敛活动，再停止 Harness 与通知进程，确认在途动作的实际回执。停止记忆和状态服务后执行 capsule preserve；所有写入单元必须显式列入 `--stopped-unit`。命令只保全数据，不恢复旧角色：

```sh
sudo /opt/misty/current/MistyAI/.venv/bin/python -m mistyai.integration.recovery preserve \
  --source /var/lib/misty --destination /PROTECTED_NEW_EMPTY_PATH/capsule \
  --stopped-unit misty-harness.service --stopped-unit misty-operator-notifier.service \
  --stopped-unit neuralcloud-memory.service --stopped-unit misty-state.service
sudo /opt/misty/current/MistyAI/.venv/bin/python -m mistyai.integration.recovery verify \
  --source /PROTECTED_NEW_EMPTY_PATH/capsule
sudo /opt/misty/current/MistyAI/.venv/bin/python -m mistyai.integration.recovery restore \
  --source /PROTECTED_NEW_EMPTY_PATH/capsule --destination /ANOTHER_NEW_EMPTY_PATH/restored
```

恢复目录默认是管理面私有副本。要替换运行数据，另行验证 OS 用户归属、三个角色的文件权限、服务根路径与门禁绑定，再开放服务；不要直接把只读演练目录作为生产根。

**`/var/lib/misty-mobile` 在 capsule 根之外。** 普通角色回滚保留当前手机后端及账本，不自动回写旧 AVD。若计划恢复整机/手机，也须停止 `misty-mobile-mcp.service` 与 `misty-mobile-emulator.service`，对该树另做完整停写快照、文件清单、加密独立副本及解密恢复验证，再进行任何替换。已有外部消息不会随数据库回滚撤回，未知操作不自动重试。

没有通用旧 schema 逆转换器。回滚只能在保全新经历与外部副作用后选择兼容的新版本修复或有限服务；不能直接启动旧快照并声称经历连续。旧服务保持 disabled，重新启用需要审查 frozen unit 清单，避免与新服务的 Conflicts 单活规则冲突。

## 离机副本与冷历史

G: 保存加密恢复包，D: 的独立受限密钥目录提供可携带恢复密钥；I: 是维护暂存。完整快照和新 resident-ready 副本分别留验证报告。恢复应从 G: 密文和 D: 密钥开始，不依赖 I: 的明文副本或当前 Windows DPAPI。

备份与冷剥离是两道门禁。生产 `cold_eviction_enabled=false`；普通角色没有 G: 的路径、挂载、密钥或回源工具。要剥离真实历史，仍须完成批准范围、revision 冻结、关系/引用、独立恢复和所有旁路副本清点，不能因为磁盘压力跳过。

## 随副本携带的恢复工具

G:\MistyRecovery\tools 保存独立解密校验工具、固定依赖说明及使用文档。工具支持本次完整 Zstd 快照和 resident-ready gzip capsule 的分块 AES-GCM 格式；两个格式的往返与损坏拒绝验证共 4 个场景通过，且从 G: 对实际 70 文件新数据包再次校验通过。工具只校验或向新路径输出已认证的压缩 tar，不自动覆盖运行数据或启动服务。源码在 MistyAI 的 tools/recover_encrypted_snapshot.py。
