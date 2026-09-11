# QQ 桌面常驻 watcher

`harness.qq.watcher` 在已登录的 Linux QQ 专用窗口内，完成观察、数字身份核验、人物记忆、两阶段发言决策和一次 GUI 发送。授权范围来自私有配置，只允许配置中的目标群。群消息是对话资料，不能修改发送范围、身份绑定或主机操作规则。此路径不启动标准 Runtime，不使用 QQ 协议机器人，也不产生 QQ 服务器 `message_id`。

2026-09-11 18:52 的新消息实测已完成首次无人辅助实际发送，保存了唯一回执；完整耗时 42.875 秒，尚未达到熟悉成员 30 秒目标。新的 8 小时连续运行和 P95 验收尚未完成；历史上人工辅助点击发送的成功不能计入本轮。当前快照见 [实施与预检证据](milestones/evidence/qq-unattended-implementation-2026-09-11.json)，本条实测见 [自动发送证据](milestones/evidence/qq-live-test-2026-09-11-1852.json)。人物记忆和决策接口分别见 [QQ_VISUAL_MEMORY.md](QQ_VISUAL_MEMORY.md)、[QQ_SESSION.md](QQ_SESSION.md)。

## 启动和暂停

前置条件是扫码登录已完成，QQ 专用窗口、Body、Agent 和私有配置已经准备好。启动脚本不会替用户完成登录、更新 Docker 或修复服务。在仓库根目录运行，支持 Windows PowerShell 5.1：

~~~powershell
# 可选：启动供本机查询记忆/决策状态的 QQ session 服务。
.\tools\qq\Start-QQSession.ps1

# 启动唯一的桌面 watcher。
.\tools\qq\Start-QQWatcher.ps1

# 请求暂停，等待确认。
.\tools\qq\Stop-QQWatcher.ps1
~~~

session 默认端口为 8789，配置为 %LOCALAPPDATA%\NeuralCloud\QQDesktop\session.json。它独立于 watcher 进程，启动 session 本身不会截图或发送。watcher 默认配置为同一私有目录中的 watcher.json；可明确选择既有配置：

~~~powershell
$qqConfig = Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDesktop\watcher.json'
.\tools\qq\Start-QQWatcher.ps1 -ConfigPath $qqConfig -TimeoutSeconds 300
.\tools\qq\Stop-QQWatcher.ps1 -ConfigPath $qqConfig -TimeoutSeconds 20
~~~

启动使用仓库 .venv\Scripts\python.exe，隐藏执行 `python -m harness.qq.watcher --config <private absolute path>`。启动等待默认 300 秒，范围 1–600 秒；暂停默认 20 秒，范围 1–120 秒。初始化和身份预检时间不等于正常消息的响应延迟。

启动幂等检查包括精确模块参数、配置路径、Python 身份、进程创建时间、新鲜心跳及实例状态。虚拟环境启动 PID 可以与实际 worker PID 不同。匹配既有进程返回 already_running；不明占用或旧心跳不会启动第二个 worker。

暂停脚本原子写入 control_file 的 `{"paused": true}`。worker 在采集、模型返回及 GUI 动作前的检查点响应；确认后写 running=false、state=paused 并退出。脚本不调用 Stop-Process，不操作 Docker/WSL。未完成的网络请求可能延后确认。启动等待超时也只请求暂停，不能据此认定进程已经退出。

JSON 退出码：0 为已经核验的运行、暂停或退出；10 为入口竞争或尚待暂停确认；20 为配置、身份或启动检查失败。重新启动前应先获得暂停确认。脚本不注册开机启动或 Codex 定时任务。

## 观察与执行路径

启用新路径后，读取和 GUI 操作分开进行：

