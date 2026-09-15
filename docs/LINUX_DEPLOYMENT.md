# Linux 迁移与部署手册

本文适用于将现有 Windows `shared` 实例迁到单台 Linux，也适用于后续 Linux 更新、备份和恢复。推荐拓扑是四个独立 Python 服务加 systemd；QQDirect/NapCat、Board 按需加入。当前共享实例使用文件和 SQLite 保存状态，迁移不要求 PostgreSQL、GPU 或 Docker。

**状态口径：部署工具已实现；首次远端安装已进入实机验证。正式数据切换、QQ 收发、公网入口、恢复演练和长期运行验收仍须由当次执行记录确认。本文是操作手册，不是上线验收报告。**

## 1. 目录、服务与配置

以 [`deploy/linux.example.json`](../deploy/linux.example.json) 为模板，把实例配置保存到 `/etc/neuralcloud/deploy.json`。模板只包含路径和参数，私有模型配置、令牌、QQ群/好友名单另行迁移。

- `/opt/neuralcloud/releases/<release-id>/`：带清单和哈希的代码，以及该版本独立的 `.venv`。
- `/opt/neuralcloud/current`：指向当前 release 的受控符号链接。状态、配置、令牌及数据库路径自身不能是符号链接。
- `/etc/neuralcloud/`：`deploy.json`、`gui-settings.json`、服务令牌与 QQ/Board 客户端配置。
- `/var/lib/neuralcloud/`：`presets/`、`cognitive/`、`homeostasis/domain/`、`runtime/`、共享记忆和可选 QQ/Board 状态。
- `/var/log/neuralcloud/`：推理 trace 和调用记录；systemd 进程输出另见 journal。
- `/var/backups/neuralcloud/`：只读验证通过的状态快照。

四核心端口默认只监听 `127.0.0.1`：GUI `8790`、Runtime `8770`、Cognitive `8771`、Homeostasis `8772`。可选 Board 为 `8793`；NapCat OneBot 为 `6301`、管理界面为 `6799`。`8766` 保留给旧 QQ Agent，不用于新 GUI。

`enabled_components` 必须包含 `cognitive`、`homeostasis`、`runtime`、`gui`，可增加 `board`、`qq_direct`。启用可选组件时补齐实际配置绑定：

```json
{
  "NC_QQ_SESSION_CONFIG": "/etc/neuralcloud/session.json",
  "NC_QQ_WATCHER_CONFIG": "/etc/neuralcloud/watcher.json",
  "NC_QQ_DIRECT_CONFIG": "/etc/neuralcloud/direct.json",
  "NC_BOARD_CONFIG": "/etc/neuralcloud/board-client.json",
  "NC_BOARD_STATE_DIR": "/var/lib/neuralcloud/board",
  "NC_BOARD_TOKEN_FILE": "/etc/neuralcloud/board.token"
}
```

这些键属于模板的 `bindings` 对象。没有使用某项能力时省略对应绑定。Linux 私有 JSON 和 token 使用 `0600`，状态/配置目录使用 `0700`，归运行账户所有；Windows staging 应使用仅当前账户和 SYSTEM 可读的 NTFS ACL，POSIX chmod 不能替代该设置。`service_user` 默认 `neuralcloud`，不得使用 root。跨服务内部令牌与 Runtime 控制令牌必须不同。

辅助模型的上下文窗口可通过 `bindings.NC_AUX_LLM_NUM_CTX` 配置，例如字符串 `"65536"`；接受不小于 1,024 的整数或整数字符串。应用启动器会清除外部 `NC_*` 环境变量，因此必须使用正式绑定，不能只在 systemd 中添加同名环境变量。Ollama 的 `OLLAMA_CONTEXT_LENGTH` 和内存限制需配套设置；重启后分别核对 Cognitive 的 `inference_config.aux_llm_num_ctx` 与 Ollama `/api/ps` 的 `context_length`，避免仅配置文件变更而实际窗口未生效。窗口扩大不改变模型或自动取消主模型的容量路由。

## 2. 先准备代码和独立演示环境

### 2.1 固定代码并打包

源端在已准备好依赖的仓库执行；release ID 每次唯一：

```powershell
.\.venv\Scripts\python.exe -m harness.deploy package `
  --source . --output C:/migration/neuralcloud-20260915-01.tar.gz `
  --release-id 20260915-01
```

