# Linux 实例与交接（2026-09-15）

## 访问与运行范围

- 网页：<https://panel.u2235830.nyat.app:60254>，账号 `admin`。
- 独立管理密码在本机私有文件 `%LOCALAPPDATA%/NeuralCloud/LinuxDeployment/access.json`；仓库和发布包不包含明文密码。
- SSH：`ssh -p 54849 qiuyu@frp-fan.com`。
- 发布版本：`20260915-linux-2`；当前代码指针 `/opt/neuralcloud/current`。
- 主配置：`/etc/neuralcloud/deploy.json`；持久状态 `/var/lib/neuralcloud`；日志 `/var/log/neuralcloud` 和 journald。
- systemd 托管四核心、Board、QQDirect；NapCat、认证代理和新 Web FRP 隧道分别由独立 unit 托管。
- QQ 账号 `2015188508`，保留原有两个群及一个私聊的权限。源 Windows 核心、Direct/watcher 和 WSL Board 已停止。正式运行只使用 Linux 这一份状态。

## 常用命令

登录服务器后：

```bash
cd /opt/neuralcloud/current
sudo .venv/bin/python -m harness.deploy --config /etc/neuralcloud/deploy.json status
sudo .venv/bin/python -m harness.deploy --config /etc/neuralcloud/deploy.json verify
sudo journalctl -u neuralcloud-runtime -u neuralcloud-qq-direct -n 80 --no-pager
```

进行一致性备份（会短暂停止入口与业务，再恢复备份前运行的服务）：

```bash
cd /opt/neuralcloud/current
sudo .venv/bin/python -m harness.deploy.backup_job --config /etc/neuralcloud/deploy.json
```

现有备份 `/var/backups/neuralcloud/scheduled-20260915T054855672286Z` 已校验成功，备份和自动恢复总计约 132 秒。默认保留 7 个日备份和 4 个周备份；定时任务模板已提供但没有启用，异机备份位置未配置，当前只有本机恢复能力。新 NapCat 的独立 HOME 不在核心定时快照内，需按通用手册单独封存。

首次迁移的完整封存快照同时保留在源仓库 `.tmp/production-snapshot-final` 和服务器 `/home/qiuyu/neuralcloud-migration/production-snapshot-final`。新端已经产生业务写入，回迁时必须先备份新状态并核对交付记录；不能直接恢复旧机快照。

## 验收与边界

- 15 个 SQLite 数据库及 94,846 条迁移前 journal 记录通过完整性/哈希链校验，恢复前后全部表计数及 Cognitive/checkpoint 元数据一致。
- 保留 1,709 条共享记忆、3 个认知会话、3 条看板历史和 93 个可解码图片缓存；QQ 加密登录资料另以 ZIP 按原字节封存。
- 新端真实模型返回、网页对话最终回复、HTTPS、全站认证和 WebSocket 验证通过；未认证返回 401，错误 Origin 返回 403。
- QQ 账号、两个群、一个联系人及私聊历史读取通过；指定私聊 `2285027817` 的单条测试消息取得回执并通过 `get_msg` 回读。
- 已完成 Mock 数据恢复、代码回滚和新端实际备份/进程恢复演练；整台共享主机重启和长时间稳定性观察未在本次执行。
- FRP 的 `.12.3` 与 `.14` 客户端均在启用 `auto_https_verify_client` 时误拒绝合法带端口请求。本站关闭该项前置检查，保留严格证书策略、Caddy 的公网 Host 限制、全站认证和 GUI 完整 HTTPS Origin 校验。现有 SSH FRP 与另一账号的 NapCat 保持运行。

完整证据见 [执行记录](milestones/evidence/linux-migration-2026-09-15.json)，通用安装、升级和回滚步骤见 [Linux 部署手册](LINUX_DEPLOYMENT.md)，登录维护见 [NapCat 手册](LINUX_NAPCAT.md)。
