# Docker Desktop 套接字启动故障修复

## 已确认的故障

2026-09-10，本机 Windows 11 ARM64、Docker Desktop 4.89.0 (238018)、WSL 2.7.12.0。
Docker 在初始化主机服务时无法将 AF_UNIX 套接字改名为 `.stale`，返回
`The file cannot be accessed by the system`，随后退出，Linux engine 管道不存在。

两个相关目录分别为：

- `%LOCALAPPDATA%\Docker\run`，其中包括 `sailor-ingest.sock`。
- `%LOCALAPPDATA%\docker-secrets-engine`，本次检查仅含 `engine.sock`。

单个 socket 改名失败；完整备份改名父目录后，可以创建干净目录。
只处理其中一个目录会在下一次启动暴露另一个错误，还会留下新 socket，因此恢复必须同时处理两处。

这不是已证实的 NTFS 损坏，也没有证据表明由 NeuralCloud 强杀 Docker 引起。
本机已复现：`docker desktop stop --timeout 40` 正常成功后，直接重新打开 Docker 仍失败。
[Docker 官方问题库](https://github.com/docker/desktop-feedback/issues/554#issuecomment-5501334546)有相同版本、相同错误和正常关闭后的复现。
完整运行时目录备份法也有[同 issue 的实测记录](https://github.com/docker/desktop-feedback/issues/554#issuecomment-5564395548)。

[微软 AF_UNIX 说明](https://devblogs.microsoft.com/commandline/af_unix-comes-to-windows/)确认此类路径是特殊 reparse point。
普通存在性、零字节或 `fsutil` 访问失败，不能单独用来判定 socket 已损坏。

### 09-11 连续启停发现的 WSL 接入故障

第二轮连续启停中，本机引擎已能返回 `29.7.2`，但 Ubuntu 的 `/usr/bin/docker` 消失，
Docker 的 WSL 代理报 `/mnt/wsl/docker-desktop/shared-sockets/host-services/backend.sock` 不存在。
Ubuntu 和 Fedora 的集成都受到影响；这与 Windows socket 目录错误分别记录。
[官方问题库 #582](https://github.com/docker/desktop-feedback/issues/582)有相同表现，当前不能把“引擎健康”当作“Linux 终端可用”。

实机对照中，在 Docker 启动前唤起并短暂保持 Ubuntu 活跃，两轮冷启动分别约 9.9 秒和 9.0 秒，
本机引擎、WSL 内 Docker 和临时容器全部通过，容器/镜像/卷清单一致。
这支持采用启动前预热来回避本机观察到的挂接问题，但不证明已确定 Docker/WSL 内部根因。
预热无需关闭全部 WSL、终止用户发行版，或禁用 Fedora 集成。

## 启动入口

在 NeuralCloud 根目录运行：

```powershell
.\tools\docker\Start-DockerDesktop.ps1 -RefreshRuntimeOnColdStart -WslDistro Ubuntu-24.04
```

该显式选项用于本机已验证的复发问题。只有本地引擎未就绪且 Docker 进程全部退出时，
才会预先备份并重建两个运行时目录。已健康运行时直接返回；后台仍在运行但未就绪时仅限时等待。
它不删除镜像、容器、卷，不重置 Docker，不改账号/WSL 数据磁盘，也不强杀 Docker 后端。

不带该选项时采用保守启动：仅在本次启动新增日志确认同类 socket 错误、并且 Docker 完全退出后，
才执行一次目录恢复及重试。历史日志不会单独触发这种恢复。

只读检查：

```powershell
.\tools\docker\Start-DockerDesktop.ps1 -CheckOnly -Json
```

探针固定连接本机 Docker Desktop Linux engine；探测有超时，不能因用户切换 Docker context 而误认远端引擎正常。
指定 `-WslDistro` 时，还要等待该发行版内 Docker 就绪。本机已观察到引擎先就绪、WSL 挂接稍后完成的窗口；
不能因立即执行一次 `docker` 返回不可用就宣布 WSL 配置损坏。超过限时仍不可用会返回 `integration_not_ready / 30`，
保留运行中的 Docker，不再次恢复目录或重启。
冷启动时还会先唤起指定发行版，通过一个带超时的自有进程回显随机标记，确认已就绪后才恢复目录并启动 Docker。
该进程仅在本次启动期间保持发行版活跃，结束时关闭；预热失败不会继续修改目录或启动 Docker。
这一步不改 WSL 配置或其他发行版的集成开关。
只读或预演模式不会调用可能唤醒发行版的 WSL 探针；如果同时指定发行版，会明确报告其接入尚未检查。
`-WhatIf` 不启动 Docker、不移动目录。所有启动经过全局互斥，单次调用至多进行一次目录隔离。
失败或并发状态变化时保留备份和记录，交给操作者核对。

## 数据与可回退性

备份是原运行时目录的同级目录，名称含时间与随机标识；新版恢复日志位于
`%LOCALAPPDATA%\NeuralCloud\DockerRecovery\logs`，前一轮验证记录保留在项目的 `logs/docker-recovery/`。
两个路径及其祖先都必须是普通目录，禁止通过 junction/symlink 重定向修复目标。
不自动清理备份，以便核对失败时的原始状态；不要把已知故障 socket 重新搬回活动目录。

2026-09-11 已将防护脚本安装在 `%LOCALAPPDATA%\NeuralCloud\DockerStartup`，不依赖项目目录保持原位置。
本机桌面 `Docker Desktop.lnk` 和既有 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 中
`Docker Desktop` 项已改为 Windows PowerShell 5.1 调用此入口，启用运行时恢复、Ubuntu 预热与接入检查。
原快捷方式和启动命令保存在安装目录的 `entry-backup-20260911/`，项目日志目录另有副本。
Windows `StartupApproved` 字节及 Docker `AutoStart=false` 均保持原值，没有开启原本禁用的自启动。
停用此缓解方式可恢复原快捷方式/启动命令，或者直接运行原 Docker Desktop 程序；
在上游问题修复前，直接启动路径仍可能复发。

## 验证口径

- 启动准备脚本测试覆盖路径约束、活动进程、并发互斥、旧日志、新错误、有限重试及超时。
- 实机验证同时检查本机 Linux engine、Ubuntu-24.04 内 Docker 接入和临时 Ubuntu 容器运行。
- 对比修复前后的容器、镜像标识和卷清单；本次未删除、迁移或重建用户数据磁盘。
- 普通启动复发与受控启动成功分别记账。脚本测试通过不等于 Docker 上游 bug 已消失。

现场失败与对照记录保留在项目 `logs/docker-recovery/`：

- `live-verification-20260910.json`：引擎先就绪而 WSL 尚未挂接的首次失败。
- `live-verification-20260911-probe-argv-failed.json`：WSL 选项引用错误；已通过参数传递修正和回归覆盖。
- `live-verification-20260911-wsl-mount-failed.json`：第一轮通过，第二轮共享挂接失败；防护明确返回 `integration_not_ready / 30`。
- `wsl-warm-start-20260911.json`：保持发行版活跃后恢复成功。
- `live-verification-20260911-wsl-prewarm-experiment.json`：停止 Docker 后再预热 Ubuntu 的两轮完整验证通过。
- `live-verification-20260911.json`：最终固定安装副本独立完成两轮真实启停，记录被测脚本 SHA-256，全部通过。
- `installed-entries-20260911.json`、`entry-verification-20260911.json`：实际入口安装和随后健康复查通过。

### 最终实测结果（2026-09-11 00:35，Asia/Shanghai）

- 安装副本无需外部辅助预热，连续两轮正常停止和冷启动均通过；启动用时约 **9.0 秒 / 8.8 秒**。
- 两轮均通过本机 Linux engine、Ubuntu-24.04 中 `docker info` 和只读、无网络临时 Ubuntu 容器运行。
- 前后 **3 个既有容器、2 个标签镜像 ID、4 个卷**清单一致；临时验证容器结束后自动移除。
- 从实际快捷方式读取目标和参数执行，退出码为 0，确认 WSL 就绪且没有新建恢复记录；结束后自有保活进程为 0。
- 启动防护回归 **27 passed**；NeuralCloud 启动器回归 **143 passed**，Ruff、PowerShell 5.1 导入及差异检查通过。
- 固定安装版本和验证报告的 SHA-256 已交叉核对。没有进行 Windows 整机重启测试；QQ 登录、群聊与人物记忆仍单独验收。

可复核摘要见 [Docker 恢复验收记录](milestones/evidence/docker-recovery-2026-09-11.json)。

[4.89.0 发布说明](https://docs.docker.com/desktop/release-notes/#4890)虽然提到 stuck socket 修复，
本机仍然复现；[4.90.0 也有报告](https://github.com/docker/desktop-feedback/issues/554#issuecomment-5580999109)。
本方案是针对已验证故障的启动缓解措施，不能承诺所有未来 Docker/Windows 故障都不再发生。