`package` 包含允许的源码、内置 preset、协议、部署脚本和 `uv.lock`，也包含当时的未提交源码修改；不会打包 `.venv`、运行数据和普通私有配置。保存返回的 archive SHA-256、release ID，以及另行记录的 Git commit/工作区说明。代码发布包和私有状态快照分别传输到目标机器。

### 2.2 Linux 前置条件和安装

目标机需要 systemd、**系统 Python 3.12**、`uv` 和出站 HTTPS。当前依赖安装固定执行 `uv sync --locked`，禁止静默更新 lockfile，也不自动下载 Python。使用 Linux x86_64 NapCat 安装器时，参考第 5 节的附加要求。

管理命令须在可信的 bootstrap checkout 或已安装 release 中运行。首次机器可先准备专用 bootstrap checkout 和 Python 环境；下面以其 `.venv/bin/python` 为例。创建运行账户与放置已审核的配置后执行：

```bash
sudo useradd --system --user-group --home-dir /var/lib/neuralcloud --shell /usr/sbin/nologin neuralcloud
sudo install -d -m 0700 /etc/neuralcloud
sudo install -m 0600 deploy/linux.example.json /etc/neuralcloud/deploy.json

sudo /opt/neuralcloud/bootstrap/.venv/bin/python -m harness.deploy \
  --config /etc/neuralcloud/deploy.json preflight

sudo /opt/neuralcloud/bootstrap/.venv/bin/python -m harness.deploy \
  --config /etc/neuralcloud/deploy.json install \
  --archive /srv/migration/neuralcloud-20260915-01.tar.gz --uv /usr/local/bin/uv
```

账户创建命令只用于尚无该账户的新主机；`uv` 路径应替换成该机实际路径。`preflight` 检查 Linux、Python、systemctl、uv 和端口占用；失败返回非零。它不验证已迁移业务数据。

`install` 解包不可覆盖的 release、创建持久目录、设置账户和令牌权限、同步锁定依赖并安装 systemd unit；**不会切换 current 或启动业务**。已有令牌会保留。`--skip-sync` 仅用于暂存源码，缺少 `.venv/bin/python` 时禁止激活。

### 2.3 演示检查必须隔离

首次先使用独立 `state_root`、`config_root`、端口与演示 preset，设置 `mode: "demo"`、`development: true`，使用 `presets/runtime/development-demo.json` 和 `presets/homeostasis/functional-proxy-v1.json`，不配置生产 QQ/Board 绑定。验证四服务启动、停止和重启后，再处理真实状态。

`--core-only` 只是不启动可选 unit；它不是模拟模式，不会阻止 Runtime 写状态或使用已有外部配置。不要对生产状态目录做演示启动。

## 3. Windows 共享状态清单与停写快照

### 3.1 生成路径清单

源码不是全部状态。当前 `shared` 的实际绑定来自 `launch.json`，通常跨越仓库 `data/` 和 `%LOCALAPPDATA%/NeuralCloud/`。在源 Windows 生成清单：

```powershell
.\.venv\Scripts\python.exe tools/deploy/export_shared.py inventory `
  --repo C:/Projects/NeuralCloud --output C:/migration/sources.json `
  --extra-source board-state=C:/migration/wsl-board=state/board
```

不使用 Board 时省略 `--extra-source`。可用 `--launch`、`--profile`、`--linux-config-root`、`--linux-state-root` 改变源配置及目标目录。清单输出必须是新文件，以免覆盖已审核版本。

生成的 JSON 包含：

- `sources`：每项的 `role`、源 `path`、相对 `target`、`required`、`rewrite_config`、`reference_only`。
- `path_rewrites`：源绝对路径或目录前缀到目标 Linux 绝对路径的映射。
- `config/*` 对应 `/etc/neuralcloud/*`；`state/*` 对应 `/var/lib/neuralcloud/*`。

必须携带工作 preset、Cognitive state/control、共享记忆库、Homeostasis snapshot/ledger、Runtime journal/checkpoint/能力/目标账本、QQ 原始记忆与交付账本、QQDirect evidence/images、私有配置和所引用令牌。Runtime 的 `web/` 中还可能存在联网配置、密钥和版本。

