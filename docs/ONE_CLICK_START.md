# 一键启动

## 日常使用

Windows 下在仓库根目录双击 **`start.cmd`**。入口读取同目录的 [`launch.json`](../launch.json)，检查并准备环境，启动 Cognitive、Homeostasis、Runtime、GUI 四个服务，在就绪后打开浏览器。双击 **`stop.cmd`** 停止选定实例。

本机需要 Python 3.12。已有可用的 `.venv` 会直接使用；缺少环境时，入口按 `prepare_environment` 准备虚拟环境和项目依赖。缺少 Python 时会提示先安装，完成后再次双击。不需要修改系统的 PowerShell 执行策略。

默认是 **现有共享小雾实例 `shared`**：沿用已有历史、模型设置、QQ 会话和记忆。无需每次输入路径、端口或模式。GUI 地址为 <http://127.0.0.1:8790>，运维页为 <http://127.0.0.1:8790/ops>。

默认入口启动上述四个核心服务，不启动 QQDirect 或 NapCat 进程，也不代替 QQ 登录。QQ 进程、登录和连接需按 [QQ 接入说明](QQ_DIRECT.md) 单独准备；保留 QQ 配置、核心服务就绪均不代表 QQ 已在线。

相同配置的健康实例已经运行时，再次双击会复用它。修改启动配置后，使用重启命令应用：

```powershell
.\launch.ps1 -Command restart
```

## 启动配置放在哪里

启动参数统一放在根目录 `launch.json`。文件中已提供两套完整默认配置，修改现有字段即可：

- `shared`：默认选中，运行现有小雾。
- `local`：独立演示实例，使用 MockLLM，无需模型 API、QQ、MistyAI、Docker 或 WSL。

顶层字段：

- `schema_version`：配置格式版本，保持 `1`。
- `profile`：默认使用的配置名，出厂值为 `"shared"`。要将双击入口切换为演示实例，改成 `"local"`。
- `open_browser`：就绪后是否打开浏览器，默认 `true`。
- `prepare_environment`：启动时是否准备 Python 虚拟环境和依赖，默认 `true`。
- `profiles`：保存各套启动参数。

路径相对于**仓库根目录**解析，也支持绝对路径和 `%LOCALAPPDATA%`。JSON 中 Windows 反斜杠要写成 `\\`，也可以使用 `/`。配置文件只接受受支持的启动字段和绑定，不执行任意命令。

启动参数与业务配置各有用途：`launch.json` 记录“用哪个文件”，模型、QQ、看板等设置仍保存在原来的业务 JSON 中；API key 和服务令牌仍保存在原设置或私有令牌文件中。不要把凭据复制进 `launch.json`。

## 默认共享小雾配置

`profiles.shared` 的默认值：

- `mode: "configured"`：从已有 GUI 设置中读取 `demo`、`live` 或 `ollama` 模式，以及模型配置。
- `development: false`、`state_root: null`：使用现有共享实例的状态和配置绑定。
- `startup_timeout_seconds: 600`：每个服务最多等待 10 分钟就绪，给现有历史恢复留出时间，就绪后立即继续；可设置为 10–1800 的整数。
- `manifest_path: "presets/runtime/shared-misty-regulation-v1.json"`：共享小雾 Runtime 配置。
- `profile_path: "presets/homeostasis/qq-social-drive-v1.json"`：现有内稳态配置。
- `embodiment_provider: "auto"`：按 manifest 选择身体实现。默认 manifest 的身体为 `off`。
- `ports`：GUI `8790`、Runtime `8770`、Cognitive `8771`、Homeostasis `8772`。
- `mistyai.enabled: false`：默认不部署或启动 MistyAI，因此不要求 Docker 或 WSL。

`bindings` 引用当前共享实例的数据位置：

- `NC_GUI_CONFIG`、`NC_GUI_PRESETS`：已有模型设置和角色预设。
- `NC_RUNTIME_STATE_DIR`、`NC_HOMEOSTASIS_STATE_DIR`：Runtime 与内稳态状态。
- `NC_SHARED_MEMORY_DB`：共享历史和记忆数据库。
- `NC_QQ_SESSION_CONFIG`、`NC_QQ_WATCHER_CONFIG`、`NC_QQ_DIRECT_CONFIG`：QQ 会话、监听及直连配置。
- `NC_BOARD_CONFIG`：看板配置。
- `NC_CONTROL_TOKEN_FILE`、`NC_SERVICE_TOKEN_FILE`：共享服务认证令牌文件。
- `NC_QQ_INPUT_TOKEN_BUDGET`：默认 `96000`，最小 `8192`。
- `NC_QQ_MEMORY_RECALL_MAX_CHARACTERS`：默认 `16000`，最小 `1`。

这两个数值字段使用 JSON 整数，不加引号；上限均为 `2147483647`。四个端口也使用整数，范围 `1`–`65535`，彼此不能重复。`mode` 可选 `configured`、`demo`、`live`、`ollama`；`embodiment_provider` 可选 `auto`、`none`、`simulator`、`mistyai`。

默认私有状态路径使用 `%LOCALAPPDATA%/NeuralCloud/` 下现有目录，GUI 设置及预设使用本仓库 `data/`。如果迁移机器或移动历史文件，应先把这些路径改为实际位置。入口无法替你生成既有历史、QQ 登录状态或模型服务凭据；缺失配置时按错误提示补齐，再次双击启动。

共享配置保留已有 QQ 与自主行为设置。要修改功能是否启用、模型地址或 QQ 账号，修改对应业务配置或 GUI 设置。

## 无外部服务的演示配置

`profiles.local` 已给出可独立启动的默认参数：

