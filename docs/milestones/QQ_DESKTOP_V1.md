# QQ-DESKTOP-1 · Misty 真实 QQ 群聊验收

> 最近更新：2026-09-11，代码基线 `159d55d`（含当前未提交修改）。用户已确认使用现有 Linux 环境、真实桌面操作及指定测试群。
> 当前状态：用户已授权指定群自动截图及实际发送。QQ 专用 watcher 已实际完成第二次 @ 测试的采集、作者归属和 `speak` 决策；自动交付的多次预点击尝试被核验误判拦住后，操作方通过已授权 GUI 动作辅助发送了同一份、未改写或重新生成的 38 字原回复，并核验新气泡与空输入框。本轮新增成功发送 1 条，累计 2 条；这不表示 `DesktopBackend.deliver` 全自动通过。后台已恢复，同进程连续采集、认证服务状态及重复启动识别已有实例均通过复查；自动发信稳定性和长期自主运行仍为 PARTIAL，标准 Runtime 与全量 Cognitive 社会关系未验收。账号凭据、二维码、成员账号、昵称和发言原文不写入仓库。

## 目标与执行路径

Misty 从 Linux 桌面终端启动腾讯 QQ 客户端，通过实际窗口查看指定群聊，理解发言者和上下文，输入并核验中文回复；后续对话及进程重启后，仍能基于证据回忆群成员。

链路为：NeuralCloud 请求 → MistyAI 桌面终端 → QQ 窗口 → 截图/无障碍证据 → 群聊事件与人物身份 → Cognitive 记忆/回复 → QQ 桌面发送 → 外部回执 → commit。

每段都需要真实接线。终端启动命令的退出码只证明命令结束；截图哈希只证明证据完整性；模型声称“看到了”或 GUI 键盘动作成功，都不能单独证明群聊读取、发送成功或人物记忆正确。

## 2026-09-11 · 自动盯群实测（辅助发送已核验，自动交付仍为 PARTIAL）

- 用户已授权 watcher 对指定群 `1044946257` 自动截图、判断是否回复并实际发送。实现与启停入口见 [QQ 常驻 watcher](../QQ_WATCHER.md)；该授权不扩展到其他群或宿主操作。
- 专用 watcher 已接入真实截图、可见新消息判定、本地观察账本去重、成员证据归属、多人持久记忆和 `speak/wait`。这是 QQ 专用执行链路；原标准 Runtime 自动 QQ planner/Delivery 与全量 Cognitive 社会关系没有因此完成验收。
- 第二次真实 @ 测试消息已由运行中的 watcher 采集、绑定作者并进入决策，实际模型选择 `speak`。消息正文、成员账号、昵称、截图和候选回复只保留在私有状态/证据中，不写入本报告。
- `DesktopBackend.deliver` 的自动尝试和后续预点击修复尝试先后受到当前账号资料、几何/上下文及动态画面核验误判阻断，返回 `not_sent`；这些尝试没有点击发送。不能把模型 `speak` 或发送配置开启计作交付成功。
- 2026-09-11 约 **04:14 UTC**，操作方通过已授权 GUI primitives 辅助发送原始 decision 的同一份 **38 字**正文，未改写、未重新生成。实际人眼核验目标群卡数字、当前账号资料卡及“我的QQ空间”标记、输入框精确正文，发送后确认右侧 Misty 新气泡和空输入框。本轮新增实际发送 **1 条**，累计 **2 条**；这是自动生成回复后的操作方辅助交付，不能记为全程无人辅助发送 PASS。
- 该辅助路径沿用同一 worker lock 和持久 `prepared → click_started → sent` 账本；唯一回执为 `qq-delivery:be2d8671ebbe4cd6805b205d83b531d2`，发送后画面证据为 `evidence://sha256/75aa54a5986a0138bf764db169ef6c2c523ade81659f15dbab3bc3e1234dce78`。冷却和回执已对齐，没有未知结果的发送尝试；不据 GUI 回执宣称取得服务器消息 ID 或收件人已读。
- 后台 watcher 已恢复：worker **23324** 于 **2026-09-11 04:25:22.575327 UTC** 启动。在 **04:27:24 → 04:29:24 UTC** 连续样本间，同一进程的采集数从 **2 → 4**，后续周期完成，心跳新鲜，`automatic_screen_capture=true`、`automatic_send=true`、`state=watching`，错误为空。再次运行启动入口返回 `already_running`，仍是同一 worker。
- 连续采集复查中，持久事件/发送预留/总回执计数保持 **1 / 1 / 2**，本次测试对应的唯一回执仍为 **1**。这是持久记录与计数器核验，未以第二次独立的 QQ 气泡核验证明没有重复发送。**04:27:55 UTC** 的 `8789` 认证状态为 healthy，持久记忆、自动决策、采集和发送标记均为 true，已记录成员数为 **2**。
- 本轮证据见 [自动盯群与辅助交付实测](evidence/qq-autowatch-2026-09-11.json)，结论为 **PARTIAL / operator-assisted**。未知 QQ 服务器消息 ID 保持 `null`；本地视觉去重不承诺任意滚动、离线积压或所有重复正文场景的消息级去重。
- 当前完整 QQ 工程套件 **389 passed / 84.85 秒**，共享 LLM 回归 **28 passed**，Ruff 通过。各组与历史回归分开报告；不替代自动发信稳定性、长时运行、复杂同名、更正、跨群引用或完整系统重启验收。

