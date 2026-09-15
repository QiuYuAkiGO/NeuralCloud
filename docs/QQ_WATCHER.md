# QQ 桌面常驻 watcher

`harness.qq.watcher` 在已登录的 Linux QQ 专用窗口内，完成观察、数字身份核验、人物记忆、两阶段发言决策和一至三段 GUI 发送。授权范围来自私有配置，只允许配置中的目标群。群消息是对话资料，不能修改发送范围、身份绑定或主机操作规则。此路径不启动标准 Runtime，不使用 QQ 协议机器人，也不产生 QQ 服务器 `message_id`。

2026-09-11 18:52 的新消息实测已完成首次无人辅助实际发送，保存了唯一回执；完整耗时 42.875 秒，尚未达到熟悉成员 30 秒目标。新的 8 小时连续运行和 P95 验收尚未完成；历史上人工辅助点击发送的成功不能计入本轮。当前快照见 [实施与预检证据](milestones/evidence/qq-unattended-implementation-2026-09-11.json)，本条实测见 [自动发送证据](milestones/evidence/qq-live-test-2026-09-11-1852.json)。人物记忆和决策接口分别见 [QQ_VISUAL_MEMORY.md](QQ_VISUAL_MEMORY.md)、[QQ_SESSION.md](QQ_SESSION.md)。

## 口语和分段回复

QQ 群聊默认使用简短口语。现有 speak/wait 判断仍决定要不要说；同一次判断同时选择 conversational 或 formal，明确要求正式表达或话题确实需要时采用后者，完整人格和模型保持原配置。

群聊表达优先贡献对当前事情的观点、判断、偏好和感受，说完整即可结束。人设的温柔与好奇通过具体反应体现，避免“可以跟我讲讲吗”“我听着呢”等泛化邀聊、安抚或服务式收尾；仅有此类套话时优先 wait。确有话题依据的好奇和影响回答的必要澄清仍可简短提问，不为表现个性编造经历、记忆或确定结论；人设中的爱好、示例和生活场景不代表当下实际观察。此规则在已有发言判断及正文生成内执行，不增加润色调用，也不事后删改模型正文。风格在 v7 接入，当前实现的决策版本为 `qq-group-turn-taking-v11`；风格验证记录见 [群友口吻调整](milestones/evidence/qq-peer-voice-2026-09-11.json)。

首段直接进入话题，不以“让我想一想”“我想想”“让我想一下”等思考预告开场，也不把此类占位句单独发成首段。各段都应包含实际内容；需要斟酌时说具体判断或不确定之处，不替换成另一句反复使用的固定开场白。

群回复复用原 pipeline 的 `[CONTINUE]` / `[END]` 解析，一次正文生成准备 1–3 条消息；通常一条，有独立补充时才分段，不强求段数。模型用空行表达的自然段也可转成独立气泡，保留全部文字和顺序；单段内换行、超过三段或超长结果明确失败，不静默删字。此适配不打开追加续写循环；原有静态解析和必要校验仍可能产生辅助模型调用。

每段有固定顺序、独立点击证明、实际点击时间和持久回执。上一段已确认且画面只新增了该己方气泡后，才允许下一段；中途新消息、暂停或到期会终止余稿并留下 partial 状态。点击后不明只作有界只读对账，不再次点击。10/60 秒间隔限制下一次逻辑回复，从最后实际发送的一段计算；同一回复内不额外等待这段冷却。所有段共享原始 60 秒发送期限，完整耗时以最后一段及总回执落库为终点，三条气泡仍只算一次成功样本。

仅发送成功的前缀也会进入后续对话上下文，明确区别于完整回复；未发送余稿不会成为记忆里的“已说过”。升级保留旧单段记录及其原始哈希，重启只关联已有段回执，不重放旧草稿。工程测试和当前运行状态见 [口语分段接入记录](milestones/evidence/qq-colloquial-segments-2026-09-11.json)。

v9 已核验一条真实两段回复，首次观察至最终聚合回执为 27.921 秒，人物归属与 111 个证据对象哈希通过。当前只有一个 v9 样本，不能据此判定 P95 或八小时验收通过；旧慢样本继续保留。详情见 [v9 实机核验](milestones/evidence/qq-v9-live-check-2026-09-11.json)。

