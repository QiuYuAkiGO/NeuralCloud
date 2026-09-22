# Linux 服务器身体

远端 Linux 通过独立 `server.observe.v1`、`server.execute.v1` 身体能力接入现有版本协商、Runtime 执行链和目标调节。运输层是 SSH：专用账号、私钥认证、固定主机公钥或 SHA256 指纹。默认不开启远程命令。

普通聊天按需执行不在本轮范围。目标页提交的任务与自主机会使用同一套 offer 目录；高危删除类命令进入 GUI 审批，超时取消。

## 使用

1. 更新并重启共享 Runtime 和 GUI，在原 GUI 地址打开 `/server`，或从首页「服务器」、目标页导航进入。
2. 填写目标主机、端口、用户名，粘贴 `ssh-keyscan` 得到的主机公钥或 SHA256 指纹。
3. 粘贴已有 OpenSSH 私钥，或点击「生成 ed25519 密钥」，把页面给出的公钥写入目标账号 `~/.ssh/authorized_keys`，保存时勾选「使用刚生成的密钥对」。
4. 点击「测试连接」。测试执行 `uname -a; id`，计入本地命令额度，不激活配置。
5. 显式选择服务器总开关和自主授权，保存后按页面提示重启共享服务。运行中的实例继续使用原配置与密钥。

自主授权还受 manifest 原有 `autonomous_capabilities.enabled`、安静期、机会间隔、目标调节和白名单约束。本页不会替其他通道打开全局自主开关。

## 目标主机准备

在目标 Linux 上为 Misty 建立低权限账号，不要复用 Runtime 服务账号：

```bash
sudo useradd --create-home --shell /bin/bash misty-ops
sudo mkdir -p /home/misty-ops/.ssh
sudo chmod 700 /home/misty-ops/.ssh
# 将 GUI 显示的公钥写入 authorized_keys 后：
sudo chown -R misty-ops:misty-ops /home/misty-ops/.ssh
sudo chmod 600 /home/misty-ops/.ssh/authorized_keys
ssh-keyscan -p 22 <host>
```

需要有限提权时再写最小 sudoers，并在配置页打开「sudo 一律视为高危」。主机指纹必须写入配置；禁止关闭 known_hosts 校验。

## 配置与权限

Runtime 在实际 `state_dir/server` 保存配置、独立私钥文件及命令账本。`NC_SERVER_CONFIG` 可指定绝对路径的配置索引；仅传入 Runtime 进程。GUI 始终通过认证的 Runtime 控制面保存，私钥不放在浏览器持久存储，也不回显给浏览器。

配置版本和密钥文件均不可变；当前实例锁定启动时版本。修改时的 `base_revision` 用于防止覆盖另一操作员的保存。空白私钥表示保留。

连接设置包含 `host`、`port`、`username`、`host_key`、`body_instance_id`、超时、每日限额、额外高危正则、禁止正则、`sudo_requires_approval` 与 `approval_timeout_seconds`。

授权独立存入配置版本中的 `authorization`，启动时合入冻结 manifest：

```json
{
  "server_access": {
    "enabled": true,
    "capability_requirements": [
      {"capability": "server.observe.v1", "required": false},
      {"capability": "server.execute.v1", "required": false}
    ],
    "approval_timeout_seconds": 1800,
    "sudo_requires_approval": false
  }
}
```

自主授权只调整 `owners.server` 和服务器两项白名单。模型无法更改配置、授权、主机、密钥、身体身份或截止时间。未配置和旧 manifest 保持关闭。

## 命令策略

Runtime 分类，模型不能自行声明风险。

- `server.observe.v1` 只接受只读白名单（uptime、df、journalctl、systemctl status、docker ps/logs、git status/log 等）。其它命令直接拒绝。
- `server.execute.v1` 常规命令自动执行。
- 删除/破坏类高危命令（`rm`、覆盖重定向、`find -delete`、强制 git/docker 删除、包卸载、`kill -9` 等）写入待审批队列，GUI `/server` 批准或拒绝。
- 无法静态分析（`eval`、`bash -c`、命令替换、未闭合引号、`xargs`）视为高危。
- `rm -rf /` 与 fork bomb 即使批准也拒绝。

命令输出各最多 64 KiB，截断有标记。证据引用为 `server-command:<id>` 与输出摘要哈希。输出只证明远端返回了这些文本。

## 限额和恢复

- 默认每日 200 次命令，其中自主最多 40 次，按 manifest 时区计日。预占在分类之后、执行或进入审批之前；失败、取消、超时与待审批均计数。
- 连接测试计入总额度。没有自动重试或跨重启重放已开始的 SSH 执行。
- 已完成请求按调用 ID 幂等返回；开始后中断保留未知状态。迟到或授权失效的结果不进入当前目标步骤。
- 待审批默认 1800 秒，超时取消；目标取消或自主配置世代变化也会取消未执行命令。

`calls.sqlite3` 是私有命令账本；Runtime journal 保存调用编号、风险等级、退出码与证据引用，不含完整输出。关闭能力并重启可停止新调用，不删除旧记录。

## 控制面与验证

认证接口为 `GET/PUT /v1/server/config`、`POST /v1/server/test`、`POST /v1/server/keys:generate`、`GET /v1/server/status`、`GET /v1/server/commands`、`POST /v1/server/commands/{id}/{approve|reject}`。GUI 通过同源 `/api/server/*` 代理。`GET /v1/capabilities` 同时呈现服务器身体和自主目录。

示例 manifest：`presets/runtime/shared-misty-server-v1.json`。生产目标调节配置不会因为存在本文件而自动打开 SSH。
