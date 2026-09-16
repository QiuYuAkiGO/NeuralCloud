# QQ 协议接入与图片理解

2026-09-14：为 Misty 增加 NapCat / OneBot 11 接入。协议消息使用明确的群号、QQ 号、消息 ID 和分段正文；回复直接提交给对应群，不依赖 QQ 当前打开的聊天窗口。旧桌面接入保留，用于回退。

本次接入继续使用原有 Runtime、Cognitive、人格和共享记忆。`transport="onebot"` 是原 `PrepareQQDecision` 的受校验输入分支，不会另建人格或目标账本。秋语 / 秋语随风（2285027817）作为唯一创造者的既有操作方事实继续保存在同一共享记忆。

## 群名单

名单保存在私有 `QQDirect/direct.json` 的 `group_roster`，每项包含 `group_id`、`label`、`observe`、`send`。

- `1044946257`（PHANTOM-C）：观察、按原认知决策回复。
- `1060979317`（超级豪人大宅门）：观察、按原认知决策回复。

2026-09-14 用户后续授权两个群均可回复，已将当前协议配置中两项 `send` 设为 `true`。先前只观察的限制属于首次接入时的历史配置。

桥必须登录配置中的 Misty 账号 `2015188508`，并能确认已经加入名单各群，才会进入连接就绪状态。事件按群分别入库；相同消息 ID 去重。历史回填用于阅读和记忆，不触发对旧消息补发回复。

## 秋语私聊通道

2026-09-14 私聊增量已部署启用，并完成真实私信接收、同一认知决定、两段回复及完整发送回执，耗时 22.645 秒。目标为秋语（全名秋语随风，QQ `2285027817`），Misty 仍使用 QQ `2015188508`。部署前后原活动任务与结果数量保持一致，见[部署记录](milestones/evidence/qq-private-deployment-2026-09-14.json)、[字段修复重载](milestones/evidence/qq-private-ws-repair-deployment-2026-09-14.json)和[通道验证](milestones/evidence/qq-private-implementation-2026-09-14.json)。

私有 `direct.json` 的 `private_contacts` 增加该好友，字段为 `peer_id`、`label`、`observe`、`send`；QQ `session.json` 的 `allowed_private_peer_ids` 同时列入该账号。连接就绪检查需确认对方在好友名单中；只接收该白名单的 `friend` 私聊，不接受群临时会话。两个群的既有观察、回复权限保持开启。配置及服务代码更新后需要重载 QQ 协议进程和相关共享服务，不能仅以配置文件已写入判定通道生效。

私聊采用独立会话键 `private:2015188508:2285027817`。旧表与部分契约中的 `group_id` 字段沿用作会话分区键；私聊发送实际调用 `send_private_msg(user_id=2285027817)`，不会把好友 QQ 号当作群号。收到私信后，Misty 沿用同一 Runtime、Cognitive、人格及记忆 owner，自主判断是否回复；私聊的原图、OCR 与 GIF 抽帧继续经过原视觉链路。

发送返回的消息 ID 必须在绑定该好友的历史查询中核对账号、对端、本人作者、消息 ID 和冻结正文，才形成协议回执。无法识别对端的本人消息事件不能单独证明送达。历史回填仅用于记忆，不补发旧回复；未知发送不自动重发，保留原有期限和有界只读对账。

实机初次私信暴露了 NapCat WS 与历史 API 的字段差异：WS 推送层补入 `target_id=peerUin`，表示会话对方，历史 API 则省略该字段。入站校验已按会话对方修正，新增对应真实字段回归，111 项相关测试通过；[真实 WS 字段](milestones/evidence/qq-private-ws-shape-2026-09-14.json)已确认该语义。发送回执仍使用好友历史绑定，旧测试消息不重新触发回复。