1. QQScopedAccessibilityClient / ScopedQQObserver 使用独立持久 HTTP 连接，以 1 秒为默认目标轮询间隔调用 Agent 的 POST /agent/v1/accessibility:read。请求绑定 Body 实例、环境修订、QQ 应用、X11 窗口和几何，单次读取预算为 1000 毫秒。实际间隔还需测量读取、取证及调度耗时。它只提供变化与首次可见时间线索，没有点击或身份授权。
2. MistyAI 的 scoped AT-SPI 读取绑定 ACTIVE 顶层祖先，并用实际 X11 堆叠、映射和几何确认资料卡覆盖关系。仅 SHOWING 的背景 QQ 窗口不能成为目标；无法唯一绑定或存在未知遮挡时拒绝。受保护节点不读取，超时或截断不生成有效树。轻量 Agent health 不再为了每个动作扫描完整无障碍树。
3. chat_change_signature 按真实消息容器去重，只摘要当前聊天标题、正文、方向、作者标签和稳定几何；不包含输入草稿、光标、成员列表或动画位图。资料卡遮挡时 chat_visible=false，记录观察空档。未变化的读数不触发完整截图和模型调用。新摘要由独立只读后端采集整对 PNG/AX 证据后发布；取证期间不提供旧 latest，pending_digest 会阻止依据旧语境点击发送。
4. GUI 唯一执行线程在有用变化时调用 Body，取得同一次采集绑定的 PNG 与 a11y-tree-v1。证据先验证 SHA256、大小和媒体类型；project_qq_accessibility 再核验窗口、祖先与采集时间，投影为现有 GroundedScreen，保留 PNG 和无障碍证据引用。Body 的连接和异步运行器复用，避免每次截图重新建立客户端。
5. 无障碍结构、正文或实际数字资料卡字段无法可靠读取时，inspect 对同一张经验证的截图使用视觉模型回退。常态目标为零视觉调用，但这尚不是已测得的性能保证。未安装新的 OCR 依赖。文字发言仍保留 speak / wait 判断及 Misty 人格正文生成两个阶段。

默认兼容行为保持不变：accessibility_enabled=false，Body capture 不带 a11y_scope 时沿用原读取方式。只有在私有 watcher 配置中显式设置 accessibility_enabled=true 才启用 1 秒观察路径。关闭该项时仍使用旧 poll_seconds 截图轮询，默认 15 秒；不能将其称为 1 秒观察。

MistyAIClient 的已验证证据字节缓存默认关闭（evidence_byte_cache_bytes=0）；只有 QQ Desktop 客户端显式启用 16 MiB、最多 128 项的 LRU。缓存只保存同一客户端经 SHA-256、规范 MIME 和大小校验的不可变 bytes，close 时清空。observe 每次仍发送新的 capture POST；复用同一摘要的已验证字节不会产生新的观察，也不会给旧截图延长动作权限。

私有 watcher 配置为独立 sensor 显式设置 skip_obscured_content=True。该选项的契约默认仍是 False，Body 完整 capture 保持 False。若实际 X11/AT-SPI 已绑定遮挡窗口，sensor 只读取真实 active/overlay 顶层节点，返回 content_state=occluded、content_coverage=top_level_bindings_only 及 scope 选项回显；不读取聊天、名单或资料卡正文。truncated=false 表示完成这个明确的轻量 scope，不能当成完整内容树；project_qq_accessibility 拒绝将它用于 grounding，chat_change_signature 只据此记录 gap。无遮挡时继续完整读取。capture 与 sensor 共用读取锁，生产传感器最多等 0.4 秒，并保留至少 1.1 秒 HTTP 时间供固定 1 秒 scope；预算不足时不发请求。

watcher 显式启用 reader 的 causal_freshness 模式：每次读取使用独立随机 request_nonce，Agent 仅在本次新鲜 scoped 读取成功后回显。reader 同时检查实例/窗口、单次网络单调时钟往返预算、远端读取耗时一致性和最多 ±5 秒的时钟偏移；nonce 不放宽窗口和身份边界。该模式下跨请求墙钟后退或跳变记录为 clock_step_detected/count、progress_error_ms 诊断，不单独把有效 nonce 绑定的新读取当成旧读。Agent v7 对 nonce 请求追加 a11y-read-timing-v1 单调时钟包络，并在每次新鲜读取后覆盖生成；客户端请求默认用高精度 perf_counter，要求包络不超过本机 RTT、RTT 不超过 1.5 秒、AX duration 不超过包络且不超过 1 秒。同请求墙钟跳变只作 intra_request_wall_step/wall_duration_error_ms 诊断，nonce、实例/修订、窗口和 ±5 秒硬偏移界不变；新 metadata 非法时拒绝，缺少新字段仍走旧严格校验。旧 reader 默认 strict_wall_clock 分支保留原有严格 UTC 门限。状态中的 read_freshness、read_timing 提供脱敏模式和时间诊断。

Agent v5 起的 scoped capture 另附 capture_timing（a11y-capture-timing-v1）：实际 PNG SHA-256 与截图开始/结束、AX 开始/结束相对同一次 Agent monotonic 起点的毫秒偏移。投影调用必须传入实际 PNG 的 expected_frame_sha256，精确核对摘要、有限非负偏移、先截图后 AX、最多 2 秒配对窗口，以及 AX duration 不超过实际读取区间和 1 秒预算。新版证据用该单调时钟证明配对，原 frame_captured_at 精确绑定和所有墙钟时间仍保留；缺少新版字段时继续旧严格墙钟检查，有字段但无效时拒绝。该机制不会把迟到树绑定到早先截图。

