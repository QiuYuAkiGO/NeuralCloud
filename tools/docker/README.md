# Docker Desktop 安全启动

此入口只用于启动及已确认的运行时 socket 故障恢复，不提供停止 Docker 的功能。
兼容 Windows 自带的 Windows PowerShell 5.1，也可用 PowerShell 7。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Projects\NeuralCloud\tools\docker\Start-DockerDesktop.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Projects\NeuralCloud\tools\docker\Start-DockerDesktop.ps1 -CheckOnly -Json
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Projects\NeuralCloud\tools\docker\Start-DockerDesktop.ps1 -WhatIf -Json
```

`-CheckOnly` 与 `-WhatIf` 均只运行有限时长的本机引擎探针，不启动、不备份或改目录。
退出码为 `0`（健康）、`10`（只读检查未就绪）、`20`（需处理或启动失败）、
`30`（本机引擎已通过检查，但指定 WSL 发行版的 Docker 接入未就绪）。
`-TimeoutSeconds` 默认为每次等待 120 秒，`-ProbeTimeoutSeconds` 默认为 5 秒。
探针只连接本机 Docker Desktop Linux 引擎管道，不受其他 Docker context 指向影响；
超时仅结束本入口创建的探针进程。

本机引擎健康可能早于 WSL 集成就绪。需要 Linux 终端立即使用 Docker 时，可添加
`-WslDistro`，默认不检查任何发行版：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Projects\NeuralCloud\tools\docker\Start-DockerDesktop.ps1 -RefreshRuntimeOnColdStart -WslDistro Ubuntu-24.04 -Json
```

指定后，所有成功路径（包括引擎原本健康）都会等待该发行版执行 `docker info`
成功，再返回健康。集成等待额外使用最多 `-TimeoutSeconds` 的等待预算；每个探针
同时受 Linux `timeout` 和 Windows 进程超时限制。探针直接使用 WSL `--exec`
及独立参数执行 `/usr/bin/docker`，不执行拼接的 shell 命令，也不依赖 Docker context。
发行版需具备 `/usr/bin/timeout`、`/usr/bin/env` 和 `/usr/bin/docker`。
`wsl.exe` 检查可能唤醒所选发行版。为了保持不启动的承诺，`-CheckOnly`/`-WhatIf`
与 `-WslDistro` 同时使用且本机引擎健康时，返回 `integration_not_checked`、退出码
`10`，不执行 WSL 探针。正常启动流程中的 WSL 接入检查不会重启 Docker 或修改运行时目录。

WSL 接入失败返回 `integration_not_ready`、退出码 `30`，保留此前恢复记录；
不会因此再次隔离运行时目录或重启 Docker。此时检查 Docker Desktop 的该发行版
WSL Integration 开关、发行版命令及 Docker socket 是否可用。

为减少冷启动时 WSL 共享挂载未及时建立的竞态，指定 `-WslDistro` 且本机引擎未就绪、
也无 Docker 进程时，启动器先在该发行版中启动一个受 Linux `timeout` 限制的 `cat`。
它向标准输入发送唯一就绪标记，并在最多 `min(TimeoutSeconds, 30)` 秒内验证回显，
才允许继续备份运行时目录或启动 Docker。此进程保持标准输入打开，覆盖启动及接入检查；
最终关闭输入并有界等待退出，必要时仅结束自己创建的 `wsl.exe` 进程。
Linux 侧寿命按本次最多两次启动和一次接入等待预算计算，父进程异常退出后也会自动到期。
预热失败返回退出码 `20`，不继续备份或启动；原本健康、已有 Docker 进程和只读模式
均不创建保活。此措施不终止发行版、不修改 WSL 或 Docker 设置；它不是上游问题的修复。

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

每次隔离记录保存在 `%LOCALAPPDATA%\NeuralCloud\DockerRecovery\logs\`，
不依赖启动器所在仓库路径。此前仓库 `logs/docker-recovery/` 的历史记录保留作为证据。
记录包含原路径、备份路径、触发依据和
各步骤状态。发生中途失败时保留当前状态和全部备份，不自动回滚、覆盖或清理。
运行时备份会累积；此启动器不会删除它们。
