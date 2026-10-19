# 独立在线安装包（Standalone）

在 GitHub Actions 的 **ci** 成功运行页下载 `neuralcloud-standalone-online`，
解开 artifact 后再解开其中 `neuralcloud-standalone.zip`。SHA-256 文件可核对 ZIP，
`RELEASE-MANIFEST.json` 记录源码提交与每个文件的哈希。
`standalone-candidate` 只是未经完整门禁验证的构建候选，不能当作验证通过的发行包。
不会自动创建 GitHub Release 或正式标签。

这是可运行的 NeuralCloud 独立 **development_debug** 四进程拓扑：
GUI、Runtime、Cognitive、Homeostasis 都是真实进程；首次默认模型响应使用 demo，也支持现有 live/ollama 模型接口，身体 provider=none。
这不代表 MistyAI 生产配对、真实模型能力或 QQ 集成已经通过验收。

## 首次运行

需要预装 **Python 3.12**（含 pip、venv）；Windows 安装时启用 PATH 或 Python Launcher。
Linux 若发行版拆分 venv，请先安装对应的 Python 3.12 venv 包。本入口不安装系统软件，
不要求管理员权限，不改防火墙或系统服务。

- Linux：终端执行 `bash /解压路径/NeuralCloud/deploy.sh`。
- Windows：双击 `NeuralCloud\deploy.bat`，保留控制台窗口。

路径可以包含空格和中文，从任意当前目录运行都可以。
首次创建项目内 `.bootstrap`、`.venv`，使用 uv 0.12.10 与 `uv.lock` 安装 GUI/QQ 依赖。
需要联网访问 PyPI 及锁文件中的包下载地址；**不是离线包**，不自带 Python、模型或 MistyAI。
每次运行都会校验锁定依赖，安装失败时查看 `logs/install.log`，修复网络后执行同一入口重试。

看到 READY 后打开 `http://127.0.0.1:18890`。服务仅监听回环地址。
按 Ctrl+C 停止本次启动的进程；Windows 直接关闭窗口可能留下子进程，优先使用 Ctrl+C。
重复启动被锁阻止，不会停止其它进程；端口冲突会明确报错，可修改 `standalone.json` 的四个端口。
退出后可重复运行，`standalone.json` 和 `standalone-data/` 不覆盖，日志追加至 `logs/standalone-*.log`。
升级请解压到新目录并先备份状态，不要覆盖正在运行的目录。

## 配置和边界

首次无需模型密钥。启动模式由 `standalone.json` 的 `mode` 指定：`demo`、`live` 或 `ollama`。
端口配置保持原样。真实模型连接使用现有 GUI 配置机制：

1. 先以 demo 启动，在 GUI 保存模型配置；也可以创建 `standalone-data/gui_settings.json`：
   ```json
   {"mode":"live","base_url":"https://your-provider.example/v1","model":"your-model","api_key":"your-key"}
   ```
2. 停止启动器，将 `standalone.json` 的 `mode` 改为 `live`，再次运行 shell/bat。
   缺少配置或密钥时启动器会报出具体文件和所需字段，不会静默退回 demo。
3. 本地 Ollama 使用 `mode=ollama`，在 GUI 配置中填写
   `base_url=http://127.0.0.1:11434/v1` 和已安装模型名称；无需 API key。
   Ollama 本身及模型需要用户事先安装并启动，包不会自动下载模型。

真实模式可能产生模型费用；只有用户主动配置并选择后启用。
密钥仅保存在本地 GUI 配置文件，勿分享该文件或运行数据，勿提交 Git。
启动器不覆盖现有配置，也不继承主机 `NC_*` 生产绑定。
`--smoke-test` 只允许 demo，避免测试时误调用真实模型。
本地模拟 OpenAI HTTP endpoint 测试覆盖 Cognitive 读取配置、模型名、Authorization
及 `/v1/chat/completions` 实际请求；没有进行线上模型质量、连通性或费用实测。

MistyAI、QQ、NapCat、外部模型均不会由此入口启动。生产部署请参阅 `LINUX_DEPLOYMENT.md`
及现有共享启动文档，并先满足明确的生产配对契约。

## CI 边界与验证

`ci` 独立运行完整 Python 测试与静态检查、现有 Windows 启动器测试，构建同一个源码 ZIP，
分别在 Linux 和 Windows 从解压包执行真实 shell/bat smoke 两次，验证四服务 HTTP 就绪、
中文/空格路径、非仓库工作目录、配置与数据保留及错误退出码。smoke 不调用付费模型或 QQ。
只有上述检查通过才上传 `neuralcloud-standalone-online`；失败时下载 smoke 日志排查。
本地可执行 `bash deploy.sh --smoke-test` 或 `deploy.bat --smoke-test`，成功后自动停止。

`production-pair` 是独立且仍然严格的集成门禁：未设置完整、已发布且契约匹配的
`MISTYAI_PRODUCTION_REVISION` 时失败，不能将其跳过当作通过。
它保留生产契约锁校验和真实跨进程集成检查，不发布生产配对包。
独立包成功不能证明生产配对成功。没有替换为任意 main SHA，也未修改另一仓库。

源码包只包含允许的 Git 跟踪文件，不包括 .env、用户密钥、日志、数据库、运行目录或个人 launch.json。
不应把任何私人文件提交到源码允许目录。锁定依赖安装后才运行，包中的 manifest 不包含运行数据。