私聊记录按 `qq_private` 渠道及专属 audience 隔离，群回复不会检索到私聊正文。只有真实本地 `chat` 请求可以读取授权私聊记忆，`internal` 能力选择和目标评审不取私聊正文；专用操作者确认事实只补充给秋语的私聊。通用身体目录仍只列两个群，私聊读取不进入全局能力目录，私聊调用及历史缓存正文也不能经该入口取出，避免通用目标账本保存这些正文。将来若提供私聊身体读取，需先让完整调用与结果路径携带并执行 audience 约束。

离线验证涵盖私聊契约、好友消息及本人历史证据、图片/OCR、记忆隔离和原群行为兼容；收发与 Runtime 集成 395 项、独立共享记忆套件 115 项通过。参见 [私聊 actor 测试](../tests/test_qq_private_actor.py)、[契约测试](../tests/test_qq_private_contracts.py)、[共享记忆测试](../tests/test_qq_private_shared_memory.py)及[能力隔离测试](../tests/test_qq_private_capabilities.py)。真实私聊验收样本是文字；私聊图片走相同已测视觉链路，尚无真实私聊图片样本，不将模拟结果当作图片实机验收。

## 图片如何保留

`image` 消息段通过原始 URL 或 `get_image` 取回原图。下载目标限制为已支持的 QQ 图片域名，逐次检查重定向、DNS 和媒体大小；桥端文件通过启用 `enableLocalFile2Url` 后的 base64 返回，无需共享登录目录或开放任意文件读取。

原图按 SHA-256 保存在私有 `QQDirect/images`。PNG、JPEG、WebP、GIF、APNG 可以解码；动画进行有界抽帧。现有视觉模型生成图片描述和 OCR，连同图片证据进入消息记忆、回复 gate、人格上下文及共享记忆。OCR 超过上下文预算会截断并明确标记，原图继续保留。

`image_analysis` 明确标为 `model_inference`，独立于作者原话。下载失败、图像过大、模型失败或超时会保留 `unknown`，不能猜图，也不能把图中文字当作系统指令。

## 启动与切换