`reference_only` 只提供路径映射，不复制文件：包括新端应重新生成的 Direct/watcher/desktop status，以及 Homeostasis 父目录。用户可追加 `SourceSpec` 或修改清单，将所有实际外置数据纳入；不能以“清单生成成功”代替停写和源文件存在检查。

### 3.2 单独导出 WSL Board

Board 客户端配置不包含服务端数据。现有 WSL 部署还可能把真实 `board.sqlite3`、`service.token`、部署元数据放在 Linux 用户私有目录，而非 Windows 的 `Board/` 客户端目录。

先在实际 WSL distro 内查明服务 unit/进程和数据位置，停止该 Board writer。再将整个真实数据目录导出到新的 Windows staging 目录，例如 `C:/migration/wsl-board`，并作为 `--extra-source` 加入。若只复制一个数据库，须使用 SQLite backup API 或确认停写后处理完整 WAL，不能漏掉已提交数据。不要把未经验证的空数据库当作成功迁移。

Board 源服务端 token 与客户端 token 必须相同；目标 `NC_BOARD_TOKEN_FILE` 应绑定到恢复后的实际文件，或同时更新两端引用。跨 WSL 和 Windows 的快照必须处于同一停写窗口。

### 3.3 停止所有 writer 后才能导出

维护窗口先关闭公网/GUI 新输入，再停止 QQDirect、桌面 watcher 等新事件来源，排空在途动作和 Cognitive maintenance，最后停止四核心与外部 Board/body。Windows 常用入口：

```powershell
.\tools\qq\Stop-QQDirect.ps1
.\tools\qq\Stop-QQWatcher.ps1
.\launch.ps1 -Command down
.\launch.ps1 -Command status
```

上述命令不自动停止独立 WSL Board、NapCat、桌面 body、QQ session 或维护导入任务。逐项检查已知 unit、进程、端口及状态；未决发送应记录并对账，不能盲目重试。进程退出失败、maintenance 排空失败或身份无法核实时，不进行正式快照。

之后才能执行：

```powershell
.\.venv\Scripts\python.exe -m harness.deploy.snapshot backup `
  --sources-manifest C:/migration/sources.json `
  --output C:/migration/snapshot-20260915-01 `
  --writers-stopped --desktop-status stopped

.\.venv\Scripts\python.exe -m harness.deploy.snapshot verify `
  --snapshot C:/migration/snapshot-20260915-01
```

`--writers-stopped` 是操作者对全部 writer 已停止的确认，不会停止任何服务；`--desktop-status stopped` 另行明确桌面已停。只有从未配置桌面的一般部署才使用 `not-configured`。文件前后漂移检测是补充检查，不能证明仍运行的进程永远不会写入。

导出对 SQLite 使用 backup API 合并已提交 WAL，不复制 WAL/SHM；源文件在导出期间变化则拒绝快照。读取干净的 WAL 模式数据库可能自行产生空 WAL/SHM，这些无数据 sidecar 不算 writer 变化；非空 WAL 仍参与漂移检查。失败报告只列出角色、路径及新增/删除/修改类型，不打印数据内容。PID、运行锁、临时文件和旧 status 不进入 payload。不要在仍运行的源端手工删除锁或 WAL。跨主机的本地文件锁不能阻止双主，迁移全过程保持唯一活跃 writer。

## 4. 校验、恢复与首次激活

快照结构是 `manifest.json`、`manifest.sha256` 和 `data/`。清单记录每个文件的相对路径、角色、类型、大小及 SHA-256；SQLite 内容以逻辑备份为准。配置只对明确 JSON 路径字段做结构化重写，历史 journal、receipt、preset 和数据库记录保持原样。

目标 Linux 使用任一可信部署环境先校验、再恢复到**全新 staging 目录**：

```bash
python -m harness.deploy verify --snapshot /srv/migration/snapshot-20260915-01
python -m harness.deploy restore \
  --snapshot /srv/migration/snapshot-20260915-01 \
  --destination /srv/migration/restore-20260915-01
```

`verify --snapshot` 只读检查清单/文件哈希、SQLite `integrity_check`、journal 哈希链及 checkpoint 对应关系，不构造会修剪损坏尾部的正常 Journal/Store。`restore` 拒绝已有目标、路径穿越、符号链接、Windows reparse point，以及源目标重叠；不会覆盖生产目录、修改部署绑定或重启业务。