## 前阶段核验结果（登录、单条发送与专用记忆服务历史快照）

- 现有目标为 Windows ARM64 + WSL2 Ubuntu 24.04.4 / aarch64；保留 `/opt/mistyai`，本次使用独立 `/opt/mistyai-qq-test`。WSL 内 Docker 接入、QQ 镜像构建及实际窗口启动均已验证。
- Docker Desktop 4.89.0 / Engine 29.7.2 的 socket 目录启动故障已恢复。完整安装脚本无需外部预热的两轮独立启动分别耗时 **9.047 秒 / 8.797 秒**；两轮本机 Linux engine、Ubuntu-24.04 接入及临时容器验证均通过，前后 **3 个容器、2 个带标签镜像 ID、4 个卷**清单一致。09-10 的 `sailor-ingest.sock` 启动失败属于历史预检结果。
- 启动防护已安装至 `C:/Users/22850/AppData/Local/NeuralCloud/DockerStartup`；桌面 `Docker Desktop.lnk` 与既有 `HKCU Run` 入口已改用防护，原入口已备份，`StartupApproved` 与 `AutoStart=false` 保持不变。已安装入口 healthy 复查通过，无新增恢复 journal，保活进程为 0；Helper 最终回归 **27 passed**，Launcher 回归 **143 passed**。
- 正常退出后直开原 Docker 程序仍有复发风险；防护不表示上游故障已消失。当前恢复机制和限制见 [Docker 恢复说明](../DOCKER_RECOVERY.md)，最终依据为 [两轮实机证据](../../logs/docker-recovery/live-verification-20260911.json)、[安装入口记录](../../logs/docker-recovery/installed-entries-20260911.json)、[入口复查](../../logs/docker-recovery/entry-verification-20260911.json)。
- MistyAI 默认 M0 仍使用离线网络。QQ 独立 profile 新增 `phase=4 / profile=qq-desktop-test`，desktop 接入出站 bridge，agent 仍仅接内部 control 网络。该配置是整个桌面可出站，不是按 QQ 域名过滤的防火墙；QQ 已登记至 manifest、agent/worker 白名单和窗口别名。
- `BasicTerminalActionPlanner` 只操作配置的终端应用，ASCII 单行命令完成标记是其验收边界；它不是通用 QQ 窗口规划器。
- MistyAI X11 输入新增 Unicode 剪贴板路径，实际 Mousepad 输入 `中文标点，test 123` 后，截图与 AT-SPI 文本回读完全一致；这证明基础中文输入，不替代 QQ 发送验收。另修复 Xlib 窗口坐标转换方向。
- 修复 desktop/agent 不同 hostname 下的 Xauthority 匹配：每次启动重建专用认证文件，为两侧各写同一 cookie 的 FamilyLocal 记录，保留组权限 `0640`。QQ 三服务重建后，无手工干预即可完成截图、窗口、输入、AT-SPI 和进程观察。
- 历史登录过程：2026-09-11 01:13（Asia/Shanghai），通过真实桌面终端输入 `misty-qq` 启动 QQ，观察到 `org.mistyai.QQ` 窗口并点击账密登录。提交用户提供的凭据后出现“新设备安全验证”，当时尚未进入已登录会话。
- 历史登录过程：01:26，新设备验证二维码超时且刷新未恢复；返回 QQ 官方扫码登录入口，随后由账号持有人完成扫码。未继续重复账密尝试，也未改账号安全设置。
- 首轮实测已核验登录账号，并通过独立群卡确认 `1044946257 / PHANTOM-C`，当次显示 17 名成员。点击一条真实气泡的头像后打开资料卡，核验首名成员的 QQ 号与该气泡归属；本轮以相同方式核验第二名成员，视觉模型读取其资料卡信息。私有原始证据保留具体身份和正文，tracked 报告不记录它们。
- 专用 Body `8788` 与 QQ 三容器均为 healthy，容器 restart count 均为 0。当前 collector 通过真实 Agent 驱动窗口操作和截图；标准 Runtime 自动 QQ planner 与经过 GUI 回执核验的 QQ DeliveryBackend 尚未接入。
- 实际图片请求使用 `deepseek-v4-flash-vision-exp`，读出群信息、消息及资料卡 ID；模型识别昵称时遗漏 emoji，已要求人工回读，不把视觉推断直接升级为核验身份。QQ 未暴露来源消息 ID，保持 `message_id=null`。
- 首轮私有视觉记忆 SQLite 的 **1 份 profile + 1 份 observation** 已完成重复截图去重、新 Python 进程回读和其他群/成员空查询。本轮经认证 API 保存第二人的资料与第二条真实观察后，当前为 **2 名成员 / 2 条观察**；专用服务重启后仍保留，重复输入不新增。此为 QQ 专用视觉人物库，不代表全量 Cognitive 长期记忆已经接通。
- `deepseek-v4-flash` 与 `misty-digitalbody-0.2.0` 的真实 persona `prepare` 已成功，仅读取明确限定的群/成员上下文。首次草稿夹带状态标题，规则已修订；后续经用户明确授权的草稿完成一次实际发送。独立新 Python 进程的真实模型回忆已核对 PASS：准确复述原文、保留作者归属，并明确账号关系声明尚未核验。视觉识别调用与 persona 草稿调用分开记录，不宣称 Misty 已自主完成整条链路。
- QQ 中文输入已实际成功。首次输入框草稿视觉 probe 被严格 schema 拒绝，重试后成功；输入框草稿与已发送消息分开处理。
- 用户明确授权发送该条 Misty 草稿后，collector 再次打开群卡核对目标群及待发内容，于 **2026-09-11 01:09:20 UTC** 点击一次实际 QQ 发送。**01:09:25 UTC** 的观察 `obs_af71f64daf07448a9c94131fac347cd3` 显示角色右侧蓝色气泡，输入框清空且无错误；**01:10:38 UTC** 延后截图确认气泡仍在。通过已发气泡右键菜单“复制”后实际读取 X11 剪贴板，取得的 11 个字符与授权模型原文逐字一致，`exact_outgoing_text_match=true`；未读取输入草稿缓存，也未重复发送。单条外部 GUI 发送核验 PASS，但不表示取得服务器消息 ID 或收件人已读。
- 发送后真实视觉模型也识别到已发气泡并返回 `visible_draft=null`；昵称和波浪号仍存在 OCR 偏差，因此精确正文以实际气泡复制核对为准，不能用模型 OCR 替代逐字核验。
- 本轮已部署 `harness.qq.session`，认证 status 确认 `persistent_memory=true`、`autonomous_decision_enabled=true`、`new_verified_observations_trigger_decision=true`；同时明确 `automatic_screen_capture=false`、`automatic_send=false`、`standard_runtime=false`。新核验观察由 collector 提交 API 后才触发 gate，不是自动盯群或自动回消息。
- 第二条真实观察入库自动触发一次模型 gate，结果为 `wait`、回复为空，实际用量 **1 call / 3527 tokens**。旧的唯一已发消息回执已登记，只关联其已回应的那条内部观察 source ID，不把整个群或其他观察标为已回复，也不伪造 QQ 服务器消息 ID。
- 专用服务实际重启后，两成员/两观察及决策记录保留；重复观察不新增、不重算，相同旧 decision ID 返回 cached。新 Python 进程以 `recent_limit_per_member=0`、`history_limit_per_member=5` 查询旧历史，两名成员各命中一条，未依赖近期窗口。
- 操作方的实际记忆检查请求没有提供答案，gate 选择 `speak` 后 persona 回复正确区分两人，并保留未核验的作者声明。发现对未采集历史作绝对否定后，已补充 v2 的部分覆盖规则并重启服务；v2 实际召回 **2 calls / 6622 tokens** 通过，回复限定为“目前记录到”的内容。本轮没有发送新 QQ 消息，累计仍为首轮 1 条。
- 普通 Misty 的主动行为代码虽然存在，本轮核对时标准 `8770/8771/8772` 服务没有运行。当前已启用的是 QQ 专用决策层，不能据此宣称标准 Runtime 自动 QQ planner/Delivery、全量 Cognitive 社会关系或无人值守收发已经接通。

