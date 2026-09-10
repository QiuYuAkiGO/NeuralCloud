# QQ-DESKTOP-1 · Misty 真实 QQ 群聊验收

> 2026-09-10。用户已确认使用现有 Linux 环境、真实桌面操作及指定测试群。
> 当前状态：部署准备；真实登录、读群、发送、人物记忆均未验收。账号凭据不写入仓库。

## 目标与执行路径

Misty 从 Linux 桌面终端启动腾讯 QQ 客户端，通过实际窗口查看指定群聊，理解发言者和上下文，输入并核验中文回复；后续对话及进程重启后，仍能基于证据回忆群成员。

链路为：NeuralCloud 请求 → MistyAI 桌面终端 → QQ 窗口 → 截图/无障碍证据 → 群聊事件与人物身份 → Cognitive 记忆/回复 → QQ 桌面发送 → 外部回执 → commit。

每段都需要真实接线。终端启动命令的退出码只证明命令结束；截图哈希只证明证据完整性；模型声称“看到了”或 GUI 键盘动作成功，都不能单独证明群聊读取、发送成功或人物记忆正确。

## 当前核验结果

- 现有目标为 Windows ARM64 + WSL2 Ubuntu 24.04.4 / aarch64；`/opt/mistyai` 已存在，WSL 内 `qq`、`docker` 命令均不可用。
- Docker Desktop 正常启动尝试失败，后台日志报 `sailor-ingest.sock` 重命名时“系统无法访问此文件”，随后退出；引擎管道不存在。
- 在确认 Docker 进程未运行后，尝试仅将该 socket 改名备份，Windows 同样拒绝访问；原文件保留。未重置 Docker、删除卷或注销 WSL。
- MistyAI 基础 Compose 的 desktop 仅接入 `internal: true` 网络。`NetworkSpec.phase` 仅允许 `0`，应用及 agent/worker 可执行文件白名单没有 QQ。安装 QQ 包并不使当前环境能够登录。
- `BasicTerminalActionPlanner` 只操作配置的终端应用，ASCII 单行命令完成标记是其验收边界；它不是通用 QQ 窗口规划器。
- MistyAI X11 输入按当前键盘映射逐字符注入；中文字符没有完整支持，不能把 ASCII 终端测试记为中文回复测试。
- 无自动 QQ 桌面消息读取器，也无经过 GUI 发送回执验证的 QQ DeliveryBackend。

现场记录见 [qq-desktop-preflight-2026-09-10.json](evidence/qq-desktop-preflight-2026-09-10.json)。这些结果不替代 BR-1 尚未完成的现场测试。

## 已准备的部署入口

相邻 MistyAI 仓库新增 `deploy/qq/`，提供包校验、独立镜像构建上下文、QQ 启动器和部署前置检查。命令及实际工具验收以 `C:/Projects/MistyAI/deploy/qq/README.md` 为准。该目录是准备工具，不表示镜像已构建或部署。

本次实际准备结果：官方包为 `linuxqq 3.2.33-52892 / arm64`，`dpkg-deb -f` 和 SHA-256 核验通过；已生成 `C:/Projects/MistyAI/artifacts/qq/build-context/`。准备工具回归 **12 passed**，Ruff 与 Ubuntu `sh -n` 通过。`readiness.py` 返回 **exit 2 / not_ready**；Docker 镜像构建与 QQ 运行未执行。工具记录保存在同一 `artifacts/qq/` 下的 `checks.json`、`preparation-report.json`、`readiness.json`。