GUI 后端自己采集的 Frame 使用本机请求开始时的 monotonic 时间判断年龄，保留最多 128 项、绑定同一 Frame 对象的时间记录；外部导入或复制帧继续使用严格墙钟检查。只有最近一张已通过 AX 投影的帧，在 HTTP 采集开始后不超过 1 秒且其后没有任何 _act 时，才可能省去重复的标题、目标或资料卡关闭预检截图。包括鼠标移动在内的每次动作请求都立即使复用资格失效；采集本身超过 1 秒就不能命中此优化。最终发送证明和发送后取证仍强制重新 capture，不复用。

启用 AX 的 GUI 动作携带可选 AgentPrimitiveRequest.expected_window，内容为 window_id、app_id 和 rect。Agent 在新的执行分支调用实际输入之前重新读取 X11，要求窗口仍 active、visible、focused 且三项匹配，否则不执行。旧请求默认不带此要求，旧 journal 请求摘要保持兼容。这是窗口前置条件，不能替代实际资料卡数字身份和最终正文证据。

聊天覆盖只包含气泡和头像均完整位于固定聊天区域的可处理消息。顶部截断历史、缺少头像或锚点的片段不进入可处理序列，不代表完整群历史已读。输入草稿、公告、成员列表和资料卡文字不会作为已发送消息入库。无变化的动画不应假造新到达事件；不确定序列和覆盖空档会被保留为限制。

## 私有配置与状态

配置、session、token、数据库、状态、控制、trace 和可选基准截图路径应使用既有私有目录内的独立绝对路径。PowerShell 启动器要求它们位于 %LOCALAPPDATA%\NeuralCloud\QQDesktop，拒绝 reparse point 及输入输出路径碰撞。启动器只校验 token 文件路径，不输出 token 内容。真实截图、群正文、人物资料、原始模型输出和令牌不提交到仓库。

主要字段如下：

- session_config_path、state_db、status_file、control_file：会话、观察/发送账本、心跳、暂停请求。
- trace_file：独立私有 JSONL。省略时使用状态目录中的 watcher-trace.jsonl；目录须已经存在。trace 仅保存标识、阶段、时长、计数和固定错误类别，不写正文或提示词。
- body_url、agent_url 及各自 *_token_file：经过鉴权的本机 HTTP 环回入口；不跟随重定向或使用代理。
- expected_instance_id、expected_revision、window_id：从实际部署核验的实例、修订和窗口绑定；不能照抄旧部署值。
- group_id、self_member_id：唯一授权群及实际登录账号的数字字符串。
- automatic_send：启动器要求显式布尔值；设置为 true 仍须通过全部身份、决策和交付检查。
- accessibility_enabled：默认 false；私有配置明确 true 才启用新观察路径。
- identity_cache_enabled：默认 false；当前私有配置已显式启用，常驻 watcher 已 ready。重启清空界面绑定，历史人物记忆保留。
- observation_interval_seconds：默认 1，范围 0.25–2；不是每次视觉请求间隔。
- settle_seconds：默认 0.65，范围 0.1–3；表示 GUI 动作后的短暂等待，不能代替对实际资料卡出现的核验。
- turn_deadline_seconds：默认 60，范围 1–60；以原首次可见时间为起点，不随重试续期。
- vision_model：明确配置的视觉回退模型，不静默替换 Misty 主文本模型。
- poll_seconds、cooldown_seconds：保留旧配置兼容，范围分别为 5–300 和 30–3600。新发送账本采用下述 direct/ambient 10/60 秒规则，不能用旧冷却字段绕过规则。
- initial_baseline_observation：仅首次创建观察账本时可使用经校验的既有私有 Body 观察作基准。常规重启使用当前窗口基准，不追发停机积压。
- recover_attribution_event_id：仅显式维护恢复的旧接口，要求原事件在 900 秒内且尚未绑定、决定或预约发送；不是正常无人运行策略。验收期间的人工恢复使该轮验收失败。

状态包含 service=qq-desktop-watcher-v1、实际 PID、started_at、heartbeat_at、配置/数据库绑定、采集与发送开关、当前阶段、最近错误及 observer 状态。长模型调用期间仍有独立心跳。启动器接受的心跳年龄为 max(60, poll_seconds × 3) 秒，并拒绝超过 5 秒的未来时间；8 小时验收使用更严格的 30 秒心跳和 10 秒观察空档约束。初始化状态、observer 故障或无有效基准都不能当成已就绪。