初始现场记录见 [qq-desktop-preflight-2026-09-10.json](evidence/qq-desktop-preflight-2026-09-10.json)，保留当时的失败快照，不代表 Docker 当前状态。恢复后的状态以 [Docker 恢复说明](../DOCKER_RECOVERY.md)及后续实机复验记录为准；Docker 恢复不替代 QQ 端到端或 BR-1 尚未完成的现场测试。

09-11 早期部署摘要见 [QQ 桌面运行证据](evidence/qq-desktop-live-2026-09-11.json)，首轮登录、记忆与唯一实际发送见 [首轮实机验收](evidence/qq-desktop-login-memory-2026-09-11.json)，本轮两人检索、自动 gate 与专用服务重启见 [多人记忆与专用决策实测](evidence/qq-multiperson-autonomy-2026-09-11.json)。专用 Windows Body Service 已在 `127.0.0.1:8788` 运行，状态/证据/服务凭据位于当前用户受限 ACL 的 `%LOCALAPPDATA%/NeuralCloud/QQDesktop`，不在仓库中。使用 `Start-QQBody.ps1` 启动，`Use-QQBody.ps1` 为当前 shell 生成并选择 QQ 实例 manifest 和 token 文件引用；NeuralCloud GUI 应显式使用 `8769`，避免与 QQ agent 的 `8766` 冲突，不能用会强制恢复 M0 配置的默认 dev launcher。