复核 staging 后，在所有 writer 停止时，将 `config/` 和 `state/` 分别安装到配置中的实际持久根。原目标目录若有数据，先完整封存；不要把新旧数据库和旧 WAL 混合覆盖。最后设置运行账户、`0700` 目录和 `0600` 文件。若不采用默认目录，要先在源清单中改变映射，再重新导出。

### 必须复核的目标配置

- `deploy.json` 的所有绑定与恢复目录一致；Windows `%LOCALAPPDATA%`、盘符、反斜杠路径不再作为活动文件路径。
- `session.json` 的模型配置、preset、记忆库及 Direct status 路径；`watcher.json` 的 Homeostasis 父目录；`direct.json` 的 `state_db`、`status_file`、`control_file`、`desktop_status_file`、证据、图片和 token 路径。
- `127.0.0.1` 指向目标 Linux。Windows GUI Agent/body 留在原机时不会因 SSH 登录自动可用。当前核心直接读取 QQ 原始库、Direct 库和 status，不能只转发 HTTP 就把数据库 writer 留在另一台机器，也不要用网络盘共享活跃 SQLite。
- `desktop_status_file` **不能留空或复制旧运行状态**。确认 Windows 桌面 writer 已停止后，在目标配置指定位置生成新 JSON，内容至少是 `{"running": false, "heartbeat_at": "<当前UTC ISO时间>"}`。文件缺失或不可解析时，QQDirect 会按桌面仍活跃处理并阻止自动发送。
- Windows 停止 QQDirect 可能持久设置 `control_file` 中的 `stop: true`。快照保留此事实；仅在目标恢复副本内，准备启动时显式改为 `{"stop": false, "paused": true}`。登录、身份和授权核验完成后再设置 `paused: false`，以免新进程立即退出或提前发送。

所有依赖就绪、入口仍处维护状态时激活指定 release：

```bash
sudo /opt/neuralcloud/bootstrap/.venv/bin/python -m harness.deploy \
  --config /etc/neuralcloud/deploy.json activate --release 20260915-01

sudo /opt/neuralcloud/current/.venv/bin/python -m harness.deploy \
  --config /etc/neuralcloud/deploy.json status

sudo /opt/neuralcloud/current/.venv/bin/python -m harness.deploy \
  --config /etc/neuralcloud/deploy.json verify
```

不带 `--snapshot` 的 `verify` 检查当前代码 release、manifest/profile、控制模式、令牌和私有配置存在性，不替代快照完整性或业务验收。

`activate` 先停旧 writer，再切 `current`，按 Cognitive/Homeostasis、可选 Board、Runtime、GUI、QQDirect 的顺序启动并等待健康。恢复若处于 `PAUSED`，不会偷偷 resume；先检查暂停原因、待处理事件和交付账本。激活失败也不会自动恢复旧快照，因为启动恢复可能已经写入新状态。

## 5. QQDirect、NapCat 与登录

NeuralCloud QQDirect 是读取 OneBot 事件并调用共享认知的 worker；NapCat 是独立 QQ 客户端桥。`harness.deploy install/start/stop` 不代表 NapCat 已安装、已登录或已停止。

[`tools/deploy/install-napcat.sh`](../tools/deploy/install-napcat.sh) 安装独立 Linux x86_64 实例，固定 NapCat 与 QQ 包版本和 SHA-256，使用独立非 root 账户、目录、xvfb 与 systemd。先准备不同的 OneBot 和 WebUI 私有 token 文件，然后检查：

```bash
sudo bash tools/deploy/install-napcat.sh \
  --account QQ_ACCOUNT \
  --onebot-token-file /etc/neuralcloud/secrets/onebot.token \
  --webui-token-file /etc/neuralcloud/secrets/napcat-webui.token --check
```

把 `QQ_ACCOUNT` 替换成实际 QQ 账号。去掉 `--check` 才安装；需要由脚本安装 Ubuntu 24.04 的 xvfb/图形库时显式加 `--install-dependencies`。只有显式 `--start` 才启动；`--enable-on-boot` 还要求 `--start`。离线包可用 `--qq-deb` 和 `--napcat-zip` 指定，摘要仍须匹配。不要复用旧系统 QQ 的安装目录。