遮挡、读取失败和新变化待取证均形成 gap。observer 状态保留当前 gap_seconds、累计 total_gap_seconds、最大 max_gap_seconds 及最近 32 项空档，恢复后也不抹去历史空档。watcher 将固定原因和起止时间追加到与 trace 同目录的 *.gaps.jsonl；写入失败有独立计数，不把空档假装成连续观察。Agent 对 scoped capture 缺树只记录固定原因、阶段和耗时，区分锁忙、截止时间、窗口与覆盖层变化，不记录无障碍正文或原始错误响应。

事件尚未建立时，initial_capture、群核验、首次本人核验及 Body/AX/视觉回退计数写入独立 *.observation.jsonl 准备 trace；它没有 event_id，失败也保留。准备成功后按来源 trace ID 和阶段索引关联到事件，统计物理截图/调用时按来源去重，不能把共享准备工作重复相加。初始 baseline 不套用候选消息的过期时间；真实变化仍保留原 first_seen 和原期限，不因准备重试续期。 chat_observation_items 仅从完整、无遮挡且已绑定的聊天树提取按原容器顺序排列的方向、正文类型和显示名判别键，供过期观察排除基线匹配；它不提供数字人物、截图证据或点击授权，未知正文/截断树直接拒绝。

## 身份、期限与发送规则

identity_cache_enabled=true 时，首次或缓存失效后的群、本人、未知成员仍从真实资料卡核验数字身份。群缓存绑定本进程会话、Body 实例/修订、固定窗口、实际群号和精确标题像素；本人缓存只绑定实际点击并验证过的本人候选，须数字账号匹配且具有明确本人标记，例如逐字可见的“查看我的QQ空间”。其他人的空间链接或编辑备注不能替代本人证明。缓存关闭时保持原逐次核验行为。

成员命中使用本次有效 AX 中的精确显示名，以及同帧头像圆内 RGB 像素和尺寸的 SHA-256，索引到此前真实数字卡的 QQ ID 与持久化 profile_record_id。头像位置变化不改变摘要，变化的昵称、头像、窗口或身份范围引起冷核验；名单中多个同名可见行拒绝缓存。该组合是弱界面线索，不是 QQ 提供的稳定身份键：完整同名同头像克隆无法仅凭截图区分，不能声称解决了这种情况。缓存最多 256 项，冲突记录不被淘汰复活；绑定发生在原 profile 落库后，核验对象、原 Frame 和当前 scope 必须完全匹配。命中保留原 profile 的 capture_id、observed_at 和完整证据链，另附当前消息证据，不把旧资料重写成新观察。

缓存只在当前进程内存在，重启后重新核验，持久人物记忆不删除。当前或已结束的观察 gap 达到 10 秒、明确窗口/标题/健康状态变化时清缓存；同一 gap 按开始时间及序号只处理一次，去重最多保留 64 项，短暂资料卡遮挡不因这条规则失效。发送前仍核验新鲜窗口、正文和上下文；最终 proof 前及 callback 内的身份 epoch 栅栏阻止已失效的缓存授权越过持久点击边界。该专用拒绝在 mark_click_started 前发生，可在原截止时间和既有最多三次总预检尝试内重新核验，不扩展为点击后重试。

GUI 操作仍由唯一执行线程执行。标题点击需要新鲜标题和锚点证据；资料卡关闭只针对已识别的同类同数字身份卡片，未知弹窗或登录页拒绝自动关闭。视觉回退最多对同一截图做三次只读识别，非法结果不缓存；不会把识别重试变为动作重试。

QQ 资料卡可能在点击后延迟出现。group/member/self 卡首次读取后，只有仍为同群、无遮挡的聊天页且没有资料卡，才允许最多两次额外整对 capture/inspect，每次等待最多 0.15 秒并继续检查原截止时间和暂停。正常首读已有卡片时立即返回；一旦出现错误数字卡片、其他场景或群切换就停止等待，交给原身份校验拒绝。该流程永不重复点击头像或标题，也不等待错误身份“变成正确”。

时间与限流使用以下定义：