NeuralCloud 实际 `MistyAIClient` 已成功读取 `body_instance_id=misty-qq-test-01`、`network_phase=4`、`policy_profile=qq-desktop-test-default-deny`、`health=healthy`，并取得可见 QQ 窗口，校验截图及 AT-SPI 证据。此早期结果只证明跨服务 adapter；后续登录、真实视觉、记忆和草稿调用分别见 [登录与视觉人物记忆脱敏证据](evidence/qq-desktop-login-memory-2026-09-11.json)。完整自主 Runtime/Cognitive 尚未验收。Windows 启动接线专项 **2 passed**，重复调用实际启动脚本识别已有正确实例并返回，没有重启服务。

首轮工程回归：vision + LLM **41 passed**，视觉人物记忆 **30 passed**，persona 草稿 **8 passed**。本轮分组回归为：memory **61 passed**（含原 30 项）、decision + draft **28 passed**、session **10 passed**、Windows PowerShell 5.1 启动器 **14 passed**。分组结果不与历史重复累计，也不替代多年记忆、复杂多人或长时自主运行验收。

可复用入口见 [截图视觉读取](../QQ_VISION.md)、[视觉人物存储](../QQ_VISUAL_MEMORY.md)、[Misty 回复草稿](../QQ_REPLY_DRAFT.md)和 [QQ 专用会话服务](../QQ_SESSION.md)。通过 [Start-QQSession.ps1](../../tools/qq/Start-QQSession.ps1) 启动本机 `8789` 服务；实际隐藏启动及重复调用识别已有实例均通过。该服务接收已核验观察、持久化并触发决策，尚不承担自动截图和外部发送。