桥默认监听 `127.0.0.1:6301` 和 `127.0.0.1:6799`；`direct.json.ws_url` 使用前者。通过 SSH 本地转发访问管理界面完成手机扫码，不公开 WebUI 或 OneBot token：

```bash
ssh -p SSH_PORT -L 16799:127.0.0.1:6799 SSH_USER@SSH_HOST
```

本地浏览器打开 `http://127.0.0.1:16799/webui`。登录成功后核验 OneBot 实际自账号、群/好友名单、事件接收、历史读取和 token；再启用目标 Direct 控制文件。发送能力由现有群/好友授权名单约束，迁移不扩大名单。出现未知交付时先 reconcile，不以重启或清空账本换取重发。

新设备可能要求扫码或额外验证，复制数据目录不保证免登录。已有 bridge/QQ Linux 客户端数据应单独封存；QQ 自带的加密 `.db` 无法用普通 SQLite backup API 读取，须在原 writer 全部停止后先按原字节封装 ZIP，记录内部相对路径、大小及 SHA-256，并回读验证。保留加密库的 WAL/rollback journal，仅排除进程 PID 和运行锁；不要把业务 SQLite 的备份例外推广到其他数据库。ZIP 输出在源目录之外，封包前后检查源清单是否变化。

然后以 `--extra-source qq-login-archive=C:/migration/qq-login-archive.zip=state/qq-login-archive.zip` 加入普通快照，外层再次校验整个 ZIP 的哈希。归档可能包含历史和登录凭据，应使用同样的私有权限；恢复时保持封存，不覆盖新 NapCat 的 HOME，也不当作加密库逻辑校验或已登录证明。Windows 桌面 watcher 与新 QQDirect 不得同时自动发送；纯协议迁移后，旧桌面相关 body/Agent 可以保持停用。

## 6. 公网入口与 FRP

公网访问链路建议是 **HTTPS 域名 → FRP 隧道 → Linux 环回认证代理 → GUI 8790**。不要把 Runtime/Cognitive/Homeostasis、OneBot 或 NapCat WebUI 直接映射到公网。

在 `deploy.json.public_origin` 填最终的完整 HTTPS origin，例如 `https://neuralcloud.example.org`。同一值用于 GUI 的 WebSocket/Origin 检查及代理 Host 校验；内部服务仍使用环回 HTTP。SSH 的域名或端口不等于已经可用的公众网站域名。

项目提供 Caddy 配置渲染器，默认代理端口 `8879`：

```bash
python -m harness.deploy.ingress --config /etc/neuralcloud/deploy.json \
  --hash-file /etc/neuralcloud/ingress-password.hash \
  --output /etc/caddy/neuralcloud.caddy --maintenance
```

`--hash-file` 保存预先生成的 bcrypt 密码摘要，不能填明文密码。维护配置返回 `503`；验收完成后去掉 `--maintenance` 重新生成并由 Caddy 的实际管理方式加载。已设定 origin 时代理校验 Host、要求 Basic Auth，转发时去掉外部 Authorization，并固定转发协议为 HTTPS。

该渲染器不会安装 Caddy、申请域名/证书、配置 FRP 控制端或重载系统服务。FRP provider 的具体配置按账号实际提供的 TLS/自定义域名能力处理；上线前必须从外部网络验证 HTTPS、未认证拒绝、WebSocket 正常、错误 Origin 被拒绝，以及后端控制端口不可直接访问。没有确定域名时保留维护入口，或先通过 SSH 隧道访问 GUI。

已有可信 Caddy 二进制时，可用安装器准备专用用户、私有 Caddyfile 和 `neuralcloud-proxy.service`。安装器会先校验配置，再以实际服务用户复验，默认不启动：

```bash
sudo bash /opt/neuralcloud/current/tools/deploy/install-ingress.sh \
  --hash-file /etc/neuralcloud/ingress-password.hash --maintenance
sudo systemctl enable --now neuralcloud-proxy.service
```

业务验收完成后，去掉 `--maintenance` 再运行安装器，并显式 `systemctl restart neuralcloud-proxy.service`。代理仅监听回环地址；站点地址使用 `http://:8879` 配合 `bind 127.0.0.1`，避免把公网 Host 错配为回环 Host。响应头禁止外部网页嵌入管理界面。

