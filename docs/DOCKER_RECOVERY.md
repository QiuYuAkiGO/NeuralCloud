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

## 启动入口

在 NeuralCloud 根目录运行：

```powershell
.\tools\docker\Start-DockerDesktop.ps1 -RefreshRuntimeOnColdStart
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
`-WhatIf` 不启动 Docker、不移动目录。所有启动经过全局互斥，单次调用至多进行一次目录隔离。
失败或并发状态变化时保留备份和记录，交给操作者核对。

## 数据与可回退性

备份是原运行时目录的同级目录，名称含时间与随机标识；恢复日志位于 `logs/docker-recovery/`。
两个路径及其祖先都必须是普通目录，禁止通过 junction/symlink 重定向修复目标。
不自动清理备份，以便核对失败时的原始状态；不要把已知故障 socket 重新搬回活动目录。

本机原桌面快捷方式和既有用户启动项如改为此入口，会单独保存原目标与参数。
不会通过修改 Windows 的启动启用状态来开启原本禁用的自启动。
停用此缓解方式可恢复原快捷方式/启动命令，或者直接运行原 Docker Desktop 程序；
在上游问题修复前，直接启动路径仍可能复发。

## 验证口径

- 启动准备脚本测试覆盖路径约束、活动进程、并发互斥、旧日志、新错误、有限重试及超时。
- 实机验证同时检查本机 Linux engine、Ubuntu-24.04 内 Docker 接入和临时 Ubuntu 容器运行。
- 对比修复前后的容器、镜像标识和卷清单；本次未删除、迁移或重建用户数据磁盘。
- 普通启动复发与受控启动成功分别记账。脚本测试通过不等于 Docker 上游 bug 已消失。

[4.89.0 发布说明](https://docs.docker.com/desktop/release-notes/#4890)虽然提到 stuck socket 修复，
本机仍然复现；[4.90.0 也有报告](https://github.com/docker/desktop-feedback/issues/554#issuecomment-5580999109)。
本方案是针对已验证故障的启动缓解措施，不能承诺所有未来 Docker/Windows 故障都不再发生。