- first_seen_at 是 observer 首次有效读到对应当前可见变化的 UTC 时间，first_seen_mono 是同一时刻的单调时钟。它不是 QQ 服务器接收时间；遮挡期间的真实到达时间未知。
- 端到端耗时从原 first_seen_mono 到唯一 GUI 交付回执持久化的 receipt_persisted 阶段结束。截图、身份、两阶段模型、排队/限流和点击前重试全部计入，不能从决策完成或发送点击才开始计时。
- 同一轮最多 60 秒。重定位、重新决策及重试不重置原时间；超时正文作废。新可见上下文使旧意图失效，最多一次合并重规划保留原起点。
- 点名/直接回复 direct 最短发送间隔 10 秒，主动插话 ambient 为 60 秒。生产路径以成功 GUI action HTTP 响应到达本机的 click_acknowledged_at 计算冷却；这个时间是晚于实际点击的保守上界，不是服务器点击时间。若 ACK 丢失，只有同进程只读核对确认新发出气泡，才用该张验真截图的本机 capture 完成时间 click_observed_at 作为一次性上界。仍未知则继续阻断；模型 speak、回执入库或重复 inspect 不续冷却。click_started_at 保留点击前持久化尝试边界的含义。旧记录保留迁移的冷却，不用远端截图时间猜测点击时间。限流不排队迟发旧正文；到可用时间重新读取当前语境并决策，仍受原截止时间约束。
- 归属读取失败最多三次总尝试，受同一截止时间约束。发送点击前，只对明确可重试的预检失败最多三次总尝试，每次重试等待 2 秒，再定位/验证。过期、身份变化或不可恢复错误直接终止。

实际点击前先持久化 reservation 和 PreClickProof，绑定当前群、本人、正文摘要、源记录、窗口、实例/修订、标题像素、上下文和原发送气泡计数。持久化回调失败绝不点击。GUI 发送按钮最多点击一次；键盘输入成功仅表示草稿输入完成。

发送 step_id 已改为由 reservation 唯一绑定，同一意图继续使用同一键，避免全局固定 send-once 与历史 action 冲突。Agent v6 提供带原有 Bearer 鉴权的 POST /agent/v1/steps:read，只查询指定 step，返回 instance/revision、state、request_hash、起止时间以及 result 的 action_id/step_id/success；不写 journal、不消费 nonce、不返回操作正文。unknown 或 started 不能证明没有执行。历史不同 action 占用的确证只用于显式工程维护，普通启动不能据此自动解除不确定状态或重复发送；维护记录与原失败记录分开保留。

PreClickProof.verified_at 和 DeliveryResult.delivered_at 分别记录本机完成预检、完成 GUI 交付核验的 UTC 时间；远端截图/AX 的原采集时间仍保留在原生证据中。端到端统计使用单调时钟 trace，不以两台机器的墙钟相减。交付核验时刻也不是服务端送达时刻。

后验要求新鲜发出气泡、正文匹配和空输入框，再存唯一回执。点击后异常或结果不明进入 uncertain，关闭后续发送；只允许本进程依据原证明做最多三次、共不超过 30 秒的只读交付核对，等待间隔为 0/1/3 秒，不再次点击。重启只能关联已经存在的可信回执，不能凭同文气泡推断自己曾发送。自动有界只读核对成功并取得唯一回执可纳入验收，未解决的不确定交付或人工补发不能通过；核对时间计入完整端到端延迟。

被取代、过期或终止的草稿，仅当本后端此前已成功输入并记录所有权、当前数字群与窗口仍匹配、输入框逐字等于该草稿时才允许清空，再截图确认空白。外部已有草稿、正文变化或无所有权时不清除。

清理使用单独的 15 秒桌面截止时间，覆盖其模型、HTTP 与动作检查；不会续期原回复的发送期限。已知无自有草稿时跳过。存在自有草稿但不能确认清空时，写入暂停、禁用采集和发送；暂停文件落盘失败也进入 fail-stop，不继续同轮新决策。

输入框单独读取 ProseMirror 的明确 Text 或完整 paragraph/static 子树。父段落与静态叶子重复公开相同文本时只保留一次，多段落保留换行；这些文字不会进入已发送 messages。清空后明确 is-empty、父子均为换行的 trailingBreak 可使用其虚拟零宽排版节点确认空框；缺失 Text、未解析对象占位符、非空内容或不一致子树仍拒绝当作空白。

scoped 读取仅在当前采集中省略明确空 Text 的额外 getText，以及完全离开绑定窗口的独立虚拟列表项后代；部分可见行、普通祖先和资料卡保留。窗口/ACTIVE/overlay 结束时重新验证，不缓存跨次树。固定 scoped_a11y_read_timing 日志仅含阶段、最后调用类别、节点/调用数量及耗时：慢读和超时可用于定位单个 DBus 请求停顿，不含正文或节点标识。1 秒预算保持不变；已在途的 DBus 调用可能延后返回，现有请求超时和锁继续阻止无界排队。

自然 wait 只计入模型有效选择不说话的结果。模型超时、HTTP 错误、无效输出、过期、缓存命中、保护性等待和限流都有独立终态，不能伪装成自然沉默。发送回执表示 GUI 证据核验，不证明收件人已读或 QQ 服务器确认。