### 独立 FRP systemd 示例

SakuraFrp 可使用 [`tools/deploy/frp_runner.py`](../tools/deploy/frp_runner.py)：它读取已有 launcher 私有配置中的 `token`，仅通过 `NATFRP_TOKEN`/`NATFRP_TARGET` 环境传给指定官方客户端，不把 token 放进命令参数。先在平台创建专用于网站的新隧道，配置本地 `127.0.0.1:8879`、公众域名及 HTTPS 证书，再填写实际 tunnel ID。

下面是可调整的 `/etc/systemd/system/neuralcloud-frp.service` 示例；账户、官方客户端位置、只读 launcher 配置及状态目录须预先准备：

```ini
[Unit]
Description=NeuralCloud website FRP tunnel
After=network-online.target neuralcloud-proxy.service
Wants=network-online.target

[Service]
Type=simple
User=neuralcloud-frp
Group=neuralcloud-frp
WorkingDirectory=/var/lib/neuralcloud-frp
ExecStart=/usr/bin/python3.12 /opt/neuralcloud/current/tools/deploy/frp_runner.py --launcher-config /etc/neuralcloud-frp/launcher.json --client /opt/sakurafrp/frpc --tunnel-id 12345
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

将 `12345` 换成已创建的网站 tunnel ID；配置文件须仅允许运行账户读取。部署此独立 unit 后 `systemctl daemon-reload`，再显式 `systemctl enable --now neuralcloud-frp.service`。保留现有 SSH FRP 服务和原映射，网站隧道不复用或重启 SSH 的客户端进程。平台已分配域名/证书或 FRP 已连接，仅证明路由准备完成，不能替代 GUI/认证/实时通信验收。

## 7. 日常更新、备份与回滚

在已激活 release 上执行管理命令时使用其完整 Python 路径；systemd 生命周期命令需足够权限。

```bash
sudo /opt/neuralcloud/current/.venv/bin/python -m harness.deploy --config /etc/neuralcloud/deploy.json stop
sudo /opt/neuralcloud/current/.venv/bin/python -m harness.deploy --config /etc/neuralcloud/deploy.json status
sudo /opt/neuralcloud/current/.venv/bin/python -m harness.deploy --config /etc/neuralcloud/deploy.json start
```

普通更新流程是 `package → install → 停写快照 → activate → verify/status → 业务检查`。每次保留旧 release，代码和数据分别记录恢复点。`stop` 会拒绝接管同名但不由部署工具管理的 unit；失败必须先处理，不继续切换。

Linux 日常备份在停止四核心和所有同状态 writer 后执行：

```bash
sudo /opt/neuralcloud/current/.venv/bin/python -m harness.deploy --config /etc/neuralcloud/deploy.json backup \
  --output /var/backups/neuralcloud/manual-20260915-01 \
  --writers-stopped --desktop-status stopped
sudo /opt/neuralcloud/current/.venv/bin/python -m harness.deploy verify --snapshot /var/backups/neuralcloud/manual-20260915-01
```

该路径完整保存 `state_root`、`config_root`，并纳入实际 release 的 manifest/profile 和外置绑定；自定义外部状态仍应在 source manifest 中明确追加。备份含密钥和私有对话，异地副本使用受控传输与私有存储。只读校验通过后才按原运行范围恢复服务。

代码回滚保留当前状态：

```bash
sudo /opt/neuralcloud/current/.venv/bin/python -m harness.deploy --config /etc/neuralcloud/deploy.json rollback --release PREVIOUS_RELEASE_ID
```

省略 `--release` 时读取上次成功激活记录中的 previous release。`rollback` 只切代码，不回滚 SQLite、journal 或 preset；使用前确认旧代码兼容现有状态格式。

若确需恢复历史数据，先停新端所有 writer，备份最新状态，再 `restore --destination NEW_DIR`。新端已发生对话或 QQ 发送时，直接切回旧快照可能丢失记忆或重复交付；须评估新增数据并完成对账。`restore` 始终不覆盖生产目录，也不会自动执行“数据库降级”。

### 可选定时备份

基础 `backup` 命令不自动停服务；可选 [`harness.deploy.backup_job`](../harness/deploy/backup_job.py) 提供完整作业：只有四核心原本健康且 Runtime 为 `RUNNING` 才执行，暂停/停止中的实例会跳过而不被唤醒。作业记录原运行范围，停止已运行的独立入口代理、QQDirect、四核心及 Board，导出并校验快照，再按原范围恢复。任何阶段失败都会阻止自动恢复流量，错误及原运行范围记录于 `backup_root/scheduled-last-run.json`。

```bash
sudo /opt/neuralcloud/current/.venv/bin/python -m harness.deploy.backup_job \
  --config /etc/neuralcloud/deploy.json \
  --offsite-dir /mnt/private-backups/neuralcloud