## 模型连接与上下文失败

2026-09-14 按用户要求移除发送前最后一次输入框复核：最终截图不再重复比较草稿 AX 正文、输入框锚点或输入框像素，也不再通过这一步的 1 秒身份缓存时效条件拒绝发送。输入完成后的群验证仍检查正文与原上下文，最终截图仍核验窗口、聊天上下文和发送按钮，发送后仍要求新气泡与空输入框回执。该变更针对 `fresh_draft_not_verified` / `draft_pixels_changed_before_send` 拦截；目标调度的 `no_progress` 计数规则不变。

同日跨群观察后的发送排查确认：12:17（04:17 UTC）的回复首段已经点击发送，并取得 `qq_ui_verified` 分段回执；第二段在第 58.281 秒进入发送阶段后立即失败，当时原期限还剩约 1.72 秒，没有进入第二段桌面输入或点击。同期独立传感器处于 `request_timeout` 缺口，现有证据支持发送能力检查拒绝，但旧错误处理只留下 `partial_precheck_failed`，没有保留原始通信错误码。`deliveries=0` 仅表示没有完整聚合回执，不能据此称为完全未发送。状态现分别显示已核验段数 `verified_segments_sent` 和部分回复数 `partial_deliveries`；这两项是主群持久账本累计量，不能与本轮完整回复计数混用。

该历史轮次没有调用 vision。主要耗时是成员归属核验 8.890 秒、共享认知 RPC 37.016 秒和首段 GUI 6.141 秒；认知中的三次 persona 请求分别为正常解析、一次正文生成和响应验证，均无网络重试，验证通过且修订次数为 0。响应验证属于模型流程，与已经移除的最终 QQ 输入框复核不同。完整诊断及耗时见 [跨群发送失败证据](milestones/evidence/qq-crossgroup-send-failure-2026-09-14.json)。

发送现在会在原始 60 秒期限内等待独立传感器从短暂缺口恢复，并继续检查新消息、暂停、上下文与 provider 能力；期限不会因等待或分段延长。历史观察缺口闭合且完成新的数字身份核验后，不再因反复读取同一条旧缺口而撤销新证明；新缺口仍会使身份缓存失效。QQ 通信错误保留固定错误码，部分发送继续保留已发前缀并取消未发余稿，不重放到期的旧段。

另一次当前现场读取复现了 QQ 标题父节点已有完整 Text、成员数子节点缺少 Text 的情况。AX 标题投影现在优先读取有界的完整父节点 Text，再回退到子标签；不以 accessible Name 补造缺失正文。这是独立的读取退化修复，不能归因为上述零 vision 历史轮次的耗时原因。实机已完成主群 → 观察群 `1060979317` → 主群的数字群号核验、本人核验及发送前证明检查，随后主动中止，未点击发送且自有草稿已清空。首次尝试仍有群资料卡首帧未核验失败，下一次 AX 核验通过；此结果不证明资料卡交互绝对稳定，也不等于修复后实际发送成功。[修复验收记录](milestones/evidence/qq-crossgroup-send-repair-2026-09-14.json)已归档。最终相关回归 428 项通过，Ruff 通过；全 QQ 初轮的 25 项适配器初始化错误已修复并复测通过，不与其他分组累计。2026-09-14 12:47（北京时间）新 worker PID 44304 已加载匹配源码，自动访问观察群并返回主群，`watching`、主群发送就绪、当前观察缺口为 0、无未知交付；历史缺口仍保留。持久累计已发送分段 37、部分发送 1，最近真实回执为 12:17:41；修复后完整回复实际交付仍待自然新消息验证。

