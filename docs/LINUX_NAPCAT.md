# Linux 独立 NapCat 桥

`tools/deploy/install-napcat.sh` 为 NeuralCloud 准备一个独立 Linux QQ/NapCat 实例。默认仅安装文件和 `neuralcloud-napcat.service`，不启动、不启用开机启动，不启动 QQDirect，不修改 QQDirect 的暂停/发送授权。只有显式 `--start` 才启动本桥；首次登录通常需手机 QQ 扫码确认。

## 固定版本及隔离

- Python 3.12+、Linux x86_64；Ubuntu 24.04 是已选择的部署目标。
- NapCat `v4.18.28` 的官方 `NapCat.Shell.zip`：SHA-256 `bcdd8bdb9e44bd0cf6a90908e572141787fd9e98cb8d8eecc5adf25bbdcabb94`，29,498,670 字节。
- LinuxQQ `3.2.23-44343` amd64 `.deb`：SHA-256 `a4252719c1beb8adce0da09ebfc310ce50c79ea548f5cce429505765d0bfba84`，171,560,322 字节。该版本是 NapCat 发布页列出的兼容构建。旧官方下载 URL 可能失效，优先提供已有原始包；程序不会退回 latest 或跳过摘要校验。
- 两个摘要来自本次实际制品校验，不代表上游签名验证。重新获取制品后仍须核对；版本升级应单独测试并显式更新版本/摘要。
- 专用服务用户 `neuralcloud-qq`，HOME `/var/lib/neuralcloud-qq`，QQ 会话默认位于其 `.config/QQ`。程序独立安装到 `/opt/neuralcloud-napcat/releases/`；代码与 QQ 包元数据归 root 管理，运行用户只写自己的 HOME 和状态目录。
- `NAPCAT_WORKDIR=/var/lib/neuralcloud-qq/napcat`，独立保存 `config/`、`cache/`、`logs/`、`plugins/`。OneBot 为 `127.0.0.1:6301`，WebUI 为 `127.0.0.1:6799`。
- 不安装系统 `linuxqq` 包、不改 `/opt/QQ`、不调用已有 `qq` 命令、不停止已有 NapCat 服务。本次已有的另一个 QQ 账号及其服务使用原路径和端口继续运行。

安装后 `/opt/neuralcloud-napcat/release-manifest.json` 记录版本、摘要、上游 URL、账号、用户、路径、端口和 unit 名称，可复制到整个 NeuralCloud 发布记录。令牌值不会进入这个清单或命令行。

## 准备与检查

令牌输入必须是两个不同的、至少 32 字符的非空白 ASCII 令牌，各放在绝对路径的私有文件中，权限 `0600`。一个用于 OneBot，另一个用于 WebUI；不要复用 Runtime control/service 凭据。安装器读取文件并将对应值写入受限的 NapCat JSON，保留输入文件不变。

以下示例使用本次迁移的账号；通用部署通过 `--account` 指定自己的账号，不在脚本中保存群或好友名单。

```bash
sudo bash tools/deploy/install-napcat.sh \
  --account 2015188508 \
  --qq-deb /home/qiuyu/napcat/linuxqq_3.2.23-44343_amd64.deb \
  --onebot-token-file /var/lib/neuralcloud/secrets/onebot.token \
  --webui-token-file /var/lib/neuralcloud/secrets/napcat-webui.token \
  --check
```

`--check` 校验参数、输入令牌和已提供的制品摘要，打印脱敏计划；不下载、不写文件、不操作服务。它不等于运行验收。省略 `--qq-deb` 时按固定 URL 下载；提供 `--napcat-zip /absolute/NapCat.Shell.zip` 可完全离线安装。覆盖下载镜像 URL 仍必须使用 HTTPS 并通过固定 SHA-256 校验。其他 QQ 包必须同时给出匹配的 `--qq-version` 与 `--qq-sha256`；安装时检查 `.deb` 实际版本、amd64 架构和最低兼容 build 40768。