```

`--offsite-dir` 是已挂载/准备好的第二存储目录，省略则只保留本地副本；工具不自行创建远端账号、SSH 隧道或存储加密。独立代理默认 `neuralcloud-proxy.service`，可用 `--proxy-unit` 指定应用专用 unit；无代理时用 `--no-proxy`。可重复 `--extra-writer-unit neuralcloud-napcat.service` 纳入额外应用 writer；默认不停止 NapCat，其独立 HOME/登录数据也不属于默认核心快照。

无人值守作业要求迁移后移除活动 `NC_QQ_WATCHER_CONFIG` 绑定，并会拒绝明确报告运行中的 desktop 状态；它不能替另一台主机证明桌面已停止。自定义源和未由作业管理的 writer 应使用人工停写流程，或先扩展作业并验证完整停止范围。

保留策略只处理校验有效且名称为 `scheduled-...` 的直接子目录：最近 7 个有快照日期各保留最新一份，同时保留当前及之前 3 个自然周每周最新一份。手工快照和校验失败快照不会被该策略删除。

提供 [`deploy/neuralcloud-backup.service`](../deploy/neuralcloud-backup.service) 与 [timer 模板](../deploy/neuralcloud-backup.timer)，默认每天 `Asia/Shanghai 03:30`，`Persistent=true` 补触发错过的调度。`install` **不会自动安装或启用这两个模板**。先验证一次完整手工作业和恢复，再按实际配置/目录编辑模板，显式安装：

```bash
sudo install -m 0644 deploy/neuralcloud-backup.service /etc/systemd/system/neuralcloud-backup.service
sudo install -m 0644 deploy/neuralcloud-backup.timer /etc/systemd/system/neuralcloud-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now neuralcloud-backup.timer
sudo systemctl list-timers neuralcloud-backup.timer
```

这一步会创建真实周期任务；是否在某台主机启用、最近成功时间及异地副本结果，以该主机执行记录为准。

## 8. 验收与故障检查

首迁至少保存以下独立证据：代码包 hash/release、源停写状态、源清单和快照 hash、目标离线校验、四核心冷启动/重启、Homeostasis 与 Cognitive 游标、preset/共享记忆数量、journal 连续性、未决交付对账、Board 原有条目、QQ 登录与授权范围内的真实收发、公网认证/Origin，以及一次恢复到新目录的演练。

```bash
sudo journalctl -u neuralcloud-runtime.service -n 100 --no-pager
sudo journalctl -u neuralcloud-cognitive.service -n 100 --no-pager
sudo systemctl status neuralcloud-qq-direct.service neuralcloud-napcat.service
```

- `PAUSED`：检查 Runtime 的真实暂停原因与维护恢复状态；不为了通过健康检查修改历史 journal。
- QQDirect 已连接但不自动发送：检查 `control_file`、`desktop_status_file`、群/好友 `send` 授权与未知交付账本。
- 缺少历史：首先核对实际 `NC_GUI_PRESETS`、Cognitive 路径和共享 SQLite，避免意外启动空目录。
- 域名能打开但 WebSocket 失败：核对 `public_origin`、浏览器 Origin、代理 Host/Upgrade 与 FRP 路由。
- 快照失败：保留源文件，处理未停 writer 或损坏原因后重新导出。不要让验证器修数据，也不要忽略失败返回码。

工程测试入口：`python -m pytest tests/test_linux_deploy.py tests/test_deploy_snapshot.py tests/test_deploy_ingress.py tests/test_deploy_backup_job.py -q`。测试覆盖部署与数据安全约束，不能替代真实登录、模型调用、消息交付或长期运行验收。