2026-09-12 回复故障排查发现，Docker/QQ 服务已停止，但旧状态文件仍显示 `running`；停机原因尚未确定。此前 15:30 的文本回复已生成，发送预检失败后又遇到捕获失败，最终在点击前取消。代码与回归复现支持“首次交付仍持有被准备流程替换的群验证对象”这一原因，但历史错误合并了对象、群号和时效三项检查，没有记录具体失败项，不能解释成 46 秒就超过 60 秒期限。两个交付入口已在首次尝试前更新群验证，继续绑定原消息上下文和截止时间，267 项相关测试及 Ruff 通过。恢复过程中保留了 26 次本人名单核验失败；最终新 watcher 在 18:32:30 起用 8.375 秒完成群与本人核验，恢复 `watching`、读取和发送就绪，未重放旧消息且没有未知发送。这是服务就绪快照，不证明名单交互问题永久消失或回复发送成功；完整脱敏记录见 [QQ 回复恢复证据](milestones/evidence/qq-reply-recovery-2026-09-12.json)。

18:38:37 收到的新文字已完成身份归属并产生 `speak` 决策，但三次发送前采集均报 `verified_frame_unavailable`，37.172 秒后结束为 `not_sent`，没有发送点击或回执。三次采集在 trace 中均为 0 毫秒异常。暂停 watcher 后的真实 Body 只读复现确认：普通 `capture` 成功，运行中的 provider 事件循环同步调用 GUI actor 时，内部 Body `Runner.run()` 因嵌套事件循环立即失败，并伴随 `collect was never awaited`。修复将 Body 异步 I/O 隔离到每个后端唯一的线程，client、Runner 及其关闭使用同一线程和循环，GUI actor 保持原线程；排队消耗原期限，超时取消协程，关闭回收线程。最终 307 项相关回归及 Ruff 通过，包含前面的 267 项，不能相加。18:45:26 的真实只读复测中，普通调用、运行中事件循环内调用、再次普通调用均获得新观察及配对 AX，分别耗时 1.531、0.703、0.875 秒，未操作 GUI 或发送消息；此前失败继续保留，新的实际回复仍待验收。

共享内稳态 HTTP 客户端的同类事件循环冲突也采用独立 I/O worker 修复，状态校验、SQLite 和 GUI 仍在原线程，排队消耗既有 3 秒或更短 tick 预算。该部分 192 项回归及 Ruff 通过，与上述选择有重叠，不能累计。真实共享 Runtime 的只读复测在已有事件循环中取得 revision 4595，并完成循环内关闭和循环外重复关闭；使用隔离状态目录，未改写生产反馈账本、未调用领域变更或发送消息。

同日 23:03 的新故障是观察恢复关闭正确群资料卡后，又立即重开卡片重复核验，后续缺失 AX 导致恢复次数耗尽。恢复现在仅关闭新鲜观察确认的本群资料卡，或与先前已核验本群绑定的本人资料卡，清除旧身份授权并确认返回原群；发送前仍重新核验群与本人。454 项相关测试通过。23:24 的真实记录已命中资料卡关闭恢复，随后重新通过完整身份核验并恢复 `watching`；保留中间失败、长观察缺口及旧消息过期，不补发旧消息。新的实际回复仍待发送回执核验，见 [资料卡恢复修复](milestones/evidence/qq-observer-dismiss-recovery-2026-09-12.json)。22:29 的 `newer_context` 已由新增群消息独立证实，属于应保留的抢占保护。

最终统一加载两项 I/O 修复的 watcher 为 PID 30084，18:54:24 启动，18:54:29 起用 13.735 秒完成群与本人准备。18:56:27 只读终检为 `watching`、读取和发送就绪、无未知交付或内稳态反馈错误；Body、Agent、session 的正式健康端点均为 HTTP 200。此新运行尚无新消息、决策或交付，修复后的真实回复仍待下一条新文字核验，18:38 的失败不删除或重放。启动期间三次观察缺口及最长 7.375 秒仍保留，不能将后台验收进程存在解释成连续八小时通过。

v8 将已经校验的回执转成供模型阅读的必要字段，保留实际正文、来源、群和本人、回执及分段编号，不重复附上整份截图证据或相同正文。数据库内完整回执和校验哈希不变，决定缓存仍绑定完整证据。48,000 字符上限保持不变；仍超限时记录 `context_budget_exceeded` 和 base/final/persona 字符计数，避免把本地预算问题误认成断网或模型坏 JSON。