## 8 小时验收

先完成已登录会话的实际预检，再单独启动只读验收监测器。它不启动 watcher、不发送消息、不干预 GUI，只读取心跳、trace、账本和记忆库并写入一个新的私有验收目录。start 在前台持续运行 8 小时，使用另一个终端查询：

~~~powershell
$qqPrivate = Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDesktop'
$qqAcceptanceConfig = Join-Path $qqPrivate 'acceptance.json'
$qqRunDir = Join-Path $qqPrivate 'acceptance\run-20260911-01'
.\.venv\Scripts\python.exe -m harness.qq.acceptance start --config $qqAcceptanceConfig

# 另一个终端；路径须与 acceptance.json 的 run_dir 一致。
.\.venv\Scripts\python.exe -m harness.qq.acceptance status --run-dir $qqRunDir
.\.venv\Scripts\python.exe -m harness.qq.acceptance report --run-dir $qqRunDir
~~~

AcceptanceConfig 要求 run_dir、watcher_status_path、trace_file、ledger_db、memory_db 五个绝对私有路径，当前实际 worker_identity={pid, started_at}，以及对应实现的 build_hash。run_dir 必须尚不存在，父目录已经存在；身份和值从当前部署读取，不使用示例占位。默认 sample_interval_seconds=15（上限 15）、max_heartbeat_age_seconds=30、max_observation_gap_seconds=10。不要与 watcher 的 1 秒观察频率混淆。

必须在同一已登录会话、同一 worker 身份和同一监测进程内连续满 8 小时，不拼接多轮。至少取得 40 个已核验成员 warm_speak、5 个首次成员 cold_speak 和 10 个模型自然 wait；样本来自授权群中的真实对话，不用验收器自动刷消息凑数。warm/cold 按该次归属前是否已有实际核验记忆区分。发送样本须关联同一事件、完成的模型决策、唯一发送预约、点击前证明及唯一持久回执。

P95 使用 nearest-rank，warm ≤30 秒，cold ≤60 秒。未满 8 小时且没有失败为 RUNNING，满足全部条件才为 PASS；满时长但样本不足为 INSUFFICIENT_SAMPLES。失败或未结束事件、观察溢出/过长空档、未解决的交付不确定、人工修复、身份/时间不连续或证据断链均不能通过。start/status/report 对 RUNNING 或 PASS 返回 0，其余返回 2；report 写私有 report.json，完成的报告冻结本轮范围。

报告是本地状态、trace、SQLite 和 GUI 回执的关联核验。它能发现记录在案的维护和控制状态，不能证明不存在未记录的人工桌面操作，也不能验证 QQ 服务端送达。原始证据继续留在私有目录，仓库只保存不含正文、身份明细或令牌的摘要。

## 当前部署与待验项目

本节是 2026-09-11 报告的 Agent v7 / worker v4 快照，不是实时状态查询。Agent 已部署 mistyai-desktop:qq-unattended-20260911-v7，镜像 manifest-list SHA-256 为 101c10ae9c0b315b27deea872adb9cd639048aae7454edd5615b4c32a6d8fe76，源码快照 SHA-256 为 31627d577f8ce0763eb5ddf85150c2405f9190b3c45e7731d30222a321e19a44。第二 supervisord 中的 worker 仍为 v4，QQ 原 PID 256 未重启。Windows Body 与 QQ session 最近报告的 PID 分别为 44192、39220。旧 watcher PID 18508 已暂停。缓存版 PID 23944 已于 2026-09-11T10:42:44.347279+00:00 启动并 ready，运行源码为 qq-unattended:c2bb307c03942457ba1e53ac84ad16a57c50645fa44b12034fa9f02cd1ad186b。私有 identity_cache_enabled、自动截图和实际发送均为 true；当前快照为 watching、无积压、无观察错误、观察空档 0 秒，尚无新消息、决定或交付。

第一台 ready watcher（PID 28892，16:48:35 启动）在用户真实 @ 输入后暴露了正文读取盲区：sensor 从 16:49:29 起报 a11y_text_unreadable，worker 等待 observer，累计 0 决策、0 发送，随后已正常暂停。这次新消息失败保留为实际运行结果，不能由此前通过的初始化预检替代。

完整父 Text 与 @ 子节点、普通 static 的严格 proxy 对应解析已修复；signature、正文投影和过期排除共用解析。未知或缺失部分仍拒绝，作者不会由 @ 对象推断。修复的 82 项专项测试及固定诊断的 149 项测试已通过；根代理用新的 Body capture 完成了完整投影复验。此次已过期输入不会补发。