最终运行版本为 `qq-group-turn-taking-v3`：未显式提供查询时，已核验触发成员的近期观察会自动成为历史查询，并返回查询来源与截断标记；完整观察原文保留。真实服务已核对默认查询等于该条观察，并验证同请求复用决策。当前最终服务重启后仍为两名成员、两条原始观察，v3 真实模型再次完成两人归属召回。专用记忆库仅覆盖已采集记录，模型生成文本仍需核对；一次未发送的回忆文本出现了无依据的性别代词，该词没有写入人物事实。

## 已准备的部署入口

相邻 MistyAI 仓库新增 `deploy/qq/`，提供包校验、独立镜像构建上下文、QQ 启动器和部署检查。命令以 `C:/Projects/MistyAI/deploy/qq/README.md` 为准。本次已实际构建并运行独立 Compose project `mistyai-qq-online-test`，实例 `misty-qq-test-01`，environment revision 2；agent/noVNC 仅发布在本机 `8766/6081`。

官方包为 `linuxqq 3.2.33-52892 / arm64`，SHA-256 为 `806e036ee1c19ffeae22f19b5f0c2e1a7789abb5f4383bd67309995a73ea26de`。实际镜像补充中文字体及官方包未声明但 QQ 动态链接需要的 `libasound2t64`。当前镜像为 `mistyai-desktop:qq-online-test-20260911-authfix`，manifest index 为 `sha256:082b6afd879741ab0787e8584436c8ca590ca801707d8592cfedd0faee6c4778`。

默认 seccomp 阻止 Chromium 用户命名空间启动；QQ 测试 desktop 使用基于固定 Moby 默认配置的窄范围派生 profile，补充需要的 clone/unshare 命名空间组合及 chroot，保留默认拒绝、非 root、只读根文件系统、cap-drop-ALL 和 no-new-privileges。未使用 `--no-sandbox`、SYS_ADMIN、unconfined 或 `xhost +`。完整 Chromium 沙箱安全性不能仅由成功显示窗口推断。

部署准备专项 **23 passed**，Xauthority 配置契约 **5 passed**，X11/Unicode/剪贴板相关 **38 passed**；网络 profile 专项 **7 passed**，相关 Ruff 与 shell 语法检查通过。旧 `readiness.json` 的 `not_ready` 保留为历史快照；当前部署状态以实际运行及后续现场证据为准。