腾讯 [Linux 下载页](https://im.qq.com/index/#/linux) 当前配置为 Linux QQ 3.2.33，更新日期 2026-09-02，提供 ARM64 deb。版本依据为 [官网配置](https://qq-web.cdn-go.cn/im.qq.com_new/latest/rainbow/pcConfig.json)。当前官网链接需要先经过其公开下载签名流程，裸下载链接返回 403；使用官网流程下载的本地包，随后固定 SHA-256 和包架构。

Linux QQ 对外提供图形客户端。无物理显示器时可以沿用现有 Xvfb/X11；[Electron 官方说明](https://www.electronjs.org/docs/latest/tutorial/testing-on-headless-ci)描述了这类运行方式。`--force-renderer-accessibility` 是 [Chromium 的可访问性开关](https://www.chromium.org/developers/design-documents/accessibility/)，不能据此保证 QQ 暴露完整消息、群号或成员 ID，必须实际检查。

## 实施顺序与完成条件

1. 恢复现有 Docker 引擎并部署当前 MistyAI 改动，验证真实终端观察及动作闭环。仅源码测试通过不算部署成功。
2. 以独立配置增加联网桌面支持，统一 manifest、capabilities、Compose 的实际网络语义；不能在宣告 phase 0 时私自接通外网。安装正确架构 QQ 与中文字体，登记应用和可执行文件白名单、窗口 app ID 别名。
3. 验证 QQ 自身沙箱在现有容器限制下能运行，不默认加 `--no-sandbox` 或关闭整个容器隔离。验证中文输入、DBus/AT-SPI 及消息显示。
4. 从真实桌面终端启动 QQ，完成账号登录，核验登录账号和当前会话指定的群号。扫码、设备验证或验证码出现时由账号持有人完成。
5. 增加 QQ 桌面任务规划与消息读取：读群信息、进入群、读取可见消息、识别发言者和引用关系、去重、草拟回复、核对当前群再发送。每步重新观察；无法辨认时停止并保留未知。
6. 将有来源证据的群消息贯通 Runtime、Cognitive、检索、持久化和交付记录，再运行下列用例。

## 人物记忆接线要求

这些是待实现要求，现有单用户聊天语义保持兼容。

- 为群聊建立稳定 `session_id`；为已核验成员建立稳定 `speaker`/参与者身份，昵称、群名片作为可变别名。同名不合并，同人改名不新建人物；只见昵称而无法核验账号时保留临时身份。
- 入口贯通 `participants`、来源消息标识及 `evidence`。当前 `RuntimeSupervisor.submit_user_message()` 和 `PipelineCognitiveAdapter` 尚未把完整来源上下文传入 `InteractionEvent`。
- 对新群聊渠道显式设置参与者，避免 `default_event_participants()` 把每个成员自动关联到通用 `user`。规则 parser、state builder、关系目标及检索需要绑定真实发送者。
- `session_id` 当前主要隔离短期 transcript，不能假设长期记忆已按群隔离。检索明确区分人物事实的来源群、可跨群复用范围和当前群上下文。
- 已发回复继承群和实际接收者信息；只有独立核验的 QQ 回执才能作为成功 `DeliveryEvidence`。未知发送结果进入对账，不能自动重发或记为已发送。
- 重复屏幕、滚动、重新进入群不应重复记忆。缺少稳定消息 ID 时须明确 GUI 去重局限，不能仅靠“文本相同”删除不同人或不同时间的真实发言。
- 群消息是外部内容，不得因其中出现命令或要求而获得宿主操作权限。人物记忆保留事实来源、时间与修订，关系判断交由认知层。

现有 `SocialWorldService` 是受实验条件及人物数限制的研究路径，不能直接启用它就宣称通用 QQ 人物记忆完成。

## 现场验收案例

以下全部为待运行；模拟数据仅用于工程回归，不能转写为真实群成员的记忆。

- **QQ-01 启动与登录**：Misty 在自己的桌面终端启动 QQ；独立观察 QQ 窗口和已登录账号。保存动作 ID、前后截图及观察 ID。
- **QQ-02 定位群**：打开本次指定群，核验群号；同名群、搜索结果或仅窗口标题不够。登录失效或目标不明时不发送。
- **QQ-03 读取**：由测试参与者提供连续消息，核对正文、发送人、顺序及引用对象。区分“只见窗口”“有可见文字”“能正确解读消息”。
- **QQ-04 中文发送**：先发送一次简短接入测试回复，核对中文标点、发送位置和发送后的实际消息。输入框里有草稿不算发出；不得用 NeuralCloud 自己的聊天回显代替 QQ 回执。
- **QQ-05 多人区分**：两名测试成员分别提供不同事实，再交叉询问；应正确区分来源，不把两人都当作默认用户。
- **QQ-06 别名与同名**：一人修改群名片后仍保持身份；不同账号使用相同昵称时仍区分。无法从 GUI 核验账号时报告未知，不猜测合并。
- **QQ-07 重启回忆**：确认记忆提交后重启 NeuralCloud，继续询问此前事实；记录实际检索证据，不能靠测试问题复述答案。
- **QQ-08 更正与遗忘**：同一成员更正此前事实，后续使用新事实并保留来源；另一成员的同主题发言不能覆盖该人的事实。
- **QQ-09 群边界与引用**：切换至其他获授权测试会话时，短期群上下文不串用；引用他人发言不能当作当前发言者的自述。
- **QQ-10 去重和失败**：重复截图、滚动、网络断开、发送状态不明、收到新任务、服务重启均不触发重复发送；失败草稿不变成已回复记忆。

最终报告逐项记录 PASS / FAIL / NOT_RUN、实际模型、两仓版本与未提交状态、QQ 包哈希、场景、观察/动作/消息证据引用和重启前后检索结果。账号密码、登录令牌与二维码不进入报告。