v10 进一步将已校验的人物与观察投影为文字资料：保留完整原文、作者断言及其不确定性、数字人物与群范围、时间、记录编号和原 `profile_record_id` 身份快照；模型不再接收资料卡、作者归属和自身别名的长证据 URI。原截图、完整引用和校验记录继续保存在私有后台，完整原始资料、别名及回执仍参与决定指纹，证据变化会使缓存失效。模型侧 `proof_refs_stored_outside_model` 明确说明证据保存在上下文之外；这不会把截图省略误称为没有证据，也不改变群原文只能作为用户侧资料的边界。

同群同人的持久历史检索范围不变，预算仅选择本轮送入模型的完整记录。所有新观察、触发者最新的完整观察及其原身份快照、操作者请求、查询、已核验自身别名，以及最近一条真实已发的完整回执或已发单段必须保留。空间不足时依次省略可选旧决定、较早回执和已见旧观察；旧观察按查询匹配优先级保留，先省略不相关或较低分记录，再按时间比较。不会截断正文、删数据库历史，或将未发余稿加入已发上下文。省略的记录编号和数量通过 `model_context_selection` 明示，元数据本身也计入预算。

发言判断和人格正文使用同一份选中资料，并在判断前预留其最大合法 JSON 结果的空间；完整人格及两阶段流程保持不变，不增加模型调用。48,000 字符上限不提高；如果必需资料仍放不下，明确记录预算失败并返回无正文结果。`source_record_ids` 仅表示实际送入判断模型的观察，模型调用前失败时为空；`seen_observation_record_ids` 单独保存已经检索见过的记录，用于避免把旧失败重新视为新需求，不声称模型考虑或回答过它们。相同失败输入保留缓存结果，旧事件不会因版本升级重新排队发送。

2026-09-12 的数据库副本复测保留全部 19 条观察，没有省略正文或历史记录；判断输入为 28,952 字符，加入判断结果后的人格输入为 29,262 字符，得到 `ready` 草稿。判断耗时约 2.564 秒、人格约 5.634 秒、隔离复测总计约 9.298 秒，不包含 GUI 发送，也不是八小时验收。此前的真实超预算失败继续保留，详情见 [上下文选择修复记录](milestones/evidence/qq-context-selection-2026-09-12.json)。表情包的语义概括和按需缓存尚未实现；本次文字投影不新增视觉识别调用，也不为尚未识别的图片编造说明。

v10 已通过全部 QQ 与共享 continuation 回归（1539 项），同步重启 session 与 watcher 后恢复自动截图和实际发送。只读审计确认 86 条历史发送及回执行哈希未变，原超预算失败仍无发送链，新运行未重放旧事件。启动阶段的五段观察缺口仍保留，最长 12.859 秒；这些维护和隔离复测不计入连续八小时验收，新版真实发送延迟仍待后续自然消息核验。

仅模型请求遇到短暂连接、读写或协议异常，以及 HTTP 408/429/500/502/503/504 时，在原期限内最多重试一次。默认退避 0.25 秒；服务要求的等待超过 2 秒或剩余期限不足时不重试。认证、参数、模型输出格式错误不重试。所有失败调用和退避计入 trace；若辅助阶段最终失败，后续阶段成功不能将它抹掉。此机制不重试 QQ 发送点击，也不重新发送到期的旧消息。

v8 阶段的故障定位、1501 项回归及隔离真实上下文复测见 [上下文与连接恢复记录](milestones/evidence/qq-context-network-recovery-2026-09-11.json)。模型复测只在数据库副本生成草稿，耗时不含 GUI 发送；此前四条完整两段回复的证据也已只读核验，但不能代替新版本八小时和延迟验收。

