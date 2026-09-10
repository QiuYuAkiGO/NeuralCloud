# Docker Desktop 安全启动

此入口只用于启动及已确认的运行时 socket 故障恢复，不提供停止 Docker 的功能。
兼容 Windows 自带的 Windows PowerShell 5.1，也可用 PowerShell 7。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Projects\NeuralCloud\tools\docker\Start-DockerDesktop.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Projects\NeuralCloud\tools\docker\Start-DockerDesktop.ps1 -CheckOnly -Json
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Projects\NeuralCloud\tools\docker\Start-DockerDesktop.ps1 -WhatIf -Json
```

`-CheckOnly` 与 `-WhatIf` 均只运行有限时长的本机引擎探针，不启动、不备份或改目录。
退出码为 `0`（健康）、`10`（只读检查未就绪）、`20`（需处理或启动失败）。
`-TimeoutSeconds` 默认为每次等待 120 秒，`-ProbeTimeoutSeconds` 默认为 5 秒。
探针只连接本机 Docker Desktop Linux 引擎管道，不受其他 Docker context 指向影响；
超时仅结束本入口创建的探针进程。

默认流程使用全局互斥。健康引擎立即返回；已有 Docker 进程时仅有限等待，
绝不强杀进程或修改运行时目录。启动失败后，只有本次启动新增且时间戳匹配的
`sailor-ingest.sock`/`engine.sock` 确切路径 rename/cannot-access 错误，
并且 Docker 相关进程已经全部退出，才允许整体隔离重建两个运行时目录，重启至多一次。
正常存在的 socket、旧日志或其他目录的同名 socket 均不会触发恢复。

本机已现场确认正常停止后仍会复发这类错误，可选择显式冷启动回避措施：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Projects\NeuralCloud\tools\docker\Start-DockerDesktop.ps1 -RefreshRuntimeOnColdStart
```

该选项默认关闭。开启后只有引擎未就绪且无 Docker 进程时，才在启动前隔离两个
已确认的运行时目录；它与失败后恢复共用一次隔离额度，失败后不会再次隔离或循环重试。
这是对已现场复现问题的启动回避措施，不声称修复了 Docker Desktop 上游原因。

唯一允许隔离的目录是 `%LOCALAPPDATA%\Docker\run` 和
`%LOCALAPPDATA%\docker-secrets-engine`。两者及所有祖先必须为普通目录，
不跟随根目录的 junction/symlink/reparse point。全部路径先校验，然后各自整体移动到
同一父目录下唯一的 `.misty-backup-…` 目录，并重建空目录。不会遍历或删除备份内容，
不触碰 WSL 磁盘、镜像、卷、Docker settings 或其他目录。

每次隔离记录保存在仓库 `logs/docker-recovery/`，包含原路径、备份路径、触发依据和
各步骤状态。发生中途失败时保留当前状态和全部备份，不自动回滚、覆盖或清理。
运行时备份会累积；此启动器不会删除它们。