修复后 watcher PID 33404 曾于 2026-09-11T16:57:31.373576+08:00 启动并 ready，build 为 qq-unattended:a3fe6b1cd714c17d57c5dde933601f49dc128110be63b521df679d7132a856c9；初始化 19.641 秒，10 次 capture、9 次 AX、1 次配对重试、1 次帧复用、0 VLM。该进程最近实际处理结果为 1 次 speak 在发送预检失败、没有点击并自动清理，另有 2 次自然 wait。PID 33404 随后已暂停，其 ready 只保留为历史；该进程仍无实际交付。

Agent v5 部署后，生产路径 probe_qq_draft_roundtrip 的工程预检通过。trace cf98fb8463e64cc0a14b32b4a6f55646 的本地记录为 17:24:16.004139–17:24:32.623255，单调时钟总耗时 16.610 秒：setup 3.375 秒、staging 8.297 秒，进入点击前边界为 11.672 秒，cleanup 4.344 秒。共 16 次 capture、15 次 AX、1 次配对重试、11 次帧复用、3 次空白卡过渡关闭、0 VLM。工程脚本在 before-click 边界故意终止，没有点击发送；自有草稿自动清理后确认输入框为空。该样本验证了生产采集、身份、输入和清理路径，未包含实际发送、新气泡回执或完整回复延迟验收。

PID 2012 的 started_at 为 2026-09-11T09:27:44.635142+00:00，source hash 为 qq-unattended:053fb6f6008abb068fd5219612a2cd312053d47fbeb98b0bc2a3104d0054567c。该进程曾 ready，随后真实新输入触发自然 speak：事件 7323b2497f404d7da2a34064db829b4a，trace aea87b6d6add43838c0cd71b68829d28，first_seen=09:29:28.261796Z。发送边界为 28.032 秒，28.094 秒收到 primitive_outcome_unavailable，没有执行成功 ACK；3 次只读 reconciliation 均未发现新气泡，33.907 秒结束为 uncertain。

实际 Agent HTTP 日志在 09:29:58.743744437Z 返回 409。v6 只读 journal API 已实查确认：全局 send-once 在 04:14:46 已由另一 action 完成并占用，本次请求发生历史冲突，没有执行 Send。上述 Agent 墙钟与本地单调时钟阶段值分别保留，不混算响应延迟。唯一 step ID 修复已冻结，但尚不能据此把原失败改为成功。显式工程维护已完成：当前 reservation 标为 not_sent，不创建回执、不重新排队、不改变冷却，并在 qq_operator_delivery_repairs 追加 legacy_step_conflict_not_sent 及完整旧记录审计。原 trace 和 uncertain attempt 原样保留，不改成成功。随后本人/群数字、窗口及草稿逐字摘要核对通过，工程清稿已完成并确认空输入；operator_assisted=true，没有 Send。清稿本地时间为 17:46:09.025410–17:46:21.818375，单调耗时 12.797 秒，12 capture、10 AX、2 配对重试、7 帧复用、2 空白卡关闭、0 VLM。该维护和清稿均不计正式验收。

后续 PID 18508 的真实输入产生 1 次 speak、0 次交付，39.281 秒以 profile_transition_chat_changed 失败，未点击发送。已定位的链路是 AX 超时回退到 VLM 后，对同一可见标题的转录不一致；该失败原始记录保留，当前进程已暂停。

会话缓存工程报告 session-cache-20260911T104038/report.json 的 source hash 为 qq-unattended:c2bb307c03942457ba1e53ac84ad16a57c50645fa44b12034fa9f02cd1ad186b。冷核验群 3.559 秒、本人 3.896 秒、成员 3.444 秒；命中本人 8.520 毫秒、群 4.445 毫秒、成员 16.478 毫秒。命中复用了同一张不超过 1 秒的已验证 AX 帧，不包含新消息发现、网络模型或完整 E2E。缓存草稿阶段 2.500 秒到达 before-click 后由工程脚本故意 abort，没有 Send；原始 result=unknown、reason=desktop_operation_unavailable 保留，不能当作实际交付。清稿 1.946 秒后确认空输入，整个工程 trace 为 16 秒、13 capture、10 AX、2 配对重试、17 帧复用、0 VLM。operator_assisted=true、formal_acceptance=false。首轮缓存 probe 的失败原始记录也保留，不用后续预检覆盖。