观察读取正常但资料卡遮挡聊天时，恢复不依赖上一轮是否失败或是否已经创建回复候选。本人核验在准备阶段失败、上一轮为 `superseded` 或自然 `wait` 时，也会重新采集并识别现场。2026-09-12 晚间进一步修正了“关闭正确资料卡后又立即重开群卡”的恢复循环：仅允许关闭数字群号与标题均匹配的本群资料卡，或在先前已核验本群标题下、具有正确本人号和明确本人标记的资料卡；关闭前清除身份缓存与准备标记，关闭后核对新鲜群聊及原标题。恢复本身不授予发送权限，下次正常准备重新核验群与本人。未知窗口、登录页、错群、其他成员、读取错误和待完成证据不触发此操作。恢复最多两次、间隔至少 3 秒，每次最多 15 秒且受已有候选剩余期限约束；不会重放旧消息或延长发送期限。新修复通过 454 项回归并完成实机部署：23:24:02 起 6.031 秒完成只关闭资料卡的恢复，后续完整群与本人核验通过，23:25:59 新 watcher PID 31024 为 `watching`、读取/发送就绪、无未知交付。两次中间准备或恢复失败、八段启动观察缺口及原进程至少 976.485 秒的未闭合中断全部保留；尚无新消息的发送回执。详情见 [安全关闭资料卡恢复记录](milestones/evidence/qq-observer-dismiss-recovery-2026-09-12.json)。原先 460 项契约测试及实机恢复见 [资料卡遮挡恢复记录](milestones/evidence/qq-idle-profile-recovery-2026-09-12.json)；不同选择有重叠，不累计。此前至少 1442.360 秒的观察中断也继续保留，工程重启不算连续验收。

轻量读取分别约束 Windows QPC 请求耗时与 Agent 单调时钟包络，两端均不得超过配置超时和 1.5 秒的较小值；AX 读取仍不得超过 1 秒或其 Agent 包络。不同机器的耗时不再作严格大小嵌套比较，差值只记录为 `remote_local_duration_delta_ms`，不校准时钟或延长期限。双 nonce、实例修订、窗口与作用域、5 秒墙钟偏差边界及旧协议规则保持不变。修复前实测成功 HTTP 响应曾因约 2–3.5 毫秒差被拒绝，修复后的 10 次独立读取全部通过；144 项观察测试及启动阶段仍存在的短超时见 [跨机器计时修复记录](milestones/evidence/qq-observer-clock-recovery-2026-09-12.json)。

## 内稳态主动交流与身体协商

v11 将 QQ 作为可协商的身体通信能力接入：`communication.qq.read.v1`、`communication.qq.send.v1`、`communication.qq.reconcile.v1` 和 `evidence.reference.v1` 由现有 `CapabilityNegotiator` 与 `EmbodimentManager` 协商及更新健康状态。provider 复用 watcher 唯一的 GUI actor，同步回调仍在该线程执行，没有第二套点击执行器。发起读取、单段发送和只读对账都经过该 port；不是仅在状态中列出能力名称。首次激活冻结授权群、本人、实例修订、窗口、provider 身份和能力限制。必需能力不足、暂停、观察失效或未知发送会关闭发送；未知结果仍可在适用的身份与证据边界内只读对账。

发送请求必须匹配账本中的原事件、冻结决定、parent/child 分段计划、正文哈希和原期限。GUI 回调完成实际核验并持久化回执后，provider 再独立读取回执，核对窗口、实例、本人、群、点击证明和实际点击时间；回调的 `sent` 声明本身不能证明成功。写回执中断不会授权再次点击。成功发送的前缀、剩余未发段、未知结果和重新启动后的旧候选继续遵守现有账本规则。

主动交流是显式启用的独立来源，使用持久 `HomeostasisEngine`，通过 `DomainCommandEnvelope` 的版本、期限和幂等边界提交 heartbeat 与已核验事实。它在现有 watcher 内运行；这不表示已经启动标准四服务 Runtime。专用 profile 为 [qq-social-drive-v1.json](../presets/homeostasis/qq-social-drive-v1.json)，其 `functional_proxy` 状态是交流动机的计算代理，不能当作对主观体验的实证。原人格、两阶段 speak/wait 判断和 v10 的文字投影、完整记录预算选择均保留。