- `mode: "demo"`：MockLLM。
- `development: true`、`state_root: "data/local-demo"`：独立保存演示状态与日志。
- `startup_timeout_seconds: 60`：每个服务的就绪等待上限。
- `manifest_path: "presets/runtime/development-demo.json"`。
- `profile_path: "presets/homeostasis/functional-proxy-v1.json"`。
- `ports`：GUI `18890`、Runtime `18870`、Cognitive `18871`、Homeostasis `18872`。
- `bindings: {}`，`mistyai.enabled: false`。

临时启动演示实例，无需修改默认选择：

```powershell
.\launch.ps1 -Profile local
.\launch.ps1 -Command status -Profile local
.\launch.ps1 -Command down -Profile local
```

GUI 地址为 <http://127.0.0.1:18890>。要让双击的启动与停止都操作演示实例，将顶层 `profile` 改为 `"local"`。临时指定 `-Profile local` 后，停止也应指定相同配置；`stop.cmd` 始终使用 `launch.json` 当前选中的配置。

## 命令行入口

在仓库根目录打开 PowerShell：

```powershell
.\launch.ps1                            # 等同于 -Command up
.\launch.ps1 -Command check             # 检查配置和 Python 依赖
.\launch.ps1 -Command status
.\launch.ps1 -Command restart
.\launch.ps1 -Command down
.\launch.ps1 -NoOpen                    # 本次不打开浏览器
.\launch.ps1 -ConfigPath launch.json     # 显式选择另一份启动配置文件
.\launch.ps1 -Profile local              # 本次覆盖顶层 profile 选择
```

`check` 不启动服务，不替代实际启动验收。`up` 以服务就绪为成功条件。端口被其他进程占用或当前运行实例与配置不同，会给出错误，按提示处理或执行 `restart`。

若 Runtime 从历史恢复为 `PAUSED`，且四服务已就绪，入口显示 `paused` 并保留暂停状态，不自动恢复。打开 GUI 或运维页查看暂停原因，处理相关事项后，按当前控制模式允许的方式恢复运行。

调整实例的状态目录、令牌路径等定位字段前，先用原配置停止实例，再保存新路径并启动；否则新配置可能无法定位原实例。端口和 manifest 等配置变更可使用 `restart` 应用。

## 可选：连同 MistyAI 一起启动

只有需要本地 MistyAI 身体服务时，才启用 `mistyai.enabled`。默认相关参数已预填：

- `source_path: "../MistyAI"`：相邻 MistyAI 工程目录。
- `distro: "Ubuntu-24.04"`：WSL 发行版。
- `mistyai_base_url: "http://127.0.0.1:8787"`。
- `mistyai_token_file: null`：需要显式凭据时填私有令牌文件路径。

此完整部署路径要求在共享配置中同时使用：

- `manifest_path: "presets/runtime/shared-misty-v1.json"`，需要从默认的 regulation manifest 修改。
- `profile_path: "presets/homeostasis/qq-social-drive-v1.json"`。
- `development: false`、`state_root: null`。
- 默认端口 GUI `8790`、Runtime `8770`、Cognitive `8771`、Homeostasis `8772`。
- `embodiment_provider: "auto"`、本机默认 `mistyai_base_url`、`mistyai_token_file: null`。

入口会拒绝不兼容的组合。启用后调用现有 `dev.ps1` 的完整部署流程，依赖 WSL2、Ubuntu、Docker Desktop 等环境，并复用其本机凭据处理。首次系统功能启用、Docker Desktop 协议、WSL integration 或系统重启仍可能需要手动完成。完整环境说明见 [新手入门](GETTING_STARTED.md#3-windows-统一启动入口)。

要连接已运行的身体服务，可保持 `mistyai.enabled: false`，另选匹配的 manifest、provider、服务地址和令牌文件。Runtime 是否使用身体能力由 manifest 和能力协商决定。

## 故障排查

- **双击报错**：读取启动窗口中的错误；在 PowerShell 执行 `.\launch.ps1 -Command check` 复查配置和依赖。
- **Python 或依赖未准备好**：保留 `prepare_environment: true`，按入口提示准备所需 Python，再次启动；安装依赖需要可访问包源。
- **共享配置文件找不到**：检查 `bindings` 中的路径，确认该 Windows 用户下已有对应私有状态。只需要体验界面时选择 `local`。
- **端口已占用**：检查 `.\launch.ps1 -Command status`；已有相同实例可直接复用，其他程序占用时先处理占用或修改 `ports`。
- **改配置后重复启动被拒绝**：使用 `.\launch.ps1 -Command restart`。修改状态路径前应先用原配置停止。
- **日志位置**：共享实例默认在仓库 `logs/`；演示实例在 `data/local-demo/logs/`。
- **模型或 QQ 不可用**：四服务启动成功只说明本机服务已就绪；检查 GUI 模型设置以及对应 QQ 配置、登录和连接状态。

兼容入口仍保留：`start.ps1` / `stop.ps1` 负责低层四进程启停，`dev.ps1` 负责完整 MistyAI 部署与诊断。日常使用统一的 `start.cmd` / `stop.cmd` 或 `launch.ps1`。

## 本机验收记录

2026-09-15 已实际验证共享小雾四服务冷启动、零参数 `start.cmd`、跨工作目录启动、重复启动复用进程，以及演示实例的批处理冷启动和停止清理。共享实例为 `live` / `RUNNING`，演示实例冷启动约 21 秒，测试结束后已停止。启动相关回归通过；各次测试范围有重叠，详见 [验收证据](milestones/evidence/oneclick-startup-2026-09-15.json)。QQ 收发及完整 MistyAI 部署未纳入此次实机验收。