先安装本项目 QQ 可选依赖。部署脚本固定镜像摘要，并验证已有容器身份：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[qq]'
powershell -NoProfile -File tools/qq/Setup-NapCatBridge.ps1 -CheckOnly
powershell -NoProfile -File tools/qq/Setup-NapCatBridge.ps1
```

桥容器为 `neuralcloud-qq-onebot`，WebUI 仅监听 `127.0.0.1:6799`，OneBot WebSocket 仅监听 `127.0.0.1:6301`。随机令牌和桥登录数据存放在 `%LOCALAPPDATA%\NeuralCloud\QQDirect`。它有独立的 QQ 登录目录，未复用桌面 QQ 的会话数据。首次使用需用手机 QQ 扫码并确认 Misty 账号登录；OneBot 端口要到登录完成后才可用。

`direct.json` 绑定独立协议账本、图片目录、旧 `session.json`、旧 watcher 状态和同一 Runtime 控制令牌文件。`NC_QQ_DIRECT_CONFIG` 加入共享服务的持久启动路径绑定；修改服务代码或这项环境绑定后，要维护性重启 Runtime / Cognitive。QQ session 的 `direct_status_file` 指向协议状态文件，加载后 `/v1/status` 分别报告连接、观察、发送和图片能力。

```powershell
powershell -NoProfile -File tools/qq/Start-QQDirect.ps1 -CheckOnly
powershell -NoProfile -File tools/qq/Start-QQDirect.ps1
```

启动脚本隐藏窗口、登记进程及创建时间、校验源码与配置，重复启动返回已有实例。它保留控制文件的暂停状态，不会因启动而取得发送权。正式切换要先用 `Stop-QQWatcher.ps1` 暂停旧 watcher 并确认 `running=false`，然后将私有 `direct.control.json` 的 `paused` 设为 `false`、`stop` 设为 `false`。协议 actor 仍会检查桌面收发所有权。

停用或回退时，先执行：

```powershell
powershell -NoProfile -File tools/qq/Stop-QQDirect.ps1 -TimeoutSeconds 90
```

确认协议进程退出、`automatic_send=false` 后，才恢复桌面 watcher。停止脚本先正常退出，必要时仅终止进程登记和创建时间均吻合的实例。

## 发送与状态解释

### 2026-09-16 模型上下文预算

历史选择和最终校验按同一编码函数计算两个模型输入 JSON 的字符总数，情绪提示提前纳入选择预算，后续独立 assessment 消息保留完整包装空间。达到预算上限可以继续；超限时仅移除允许省略的完整历史项，当前触发消息和必需观测不被截断。稳定历史前缀与动态末段仍保持分离。故障事件与验收见 [上下文预算修复记录](milestones/qq-context-budget-recovery-2026-09-16.md)。

### 2026-09-15 校验输入精简与独立审计

校验器在证据准入完成后，把完整历史/记忆正文及作者、渠道、会话、观察时间、状态、类型投影为紧凑表。重复结构使用短别名与元数据表，不截断正文、不合并记录；未知身份仍是未知。同源行分组不代表多份独立佐证。来源引用、长内部 ID、segment 哈希和人格资料的来源链接只保留在原对象与私有审计日志中。

既有 CallLog 新增 `validator_input_before`、`validator_input_after`、`validator_output`，记录完整原证据/人格资料、别名到原来源映射、实际 system/user、SHA-256 及原始模型结果或错误类型。审计字段不会回灌模型；日志开关和父调用关联沿用原实现。

仅对已部署的 `qwen3:4b-instruct-2507-q4_K_M` 使用随包固定哈希的官方 tokenizer 精确计数；其他模型或依赖不可用时继续采用字节上界。仍预留 256 安全余量与 1024 输出 tokens，超出窗口的完整输入继续走主模型。线上窗口保持 8192。

两次实际失败请求的全部 120 / 117 条记录保留，完整模型输入分别由 51,794 / 47,679 降至 6,739 / 5,209 tokens。测试与上线结果见 [精简及审计证据](milestones/evidence/qq-qwen-compact-input-2026-09-15.json)。额外的 5 项语义抽查中，旧完整格式与新格式都漏判了 4 项错误归属/暂定事实案例；这组对照未发现新增漏判，但不能据此宣称小模型的语义校验可靠。容量修复不改变原有 60 秒 QQ 总期限，也不证明所有自然群回复都能在期限内完成。

### 2026-09-15 校验容量与单轮到期修复

Linux 实例的两次图片提问曾在正文生成成功后，于 Qwen 校验阶段返回 HTTP 400：校验请求分别为 47,679 和 51,794 tokens，辅助模型实际窗口为 8,192。校验现按完整输入的保守 token 上界及模板、输出预留选择模型；超出辅助窗口时使用原有主模型，短输入仍使用辅助模型。所有已准入证据保留，HTTP 错误仍显式返回，不能用截断证据或跳过校验代替成功。

QQ 的 Web 准备、情绪观察、正文准备和最终快照共享原始 60 秒期限。到期终止该轮并记录 `ignored / qq_turn_expired`，不会因此暂停整个 Runtime，也不会在恢复或重启时重新执行过期准备。提前发生的超时、真实领域失败和非 QQ 故障继续按原恢复流程处理；Runtime 放弃等待不表示 Cognitive 后台操作已取消，原命令锁仍持续到操作完成。

新增 41 项回归覆盖容量路由、完整证据、QQ 可发送结果、错误传播、全部准备阶段到期、命令取消锁、真实失败及重启恢复。修复不延长回复期限，不补发原失败消息。

修复已部署至 Linux release `20260915-qq-reply-repair-v1`。停写备份的 1,568 个文件与 15 个 SQLite 数据库校验通过；上线后六服务 active、Runtime `RUNNING`、队列为 0，QQ 已连接。真实模型短校验由 Qwen 通过，39,377-token 大校验由 DeepSeek 通过；首条上线后的自然消息正常完成模型判断并选择 `wait`。尚未观察到本版本完整群回复交付，见 [修复及恢复证据](milestones/evidence/qq-reply-repair-2026-09-15.json)。

每条候选沿用原先 60 秒总期限。Runtime 对原始事件哈希、账号、群、作者、时间、观察、图片槽位和活动运行记录进行交叉核验；发送前再次核对已冻结的正文和来源。发送预约先持久化，协议发送不自动重试。跨群事件与历史回填不会抢占本群新候选；本群新消息、断连或权限变化会终止未发子段。

`send_group_msg` 返回消息 ID 后，还需用 `get_msg` 核对实际群号、本人、消息 ID 和正文，才能记录 `qq_protocol_verified` 回执。缺少群字段时，用本群历史交叉验证。已发前缀独立保留，不把部分交付记成完整成功。无法确认副作用的发送进入 `unknown`，阻止盲目重复发送。

发送前至少保留 2 秒原始预算，不足时不发起协议发送。若发送 ACK 超时且消息 ID 缺失，只能用发送时间附近唯一的、已持久化的同群本人协议回声提出候选 ID，并再次通过只读协议查询核实；原始证据哈希、群号、本人、正文和冻结发送计划均一致后才补记回执。此过程不会重发消息或续发未完成分段；缺少可靠证据仍保留未知状态。

不确定记录会在连接后及每 30 秒进行有界只读恢复，每批最多四条；核验成功后补齐回执，不重发消息或续发过期子段。没有消息 ID 且缺少可验证本人回声证据的发送保留现场，等待人工查证。

Misty 的原身体能力目录提供按群的 `communication.qq.read.v1`，可读取或检索持续留存的协议消息及图片分析。结果附带最后接收时间和部分覆盖说明，明确属于留存消息，不冒充一次新的实时抓取。协议模式启用后隐藏旧 GUI 能力入口，常规回复继续经过同一认知决策链。

协议状态是测量信息，消息正文、图片、令牌不出现在公开健康接口。`connected`、`automatic_send`、`paused`、`desktop_owner_active` 分别描述实际状态；容器启动或二维码出现本身不代表 QQ 已连通。

## 验证边界与上游

2026-09-14 实机已切换：Misty 账号登录成功，两个群均可直接读取；身体目录提供两个协议只读入口。用户在 PHANTOM-C 发送的带图 @ 消息完成了原图识别、同一认知决定、两段自动发送和完整协议回执，首次接收到完整回执耗时 58.810 秒。这个真实样本验证了收发闭环；模型与识图延迟仍需继续观察，不能由一个样本宣称长期稳定。详细测量见 [接入验证](milestones/evidence/qq-direct-implementation-2026-09-14.json)。

随后连续输入暴露了一次自然等待和一次原期限内未完成的决定。定位到 QQ ingress 为每个新事件遍历约 146 MB、6.82 万条日志，入队前额外耗时约 13–14 秒。已复用 Runtime 现有增量日志索引，按序号读取原接受/完成记录，保留原幂等与 60 秒期限；新增请求在索引无增量时不读取旧日志页。78 项相关回归通过，原失败和隔离索引基准分别保存在 [延迟诊断与修复](milestones/evidence/qq-direct-ingress-index-2026-09-14.json)。协议跟踪另记录排队、读取、共享准备和发送时间；模型或网络本身的长耗时仍可耗尽原期限。

离线测试覆盖真实协议账本与记忆、冻结决定、模拟 WebSocket、跨群权限、分段回执、重复消息、图片字节与视觉模型接口。实际登录、真实群消息、原图取回和自然回复要在扫码后单独验证；模拟协议测试不能替代实机收发验收。

本次采用 [NapCat v4.18.28](https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.28) 和其 [官方 Docker 部署](https://github.com/NapNeko/NapCat-Docker)，消息格式依据 [OneBot 11 消息段规范](https://github.com/botuniverse/onebot-11/blob/master/message/segment.md)。Lagrange 当前接口演进以 [官方仓库](https://github.com/LagrangeDev/Lagrange.Core) 为准；本实现只声明 OneBot 11 兼容，不声明已兼容 Milky。