腾讯 [Linux 下载页](https://im.qq.com/index/#/linux) 当前配置为 Linux QQ 3.2.33，更新日期 2026-09-02，提供 ARM64 deb。版本依据为 [官网配置](https://qq-web.cdn-go.cn/im.qq.com_new/latest/rainbow/pcConfig.json)。当前官网链接需要先经过其公开下载签名流程，裸下载链接返回 403；使用官网流程下载的本地包，随后固定 SHA-256 和包架构。

Linux QQ 对外提供图形客户端。无物理显示器时可以沿用现有 Xvfb/X11；[Electron 官方说明](https://www.electronjs.org/docs/latest/tutorial/testing-on-headless-ci)描述了这类运行方式。`--force-renderer-accessibility` 是 [Chromium 的可访问性开关](https://www.chromium.org/developers/design-documents/accessibility/)，不能据此保证 QQ 暴露完整消息、群号或成员 ID，必须实际检查。

## 实施顺序与完成条件

1. Docker 启动防护安装及两轮独立实机复验已完成；专用 MistyAI desktop/agent/gateway 已重建并通过真实观察及终端输入验证。
2. 独立联网 profile、ARM64 QQ、字体/运行依赖、应用登记与实际窗口别名已验证；Body Service 及 NeuralCloud 实际客户端已对接该实例，capabilities、health 和观察证据均通过。完整自主 Runtime 与 QQ 规划器仍待接入。
3. QQ 已保留非 root、NNP 和受限 seccomp 启动，基础中文输入及实际 QQ 输入均通过。登录页当时未暴露 AT-SPI 文本节点；后续群卡及两名成员资料卡已有实际视觉模型读取，两条正文按真实截图完成归属核验，连续消息顺序与复杂引用关系仍待验。
4. 已从真实桌面终端启动 QQ，由账号持有人完成扫码登录，并分别核验登录账号、目标群卡和两名发言者的资料卡。
5. 首轮授权草稿已有单条 GUI 发送核验；后续 QQ 专用 watcher 已接入自动截图、新可见消息去重、归属、多人记忆及发言决策。第二次真实 @ 测试进入 `speak` 后，其原始 38 字回复经操作方 GUI 辅助完成发送，累计 2 条。`DesktopBackend.deliver` 全自动交付稳定性及长期持续收发仍待验收，标准 DeliveryBackend 未接通，未知消息 ID 保持空值。
6. QQ 专用多人视觉库已完成持久写入、旧历史检索和专用服务重启验证；标准 Runtime、全量 Cognitive 长期记忆与社会关系仍需贯通，再完成其余现场用例。

## 人物记忆接线要求

已新增独立的 [QQ 观察存储入口](../QQ_OBSERVATIONS.md)：按实际群号、作者 QQ 号和来源消息 ID 保存原文、时间、显示名及证据到 Runtime 状态目录下的 SQLite；支持持久去重、来源冲突拒绝、按群/成员查询，无全局回退。新测试 35 项加既有 API 测试 11 项共 **46 passed**，覆盖重启读取、同名区分、并发重复及外部正文不进入控制通道。

该正规消息入口只持久保存观察，不触发 Cognitive、控制器或发送。真实 QQ GUI 当前无法提供稳定消息 ID，因此未将截图记录伪装成该入口要求的正式消息记录。

另新增 [QQ 视觉人物记忆](../QQ_VISUAL_MEMORY.md)：已核验群/成员资料与截图发言归属分别保留证据，提取内容明确标为作者断言；不因“小号”等陈述自动合并账号。只按 `group_id + capture_id + capture_item_id` 去重同一观察，不承诺跨截图消息级去重。本轮 `group_context` 已接入 QQ 专用 gate/persona：按群与成员分别分配近期、历史额度，从完整成员历史检索并保留旧资料证据，报告候选、命中与截断。两名真实成员在 history-only 新进程查询中各命中一条，服务重启后仍可正确分人召回；未采集历史明确为未知，不得作绝对否定。这证明基础两人持久记忆与专用服务恢复，不代表多年规模、复杂同名/更正、全量 Cognitive 社会关系或完整标准 Runtime 重启通过。

以下认知接线仍待实现，现有单用户聊天语义保持兼容。

- 为群聊建立稳定 `session_id`；为已核验成员建立稳定 `speaker`/参与者身份，昵称、群名片作为可变别名。同名不合并，同人改名不新建人物；只见昵称而无法核验账号时保留临时身份。
- 入口贯通 `participants`、来源消息标识及 `evidence`。当前 `RuntimeSupervisor.submit_user_message()` 和 `PipelineCognitiveAdapter` 尚未把完整来源上下文传入 `InteractionEvent`。
- 对新群聊渠道显式设置参与者，避免 `default_event_participants()` 把每个成员自动关联到通用 `user`。规则 parser、state builder、关系目标及检索需要绑定真实发送者。
- `session_id` 当前主要隔离短期 transcript，不能假设长期记忆已按群隔离。检索明确区分人物事实的来源群、可跨群复用范围和当前群上下文。
- 已发回复继承群和实际接收者信息；只有独立核验的 QQ 回执才能作为成功 `DeliveryEvidence`。未知发送结果进入对账，不能自动重发或记为已发送。
- 重复屏幕、滚动、重新进入群不应重复记忆。缺少稳定消息 ID 时须明确 GUI 去重局限，不能仅靠“文本相同”删除不同人或不同时间的真实发言。
- 群消息是外部内容，不得因其中出现命令或要求而获得宿主操作权限。人物记忆保留事实来源、时间与修订，关系判断交由认知层。

现有 `SocialWorldService` 是受实验条件及人物数限制的研究路径，不能直接启用它就宣称通用 QQ 人物记忆完成。

## 现场验收案例

状态以本轮脱敏实机证据为准。collector 通过真实 Agent 执行的操作、视觉模型调用和 persona 草稿调用分别计证；模拟数据只用于工程回归，不能转写为真实群成员记忆。

- **QQ-01 启动与登录 — PASS**：collector 通过真实 Agent 的桌面终端启动 QQ；账号持有人扫码后，已独立核验登录账号与 QQ 窗口。此项不表示标准 Runtime 已能自主登录。
- **QQ-02 定位群 — PASS**：独立群卡确认指定群号及群名，不仅依赖窗口标题或搜索结果。
- **QQ-03 读取 — PARTIAL（已归属观察与自动新消息采集）**：前阶段两条真实截图消息完成作者核验；后续 watcher 已实际采集第二次 @ 测试、绑定作者并触发 `speak`。连续消息顺序和复杂引用对象未完成现场验收，昵称 OCR 遗漏仍需回读修正。
- **QQ-04 中文发送 — PASS（外部 GUI 核验，自动交付稳定性 PARTIAL）**：首轮 11 字授权回复通过发送后气泡复制逐字核验；本轮 watcher 自动生成的原始 38 字回复经操作方 GUI 辅助发送，实际核验目标群、当前账号、输入正文及发送后新气泡和空输入框，累计 2 条。`DesktopBackend.deliver` 的预点击尝试仍曾误判阻断，不能据辅助发送宣称无人辅助自动交付通过；未取得服务器消息 ID 或已读回执，标准 Runtime DeliveryBackend 未接通。
- **QQ-05 多人区分 — PASS（基础两人归属与召回）**：两名真实成员分别完成资料与气泡归属核验、持久入库；不提供答案的记忆检查触发 `speak` 后，persona 正确分人并保留未核验声明。该结果不覆盖复杂同名、关系合并、更正或多年多人场景。
- **QQ-06 别名与同名 — NOT_RUN**：仍需实际改名和同名账号场景；工程测试中的隔离不替代现场验证。
- **QQ-07 重启回忆 — PASS（QQ 专用服务范围）**：实际服务重启后两成员/两观察和缓存决策仍在，重复输入不新增、不重算；新进程旧历史检索及 v3 真实模型召回通过。完整 NeuralCloud/标准 Runtime 重启与长期持续自主运行仍未验收。
- **QQ-08 更正与遗忘 — NOT_RUN**：仍需实际更正、后续检索和来源保留验证。
- **QQ-09 群边界与引用 — NOT_RUN**：独立库对其他群/成员返回空的实测已通过，但尚未执行切换真实会话及引用归属场景。
- **QQ-10 去重和失败 — PARTIAL（观察去重、发送前阻断与辅助交付账本）**：同截图去重、服务重启后不重算、相同决策返回 cached 已单独通过；自动交付核验误判时实际阻断在点击之前。后续同一原回复的操作方辅助发送沿用 worker lock 与持久发送状态，唯一回执和冷却已对齐，没有未知结果的尝试。恢复后台后采集数 2 → 4，持久事件/预留/总回执计数保持 1 / 1 / 2，本测试回执唯一；该复查依据记录与计数器，不是第二次独立气泡核验。任意滚动、断网、发送状态不明、任务抢占及标准 Runtime 完整交付仍待验收。

最终报告逐项记录 PASS / PARTIAL / FAIL / NOT_RUN、实际模型、两仓版本与未提交状态、QQ 包哈希、场景、观察/动作/消息证据引用和重启前后检索结果。账号密码、登录令牌、二维码及真实成员身份和发言原文不进入 tracked 报告。