此前开启字节缓存后的并行身份 probe 为 capture 2234 毫秒、群核验 2734 毫秒、本人核验 4485 毫秒、成员核验 4187 毫秒，总计 13.640 秒；全程本地 AX，群/本人/成员数字正确，0 VLM，无发送。另有静止聊天页的只读 sensor 工程采样 10/10 成功，范围 0.328–0.453 秒，P50=0.414 秒，nearest-rank P95=0.453 秒。身份 probe 与早先样本发生在不同时间，不能归因于缓存提速；十次静止采样也不是 GUI 并发、端到端回复或 8 小时 P95 验收。中文自有草稿工程往返此前已通过，但均在发送点击前终止。

Linux worker 由 /run/misty/qq-unattended-20260911/worker-supervisor.conf 的第二 supervisord 加载，控制 socket 为同目录 supervisor.sock；主 supervisord 的旧 body-worker 已停止。配置位于 mode 0700 的私有运行目录。容器 UID 0 已丢弃相关 capability，不能绕过目录权限；权限错误不表示配置路径不存在。必须以 misty-body 用户连接，例如在该 QQ 容器所在的本地 Linux Docker engine 上只读检查：

~~~powershell
docker exec --user misty-body mistyai-qq-online-test-desktop-1 supervisorctl -c /run/misty/qq-unattended-20260911/worker-supervisor.conf status
~~~

第二 supervisord 属于运行时热部署；/run 接线不能视为容器重建或系统重启后的持久启动保证。重建后须重新核验 live.env 和实际镜像引用，并重新核验实际 worker、Agent、Body 版本和唯一 socket 服务。回退时先暂停 watcher，再以 misty-body 用户停止并确认第二 supervisord 的新 worker 已退出，最后才允许恢复主 supervisord 的旧 body-worker；两套 worker 不得同时占用 socket。此回退不需要重启已登录 QQ。

缓存实现组合选择为 615 passed / 93.92 秒（25 文件），后续更新选择为 245 passed / 33 秒。显式维护相关选择为 194 passed / 13.52 秒，独立 Python 硬中断选择为 23 passed / 10.40 秒；后者是离线进程测试，不是 QQ 实机重启。其他已完成的 nonce、缓存失效、发送前栅栏、diagnostics、journal、恢复和投影选择保留在实施证据中。最终光标/AX 草稿专项为 3 passed / 1.16 秒；随后四文件缓存与最终截图回归为 62 passed / 16.36 秒，Ruff 通过。各组有重叠，不能相加。

18:52 实测已完成新消息→数字归属→两阶段决策→唯一 GUI 点击→持久回执，整个候选未人工操作 Misty 界面。首次可见为 10:52:09.393372 UTC，点击发起为 +40.547 秒，点击 ACK 为 +40.969 秒，新气泡核验和回执落库为 +42.875 秒。阶段为读取、重试和身份准备约 18.53 秒，决策与人格约 17.44 秒，实际发送和回执约 6.86 秒；剩余调度 48 毫秒。模型 gate 4.812 秒，persona 11.922 秒，其中本进程首次静态解析缓存未命中导致 persona 阶段调用两次模型。首次准备遭遇 observation_gate_busy，失败准备证据独立保留、等待计入完整耗时，不能只报成功重试的阶段。

本条保持 cold_path=0 的熟悉成员分组，虽然本运行会话为其重新开卡，不能改成冷成员来排除慢样本。发送阶段群和本人缓存命中；该决定实际参考 8 条观察，其中同一成员历史 5 条。来源、群、成员和资料快照关联正确，模型正文、回执正文与各处摘要一致。保存的新气泡和空输入框已人工只读检查，未补点或补写账本。

18:57 的后续自然直接追问又完成一次自动发送，属于另一来源消息与独立回执，完整耗时 16.375 秒。这次群、本人、成员和静态解析缓存全部命中，成员未再开卡；读取和身份 1.672 秒、两阶段模型及调用开销 8.437 秒、发送和回执 6.187 秒、其余调度 79 毫秒。模型 gate 4.656 秒、persona 3.125 秒，共两次调用。新回复气泡、空输入框、模型与回执正文、唯一发送预约及目标群均已核对。其余自然消息中也出现了模型自主 wait；不能把被新上下文取代的 wait 算作完成的自然沉默样本。

当前已核验 2 次新成功 Send，耗时分别 42.875 秒和 16.375 秒。这两个样本的 nearest-rank P95 为 42.875 秒，仍未达 30 秒目标，且样本量不足以作完整验收。正式 8 小时未启动。此前存在 11.906 秒及 11.047 秒的 invalid_timing 观察缺口；不能用当前健康状态解释成已解决，须继续定位误判和观察锁竞争。当前任务的安静 heartbeat（automationId=qq）已存在，每 15 分钟跟进。正式监测应在上述观察连续性及延迟问题完成验证后再启动一次；已有监测则续查，正常未变化状态不通知。