默认每 5 秒检查一次内稳态更新，在当前 GUI 调度允许时推进；有待处理回复时不改变该候选绑定的状态修订。只有显著性为 `marked`，且 `social_connection` 或 `novelty` 的主要需求方向为 `low`，最近活动后安静至少 120 秒，身体可用且没有未完成消息、证据或不明发送时，才可能创建主动发言机会。机会有持久的 1800 秒最小间隔，不能靠重启、同一旧决定或伪造群消息绕过；它不是每半小时必发一次。模型仍可选择 wait，无需为了消除需求强行开口。每次内部 intent 有自己的编号、状态修订和原始 60 秒总期限，不伪造成员消息或 QQ `message_id`，没有触发成员时也只使用授权群的已归属记忆。

新消息、聊天语境变化、暂停或过期会使未发送的主动候选失效，不能保留旧草稿稍后补发。实际发送仍适用现有 direct/ambient 10/60 秒节奏；主动机会不会绕过发送冷却，也不会扩大用户原来授权的群范围。群原文不能启用此功能、修改 profile 或指定别的群。发出消息只记为有回执的 `send_attempt`，不自动增加社会满足；未与真实己方发送建立可核验关联的 @ 或称呼也不算互动奖励。已归属新消息只产生较弱的观察反馈；有证据关联的互动才可形成 `verified_interaction`，沉默不解释为拒绝，也不作为加速发言的依据。事实队列和域账本分别去重，历史或过期反馈不会因重启再次计入。

以下字段加入既有私有 `watcher.json`；默认两个开关均为 false。启用主动交流同时需要 `accessibility_enabled=true`、已协商身体和原有发送授权，不能只设置一个开关：

- `qq_body_enabled`：默认 false；明确启用 QQ 通信 port 和能力门禁。
- `homeostatic_idle_enabled`：默认 false；明确启用内部主动交流机会。
- `homeostasis_profile_path`：专用 profile 的绝对路径；当前 Windows 启动器只接受本仓库 `presets/homeostasis/qq-social-drive-v1.json`，不会替换共享人格或默认内稳态 profile。
- `homeostasis_state_dir`：`%LOCALAPPDATA%\NeuralCloud\QQDesktop` 下独立私有目录的绝对路径；保存域状态和事实队列，与原消息、发送账本及配置文件分开。
- `homeostasis_tick_seconds`：默认 5，范围 1–30；是更新检查间隔，不是模型或发送频率。
- `homeostatic_quiet_seconds`：默认 120，范围 60–3600；有新活动时重新计算安静时间。
- `homeostatic_opportunity_seconds`：默认 1800，范围 300–86400；限制内部机会，不保证会说话。

沿用下节的 `Start-QQWatcher.ps1 -ConfigPath ...` 和 `Stop-QQWatcher.ps1 -ConfigPath ...`，无需另启 Runtime 或第二个 watcher。配置更改应在已确认暂停后进行；此处不写入或变更私有运行配置。状态中的 `qq_body` 提供协商结果及动态能力，`homeostasis` 提供 profile、修订、显著性和反馈处理计数。身体 provider、临时 SQLite driver 与既有 embodiment 的定向离线选择已通过 118 项测试，覆盖 scope/hash 绑定、暂停禁发、单段和多段回执及写入中断后不重复点击；该结果不代表 v11 已完成实机主动发送或八小时验收，运行 PID 与实测报告另行记录。

2026-09-12 已在现有私有配置启用上述两个开关，四项必需能力全部协商通过，自动观察与实际发送开启。内稳态在进程重启后保留状态，目前为 nominal；低能量或高负荷会抑制机会。隔离八小时计算在第 162 分钟首次达到 marked，这不是实际开口时间保证。真实模型在数据库副本中接到模拟需求后自然选择 wait，总计 3.016 秒，没有 GUI 或发送；单段与分段 speak 路径由模拟界面的完整控制器测试覆盖。全部 QQ、内稳态、身体契约与共享 continuation 回归 1828 项通过，集成增量 183 项通过，两个集合有重叠。344 条历史记录及 150 条成功记录哈希未变。

