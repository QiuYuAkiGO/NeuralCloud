# Misty 服务器 goal 低权限启用与验收

日期：2026-10-08。用户要求先确认 goal 服务器操作路径可用，再启用较低权限，至少禁止删除。

## 初始状态

线上服务器能力关闭，服务器命令账本为空。已有自主机会中没有服务器候选能力，不能把其他渠道的成功视为服务器路径已验证。

## 初始权限边界

- 新建独立系统账号 `misty-observe`，无 sudo 权限；账号没有附加管理组。
- 连接目标固定为本机 `127.0.0.1:22`，使用专用 Ed25519 密钥，并固定 SSH 主机公钥。
- Runtime 的 `forbidden_patterns` 使用完整字符串白名单：除列出的固定命令外一律拒绝。
- SSH 公钥设置 `restrict` 和 root 管理的强制命令 `/usr/local/libexec/misty-observe-command`。该程序再次校验完整命令，再用绝对程序路径和参数数组执行，不交给 shell 解释。
- 账号 home、`.ssh`、授权公钥和强制命令均由 root 管理；禁用该密钥的终端、转发、agent 转发和用户 rc。
- 每日总上限 30 次，自主调用上限 12 次。删除、覆盖、写入、sudo、解释器、任意脚本、安装软件、重启或停止服务均不在白名单中。

允许的固定命令：`uptime`、`uname -a`、`id`、`whoami`、`hostname`、`date`、`df -h`、`free -m`、`nproc`，以及分别检查 Runtime、Cognitive、Homeostasis 的 `systemctl is-active neuralcloud-…`。另允许连接测试所需的精确字符串 `uname -a; id`，由强制命令分成两次参数数组调用，不执行 shell。

现有授权接口一起启用 `server.observe.v1` 和 `server.execute.v1`；两个能力均受相同白名单与 SSH 限制。界面显示 execute 可用不意味着允许任意写操作。

## 已完成的验证

- 本地服务器相关测试 39 项通过；线上部署源码同组测试 39 项通过。
- 隔离 goal 集成测试使用确定性模型和真实 SSH，执行 `uptime` 返回 0；Runtime 将目标步骤记为 executed，结果具有证据引用。该测试不是线上模型自主选择的证明。
- 12 种非白名单命令通过真实 SSH 均返回拒绝码 126，包括 rm、rmdir、find -delete、脚本、sudo、命令拼接、重定向、服务重启及路径/引号变体。没有执行删除或文件写入。
- 首次配置连接测试返回独立账号身份和内核信息，退出码 0。
- 配置 revision 2 启用普通及自主服务器能力。Runtime 重启期间逐条验证历史事件日志，恢复后 Runtime、Cognitive、Homeostasis、GUI、QQ Direct 均 active。
- 第一版线上验收目标遇到两次 `capability_invalid_server_choice`，未执行服务器命令；已取消该目标并提交明确说明 choice 契约的只读验收目标。一次与取消交叠的机会标记为 superseded。

## 最终线上结果与限制

北京时间 08:41:44，线上模型通过 goal `goal:b16fba3fcd0abab4d7696e8c8fd2ca9f` 选择 `server.observe.v1`，执行 `uptime`，退出码 0。命令账本、能力账本和目标步骤均能关联到真实 `server-command` 回执及输出摘要；对应目标步骤为 executed。

08:42:57 最终核查：配置 active/saved revision 均为 2，不再要求重启；服务器能力和自主开关均为 true，六个核心相关服务均 active，没有待审批命令。验收目标在确认成功后由操作方取消，未评价模型独立结束整个目标的能力。

本次证明了真实模型 → goal → Runtime → 受限 SSH → 回执 → executed 步骤的可用性。首次普通描述存在两次格式错误，补充明确 choice 契约后成功；没有部署全局提示词或 schema 修复，因此不能宣称普通自然语言目标的格式稳定性已解决，也未验证长期自主成功率。

完整结果见 [validation.json](../harness/evidence/server-goal-readonly-20261008/validation.json)。没有发布或覆盖当前工作区其他未提交功能代码。
