# 一键启动验证（2026-09-15）

> 历史说明：下文为同日改造前的验证结果。后续一键入口已修复相关启动问题，默认共享实例的四服务真实启动已通过；现行入口与配置见 [一键启动说明](../ONE_CLICK_START.md)。本文保留原始结论与证据，不代表新入口的当前状态；完整 MistyAI 与 QQ 实机收发未纳入本次新入口验收。

## 结论

当前工作区和本机环境下，默认入口不能完成“一键启动”。隔离开发模式的四服务启动、重复启动、HTTP 就绪检查及停止清理均通过；这不能替代共享模式与完整 MistyAI 拓扑的验收。

本次仅验证，未修改启动实现或共享配置。测试没有调用 QQ 发消息接口，也没有启动共享 Runtime。所有实际启动的隔离服务已停止，测试端口已释放。

## 实际执行结果

- `.\start.ps1`：立即退出，退出码 1，未启动服务。错误为 `Configuration error: shared-misty-launch.json must contain only approved absolute path bindings.`
- `.\dev.ps1 doctor -NonInteractive -Json`：结果 `blocked`，JSON 退出码 40。
- `.\dev.ps1 up -NonInteractive -NoOpen -Json`：结果 `action_required`，JSON 退出码 10，在前置检查阶段退出，未进入部署/四服务启动。非交互参数用于防止依赖安装提示阻塞验证。
- `start.ps1 -Development -StateRoot .tmp\startup-verification-20260915 -GuiPort 18890 -RuntimePort 18870 -CognitivePort 18871 -HomeostasisPort 18872`：退出码 0；以独立 demo 配置真实启动 Cognitive、Homeostasis、Runtime 和 GUI。
- 再次执行相同隔离启动命令：退出码 0；四服务重启成功，Runtime 为 `RUNNING`，GUI 模式为 `demo`。
- `stop.ps1 -StateRoot .tmp\startup-verification-20260915`：退出码 0；四服务停止，隔离 PID 文件删除，18870/18871/18872/18890 无监听者。

### HTTP 与日志验证

- Cognitive `/v1/health`、Homeostasis `/v1/health`：认证请求成功，`healthy=true`。
- Runtime `/v1/status`：认证请求成功，`lifecycle=RUNNING`。
- GUI `/api/status`、首页 `/`、运维页 `/ops`、`/static/app.js`：均返回 HTTP 200。
- 隔离 Runtime 的身体能力为 `disabled`；本次没有验证真实身体服务、模型推理或 QQ 收发。
- 四服务错误日志未发现 `ERROR`、`Traceback` 或 `CRITICAL`。
- 隔离日志位于 `.tmp/startup-verification-20260915/logs/`。

## 已确认的问题

### 1. 本机启动配置无法通过入口校验

`data/shared-misty-launch.json:13-14` 包含：
- `NC_QQ_INPUT_TOKEN_BUDGET = "96000"`
- `NC_QQ_MEMORY_RECALL_MAX_CHARACTERS = "16000"`

`start.ps1:24-47` 只允许白名单中的绝对路径字段，这两个数值字段不在白名单内。错误发生在读取模式、检查进程或启动服务之前。应明确支持这些数值配置的类型与范围，或将它们迁移到正确的配置来源。

### 2. WSL 桥接未转换 CRLF，导致依赖与部署检查误报

`tools/launcher/Launcher.psm1:391-397` 原样将 PowerShell 多行字符串传入 Bash。当前文件包含 3637 个 CRLF 换行和 18 个独立 LF 换行。

直接提取该模块的真实 `$wslProbe` 并执行：
- 原始内容：退出码 2，出现 `syntax error: unexpected end of file`，路径含尾随回车。
- 仅在内存中将 CRLF 转为 LF：退出码 0，正确识别 `Python 3.12.3` 和 `uv 0.12.9`。

另以最小探针对照：LF 退出码 0；CRLF 退出码 127，并尝试执行不存在的 `/usr/bin/python3.12\r`。因此 doctor 报 Python/uv 缺失不能直接当作真实依赖缺失；其余基于同类 Bash 探针的部署误报也需修正换行后复查。实现文件保持原样。