状态 `latency_ms` 继续统计群消息首次可见至回执的耗时；`idle_latency_ms` 单独统计内部机会生成至回执，不将两类样本混算。当前 v11 两类真实发送样本均为 0，正式八小时尚未开始。最终启用轮次的启动观察缺口最长 7.141 秒，维护时另一轮 10.61 秒缺口及启动失败也保留；不能据此声称长期稳定或延迟达标。完整运行身份、验证和待验收边界见 [内稳态身体接入记录](milestones/evidence/qq-homeostatic-body-2026-09-12.json)。

验收报告的 `idle.counts` 与 `idle.latency_seconds` 单列主动 speak/wait，原来 40 次熟悉成员、5 次完整核验及 10 次来信自然 wait 的目标保持不变。来源由事件、typed intent、决定和 trace 标记交叉验证，不能只加一个 counter 就绕过作者归属检查；未知发送、缺失回执、暂停和观察缺口继续计入全局失败。长观察缺口使身份缓存失效后，主动路径会重新核验群与本人，并在核验成功后恢复绑定，静默群不必等新消息才能恢复该能力。

验收兼容专项及原验收回归 191 项通过。实机启动发现，内稳态仍为 nominal 时也必须恢复身份；已让未完成身份准备的静默群重新进入正常核验流程，相关 100 项回归通过。最终运行于 02:40 CST 启动并恢复身体能力，版本与持久状态均已核对，具体身份以报告中的最终快照为准。

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

## 群观察名单

`watcher.json` 的 `group_roster` 配置可观察的群，`group_id` 仍指定常驻主群。省略名单时保持原来的单群行为。示例：

```json
{
  "group_id": "1044946257",
  "group_roster": [
    {"group_id": "1044946257", "label": "PHANTOM-C", "observe": true, "send": true},
    {"group_id": "1060979317", "label": "超级豪人大宅门", "observe": true, "send": false}
  ],
  "group_observation_seconds": 300
}
```

名单最多 16 个群，群号唯一，主群必须可观察。名单各群也必须加入 `session.json` 的 `allowed_group_ids`；多群观察需要启用 QQ 身体与无障碍读取。`label` 用于在已加入的会话中找群，打开后仍通过群资料卡核对数字群号。新增群默认只读，目前发送仅支持主群，且同时受 `automatic_send` 控制。

Misty 的身体能力目录为每个可观察群分别提供读取入口。空闲时 watcher 也会按名单轮流查看其他群，默认每次访问相隔 300 秒，可设为 60–3600 秒。所有访问共用一个 QQ 窗口和 GUI 执行线程：确认没有草稿或待发送任务后切群、核对身份、读取当前可见消息，再返回主群。访问期间主群传感器暂停，避免跨群归因；返回失败会暂停 watcher。每次只读取当前可见页面，不代表完整历史或连续覆盖所有群。

各群最新读取保存在观察账本旁的 `*.roster.sqlite3`，状态页显示每群最近读取时间、消息数和错误。核对范围及证据对象后，读取内容作为带群号、观察时间和来源说明的有界资料供共享认知使用；显示昵称不等于已验证的作者 QQ 号，也不会自动写入人物身份档案。

## 私有配置与状态

配置、session、token、数据库、状态、控制、trace 和可选基准截图路径应使用既有私有目录内的独立绝对路径。PowerShell 启动器要求它们位于 %LOCALAPPDATA%\NeuralCloud\QQDesktop，拒绝 reparse point 及输入输出路径碰撞。启动器只校验 token 文件路径，不输出 token 内容。真实截图、群正文、人物资料、原始模型输出和令牌不提交到仓库。

主要字段如下：

- session_config_path、state_db、status_file、control_file：会话、观察/发送账本、心跳、暂停请求。
- trace_file：独立私有 JSONL。省略时使用状态目录中的 watcher-trace.jsonl；目录须已经存在。trace 仅保存标识、阶段、时长、计数和固定错误类别，不写正文或提示词。
- body_url、agent_url 及各自 *_token_file：经过鉴权的本机 HTTP 环回入口；不跟随重定向或使用代理。
- expected_instance_id、expected_revision、window_id：从实际部署核验的实例、修订和窗口绑定；不能照抄旧部署值。
- group_id、self_member_id：常驻主群及实际登录账号的数字字符串；额外观察群通过上述名单配置。
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