去掉 `--check` 即准备安装。机器已有 `xvfb-run`、`xauth`、`dpkg-deb`、`ldd` 和 QQ 所需动态库时，无需包管理器操作。Ubuntu 24.04 缺少这些依赖时，可增加 `--install-dependencies`，只安装相关运行库与 Xvfb，不安装或升级系统 QQ。

## 启动、扫码与切换

先保证目标 QQDirect 保持暂停。用同一安装命令增加 `--start`，仅启动新 NapCat 服务；再增加 `--enable-on-boot` 才启用该服务开机启动。

```bash
# 在操作员电脑建立隧道，SSH 的主机、端口按部署环境填写。
ssh -N -p 54849 -L 6799:127.0.0.1:6799 qiuyu@frp-fan.com
```

通过浏览器打开 `http://127.0.0.1:6799/webui`，用私有 WebUI 令牌进入并扫码确认正确账号。服务使用 `-q <account>` 尝试复用自己的已有会话；快速登录失败仍需扫码。服务 stdout 不记录二维码或 WebUI 登录 URL，操作员使用隧道访问 WebUI。

NapCat 按账号优先读 `onebot11_<account>.json`；安装器仅为指定账号写入有效 OneBot 配置，通用 `onebot11.json` 保持网络接口关闭。不能只改通用文件而忽略账号文件。配置要求正向 WebSocket、Bearer token、array 消息格式、自身消息事件、`enableLocalFile2Url=true`；原 Docker 的内部 `3001/6099` 端口不可直接套用到宿主机。

也可使用私有登录辅助。默认仅查询状态；只有 `--refresh` 才刷新并输出二维码文件，命令输出不含登录令牌或二维码 URL：

```bash
sudo /opt/neuralcloud/current/.venv/bin/python /opt/neuralcloud/current/tools/deploy/napcat_login.py
sudo install -d -m 0700 /var/lib/neuralcloud-login
sudo /opt/neuralcloud/current/.venv/bin/python /opt/neuralcloud/current/tools/deploy/napcat_login.py \
  --refresh --qr-output /var/lib/neuralcloud-login/qq-login.png
```

之后按迁移流程分别验证：登录账号、指定群/好友存在、OneBot 认证与真实消息读取、图片理解、QQDirect 新鲜心跳。旧发送方完全停止并完成账本对账后，才按 NeuralCloud 迁移流程恢复目标 QQDirect 发送。安装成功、systemd active 或出现二维码都不证明收发链路已经就绪。

## 重复运行与恢复

相同参数、制品、令牌和私有配置可重复运行，保留已有登录数据；不会默认重启正在运行的服务。prefix、unit、用户身份或关键配置不匹配时拒绝接管，不覆盖，不停止其他服务。NapCat 自身补充的默认字段不视为冲突，但绑定端口、凭据、网络服务器列表和消息格式必须保持一致。

升级、换账号或修改令牌不通过隐式重装处理：先单独停本服务并备份 HOME、NAPCAT_WORKDIR 与 release manifest，再审阅新发布配置和账本迁移。需要停止时只操作 `sudo systemctl stop neuralcloud-napcat.service`；需要取消自启时另执行 `sudo systemctl disable neuralcloud-napcat.service`。不要删除尚需恢复的 QQ 会话、协议账本或图片目录。

## 官方参考

- [NapCat v4.18.28](https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.28)
- [Linux Shell 部署方式](https://napneko.github.io/guide/boot/Shell)
- [Xvfb 与 systemd 部署示例](https://napneko.github.io/guide/boot/Shell-Linux-SemiAuto)
- [官方 rootless 安装器源码](https://github.com/NapNeko/NapCat-Installer/blob/main/script/install.sh)：本脚本只采用独立目录解包方式；不运行其可能卸载系统 QQ 的入口，也不使用其 latest 下载策略。
- [固定版本的 NAPCAT_WORKDIR 实现](https://github.com/NapNeko/NapCatQQ/blob/v4.18.28/packages/napcat-common/src/path.ts)
- [固定版本的账号配置优先级](https://github.com/NapNeko/NapCatQQ/blob/v4.18.28/packages/napcat-core/helper/config-base.ts)