### 3. Docker 尚未就绪，完整入口不能自动补齐当前状态

Windows Docker Desktop 已安装，但 Linux engine 命名管道不存在。规范化后的 WSL 探针仅找到 Windows 挂载路径下的 Docker 包装命令，不能访问原生 Docker/Compose integration。

`dev.ps1 up` 将这些条件列为需手动处理；模块没有把已有的 `tools/docker/Start-DockerDesktop.ps1` 接入本次启动流程。因此在本机当前状态下，单个 up 命令不足以启动完整拓扑。

### 4. 残留 PID 文件会阻止共享模式冷启动

工作区 `.neuralcloud.pids.json` 记录的四个 PID 在实测时均不存在。`start.ps1:632-635` 仅按文件存在与否拒绝共享模式启动，不核验这些进程是否仍存活。

这属于修复首个配置错误后仍会遇到的代码路径；本次未删除原 PID 文件或绕过其保护。可复用 `stop.ps1` 的进程身份与端口核验逻辑来安全识别残留记录。

### 5. 重复 dev up 的失败清理可能停止原有实例

`Launcher.psm1:3241-3245` 在调用低层启动前就设置本次已尝试启动、拥有服务；低层因现有注册文件拒绝启动后，`:3291-3292` 仍会调用 `stop.ps1`。

隔离模拟保留真实 `Invoke-LauncherUpCommand`、`Start-NeuralCloud` 和 `Stop-NeuralCloud`，仅替换外部依赖/命令结果。当前置通过、MistyAI 已运行、低层 start 因原有 registry 返回 1 时，得到：
- 上层错误码 30；
- start 调用 1 次；
- stop 调用 1 次；
- MistyAI 停止调用 0 次；
- 最终 registry health 检查 0 次。

这是模拟复现，未对真实已有服务执行该场景。清理应仅针对本次确实新建的进程，不能把“尝试启动”当作取得已有实例的所有权。

可复测脚本：`.tmp/startup-verification-20260915/duplicate_dev_up_mock.py`；输出：同目录 `duplicate_dev_up_mock.json`。使用仓库 `.venv/Scripts/python.exe` 执行脚本即可重现模拟结果。

## 文档与测试边界

README 第 88 行仍描述 grounded manifest 与 GUI 8766，而当前统一入口使用 shared manifest 与 GUI 8790。第 138-142 行关于自动重启、读取上次配置及 demo 默认值的说明也与当前共享启动分支不一致。

现有 WSL 探测测试使用模拟输出，未真正执行 Bash；启动测试的脚本复制也可能通过文本读写归一换行。低层重复启动保护测试不能覆盖外层 dev up 的失败清理交互。单元测试通过不足以证明当前工作区能一键启动。

## 自动化回归测试

运行了 `test_start_script.py`、`test_dev_launcher.py`、`test_shared_launcher.py`、`test_shared_launch_bindings.py`、`test_shared_start_mode.py`、`test_shared_start_tokens.py` 六组测试：**208 passed，1 failed，203.41 秒**。

唯一失败为 `tests/test_dev_launcher.py::test_start_process_registry_v2_records_full_service_identity`（第 1387 行调用）。该测试只抽取 `Write-ProcessRegistry` / `Start-NeuralService` 函数，在 StrictMode 下没有初始化新增注册表字段所需的 `$effectiveHomeostasisStateDir`，报变量未设置。真实 `start.ps1` 在运行这些函数前会初始化此变量，且本次真实隔离启动已通过，因此该结果表明测试 fixture 与当前函数上下文不同步，不能据此认定真实启动也会出现同一错误。

JUnit 报告：`.tmp/launcher-verification-c8943041240542eca2ea726070a65830/results.xml`。

## 建议修复顺序

1. 统一启动配置的字段契约。
2. 在 WSL 桥接边界规范化换行，并加入真实 Bash 最小集成验证。
3. 修复重复 up 的进程所有权与清理逻辑，以及残留 PID 恢复。
4. 接入已安装 Docker 的有界启动流程，更新入口文档。
5. 在共享配置、真实 MistyAI 和 Docker 全部就绪后重做完整冷启动及重复启动验收。
